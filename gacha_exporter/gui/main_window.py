"""主窗口 - 组装 GUI 与业务逻辑。"""
import logging
import os
import time
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis.manual_patches import build_patched_engine, compute_editable, synthesize_records
from ..analysis.parser import GachaParser
from ..analysis.pool_view import (
    LIST_TYPE_ALL,
    LIST_TYPE_LABELS,
    PoolViewTracker,
    is_all_pools,
    make_view,
    split_view,
)
from ..analysis.stats import StatsEngine
from ..debug_flags import is_debug_capture_enabled
from ..exporters.excel import export_excel
from ..models import CaptureConfig, Character, GachaRecord, PityGroup, Pool, SessionContext
from ..storage.char_shields import load_shields, save_shields
from ..storage.history import HistoryService
from ..storage.pool_patches import MANUAL_FILL_ID, load_pool_patches, save_pool_patches
from ..storage.pool_assignments import (
    apply_pool_assignments,
    load_pool_assignments,
    save_pool_assignments,
)
from ..storage.profiles import ProfileManager
from ..storage import sync_apply, sync_file
from ..storage.settings import (
    load_capture_iface,
    load_discovered_addresses,
    load_last_capture,
    save_capture_iface,
    save_discovered_addresses,
    save_last_capture,
)
from ..utils.helpers import safe_filename
from ..utils.net_iface import IFACE_AUTO, list_capture_ifaces, resolve_capture_iface
from ..utils.logger import get_logger
from .. import __version__
from .app_icon import app_icon
from .branding import sponsor_label, faq_label
from .capture_worker import CaptureWorker
from .discovery_dialog import DiscoveryDialog
from .maintenance_dialog import MaintenanceDialog
from .npcap_dialog import NPCAP_DOWNLOAD_URL, NpcapDependencyDialog, find_bundled_npcap_installer
from .pity_panel import PityPanel
from .record_table import RecordTable
from .settings_dialog import SettingsDialog
from .sync_dialog import (
    ExportSyncDialog,
    ImportSyncDialog,
    parse_error_text,
)

logger = get_logger(__name__)

_frame_logger: Optional[logging.Logger] = None
_request_logger: Optional[logging.Logger] = None


def _frame_log() -> logging.Logger:
    """返回专用于原始帧取证的 logger（惰性初始化，独立于 app.log）。

    propagate=False 防止 frame hex 冒泡进 app.log / 控制台；RotatingFileHandler
    限制总占用，镜像 utils/logger.setup_logging() 的配置。
    """
    global _frame_logger
    if _frame_logger is None:
        from logging.handlers import RotatingFileHandler
        from ..utils.paths import get_logs_dir

        log_path = os.path.join(get_logs_dir(), "frame_debug.log")
        handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        fl = logging.getLogger("frame_debug")
        fl.setLevel(logging.INFO)
        fl.propagate = False
        fl.addHandler(handler)
        _frame_logger = fl
    return _frame_logger


def _request_log() -> logging.Logger:
    """返回专用于 C->S 请求帧取证的 logger（独立于 frame_debug.log）。

    排查「切池/翻页请求为何偶发缺失/解析失败」：请求帧很小（9~15 字节，单包
    即一帧），只要工具真的收到了完整 C->S 帧就会写进 request_debug.log。若某段
    抓包期间响应帧在、这里却没请求帧，说明该方向流重组失步或包没被抓到；若请求
    帧进来了但 app.log 没有对应的「切池/翻页请求」，则是解析对不上。
    """
    global _request_logger
    if _request_logger is None:
        from logging.handlers import RotatingFileHandler
        from ..utils.paths import get_logs_dir

        log_path = os.path.join(get_logs_dir(), "request_debug.log")
        handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        rl = logging.getLogger("request_debug")
        rl.setLevel(logging.INFO)
        rl.propagate = False
        rl.addHandler(handler)
        _request_logger = rl
    return _request_logger


_ACTION_NEW = "__new__"
_ACTION_RENAME = "__rename__"
_ACTION_DELETE = "__delete__"


class MainWindow(QWidget):
    """主窗口。"""

    def __init__(
        self,
        characters: Dict[str, Character],
        pools: Dict[str, Pool],
        pity_groups: Dict[str, PityGroup],
        profiles: ProfileManager,
        parent=None,
    ):
        """初始化。

        Args:
            characters: 角色映射
            pools: 卡池映射
            pity_groups: 保底分组配置（role.json pity_groups 段）
            profiles: 账号档案管理器（决定当前激活账号）
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.characters = characters
        self.pools = pools
        self.pity_groups = pity_groups
        self.profiles = profiles
        self._active_profile_id = profiles.active_id()
        self._shields = load_shields()  # 陌生ID屏蔽集（跨账号全局，角色维度）
        self._pool_assignments = load_pool_assignments()  # 陌生池->共享组指派（跨账号全局）
        # 生效池映射 = 元数据池 + 陌生池指派合成的 Pool；统计/展示/导出统一用它
        self._pools_eff = apply_pool_assignments(pools, pity_groups, self._pool_assignments)
        self.stats = StatsEngine(characters, self._pools_eff, pity_groups, self._shields)
        self.parser = GachaParser()
        # 视图感知的跨卡池重复过滤器（在 parser.parse 之后、stats.add 之前生效）。
        # 只做跨视图去重，不改 stats 去重算法/历史合并（保底算法冻结）。
        self._pool_tracker = PoolViewTracker()
        self.history = HistoryService(self._active_profile_id)
        self.worker: CaptureWorker | None = None
        # 停止时短等待没等完的旧抓包线程（引擎在无流量时只在下个包才检查停止
        # 事件，线程可能残留）：断开回调并保留引用待其自然结束，避免 QThread
        # 运行中被析构、也不让旧线程与新抓包双计。见 _retire_worker。
        self._retired_workers: List[CaptureWorker] = []
        self.session = SessionContext(current_pool_id="all")
        self._current_pool_id = "all"
        self._last_capture_iso = load_last_capture(self._active_profile_id)  # 上次抓包完成时间（按账号持久化）
        # 自助发现的抽卡地址（按账号隔离：地址因账号而异，切换账号时重新加载）
        self._discovered_addresses = load_discovered_addresses(self._active_profile_id)
        self._forced_update = False  # 强制更新未完成：禁用「开始抓包」/地址发现
        # Npcap 未安装、用户选择「仅查看已有数据」：可查看/导出历史但不能抓包。
        # 按钮保持可点击（Qt 禁用按钮收不到点击），置灰样式 + 点击重新弹安装引导。
        self._npcap_viewonly = False
        # 「抓取不完整」弹窗的已提示签名（"已抓/总数"）。停止抓包路径会被
        # stop_capture 与 worker finished 先后调用两次，若不去重会弹两次；
        # 缺口内容变化（换了账号/又抓了一轮）时签名不同 → 仍会提示。
        self._gap_notified_sig: Optional[str] = None
        # 收尾去重闸：start_capture 置位，_finalize_capture_session 消费。
        # stop 按钮与 worker finished 会先后各调一次收尾，第二次只是把空
        # session_records 再 merge 一遍（纯 no-op 却写盘+备份）→ 跳过。
        self._pending_finalize = False
        # 会话 Δ（位置平移量）与其基准 total：首个响应到达后由 _ensure_session_shift
        # 计算并下发给 stats（开启实时位置判重）；新会话开始时重置
        self._session_shift: Optional[int] = None
        self._shift_base_total: Optional[int] = None

        self._setup_ui()
        self._load_history()
        self._update_title()

    def _setup_ui(self) -> None:
        """构建界面。"""
        self.setWindowTitle("金鳞抽卡导出工具")
        self.resize(1200, 700)

        # 应用图标（窗口标题栏 + 任务栏）；解码失败则跳过，不影响启动
        icon = app_icon()
        if not icon.isNull():
            QApplication.instance().setWindowIcon(icon)
            self.setWindowIcon(icon)

        main_layout = QVBoxLayout(self)

        # 顶部控制栏
        top_layout = QHBoxLayout()
        self.combo_account = QComboBox()
        self._populate_accounts()

        self.btn_start = QPushButton("开始抓包")
        self.btn_stop = QPushButton("停止抓包")
        self.btn_stop.setEnabled(False)
        self.btn_clear = QPushButton("清空")
        self.btn_unknown = QPushButton(self._unknown_label())
        self.btn_unknown.setVisible(False)  # 仅当有未映射ID（N>0）时显示作提醒

        # 低频操作收进「更多」下拉
        self._more_menu = QMenu(self)
        self._act_export = self._more_menu.addAction("导出 Excel")
        self._act_detail = self._more_menu.addAction("查看明细")
        self._act_restore = self._more_menu.addAction("还原抽卡数据")
        self._act_maintenance = self._more_menu.addAction("抽卡数据维护")
        self._act_maintenance.setVisible(False)  # 暂隐藏「抽卡数据维护」入口
        # U1 跨端记录文件互通（与另一端工具交换账号历史的独立信封文件）。
        # 抓包中整组置灰（契约 §6.3/§9）——文件读写与抓包互斥，见 _refresh_sync_actions。
        self._more_menu.addSeparator()
        self._act_sync_export = self._more_menu.addAction("导出记录文件…")
        self._act_sync_import = self._more_menu.addAction("导入记录文件…")
        self._more_menu.addSeparator()
        self._act_unknown_menu = self._more_menu.addAction(self._unknown_label())
        self._more_menu.addSeparator()
        self._act_discovery = self._more_menu.addAction("自助获取抽卡地址")
        # 抓包网卡选择：改用 QActionGroup 互斥子菜单（勾选式）。懒枚举——打开菜单时
        # （aboutToShow）才 list_capture_ifaces() 刷新，避免启动拉 scapy/枚举成本。
        self._netiface_menu = QMenu("抓包网卡", self)
        self._netiface_group = QActionGroup(self)
        self._netiface_group.setExclusive(True)
        self._netiface_menu.aboutToShow.connect(self._refresh_netiface_menu)
        self._act_netiface = self._more_menu.addMenu(self._netiface_menu)
        self._act_settings = self._more_menu.addAction("版本检查")
        self.btn_more = QToolButton(self)
        # 原生 menu-indicator（Windows 样式）位置易偏移：隐藏它，箭头直接画进
        # 文字（▾），与文字天然同行对齐。
        self.btn_more.setText("更多 ▾")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_more.setMenu(self._more_menu)
        self.btn_more.setStyleSheet(
            "QToolButton { padding: 4px 8px; }"
            "QToolButton::menu-indicator { image: none; width: 0; height: 0; border: none; }"
        )

        top_layout.addWidget(QLabel("账号:"))
        top_layout.addWidget(self.combo_account, 1)
        top_layout.addStretch()
        top_layout.addWidget(self.btn_start)
        top_layout.addWidget(self.btn_stop)
        top_layout.addWidget(self.btn_clear)
        top_layout.addWidget(self.btn_unknown)
        top_layout.addWidget(self.btn_more)
        main_layout.addLayout(top_layout)

        # 常驻提示：B8 后正式统计只采纳「全部卡池」页面的数据，其余页面一律不计入。
        # 放顶部控制栏正下方、点「开始抓包」前就能看到；用独立 QLabel（而非状态栏
        # status_label），不随切页/抓包帧刷新被覆盖。刻意压成浅灰小字，避免误认作可点操作区。
        self.capture_note_label = QLabel(
            "ⓘ 抓取抽卡记录时只采纳「全部卡池」页面的数据——请在该页翻页，其余页面翻到的记录不计入"
        )
        self.capture_note_label.setWordWrap(True)
        self.capture_note_label.setStyleSheet(
            "color:#888; font-size:11px; padding:2px 0 6px 0;"
        )
        main_layout.addWidget(self.capture_note_label)

        # 主面板：保底/六星命中（每抽明细走「查看明细」弹窗）
        self.pity_panel = PityPanel()
        main_layout.addWidget(self.pity_panel, 1)

        # 状态栏：状态（左）+ 赞助信息（右下角，与公告弹窗同一文案来源）
        self.status_label = QLabel("就绪")
        bottom_bar = QHBoxLayout()
        bottom_bar.addWidget(self.status_label)
        bottom_bar.addStretch()
        bottom_bar.addWidget(sponsor_label())
        bottom_bar.addWidget(faq_label())
        main_layout.addLayout(bottom_bar)

        # 事件绑定
        self.btn_start.clicked.connect(self.start_capture)
        self.btn_stop.clicked.connect(self.stop_capture)
        self.btn_clear.clicked.connect(self.clear_records)
        self.btn_unknown.clicked.connect(self._show_unknown_id_dialog)
        self._act_export.triggered.connect(self.export_excel)
        self._act_detail.triggered.connect(self._show_detail_dialog)
        self._act_restore.triggered.connect(self._restore_data)
        self._act_maintenance.triggered.connect(self._open_maintenance)
        self._act_sync_export.triggered.connect(self._export_sync_file)
        self._act_sync_import.triggered.connect(self._import_sync_file)
        self._act_unknown_menu.triggered.connect(self._show_unknown_id_dialog)
        self._act_discovery.triggered.connect(self._run_discovery)
        self._act_settings.triggered.connect(self._open_settings)
        self._netiface_group.triggered.connect(self._on_netiface_chosen)
        self.combo_account.currentIndexChanged.connect(self.on_account_changed)
        self._refresh_sync_actions()  # 初始（未抓包）状态：互通菜单可用

    def _refresh_sync_actions(self) -> None:
        """按抓包状态把「导出/导入记录文件」整组置灰（契约 §6.3/§9）。

        状态**由 `worker.isRunning()` 现算**而不是靠调用点传参：`on_capture_finished`
        在新一轮「继续抓取」已启动时也会被旧线程触发，传参数会把禁用状态顶掉。
        """
        running = bool(self.worker and self.worker.isRunning())
        self._act_sync_export.setEnabled(not running)
        self._act_sync_import.setEnabled(not running)

    # ---- 账号管理 ----
    def _populate_accounts(self) -> None:
        """重建账号下拉：所有账号 + 分隔 + 动作项，选中当前账号。"""
        self.combo_account.blockSignals(True)
        self.combo_account.clear()
        active_idx = 0
        for i, prof in enumerate(self.profiles.list_profiles()):
            self.combo_account.addItem(prof.name, prof.id)
            if prof.id == self._active_profile_id:
                active_idx = i
        self.combo_account.insertSeparator(self.combo_account.count())
        self.combo_account.addItem("＋ 新建账号…", _ACTION_NEW)
        self.combo_account.addItem("✎ 重命名当前账号…", _ACTION_RENAME)
        self.combo_account.addItem("🗑 删除当前账号…", _ACTION_DELETE)
        self.combo_account.setCurrentIndex(active_idx)
        self.combo_account.blockSignals(False)

    def _set_account_combo_active(self) -> None:
        """把账号下拉选回当前激活账号（动作执行后调用）。"""
        idx = self.combo_account.findData(self._active_profile_id)
        if idx >= 0:
            self.combo_account.blockSignals(True)
            self.combo_account.setCurrentIndex(idx)
            self.combo_account.blockSignals(False)

    def on_account_changed(self, index: int) -> None:
        """账号下拉切换或动作项触发。"""
        data = self.combo_account.itemData(index)
        if data == _ACTION_NEW:
            self._new_account()
            self._set_account_combo_active()
        elif data == _ACTION_RENAME:
            self._rename_account()
            self._set_account_combo_active()
        elif data == _ACTION_DELETE:
            self._delete_account()
            self._set_account_combo_active()
        else:
            if data and data != self._active_profile_id:
                self._switch_account(data)

    def _switch_account(self, profile_id: str) -> None:
        """切换到指定账号：停抓包、把当前已抓记录并回原账号、重建引擎与历史。"""
        self._stop_worker(2000)  # 切账号需确保旧线程真正结束，避免残留回调污染新账号统计
        try:
            was_empty = self.history.is_empty()  # 会话开始前历史状态（重叠自检用）
            # self.stats.finalize()  # 旧账本收尾钩子已注释（位置对齐方案下为空操作，保留接线）
            self.history.merge(
                self.stats.session_records, self._pool_tracker.view_totals
            )
            session_overlap = self.stats.overlapped_history
            # 以落库记录为准重建内存（合并时新记录的 pos 已按 Δ 归一化；
            # 保底消费序与事件内连续性判定都依赖统一坐标系）
            self.stats.load_records(self.history.records, replace=True)
            self._log_invariant_alarms(
                notify=True, had_history=not was_empty, session_overlap=session_overlap
            )  # 切走前是最后机会，缺口要弹
        except Exception as e:
            logger.warning(f"切换账号时保存原账号历史失败: {e}")
        self.profiles.set_active(profile_id)
        self._active_profile_id = profile_id
        self._last_capture_iso = load_last_capture(profile_id)
        self._discovered_addresses = load_discovered_addresses(profile_id)
        self.history = HistoryService(profile_id)
        self.stats = StatsEngine(self.characters, self._pools_eff, self.pity_groups, self._shields)
        self.parser = GachaParser()
        self._pool_tracker = PoolViewTracker()
        self.session = SessionContext(current_pool_id="all")
        self._current_pool_id = "all"
        self._load_history()
        self._populate_accounts()
        self._update_title()
        logger.info(f"已切换到账号 {profile_id}")
        # 地址因账号而异：新账号无已保存地址时主动引导获取，而不是等到抓不到才提示
        self.check_account_address()

    def _new_account(self) -> None:
        """新建账号并切换过去。"""
        name, ok = QInputDialog.getText(self, "新建账号", "账号名称:")
        if not ok or not name.strip():
            return
        prof = self.profiles.create(name.strip())
        self._switch_account(prof.id)

    def _rename_account(self) -> None:
        """重命名当前账号。"""
        prof = self.profiles.get(self._active_profile_id)
        if not prof:
            return
        name, ok = QInputDialog.getText(self, "重命名账号", "新名称:", text=prof.name)
        if not ok or not name.strip():
            return
        self.profiles.rename(self._active_profile_id, name.strip())
        self._populate_accounts()
        self._update_title()

    def _delete_account(self) -> None:
        """删除当前账号（归档其历史），切换到剩余账号。"""
        if len(self.profiles.list_profiles()) <= 1:
            QMessageBox.information(self, "无法删除", "至少保留一个账号。")
            return
        prof = self.profiles.get(self._active_profile_id)
        if not prof:
            return
        reply = QMessageBox.question(
            self,
            "删除账号",
            f"确定删除账号「{prof.name}」？\n其已保存的历史记录会先归档为备份文件，不会直接丢失。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._stop_worker(2000)  # 删除账号前确保旧线程结束
        self.profiles.delete(self._active_profile_id)
        # 激活已回退到剩余账号
        self._active_profile_id = self.profiles.active_id()
        self._last_capture_iso = load_last_capture(self._active_profile_id)
        self._discovered_addresses = load_discovered_addresses(self._active_profile_id)
        self.history = HistoryService(self._active_profile_id)
        self.stats = StatsEngine(self.characters, self._pools_eff, self.pity_groups, self._shields)
        self.parser = GachaParser()
        self._pool_tracker = PoolViewTracker()
        self.session = SessionContext(current_pool_id="all")
        self._current_pool_id = "all"
        self._load_history()
        self._populate_accounts()
        self._update_title()
        QMessageBox.information(self, "已删除", f"已删除账号「{prof.name}」，历史已归档。")
        # 回退到的账号可能没有已保存地址：主动引导，而不是等到抓不到才提示
        self.check_account_address()

    def _update_title(self) -> None:
        """标题栏显示当前账号名，降低串号风险。"""
        prof = self.profiles.get(self._active_profile_id)
        name = prof.name if prof else "?"
        self.setWindowTitle(f"金鳞抽卡导出工具 - {name}")

    def _load_history(self) -> None:
        """加载历史记录到统计引擎。"""
        try:
            records = self.history.records
            self.stats.load_records(records)
            self.refresh_ui()
        except Exception as e:
            logger.warning(f"加载历史失败: {e}")

    def on_pool_changed(self, index: int) -> None:
        """卡池下拉框切换（已移除下拉，保留空实现避免外部引用报错）。"""
        self._current_pool_id = "all"
        self.refresh_ui()

    def set_forced_update(self, pending: bool) -> None:
        """设置强制更新状态：未完成时禁用「开始抓包」（仍可查看/导出历史）。

        由 __main__ 在启动同步闸门（持久化的 rejected_forced_version）与异步
        版本检查（force 弹窗拒绝后）调用。停包/抓包结束恢复按钮时也要尊重
        该状态，避免把闸门顶掉。
        """
        self._forced_update = pending
        self.btn_start.setEnabled(not pending)
        self.btn_start.setToolTip("请更新到最新版本" if pending else "")
        if pending:
            self.status_label.setText("检测到强制更新，抓包已禁用，请先更新")

    def enter_npcap_viewonly(self) -> None:
        """进入 Npcap 未安装时的仅查看模式。

        可查看/导出已有历史记录，但不能开始抓包/地址发现。「开始抓包」按钮
        保持 enabled（禁用按钮收不到点击事件）但置灰：点击时重新弹出 Npcap
        安装引导。安装完成后需重启程序恢复完整功能。
        """
        self._npcap_viewonly = True
        self.btn_start.setStyleSheet(
            "QPushButton { color: #999999; background-color: #ececec; }"
        )
        self.btn_start.setToolTip("未安装 Npcap，当前仅可查看已有数据；点击重新查看安装引导")
        self.status_label.setText("未安装 Npcap，仅查看模式：可查看/导出已有记录")

    def _prompt_npcap_install(self) -> None:
        """仅查看模式下重新弹出 Npcap 安装引导（点击置灰的「开始抓包」触发）。"""
        bundled = find_bundled_npcap_installer()
        dlg = NpcapDependencyDialog(bundled, parent=self)
        dlg.exec()
        action = dlg.chosen_action
        if action == "install" and bundled:
            try:
                os.startfile(bundled)  # 拉起随包安装向导（会触发 UAC）
            except Exception as e:
                logger.warning(f"启动随包 Npcap 安装包失败: {e}")
                QMessageBox.warning(
                    self, "无法启动安装包",
                    f"未能启动随包安装包：\n{e}\n请改用「前往官网下载」。",
                )
                return
        elif action == "download":
            webbrowser.open(NPCAP_DOWNLOAD_URL)
        elif action == "exit":
            logger.info("仅查看模式下用户选择退出程序")
            self.close()
            return
        else:  # view：关闭弹窗继续仅查看
            return
        if action in ("install", "download"):
            QMessageBox.information(
                self, "安装完成后请重启",
                "Npcap 安装完成后，请重启本程序以启用抓包功能。",
            )

    def _open_settings(self) -> None:
        """打开版本检查对话框（手动检查更新入口）。"""
        SettingsDialog(self).exec()

    def _refresh_netiface_menu(self) -> None:
        """打开「抓包网卡」菜单前懒枚举并勾选当前值。"""
        # 清空旧的互斥项
        for act in self._netiface_group.actions():
            self._netiface_group.removeAction(act)
            self._netiface_menu.removeAction(act)
        current = load_capture_iface()
        # 静态项：智能（自动）
        act_auto = self._netiface_menu.addAction("智能（自动）")
        act_auto.setCheckable(True)
        act_auto.setData(IFACE_AUTO)
        self._netiface_group.addAction(act_auto)
        # 动态项：各健康接口（含 IP 文案）
        for name, ip in list_capture_ifaces():
            label = f"{name}（{ip}）" if ip else name
            act = self._netiface_menu.addAction(label)
            act.setCheckable(True)
            act.setData(name)
            self._netiface_group.addAction(act)
        # 勾选当前值：优先匹配显式值；空则智能
        target = current if current else None
        matched = None
        for act in self._netiface_group.actions():
            if act.data() == target:
                matched = act
                break
        (matched or act_auto).setChecked(True)

    def _on_netiface_chosen(self, action) -> None:
        """用户在「抓包网卡」选了某项：写入显式槽（P1）。空=智能（自动）。"""
        val = action.data()
        save_capture_iface(val if isinstance(val, str) else "")
        logger.info(f"抓包网卡手动选择: {val if val else '智能（自动）'}")
        # 显式给当前这个用，避免用户刚选完又要等下次；空则回退智能解析
        if val:
            # 更新运行中 worker 立即生效
            if self.worker and self.worker.isRunning() and hasattr(self.worker, "engine"):
                self.worker.engine.config.iface = val

    def check_account_address(self) -> None:
        """主动检查当前账号是否有已保存的抽卡地址，没有则引导自助获取。

        抽卡地址因账号而异：切换账号/启动时调用，地址缺失时提前引导，
        而不是开始抓包后一条都抓不到才提示。强制更新未完成时不打扰；
        Npcap 未安装（仅查看模式）时地址发现不可用，也不打扰。
        """
        if self._forced_update or self._npcap_viewonly or self._discovered_addresses:
            return
        prof = self.profiles.get(self._active_profile_id)
        name = prof.name if prof else "?"
        msg = QMessageBox(self)
        msg.setWindowTitle("尚未获取抽卡地址")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(f"账号「{name}」还没有获取过抽卡地址。")
        msg.setInformativeText(
            "抽卡地址因账号而异，每个账号需要分别获取。\n"
            "是否现在自助获取？\n"
            "（在游戏抽卡记录的单独卡池页翻几页即可识别，请勿切到「全部卡池」）"
        )
        btn_yes = msg.addButton("自助获取", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_yes)
        msg.exec()
        if msg.clickedButton() is btn_yes:
            self._run_discovery()

    def _log_slow(self, tag: str, t0: float) -> None:
        """计时探针：超过 100ms 的 GUI 路径操作打 WARNING，便于定位本机卡点。

        正常机器上开始/停止/刷新都在毫秒级；若某台机器某步超 100ms（如 Npcap/
        网卡/杀软等环境差异），此日志会指明是哪个阶段，避免盲猜。
        """
        ms = (time.perf_counter() - t0) * 1000
        if ms > 100:
            logger.warning(f"[耗时探针] {tag}: {ms:.0f}ms")

    def start_capture(self) -> None:
        """开始抓包。"""
        _t0 = time.perf_counter()
        # 新会话：清除上一会话的 Δ 基准（stats 侧已在收尾 begin_new_session 清过；
        # 新 Δ 等首个响应的权威 total 到达后由 _ensure_session_shift 初始化）
        self._shift_base_total = None
        logger.info(
            f"start_capture 进入: 地址={self._discovered_addresses} "
            f"worker_running={self.worker.isRunning() if self.worker else False}"
        )
        if self._forced_update:
            QMessageBox.warning(self, "需要更新", "请先更新到最新版本，再开始抓包。")
            return
        # 仅查看模式（Npcap 未安装）：按钮置灰但保持可点击，点击重新弹安装引导
        if self._npcap_viewonly:
            self._prompt_npcap_install()
            return
        if self.worker and self.worker.isRunning():
            # 上次停止只短等待、旧线程残留（引擎无流量时等不到下一个包不退出）：
            # 断开其回调并保留引用待其自然结束；否则此处 return 会让「开始」点了
            # 没反应，且旧线程继续回调会与新抓包双计。见 _retire_worker。
            self._retire_worker(self.worker)
        # 当前账号无已保存地址：提前引导获取（仍可选择稍后，回退默认端口直接抓）
        if not self._discovered_addresses:
            self.check_account_address()
        # 网卡：有已发现地址则以其 IP 为目标精确路由；无则哨兵。resolve 微秒级现算。
        _target = (self._discovered_addresses[0][0]
                   if self._discovered_addresses else None)
        self.worker = CaptureWorker(CaptureConfig(
            addresses=self._discovered_addresses,
            iface=resolve_capture_iface(_target),
            pcap_enabled=is_debug_capture_enabled(),
        ))
        self.worker.packet_received.connect(self.on_packet)
        self.worker.request_received.connect(self.on_request)
        self.worker.error_occurred.connect(self.on_capture_error)
        self.worker.finished.connect(self.on_capture_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._refresh_sync_actions()  # 抓包中：互通菜单整组置灰
        self.status_label.setText("抓包中...")
        self.session.capture_start = datetime.now()
        self._pending_finalize = True  # 本会话待收尾（stop / worker finished 二选一执行）
        logger.info("开始抓包")
        self._log_slow("start_capture 全流程", _t0)

    def stop_capture(self) -> None:
        """停止抓包。

        若本地已有历史记录、本次会话抓到了新记录、却没抓到任何与历史重叠的
        旧记录（即还没翻到上次导出的边界位置），弹出提示让用户确认，避免
        增量导出漏抓中间段。判定只看本次会话的抓取信号，不含已加载的历史
        本身（否则恒为“有重叠”，提示永远不弹）。
        """
        _t0 = time.perf_counter()
        logger.info(
            f"stop_capture 进入: session_new_count={self.stats.session_new_count} "
            f"overlapped={self.stats.overlapped_history} worker_running={self.worker.isRunning() if self.worker else False}"
        )
        self._stop_worker()

        # 本次会话一条都没抓到：可能抽卡地址已变更，引导自助发现
        if self.stats.session_new_count == 0:
            self._maybe_run_discovery()
            return

        if (not self.history.is_empty()
                and self.stats.session_new_count > 0
                and not self.stats.overlapped_history):
            msg = QMessageBox(self)
            msg.setWindowTitle("未检测到历史重叠")
            msg.setIcon(QMessageBox.Icon.Question)
            msg.setText(
                "检测到当前抓取记录与本地历史记录无重叠。\n"
                "请确认是否已经翻阅到上次导出时的抽卡记录位置。"
            )
            msg.setInformativeText(
                "“继续抓取”将恢复抓包继续翻页；\n"
                "“停止抓取”将把本次抓取的数据作为新记录保存。"
            )
            btn_continue = msg.addButton("继续抓取", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("停止抓取", QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(btn_continue)
            msg.exec()
            if msg.clickedButton() is btn_continue:
                logger.info("用户选择继续翻页，恢复抓包")
                # 马上要继续抓，不弹"抓取不完整"提示（仍写日志）
                self._finalize_capture_session(notify=False)
                self.start_capture()
                return
            logger.info("用户确认停止，本次数据将作为新记录保存")
        # 停止抓包（无论是否触发过“继续/停止”确认）都先落库并刷新去重基线，
        # 避免同一账号连续多次抓包（翻池缺页后换视图/重新登录补齐）之间不去重：
        # 下一次抓取将以已持久化的全部记录为快照，跨批次的重复抓取作为旧记录跳过。
        self._finalize_capture_session()
        self._log_slow("stop_capture 全流程", _t0)

    def _coverage_gap_text(self) -> Optional[str]:
        """生成「抓取不完整」提示文本；数据已齐或无权威 total 时返回 None。

        依据：响应里的 total 是**服务器权威总抽数**，与内存中实际条数对比即可
        硬校验有没有窟窿（不依赖任何推测）；再用 `PoolViewTracker.missing_pages()`
        把还缺的页 offset 列出来，用户可直接回翻补齐。

        为什么必须做：B8（仅采纳「全部卡池」数据）后**失去多视图冗余**——以前
        全部卡池漏页还能从单池补，现在只剩一个来源，漏页只能靠本提示发现
        （见 `协议结构说明` B8 小节）。
        """
        view = make_view(0, LIST_TYPE_ALL)
        total = self._pool_tracker.view_totals.get(view, 0)
        grabbed = self.stats.total
        if total <= 0 or grabbed >= total:
            return None
        short = total - grabbed
        pages = ""
        report = self._pool_tracker.missing_pages(view)
        if report:
            _, missing, n_seen, n_expected = report[0]
            head = "、".join(str(o) for o in missing[:10])
            tail = f" 等共 {len(missing)} 页" if len(missing) > 10 else ""
            if tail:
                pages = f"\n未拿到的页 offset：{head}{tail}；"
            else:
                pages = f"\n未拿到的页 offset：{head}；"
            pages += f"已拿到 {n_seen}/{n_expected} 页。"
        return (
            f"「全部卡池」已抓 {grabbed}/{total} 条，还差 {short} 条。{pages}\n\n"
            "请回到「全部卡池」页面把这些页翻到；或重新抓取（多翻一页以便与"
            "历史记录衔接）。\n若本次是有意分段抓取，可忽略本提示。"
        )

    def _ensure_session_shift(self) -> None:
        """确保本会话的 Δ（位置平移量）已初始化并随权威 total 动态更新。

        会话内**首个响应**到达后：以 `history.last_total`（上次落库时的权威
        total）为基准调 `stats.begin_session_shift()` → Δ 从 0 起算，`stats.add`
        开启实时位置判重（命中的旧记录当场不收，抓包过程中界面计数即为准）。

        之后每次响应把权威 total 上报给 `stats.notify_total()`：**会话中途抽卡
        会让 total 增长，Δ 必须动态跟随**（Δ = 当前 total − 落库基准 total）——
        v1 把 Δ 锁死在会话开始时的值，用户中途抽 1 抽后所有旧记录位置 +1 而
        Δ 仍为 0 → 旧记录全部被当成新记录重复计入（2026-09-13 13:17 实测
        多算 10 条）。
        """
        total = self._pool_tracker.view_totals.get(make_view(0, LIST_TYPE_ALL))
        if total is None:
            return
        if self._shift_base_total is None:
            baseline = self.history.last_total
            if baseline is None:
                baseline = total          # 首次抓包/清空后：无历史，Δ 从 0 起算
            self._shift_base_total = baseline
            self.stats.begin_session_shift(baseline)
            logger.info(
                f"会话位置基准：基准 total={baseline}（上次落库），"
                f"本次响应 total={total}，Δ 从 {total - baseline} 起算"
            )
        self.stats.notify_total(total)

    def _log_invariant_alarms(self, notify: bool = False,
                              had_history: Optional[bool] = None,
                              session_overlap: Optional[bool] = None) -> None:
        """输出去重不变量报警（§1.8）：**所有落库路径**都要调用，绝不静默。

        四条独立判据：
        - `suspicious_events()`：事件大小 ∉ {1,10} → 漏抓（<10）或多算（>10）；
        - `unresolved_events()`：槽位有缺口 / 事件未定序 → 计数可用，但事件内
          顺序不可信（保底数学不消费列表顺序，故只提示不影响计数）；
        - **抓取缺口**：权威 total 与实际条数/页覆盖对比 → 还差多少、缺哪几页；
        - **重叠自检**：本次新抓没有命中任何历史事件的位置 → 两次数据衔接无法校验。

        Args:
            notify: 缺口是否弹窗提示。**只在用户主动收尾的路径**传 True（停止
                抓包/切账号/还原历史）；"继续翻页"分支与关窗不弹（前者用户马上
                要接着抓，弹窗纯打扰；后者避免打断退出流程），两者仍写日志。

        只写日志、不打断流程——落库已完成，报警是给用户/排查看的。
        调用点：`_finalize_capture_session`（停止抓包）、`_switch_account`
        （切账号）、还原历史、`closeEvent`（关窗）。
        """
        try:
            susp = self.stats.suspicious_events()
            if susp:
                logger.warning(
                    f"事件不变量异常 {len(susp)} 个（大小 ∉ {{1,10}}，可能漏抓/多算，"
                    f"建议重翻缺口页）: {susp[:5]}"
                )
            unres = self.stats.unresolved_events()
            if unres:
                logger.warning(
                    f"事件未定序 {len(unres)} 个（槽位有缺口，暂不影响计数）: "
                    f"{unres[:5]}"
                )
            gap = self._coverage_gap_text()
            if gap:
                logger.warning("抓取不完整：" + gap.replace("\n", " "))
                # 同一次缺口只弹一次：stop_capture 与 worker finished 会先后
                # 各走一遍收尾，不去重就会连弹两次。
                if notify and gap != self._gap_notified_sig:
                    self._gap_notified_sig = gap
                    QMessageBox.warning(self, "抓取可能不完整", gap)
            # 重叠自检（设计稿 §6 报警点 2）：本次抓取与历史零重叠时，两次数据
            # 是否正确衔接无法校验（Δ 可能推错而不自知）。
            # 重叠证据有两处（2026-09-13 修正误报）：
            #   ① **实时层**：实时判重（事件级/位置级）命中的旧记录根本到不了
            #      merge，只会体现在 stats.overlapped_history —— 这是主要证据，
            #      必须在 begin_new_session 重置前取好快照传进来；
            #   ② **merge 层**：merge 报告的 merged_events / event_skipped。
            # had_history 必须是**会话开始前**的历史状态：merge 之后 is_empty()
            # 恒为 False，用它当守卫会让新账号首次抓包也误报"没有重叠"。
            rep = self.history.last_report
            overlap = (self.stats.overlapped_history if session_overlap is None
                       else session_overlap)
            if rep is not None:
                overlap = overlap or rep.merged_events > 0 or rep.event_skipped > 0
            had = had_history if had_history is not None else not self.history.is_empty()
            if rep is not None and rep.total_added > 0 and had and not overlap:
                overlap_warn = (
                    "本次抓取与历史记录没有重叠（一条已见记录都没遇到），无法校验"
                    "两次数据是否正确衔接。\n\n若两次抓包之间抽过卡，工具会按总数"
                    "差校正位置，但该校正依赖重叠段验证。\n建议下次抓包多翻一页，"
                    "让新旧数据有交集。"
                )
                logger.warning("重叠自检：" + overlap_warn.replace("\n", " "))
                if notify:
                    QMessageBox.warning(self, "数据衔接未校验", overlap_warn)
        except Exception as e:  # 报警自身绝不能影响主流程
            logger.warning(f"不变量报警检查失败（不影响已落库数据）: {e}")

    def _finalize_capture_session(self, notify: bool = True) -> None:
        """停止抓包时把本次会话数据并入历史，并刷新会话级去重基线。

        历史合并原本只在切账号/关窗时执行，导致同一账号连续多次抓包时 history
        一直为空、stats 的去重快照不更新：跨批次的重复抓取（翻池缺页后换视图/
        重新登录把同一批记录再抓一遍）就全被当成新记录留下（曾造成某账号常驻/UP
        池角色成倍计入）。这里在每次停止抓包后把 self.stats.records 并入历史，
        并重建 stats 的会话级去重快照，让下一次抓取以已持久化的全部记录为基准。

        stats 的合并/去重算法（add / shared_pity_tracking）保持冻结，仅调用其
        新增的 begin_new_session() 重置快照与批指纹。merge 为快照式增量：已落库
        的旧 key 跳过、新 key 追加，不会重复写盘。

        Args:
            notify: 收尾报警（含"抓取不完整"缺口报告）是否弹窗。用户主动停止
                抓包时 True；"继续翻页"分支传 False（马上要继续抓，弹窗纯打扰）。

        Returns:
            None
        """
        _t0 = time.perf_counter()
        if not self._pending_finalize:
            # stop 按钮与 worker finished 会先后各调一次收尾：第二次进来时本
            # 会话已落库、session_records 已清空，再 merge 是纯 no-op 却写盘
            # +备份（users/ 备份泛滥的根源之一）→ 直接跳过。
            return
        try:
            was_empty = self.history.is_empty()  # 会话开始前历史是否为空（重叠自检用）
            # self.stats.finalize()  # 旧账本收尾钩子已注释（位置对齐方案下为空操作，保留接线）
            self.history.merge(
                self.stats.session_records, self._pool_tracker.view_totals
            )
            # begin_new_session 会重置会话信号 → 重叠证据要先取快照
            session_overlap = self.stats.overlapped_history
            # 以落库记录为准重建内存（合并时新记录的 pos 已按 Δ 归一化；
            # 保底消费序与事件内连续性判定都依赖统一坐标系）
            self.stats.load_records(self.history.records, replace=True)
            self.stats.begin_new_session()
            self._pending_finalize = False  # 落库成功才消费闸门（失败保留，下次收尾重试）
            self._log_slow("_finalize_capture_session(merge+去重置零)", _t0)
            self._log_invariant_alarms(
                notify, had_history=not was_empty, session_overlap=session_overlap
            )
        except Exception as e:
            # 写盘失败（磁盘满/杀毒软件锁文件等）不能静默：去重基线未刷新，
            # 下次抓包会把这些记录重复计入，且用户以为已保存。弹窗告知并
            # 提示关窗时会再试，避免用户直接结束进程丢数据。
            logger.warning(f"停止抓包时保存历史失败: {e}")
            QMessageBox.warning(
                self,
                "保存历史失败",
                "本次抓取的记录仍在程序内存中，但写入磁盘失败，下次抓包可能"
                "重复计入这些记录。\n\n"
                f"失败原因：{e}\n\n"
                "请检查磁盘空间，或杀毒软件/同步盘是否拦截了 data 目录；\n"
                "关闭程序时会再次尝试保存，请勿直接结束进程。",
            )

    def _maybe_run_discovery(self) -> None:
        """本次会话未抓到记录，提示先翻「全部卡池」，再引导自助获取抽卡地址。

        B8 后仅采纳「全部卡池」页面的数据：只翻其他页面 / 没翻「全部卡池」
        时一整会话 0 记录是**正常过滤**，不是故障——提示必须把这个原因排在
        地址变更前面，否则用户会被误导去重取地址。
        """
        msg = QMessageBox(self)
        msg.setWindowTitle("未抓取到抽卡记录")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(
            "本次抓包未捕获到任何抽卡记录。\n\n"
            "常见原因：① 只翻了「全部卡池」以外的页面——正式统计只采纳「全部卡池」"
            "页面的数据，其他页面翻到的记录一律不计入，请回到「全部卡池」"
            "页面翻页；② 抽卡服务器地址已变更。"
        )
        msg.setInformativeText(
            "若已在「全部卡池」页面翻页仍无任何记录，可能是地址已变更，"
            "是否自助获取抽卡地址？\n"
            "（地址识别需在游戏抽卡记录的单独卡池页翻几页，识别过程请勿切到「全部卡池」）"
        )
        btn_yes = msg.addButton("自助获取", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_yes)
        msg.exec()
        if msg.clickedButton() is btn_yes:
            self._run_discovery()

    def _run_discovery(self) -> None:
        """弹出地址发现对话框，识别成功则替换地址并提示重新抓包。"""
        if self._forced_update:
            QMessageBox.warning(self, "需要更新", "请先更新到最新版本，再开始抓包。")
            return
        # 地址发现同样依赖 Npcap：仅查看模式下改为弹安装引导
        if self._npcap_viewonly:
            self._prompt_npcap_install()
            return
        dialog = DiscoveryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        addresses = dialog.get_addresses()
        if not addresses:
            QMessageBox.information(self, "未识别到地址", "未能识别到抽卡地址，请稍后重试。")
            return
        self._discovered_addresses = tuple(addresses)
        try:
            save_discovered_addresses(self._active_profile_id, self._discovered_addresses)
        except Exception as e:
            logger.warning(f"保存发现的地址失败: {e}")
        addrs_str = ", ".join(f"{ip}:{port}" for ip, port in addresses)
        # 识别成功给两个选择：直接开始抓包，或稍后再抓（关提示框回主界面）。
        # 不再要求用户先看完信息框再手动点「开始抓包」。
        msg = QMessageBox(self)
        msg.setWindowTitle("地址已更新")
        msg.setIcon(QMessageBox.Information)
        msg.setText(f"已识别并保存抽卡地址：{addrs_str}")
        msg.setInformativeText(
            "选择「开始抓包」将立即开始抓取；\n选择「关闭窗口」则回到主界面，稍后再抓。"
        )
        btn_start = msg.addButton("开始抓包", QMessageBox.AcceptRole)
        msg.addButton("关闭窗口", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_start)
        msg.exec()
        if msg.clickedButton() is btn_start:
            logger.info("识别地址后用户选择直接开始抓包")
            self.start_capture()
        else:
            logger.info("识别地址后用户选择稍后再抓，留在主界面")

    def _stop_worker(self, wait_ms: int = 250) -> None:
        """停止抓包线程（不弹窗）。

        默认短等待（250ms）：常见「停止抓包」路径不再干等 2s——引擎 sniff 在
        无流量时只在下个包才检查停止事件，wait 容易空等到超时。切账号/删账号/
        还原/关窗等需确保线程真正结束的路径传较大 wait_ms。
        供 closeEvent 与账号切换复用：只需停止线程，不应再弹“是否恢复抓包”对话框。
        """
        _t0 = time.perf_counter()
        if self.worker:
            self.worker.stop()
            self.worker.quit()
            self.worker.wait(wait_ms)
        # 尊重强制更新闸门：停包恢复按钮时不把禁用状态顶掉
        self.btn_start.setEnabled(not self._forced_update)
        self.btn_stop.setEnabled(False)
        self.status_label.setText("已停止")
        self._refresh_sync_actions()
        self._log_slow(f"_stop_worker(wait_ms={wait_ms})", _t0)

    def _retire_worker(self, worker: CaptureWorker) -> None:
        """旧抓包线程仍在运行（停止时短等待没等完）时，断开其对主流程的全部回调。

        引擎 sniff 在无流量时只在下个包才检查停止事件，线程可能残留。直接丢弃
        会让 QThread 在运行中被析构，且残留回调会与新抓包双计/误触发结束流程。
        这里断开信号并保留引用，待其自然结束（收到下一个包即退出）后由
        finished 信号清理；期间不再向统计喂数据。
        """
        for sig in (
            worker.packet_received,
            worker.request_received,
            worker.error_occurred,
            worker.finished,
        ):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._retired_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._prune_retired(w))

    def _prune_retired(self, worker: Optional[CaptureWorker] = None) -> None:
        """清理已结束的退休线程引用。worker 给定时只清它，否则清全部已结束的。"""
        for w in list(self._retired_workers):
            if w is worker or (worker is None and not w.isRunning()):
                try:
                    self._retired_workers.remove(w)
                except ValueError:
                    pass

    def on_capture_finished(self) -> None:
        """抓包线程结束：记录并持久化本次抓包完成时间，刷新面板顶部「最近更新」。

        兜底落库：线程结束（含异常退出，如 Npcap socket 错误/网卡断开）时也
        把已抓记录并入历史并刷新去重基线--此前只有点「停止」才落盘，线程自己
        死掉后用户直接再抓，去重快照还是旧状态，重发的记录会被当成新记录
        重复计入。正常点停止时 stop_capture 已 finalize 过，这里重复调用是无害
        空操作（merge 为增量）。

        若用户已通过「继续抓取」开启新一轮抓包（新 worker 运行中），跳过：
        会话中途重置批指纹/快照会误吞新一轮会话内的十连同名记录。
        """
        # 清理已结束的退休线程引用（安全兜底，正常由 finished 信号清理）
        self._prune_retired()
        # 尊重强制更新闸门：恢复按钮时不把禁用状态顶掉
        self.btn_start.setEnabled(not self._forced_update)
        self.btn_stop.setEnabled(False)
        self.status_label.setText("抓包结束")
        self._refresh_sync_actions()
        if self.worker is None or not self.worker.isRunning():
            self._finalize_capture_session()
        iso = datetime.now().isoformat(timespec="minutes")
        try:
            save_last_capture(self._active_profile_id, iso)
        except Exception as e:
            logger.warning(f"保存最近抓包时间失败: {e}")
        self._last_capture_iso = iso
        self.refresh_ui()

    def on_capture_error(self, msg: str) -> None:
        """抓包错误处理。"""
        QMessageBox.critical(self, "抓包错误", msg)
        self.status_label.setText(f"错误: {msg}")

    def on_packet(self, frame: bytes) -> None:
        """收到完整帧（服务器->客户端，抽卡记录响应）。

        parser.parse 之后、stats.add 之前经视图感知过滤器：跨卡池的「同一条
        记录在具体池页和全部卡池页各传一次」的重传按份数配对丢弃，只把保留
        的记录喂给冻结的 stats.add。视图未知（请求没被监听到）时不过滤，
        宁可重复不可漏抓（与现状一致）。stats 去重算法与历史合并逻辑不变。
        """
        # 逐帧取证写盘仅 debug 模式（打包态恒 False，见 debug_flags）：原始流量
        # 不进磁盘，也顺带避免每帧 hex+I/O 拖慢抓包响应。
        if is_debug_capture_enabled():
            self._debug_dump_frame(frame)
        # 被拒（限流）识别必须先于「无记录提前返回」：被拒响应是 8 字节截断帧、
        # 不含任何记录，放在 parser.parse 之后就会永远检测不到（此前正是这种
        # 接法，导致频繁限流丢页时用户无感知）。
        rejected = self._pool_tracker.note_response(frame)
        if rejected is not None:
            view, offset = rejected
            logger.warning(
                f"响应被频繁拒绝: 页面={self._view_label(view)} offset={offset}，"
                f"该次响应无数据，客户端会自动重发该页补齐"
            )
            self._refresh_status()
        # 会话 Δ（位置平移量）在首个响应的权威 total 到达后即可确定——必须先于
        # stats.add，实时位置判重才能从第一批就生效。
        self._ensure_session_shift()
        records = self.parser.parse(frame)
        if not records:
            return
        view = self._pool_tracker.resolve_view(frame)
        kept, dropped = self._pool_tracker.filter_records(view, records)
        if dropped:
            logger.info(f"跨卡池视图过滤 {dropped} 条（当前页面: {self._view_label(view)}）")
        # **仅采纳「全部卡池」视图的数据**（运行约定，见 协议结构说明 B8）：
        # 全部卡池是其它所有视图（单池 / 活动契约 / 遴选契约汇总页）的超集——
        # 实测各视图权威 total 与对应池求和完全吻合（792 / 421 / 70 …）。只保留
        # 它即可覆盖全部记录，且**只剩单一视图后去重退化为「同视图 + 位置可比」
        # 的逐槽判定**，跨视图的全部歧义（页边界错位、起点歧义、方向依赖）随之
        # 消失。视图未知（None）仍接受：超集语义下混入的必然是重复，账本会去掉。
        if kept and view is not None and not is_all_pools(view):
            logger.info(
                f"忽略非全部卡池视图 {self._view_label(view)} 的 {len(kept)} 条"
                f"（仅统计全部卡池）"
            )
            kept = []
        added = 0
        if kept:
            # 位置对齐方案：view/direction 已不参与判定（旧账本视图/方向信号退役，
            # direction_of 链路已注释，见 pool_view.__init__）。这里只传 records；
            # 实时判重由 stats 内部按 `(ts, pos − Δ)` 完成（Δ 见 _ensure_session_shift）
            # --- 旧接线（view / direction_of(view) 两个实参）已注释保留 ---
            # added = self.stats.add(kept, view, self._pool_tracker.direction_of(view))
            added = self.stats.add(kept)
            if added:
                self.refresh_ui()
        # 逐页视图归属 + 去重流水：排查「切池请求缺失→视图失效→同批重发漏记」用
        logger.info(
            f"响应页 视图={self._view_label(view)}: 解析{len(records)}条 "
            f"跨视图过滤{dropped} 去重新增{added} 累计{self.stats.total}"
        )
        self._refresh_status()

    def on_request(self, frame: bytes) -> None:
        """收到完整帧（客户端->服务器，切池/翻页请求）。

        登记请求 seq->视图并更新当前视图，用于后续响应的视图归属配对。
        切到非全部卡池页面时状态栏提示「建议回到全部卡池页面继续」（只提示
        不阻断，避免一次改太多）。pool_id=0 表示「全部卡池」。
        """
        # 取证日志仅 debug 模式（打包态恒 False，见 debug_flags）
        if is_debug_capture_enabled():
            self._debug_dump_request(frame)
        result = self._pool_tracker.track_request(frame)
        if result is None:
            return
        pool_id, _offset = result
        self._refresh_status()
        if str(pool_id) != "0":
            # 仅提示不阻断；规范流程是只在全部卡池翻页，具体池页翻过的页会被
            # 游戏缓存导致后续抓不到（地址发现对话已说明该约束）。
            self.status_label.setText(
                f"当前页面: {self._view_label(str(pool_id))}  "
                f"（建议回到「全部卡池」页面继续抓取）"
            )

    def _view_label(self, view: Optional[str]) -> str:
        """视图标识转可读名。

        标识形如 "<pool_id>@<list_type>"（见 pool_view.make_view）：pool_id=0
        时按列表类型取名（全部卡池 / 活动契约 / 遴选契约），否则取卡池名；
        老协议无类型段时按纯 pool_id 处理。
        """
        if view is None:
            return "未知"
        pool_id, list_type = split_view(view)
        if pool_id == "0":
            if list_type is None or list_type == LIST_TYPE_ALL:
                return "全部卡池"
            return LIST_TYPE_LABELS.get(list_type, f"卡池组#{list_type}")
        pool = self.pools.get(pool_id)
        return pool.name if pool else f"未知卡池({pool_id})"

    def _refresh_status(self) -> None:
        """刷新状态栏：总条数 + 当前页面 + 累计跨视图过滤条数 + 被拒(重发中)页数。

        完整性进度：响应里的 total 是服务器权威总抽数，与已抓条数对比即可
        硬校验有没有漏页（翻到底时应正好相等），不依赖任何推测。
        """
        parts = [f"共 {self.stats.total} 条记录"]
        view = self._pool_tracker.current_view
        if view is not None:
            parts.append(f"当前页面: {self._view_label(view)}")
        if self._pool_tracker.filtered_count > 0:
            parts.append(f"已过滤跨卡池重复 {self._pool_tracker.filtered_count} 条")
        n_rejected = len(self._pool_tracker.rejected_pages)
        if n_rejected > 0:
            parts.append(f"⚠ {n_rejected} 页当前被频繁拒绝，客户端自动重发中")
        # 完整性进度（total 来自服务器，注意 stats.total 含全部卡池，口径一致）。
        # 视图键是 "<pool_id>@<list_type>"（make_view），全部卡池 = "0@0"；
        # 裸 "0" 仅作老数据兼容——此前 get("0") 永远取不到，正向指示成死代码。
        view_totals = self._pool_tracker.view_totals
        all_total = view_totals.get("0@0", view_totals.get("0"))
        if all_total:
            n = self.stats.total
            done = "✓ 已抓全" if n >= all_total else f"还差 {all_total - n} 条"
            parts.append(f"全部卡池 {n}/{all_total}（{done}）")
        # 具体卡池覆盖：仅 pool_id ≠ 0 的具体池页才做本池比对。注意视图键带
        # "@list_type" 后缀，必须拆键取 pool_id 再与记录的 pool_id 比对——
        # 此前拿整键去比恒为 0，且守卫拦不住 "0@0"，导致全部卡池页也误报
        # 「本池还有缺口」（2026-09-13 实机反馈）。
        if view is not None:
            pool_id, _list_type = split_view(view)
            pool_total = view_totals.get(view)
            if pool_id != "0" and pool_total:
                n_pool = sum(1 for r in self.stats.records if r.pool_id == pool_id)
                if n_pool < pool_total:
                    parts.append(
                        f"⚠ {self._view_label(view)} {n_pool}/{pool_total}，本池还有缺口"
                    )
        self.status_label.setText("  ".join(parts))

    def _debug_dump_frame(self, frame: bytes) -> None:
        """记录原始帧字节到 frame_debug.log，用于事后排查 parser 静默丢失。

        取证用途：parser 若把某条记录误判为非记录标记而漏解析（如曾出现的
        帧头计数器含 0x1a、每 256 帧漏 1 条的 bug），app.log 只能看到「本帧
        解析到 N 条」却无法判断 N 是否正确；原始帧 hex 是定位此类问题的唯一
        依据。写入由 RotatingFileHandler 限制总占用，镜像 app.log 配置。
        """
        _frame_log().info(
            f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
            f"len={len(frame)} hex={frame.hex()}"
        )

    def _debug_dump_request(self, frame: bytes) -> None:
        """记录一次收到的 C->S 完整帧字节到 request_debug.log。

        排查「切池/翻页请求为何缺失」的关键取证：请求帧很小（9~15 字节），
        单个 TCP 包即一帧。判断逻辑：
        - 若某段期间响应帧在、本文件却没有请求帧 → 该方向流重组失步或包未捕获；
        - 若请求帧进来了但 app.log 无对应「切池/翻页请求」→ parse_pool_request 解析失败
          （部分原因在 pool_view 的「未识别的 C->S 请求帧」日志里）。
        与 frame_debug.log 同用 RotatingFileHandler 限制总占用。
        """
        _request_log().info(
            f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
            f"len={len(frame)} hex={frame.hex()}"
        )

    def refresh_ui(self) -> None:
        """刷新保底主面板与状态栏。"""
        _t0 = time.perf_counter()
        prof = self.profiles.get(self._active_profile_id)
        account_name = prof.name if prof else "?"
        # 手动补丁（按账号）只在展示层合成，绝不写回 self.stats.records（会被
        # history.merge 持久化）。有补丁时用影子引擎算面板，真实引擎零污染。
        display_stats = self.stats
        manual_bound = None
        try:
            patches = load_pool_patches(self._active_profile_id)
            if patches and self.stats.records:
                manual, aug, bound = synthesize_records(
                    self._pools_eff, self.characters, self.pity_groups,
                    patches, self.stats.records,
                )
                if manual:
                    display_stats = build_patched_engine(self.stats, manual, aug)
                    manual_bound = bound
        except Exception as e:
            logger.warning(f"合成手动补丁失败，本次按无补丁展示: {e}")
        self.pity_panel.update_data(
            display_stats, self.characters, self._pools_eff,
            account_name, self._last_capture_iso, manual_bound,
        )
        self._refresh_status()
        # 未映射ID：N>0 时单独显示按钮作提醒，菜单项文案同步
        n_unknown = len(self.stats.unknown_id_stats()) + len(self._unknown_pool_ids())
        self.btn_unknown.setText(self._unknown_label())
        self.btn_unknown.setVisible(n_unknown > 0)
        self._act_unknown_menu.setText(self._unknown_label())
        self._log_slow("refresh_ui(面板重建)", _t0)

    def _open_maintenance(self) -> None:
        """「抽卡数据维护」：手动补齐缺失历史。仅展示层生效、不改真实记录。"""
        editable = compute_editable(
            self._pools_eff, self.pity_groups, self.stats.records
        )
        existing = load_pool_patches(self._active_profile_id)
        dlg = MaintenanceDialog(
            self,
            pools=self._pools_eff,
            characters=self.characters,
            pity_groups=self.pity_groups,
            real_records=self.stats.records,
            editable=editable,
            existing=existing,
        )
        if dlg.exec() == QDialog.Accepted:
            save_pool_patches(self._active_profile_id, dlg.result_patches())
            self.refresh_ui()

    def _show_detail_dialog(self) -> None:
        """弹出每抽明细窗口（原常驻表格，现降级为按需查看）。"""
        prof = self.profiles.get(self._active_profile_id)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"抽取明细 - {prof.name if prof else ''}")
        dlg.resize(900, 600)
        layout = QVBoxLayout(dlg)
        table = RecordTable()
        table.update_records(self._filter_records(), self.characters, self._pools_eff)
        layout.addWidget(table)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        layout.addWidget(btn_close)
        dlg.exec()

    def _unknown_label(self) -> str:
        """未映射ID按钮文案（带数量，含角色ID与卡池ID）。"""
        n = len(self.stats.unknown_id_stats()) + len(self._unknown_pool_ids())
        return f"未映射ID({n})" if n else "未映射ID"

    def _unknown_pool_ids(self) -> Dict[str, int]:
        """记录中出现但元数据未收录的卡池ID -> 出现次数。"""
        counts = Counter(r.pool_id for r in self.stats.records)
        return {pid: n for pid, n in counts.items() if pid not in self.pools}

    def _show_unknown_id_dialog(self) -> None:
        """弹出未映射ID管理对话框（角色ID屏蔽 + 陌生卡池分组指派）。

        陌生角色ID默认按6星处理；若某ID出现次数过多（不可能是6星），勾选"屏蔽"
        将其按4/5星处理。可取消勾选还原（防误触）。屏蔽集与 StatsEngine 共享
        同一 set 对象，mutate 后下次刷新即生效。

        陌生卡池ID默认按独立卡池计算；新卡池通常属于 UP/遴选共享组，可在此
        指派并入对应组一起跑共享保底（也可保持独立不修改）。
        """
        unknowns = self.stats.unknown_id_stats()
        unknown_pools = self._unknown_pool_ids()
        if not unknowns and not unknown_pools:
            QMessageBox.information(self, "未映射ID", "当前没有未映射的角色/卡池ID。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("未映射ID管理")
        dlg.resize(480, 460)
        layout = QVBoxLayout(dlg)

        if unknowns:
            layout.addWidget(QLabel(
                "以下角色ID不在卡池元数据中，保底计算默认按6星处理。\n"
                "若某ID出现次数过多（不可能是6星），勾选\"屏蔽\"将其按4/5星处理。\n"
                "屏蔽仅对未收录ID生效，元数据收录后自动失效。"
            ))
            table = QTableWidget(len(unknowns), 4)
            table.setHorizontalHeaderLabels(["ID", "出现次数", "当前处理", "屏蔽"])
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            ids_sorted = sorted(unknowns.keys(), key=lambda x: (-unknowns[x], x))
            for row, iid in enumerate(ids_sorted):
                shielded = iid in self._shields
                table.setItem(row, 0, QTableWidgetItem(iid))
                table.setItem(row, 1, QTableWidgetItem(str(unknowns[iid])))
                table.setItem(row, 2, QTableWidgetItem("已屏蔽(4/5星)" if shielded else "按6星"))
                cb = QCheckBox()
                cb.setChecked(shielded)
                cb.toggled.connect(
                    lambda checked, iid=iid, tbl=table: self._on_shield_toggled(iid, checked, tbl)
                )
                table.setCellWidget(row, 3, cb)
            layout.addWidget(table, 1)

        if unknown_pools:
            # 可指派的目标组：有状态机的共享组（UP/遴选）；"" 表示保持独立卡池
            group_opts = [("", "独立卡池(默认)")] + [
                (g.id, g.label) for g in self.pity_groups.values() if g.has_state
            ]
            layout.addWidget(QLabel(
                "以下卡池ID不在元数据中，默认按独立卡池计算。\n"
                "新卡池通常属于 UP/遴选 池，选择对应组可并入该组共享保底。\n"
                "指派仅对未收录卡池生效，元数据收录后自动失效。"
            ))
            pool_table = QTableWidget(len(unknown_pools), 3)
            pool_table.setHorizontalHeaderLabels(["卡池ID", "出现次数", "并入保底组"])
            pool_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            pool_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            pids_sorted = sorted(unknown_pools.keys(), key=lambda x: (-unknown_pools[x], x))
            for row, pid in enumerate(pids_sorted):
                assigned = self._pool_assignments.get(pid, "")
                pool_table.setItem(row, 0, QTableWidgetItem(pid))
                pool_table.setItem(row, 1, QTableWidgetItem(str(unknown_pools[pid])))
                combo = QComboBox()
                for gkey, glabel in group_opts:
                    combo.addItem(glabel, gkey)
                idx = next((i for i, (gkey, _) in enumerate(group_opts) if gkey == assigned), 0)
                combo.setCurrentIndex(idx)
                combo.currentIndexChanged.connect(
                    lambda _, pid=pid, cmb=combo: self._on_pool_group_changed(pid, cmb.currentData())
                )
                pool_table.setCellWidget(row, 2, combo)
            layout.addWidget(pool_table, 1)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        layout.addWidget(btn_close)
        dlg.exec()
        self.refresh_ui()

    def _on_pool_group_changed(self, pid: str, gkey: str) -> None:
        """陌生池指派变更：持久化、重建生效池映射、刷新主面板。

        gkey 为空串表示取消指派（回到独立卡池）。生效池映射直接换掉
        StatsEngine.pools 引用，分组解析在下次统计时自然按新映射执行，
        统计算法本身不变。
        """
        if gkey:
            self._pool_assignments[pid] = gkey
        else:
            self._pool_assignments.pop(pid, None)
        save_pool_assignments(self._pool_assignments)
        self._pools_eff = apply_pool_assignments(
            self.pools, self.pity_groups, self._pool_assignments
        )
        self.stats.pools = self._pools_eff
        self.refresh_ui()

    def _on_shield_toggled(self, iid: str, checked: bool, table: QTableWidget) -> None:
        """屏蔽/取消屏蔽单个陌生ID：更新集合、持久化、刷新行状态与主面板。"""
        if checked:
            self._shields.add(iid)
        else:
            self._shields.discard(iid)
        save_shields(self._shields)
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item and item.text() == iid:
                table.item(row, 2).setText("已屏蔽(4/5星)" if checked else "按6星")
                break
        self.refresh_ui()

    def _filter_records(self) -> List[GachaRecord]:
        """过滤记录（暂仅全部卡池）。"""
        return self.stats.records

    def export_excel(self) -> None:
        """导出 Excel（默认文件名带当前账号名）。"""
        prof = self.profiles.get(self._active_profile_id)
        name = safe_filename(prof.name) if prof else "all"
        default_name = name + "_抽卡记录.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", default_name, "Excel 文件 (*.xlsx)"
        )
        if not path:
            return
        try:
            # 有手动补丁时按影子引擎导出（含补齐命中 + (补) 标记）；填充占位不落明细
            records = self.stats.records
            stats = self.stats
            bound = None
            try:
                patches = load_pool_patches(self._active_profile_id)
                if patches and self.stats.records:
                    manual, aug, bnd = synthesize_records(
                        self._pools_eff, self.characters, self.pity_groups,
                        patches, self.stats.records,
                    )
                    if manual:
                        records = [
                            r for r in (self.stats.records + manual)
                            if r.item_id != MANUAL_FILL_ID
                        ]
                        stats = build_patched_engine(self.stats, manual, aug)
                        bound = bnd
            except Exception as e:
                logger.warning(f"导出时合成手动补丁失败，按无补丁导出: {e}")
            export_excel(
                records,
                self.characters,
                self._pools_eff,
                path,
                stats=stats,
                manual_bound_by_group=bound,
            )
            self.status_label.setText(f"已导出: {path}")
            QMessageBox.information(self, "导出成功", f"已保存到:\n{path}")
        except Exception as e:
            logger.exception("导出失败")
            QMessageBox.critical(self, "导出失败", str(e))

    # ---- U1 跨端记录文件互通（导出/导入） ----
    def _export_sync_file(self) -> None:
        """导出记录文件（跨端互通）：封装信封 + **原样**写盘。

        取材直接读盘（`sync_apply.load_device_accounts`）——非抓包态下磁盘即真相；
        写盘走 `sync_apply.write_export_file`（`newline=""`，LF 原样），保证「同一份
        内容在任何平台逐字节相同」，两端样本可直接对哈希。UI **不重算任何判据**。
        """
        try:
            accounts = sync_apply.load_device_accounts(self.profiles)
        except Exception as e:
            logger.exception("读取账号快照失败")
            QMessageBox.critical(self, "导出失败", f"读取账号数据失败：{e}")
            return
        if not accounts:
            QMessageBox.information(self, "导出记录文件", "当前没有可导出的账号。")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        # 默认文件名（含「当前账号 / 全部账号」范围）由对话框自己按范围现算
        # （`ExportSyncDialog._refresh`）；此处只把戳交过去，避免两边各写一份命名规则。
        dlg = ExportSyncDialog(accounts, self._active_profile_id, stamp, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        picked = dlg.selected_accounts()
        if not picked:
            return
        try:
            env_accounts = [
                sync_file.build_export_account(
                    a.get("id"), a.get("name"), a.get("records") or [],
                    a.get("total"), a.get("view_totals") or {},
                )
                for a in picked
            ]
            envelope = sync_file.build_envelope(
                env_accounts,
                exported_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                source={
                    "platform": "desktop",
                    "app": "jinlin-gacha-exporter",
                    "app_version": __version__,
                },
            )
            sync_apply.write_export_file(dlg.path(), sync_file.dumps(envelope))
        except Exception as e:
            logger.exception("导出记录文件失败")
            QMessageBox.critical(self, "导出失败", f"写入文件失败：{e}")
            return
        n_records = sum(len(a.get("records") or []) for a in picked)
        self.status_label.setText(f"已导出记录文件: {dlg.path()}")
        QMessageBox.information(
            self,
            "导出成功",
            f"已导出 {len(picked)} 个账号 · {n_records} 条记录：\n{dlg.path()}\n\n"
            f"可在另一端的「导入记录文件」里使用；本机原有记录不受影响。",
        )

    def _import_sync_file(self) -> None:
        """导入记录文件（跨端互通）：一次提交全部账号，整组原子落库。

        判据全部取自纯函数层（`sync_file.parse` / `default_choices` / `plan_import`），
        落库全部交给事务壳（`sync_apply.apply_plan`）—— UI 只收集用户选择，**不重写
        任何判据**（两端的「什么算冲突 / 谁更新」必须与纯函数层逐字一致）。
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "导入记录文件", "", "记录文件 (*.json)"
        )
        if not path:
            return
        text = sync_apply.read_text_file(path)
        if text is None:
            QMessageBox.warning(self, "导入记录文件", "文件无法读取，请确认文件是否存在。")
            return
        parsed = sync_file.parse(text)
        if not parsed.get("ok"):
            # 解析失败根本进不到选择页（契约 §10-d）：直接弹错误框，不产生任何写盘
            QMessageBox.warning(self, "无法导入", parse_error_text(parsed.get("error")))
            return
        device = sync_apply.load_device_accounts(self.profiles)
        choices = sync_file.default_choices(parsed, device)
        dlg = ImportSyncDialog(parsed, device, choices, os.path.basename(path), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            plan = dlg.plan()
        except ValueError as e:  # 调用方 bug（choices 与文件账号不匹配等）
            logger.warning(f"导入计划产出失败: {e}")
            QMessageBox.warning(self, "选择有误", str(e))
            return
        if not plan.get("accounts"):
            # 全部用户都选了「不导入」⇒ 没有任何写盘。事务壳的「计划为空」守卫是
            # **防御性断言**（表达调用方 bug），不用它表达用户的合法选择（定案稿 §6 裁定）。
            logger.info("导入：全部用户均选择不导入，未写盘")
            QMessageBox.information(
                self, "没有要导入的内容", "全部用户都选择了「不导入」，没有需要写入的记录。")
            return
        result = sync_apply.apply_plan(self.profiles, plan)
        if not result.ok:
            logger.warning(f"导入记录文件失败: {result.error}")
            QMessageBox.critical(self, "导入失败", result.error or "导入未完成。")
            return
        # 本批可能新建账号、也可能覆盖当前账号 ⇒ 重建账号列表并从磁盘重载内存态。
        # 用 _reload_from_disk（**不 merge**）：内存里若有残留会话记录，merge 会把
        # 它们回灌进刚导入的文件（与「还原」同款陷阱）。
        self._populate_accounts()
        self._reload_from_disk()
        self._update_title()
        changed = [w for w in result.written if w.get("changed")]
        backups = [w.get("backup") for w in result.written if w.get("backup")]
        lines = [f"{w['name']}：{w['records']} 条" for w in result.written]
        msg = "已导入：\n" + "\n".join(lines)
        if not changed:
            msg += "\n\n（所选内容与现有数据一致，未产生改动。）"
        if backups:
            msg += "\n\n已自动备份一次，可在「数据清理 → 还原」里找回。"
        # 必读结论（契约 §9）：坐标互不干涉，是两套独立基准
        msg += (
            "\n\n提示：导入进来的历史与本端坐标系相互独立，"
            "下次抓包将按所选那一份的总抽数继续对齐。"
        )
        self.status_label.setText("已导入记录文件")
        QMessageBox.information(self, "导入完成", msg)

    def clear_records(self) -> None:
        """清空当前会话记录与当前账号本地历史。

        “清空”的本意是放弃当前账号的旧本地历史、从当前抓取开始重新积累，
        因此同时清空内存统计与该账号的 users/<id>.json。清空前会自动把旧数据
        备份为带时间戳的文件，便于后续手动恢复。因该操作不可恢复（仅留备份），
        需用户二次确认。
        """
        reply = QMessageBox.question(
            self,
            "清空历史",
            "清空将删除当前账号的所有本地历史记录（清空前会自动备份）。\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.stats.clear()
        backup = self.history.clear()
        self.parser = GachaParser()
        self._pool_tracker.reset()
        self.refresh_ui()
        if backup:
            self.status_label.setText("已清空，历史已备份")
            QMessageBox.information(
                self,
                "已清空并备份",
                f"当前账号历史已清空，旧数据已备份到：\n{backup}\n\n"
                f"如需恢复，请退出程序后将该备份文件重命名为 {Path(self.history.history_file).name}。",
            )
        else:
            self.status_label.setText("已清空")

    def _restore_data(self) -> None:
        """从备份还原当前账号的抽卡数据（进程内重载，不重启）。

        流程：
        1. 若正在抓包，先提示并自动停止（先把本会话记录并入当前文件，等价于
           手动「停止抓包」的落库动作——stop_capture 里的「未检测到历史重叠」
           确认框不适用于此处，直接 finalize）；
        2. 扫描该账号的历史备份（用户/备份文件规约 `<pid>_<时间戳>.json`，
           含 _N 后缀），无则提示结束；
        3. 弹出选择还原点（显示人类可读时间而非原始时间戳）；
        4. 二次确认；
        5. 执行：restore() 先把当前文件备份成当天时间戳、再将所选备份覆盖为
           当前文件；
        6. 内存从磁盘（已还原）重载——**不 merge**。关键：还原前内存可能还
           装着本会话的污染记录，若按切账号那般先 merge 会把污染又写回刚还原
           干净的文件；重载后 stats 装的即还原数据，任何后续 merge（关窗/停止）
           写回的都是干净数据，回灌通道自然堵死。
        """
        # —— 1. 正在抓包：提示并自动停止（先把本会话并入当前文件） ——
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "正在抓包",
                "还原前需先停止抓包（本次已抓的少量记录会先保存，不会丢失）。\n确定停止并继续还原？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._stop_worker(2000)  # 还原前确保旧线程结束，避免残留回调干扰还原后状态
            try:
                was_empty = self.history.is_empty()  # 会话开始前历史状态（重叠自检用）
                # self.stats.finalize()  # 旧账本收尾钩子已注释（位置对齐方案下为空操作）
                self.history.merge(
                    self.stats.session_records, self._pool_tracker.view_totals
                )
                session_overlap = self.stats.overlapped_history
                # 以落库记录为准重建内存（合并时新记录的 pos 已按 Δ 归一化；
                # 保底消费序与事件内连续性判定都依赖统一坐标系）
                self.stats.load_records(self.history.records, replace=True)
                self._log_invariant_alarms(
                    notify=True, had_history=not was_empty,
                    session_overlap=session_overlap
                )  # 即将丢弃内存副本，缺口要弹
                self.stats.begin_new_session()
            except Exception as e:
                # 落库失败不能静默继续：随后 _reload_from_disk 会丢弃内存中的
                # 本会话记录，造成永久丢失，且与确认框「不会丢失」的承诺矛盾。
                # 中止还原，警示口径与 _finalize_capture_session 的失败处理一致。
                logger.exception("还原前保存当前会话失败")
                QMessageBox.warning(
                    self,
                    "保存当前会话失败",
                    f"还原前保存本次抓取的记录失败：{e}\n\n"
                    f"已中止还原。本次记录仍在程序内存中，请检查磁盘空间、"
                    f"杀毒软件/同步盘后重试；直接关闭程序时会再尝试保存。",
                )
                return
        # —— 2. 扫描备份 ——
        backups = self.history.backups()
        if not backups:
            QMessageBox.information(self, "还原抽卡数据", "该账号暂无可用备份。")
            return
        # —— 3. 选择还原点（人类可读时间，倒序） ——
        items = [b.label for b in backups]
        item, ok = QInputDialog.getItem(
            self, "选择还原点", "要还原到哪个备份？（按时间倒序，可反复还原）", items, 0, False
        )
        if not ok:
            return
        chosen = backups[items.index(item)]
        # —— 4. 二次确认 ——
        reply = QMessageBox.question(
            self,
            "确认还原",
            f"将把当前账号数据还原到 {chosen.label}。\n\n"
            f"还原前当前数据会自动备份为今天的时间戳文件，不会丢失。\n"
            f"确认后立即刷新当前界面为还原数据。\n\n确定还原？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # —— 5. 执行还原（备份当前 + 覆盖为所选） ——
        try:
            backup_cur = self.history.restore(chosen.path)
        except Exception as e:
            logger.exception("还原数据失败")
            QMessageBox.critical(self, "还原失败", f"还原数据时出错：{e}")
            return
        # —— 6. 内存从磁盘重载（不 merge） ——
        self._reload_from_disk()
        if backup_cur:
            self.status_label.setText("已还原，界面已刷新")
            QMessageBox.information(
                self,
                "还原完成",
                f"已还原到 {chosen.label}。\n\n"
                f"还原前的数据已备份到：\n{backup_cur}\n\n"
                f"（如需退回还原前的状态，同样可通过「还原抽卡数据」再次还原。）",
            )
        else:
            self.status_label.setText("已还原，界面已刷新")

    def _reload_from_disk(self) -> None:
        """从磁盘（当前已还原的文件）重建全部内存态，**不执行 merge**。

        与 `_switch_account` 的尾部重建等价，但省略其开头对原账号的 merge：
        还原场景下当前文件刚被换成备份，若再读 stats.records 去 merge 会把
        还原前的残留会话又写回还原文件。重建后 stats 装的是还原数据，此后
        任何 merge 写回的都是干净数据。
        """
        self.history = HistoryService(self._active_profile_id)
        self.stats = StatsEngine(self.characters, self._pools_eff, self.pity_groups, self._shields)
        self.parser = GachaParser()
        self._pool_tracker = PoolViewTracker()
        self.session = SessionContext(current_pool_id="all")
        self._current_pool_id = "all"
        self._load_history()

    def closeEvent(self, event) -> None:  # noqa: N802
        """窗口关闭时合并历史（增量，不覆盖）。"""
        # 关窗需尽量让抓包线程结束：当前线程与先前短等待残留的退休线程一并
        # 停止，避免退出进程时 QThread 仍在运行被析构。线程无流量时会等下一个
        # 包才退出，故当前线程给足 2s；退休线程共享同一截止时间——不逐线程
        # 累加等待（快速多次开始/停止可能残留多线程，避免关窗拖到 N×2s）。
        deadline = time.monotonic() + 2.0
        if self.worker:
            self.worker.stop()
            self.worker.quit()
            remain = int(max(0, deadline - time.monotonic()) * 1000)
            self.worker.wait(remain)
        for w in list(self._retired_workers):
            if not w.isRunning():
                continue
            remain = int(max(0, deadline - time.monotonic()) * 1000)
            if remain <= 0:
                break
            w.stop()
            w.quit()
            w.wait(remain)
        self._prune_retired()
        try:
            had_history_before_merge = not self.history.is_empty()  # 会话开始前状态（重叠自检用）
            # self.stats.finalize()  # 旧账本收尾钩子已注释（位置对齐方案下为空操作，保留接线）
            self.history.merge(
                self.stats.session_records, self._pool_tracker.view_totals
            )
            session_overlap = self.stats.overlapped_history
            # 以落库记录为准重建内存（合并时新记录的 pos 已按 Δ 归一化；
            # 保底消费序与事件内连续性判定都依赖统一坐标系）
            self.stats.load_records(self.history.records, replace=True)
            # 退出前最后一次机会：抓得不全要在这里说清，否则用户只会觉得
            # "统计数字对不上"而找不到原因（缺口签名去重，同一缺口不重复弹）。
            self._log_invariant_alarms(
                notify=True, had_history=had_history_before_merge,
                session_overlap=session_overlap
            )
        except Exception as e:
            logger.warning(f"关闭时保存历史失败: {e}")
        event.accept()
