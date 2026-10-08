"""程序入口 - 初始化日志、加载角色/卡池、启动 GUI。"""
import os
import sys
import threading
import webbrowser

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QProgressDialog

from . import __version__
from .loaders.remote_data import (
    check_health,
    load_announcements,
    load_role_data,
    load_version_info,
    set_remote_available,
)
from .gui.announcement_dialog import AnnouncementDialog
from .gui.main_window import MainWindow
from .gui.npcap_dialog import NPCAP_DOWNLOAD_URL, NpcapDependencyDialog, find_bundled_npcap_installer
from .gui.theme import apply_light_theme
from .gui.update_dialog import UpdateDialog
from .models import DataNotReadyError
from .storage.announcements import load_read_ids
from .storage.coord_migration import migrate_coord_frame
from .storage.profiles import ProfileManager
from .storage.settings import (
    clear_rejected_forced_version,
    load_acknowledged_version,
    load_rejected_forced_version,
    migrate_global_discovered_addresses,
)
from .utils.helpers import check_npcap_installed
from .utils.logger import get_logger, setup_logging
from .utils.paths import get_data_dir, get_logs_dir, migrate_legacy_flat_files
from .utils.version import is_newer

logger = get_logger(__name__)

# 主窗口强引用持有者：_on_init_done 是 Qt 信号槽，window 若是局部变量，槽返回
# 后 Python 引用被回收，PySide6/shiboken 会删除无父的顶层 QWidget -> 窗口闪
# 现即消失，`quitOnLastWindowClosed` 随之退出整个应用。必须持有一份存活到
# app.exec() 结束的引用。
_main_window = None


def cleanup_legacy_pcap(logs_dir: str) -> None:
    """删除历史版本无条件落盘的 capture_*.pcap（2026-08-24 起仅 debug 模式才写）。

    旧版本每次抓包把原始流量原样写盘（上限 256MB）；新版打包态不再产生
    （仅 debug 模式，见 debug_flags），启动时把历史 capture_*.pcap 一次性
    清掉，避免继续占用磁盘。仅删此通配，不动其他文件。
    """
    import glob

    removed = 0
    for p in glob.glob(os.path.join(logs_dir, "capture_*.pcap")):
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            logger.warning(f"清理历史 pcap 失败: {p}: {e}")
    if removed:
        logger.info(f"已清理 {removed} 个历史 capture_*.pcap")


def _current_up_character(pools) -> str:
    """已加载卡池中 pool_id 最大的那个的 up 角色（视为"当前"卡池）。"""
    if not pools:
        return "未知"
    try:
        max_pid = max(pools, key=lambda pid: int(pid))
    except ValueError:
        max_pid = max(pools)
    pool = pools[max_pid]
    return pool.up_character or pool.name or "未知"


def _handle_npcap_missing(parent) -> str:
    """Npcap 未安装/不可用：弹依赖说明并按用户选择拉起安装/下载流程。

    返回用户选择的动作：
    - "install"/"download"/"exit"：程序随后退出（需用户在程序外完成安装后重启）
    - "view"：进入仅查看模式（不退出，可查看/导出已有记录，不能抓包）
    """
    bundled = find_bundled_npcap_installer()
    dlg = NpcapDependencyDialog(bundled, parent=parent)
    dlg.exec()
    action = dlg.chosen_action

    if action == "install" and bundled:
        try:
            os.startfile(bundled)  # 拉起随包安装向导（会触发 UAC）
        except Exception as e:
            logger.warning(f"启动随包 Npcap 安装包失败: {e}")
            QMessageBox.warning(parent, "无法启动安装包", f"未能启动随包安装包：\n{e}\n请改用「前往官网下载」。")
    elif action == "download":
        webbrowser.open(NPCAP_DOWNLOAD_URL)

    if action == "view":
        logger.info("Npcap 依赖处理：用户选择「仅查看已有数据」，进入仅查看模式")
    else:
        logger.info(f"Npcap 依赖处理：用户选择「{action}」，退出程序等待安装/重启")
    return action


class _InitWorker(QThread):
    """启动初始化后台线程（第二批 E）。

    把原本阻塞 GUI 的启动序列（Npcap 检测 / 远程拉取 role·公告·版本 / 账号档案
    准备）移进线程：GUI 不再冻结（原窗口前 10s + 窗口后公告/版本各 10s 阻塞
    消除）。每步经 step 信号更新「正在初始化」进度文案；全部完成 emit done
    （结果 dict）；致命失败 emit failed(信息, 是否弹严重框)。
    """

    step = Signal(str)
    done = Signal(object)
    failed = Signal(str, bool)  # (message, show_critical_box)

    def run(self) -> None:
        try:
            self.step.emit("检测 Npcap…")
            installed, working = check_npcap_installed()

            self.step.emit("拉取远程信息…")
            # D：先健康探测（3s 上限），失败则三个远程加载器整体回退
            # （role→缓存 / 公告→[] / 版本→None），不再逐接口 10s 串行超时。
            set_remote_available(check_health())
            characters, pools, pity_groups, remote_ok = load_role_data()
            announcements = load_announcements()
            version_info = load_version_info()

            self.step.emit("准备账号档案…")
            profiles = ProfileManager()
            # 旧版全局抽卡地址迁移为按账号隔离（须先于 MainWindow 读取设置）
            migrate_global_discovered_addresses([p.id for p in profiles.list_profiles()])

            # 历史坐标统一（幂等：已统一则零写入、零备份）。必须早于
            # MainWindow 构造 —— HistoryService 在窗内构造，读到即已统一，
            # 不会出现「内存旧、磁盘新」。失败不阻断启动（内部已逐账号记日志）。
            self.step.emit("整理历史坐标…")
            try:
                migrate_coord_frame()
            except Exception:
                logger.exception("历史坐标统一迁移整体失败，已跳过")

            self.done.emit({
                "characters": characters,
                "pools": pools,
                "pity_groups": pity_groups,
                "remote_ok": remote_ok,
                "announcements": announcements,
                "version_info": version_info,
                "npcap_ok": bool(installed and working),
                "profiles": profiles,
            })
        except DataNotReadyError:
            logger.exception("无可用角色/卡池数据")
            self.failed.emit(
                "无法加载角色/卡池数据（远程与本地缓存均失败），请检查网络或联系管理员。",
                True,
            )
        except Exception:
            logger.exception("启动初始化失败")
            self.failed.emit("启动初始化失败，详见日志。", False)


def _prewarm_lazy_deps() -> None:
    """后台预热懒加载的重依赖（scapy ~3s / openpyxl ~0.2s）。

    engine / address_discovery / excel 已改为函数内懒加载，启动路径不再导入；
    window.show() 后起 daemon 线程在这里预热，用户点「开始抓包」/首次导出时
    命中 sys.modules，不再现导（现导意味着首次抓包开头漏包、首次导出卡顿）。
    打包态同样生效：scapy / openpyxl 已作为 hidden-import 打进 exe。
    """
    import scapy.all  # noqa: F401  预热；实际使用走各函数的 from scapy.all import ...
    import openpyxl  # noqa: F401


def main() -> int:
    """主函数。"""
    # 迁移旧平铺布局到子目录（须早于 setup_logging，避免 app.log 句柄占用导致移动失败）
    migrate_legacy_flat_files()

    setup_logging()

    # 确保用户数据目录存在
    get_data_dir()

    # 历史 pcap 清理：新版打包态不再写 pcap（仅 debug 模式），启动清掉旧的，
    # 避免历史原始流量继续占用磁盘。
    cleanup_legacy_pcap(get_logs_dir())

    # QApplication 需先于任何弹窗创建
    # Windows 任务栏图标：显式设置 AppUserModelID，任务栏才会跟随窗口图标
    # （setWindowIcon）而不是回退到 exe/Python 默认图标
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("example-key.gacha.exporter")
        except Exception as e:
            logger.debug(f"设置 AppUserModelID 失败（忽略）: {e}")
    app = QApplication(sys.argv)

    # 固定浅色主题：不跟随系统深色模式，保证所有用户界面一致（问题反馈 5）
    apply_light_theme(app)

    # E：启动初始化进度窗——阻塞序列（Npcap 检测 / 远程拉取 / 账号档案准备）
    # 移入后台线程，GUI 不再冻结（原窗口前 10s + 窗口后公告/版本各 10s 阻塞
    # 消除）；每步更新文案，跑完再建主窗口。
    splash = QProgressDialog("正在初始化…", "", 0, 0, None)
    splash.setWindowTitle("金鳞抽卡导出工具")
    splash.setWindowModality(Qt.WindowModal)
    splash.setCancelButton(None)
    splash.setMinimumDuration(0)
    splash.setAutoClose(False)
    splash.setAutoReset(False)
    splash.setLabelText("正在初始化…")
    splash.show()
    app.processEvents()  # 让进度窗先渲染出来，避免黑屏空窗

    worker = _InitWorker()
    worker.step.connect(splash.setLabelText)
    worker.failed.connect(lambda msg, critical: _on_init_failed(app, splash, msg, critical))
    worker.done.connect(lambda result: _on_init_done(app, splash, result))
    worker.start()

    return app.exec()


def _on_init_failed(app, splash, message: str, critical: bool) -> None:
    """初始化致命失败：关进度窗，DataNotReadyError 弹严重框后退出。"""
    splash.close()
    if critical:
        QMessageBox.critical(None, "无法启动", message)
    logger.info(f"启动初始化失败，退出: {message}")
    app.exit(1)


def _on_init_done(app, splash, result: dict) -> None:
    """初始化完成（GUI 线程）：关进度窗，建主窗口并按原语义继续。

    原启动后半段（remote_ok 确认 / Npcap 处理 / 公告 / 版本检查）在窗口
    show 后执行；公告与版本数据已由初始化线程拉好，直接应用，不再有网络阻塞。
    历史加载在 MainWindow 构造内（本地 JSON 读，通常毫秒级），保留在 GUI 线程。
    """
    splash.close()
    characters = result["characters"]
    pools = result["pools"]
    pity_groups = result["pity_groups"]
    remote_ok = result["remote_ok"]
    profiles = result["profiles"]
    npcap_ok = result["npcap_ok"]
    announcements = result["announcements"]
    version_info = result["version_info"]

    # 远程没拉到最新数据（回退了缓存/内置）：让用户确认是否继续
    if not remote_ok:
        up = _current_up_character(pools)
        msg = QMessageBox()
        msg.setWindowTitle("卡池信息未更新")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(
            f"未获取到最新卡池信息，当前已缓存卡池信息中 up 角色为「{up}」，"
            f"请确认是否继续进行抽卡信息导出。"
        )
        btn_continue = msg.addButton("继续", QMessageBox.AcceptRole)
        msg.addButton("取消", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() is not btn_continue:
            logger.info("用户取消，退出程序")
            app.exit(1)
            return

    global _main_window

    window = MainWindow(characters, pools, pity_groups, profiles)
    _main_window = window  # 持强引用，防槽返回后窗口被回收销毁（见模块顶部注释）

    # 强制更新同步闸门（建窗后、show 前）：上轮拒绝强更 -> 本轮启动直接禁用
    # 「开始抓包」（仍可查看历史/导出；后续版本检查到已最新会解除）。
    rejected = load_rejected_forced_version()
    if rejected and is_newer(rejected, __version__):
        logger.info(f"检测到上轮拒绝的强制更新 {rejected}，禁用「开始抓包」")
        window.set_forced_update(True)

    window.show()

    # 后台预热懒加载的重依赖（scapy ~3s / openpyxl ~0.2s）：窗口已可见即可
    # 交互，首次「开始抓包」/导出时命中最热，不再现导（见 _prewarm_lazy_deps）。
    threading.Thread(target=_prewarm_lazy_deps, daemon=True).start()

    # Npcap 依赖检测：抓包强依赖该开源组件。未安装/不可用时弹依赖说明，
    # 用户选「安装随包」/「前往官网」则拉起安装流程后退出（安装完成需重启本程序），
    # 选「不同意，退出」直接退出；选「仅查看已有数据」则进入仅查看模式，
    # 程序继续运行（公告/版本检查等照常），但「开始抓包」置灰不可用。
    # 检测结果已由初始化线程探得（npcap_ok），此处只处理缺失分支。
    if not npcap_ok:
        action = _handle_npcap_missing(window)
        if action != "view":
            app.exit(0)
            return
        window.enter_npcap_viewonly()

    # 公告：初始化线程已拉好数据，仅按未读弹窗（不再有网络阻塞）。
    # singleShot(0) 让窗口先渲染，再弹公告。
    def _maybe_show_announcement():
        if not announcements:
            return
        unread = [a for a in announcements if a.id not in load_read_ids()]
        if not unread:
            return
        # 合并弹窗展示所有未读（服务端已按 type+ts 排序，dialog 内再分类）
        AnnouncementDialog(unread, parent=window).exec()

    QTimer.singleShot(0, _maybe_show_announcement)

    # 版本检查：初始化线程已拉好 version_info，仅应用更新决策（不再有网络阻塞）。
    def _maybe_check_update():
        info = version_info
        if info is None:
            return
        if not is_newer(info.latest_version, __version__):
            # 已是最新：解除此前的强更拒绝记录，恢复正常抓包
            clear_rejected_forced_version()
            window.set_forced_update(False)
            return
        if info.update_type == "silent":
            # 静默小更新：不弹窗（用户可在设置页手动检查）
            return
        if info.update_type == "force":
            dlg = UpdateDialog(info, mode="force", parent=window)
            if dlg.exec() != QDialog.Accepted:
                # 用户拒绝强更：本次会话禁用「开始抓包」（持久化由对话框完成）
                window.set_forced_update(True)
            return
        # prompt：latest != acknowledged 才弹，避免反复打扰
        if info.latest_version != load_acknowledged_version():
            UpdateDialog(info, mode="prompt", parent=window).exec()

    QTimer.singleShot(0, _maybe_check_update)

    # 启动时主动检查当前账号是否有抽卡地址（问题反馈 6：地址因账号而异，
    # 缺失时提前引导获取，而不是抓不到才提示）。排在公告/更新之后，避免
    # 遮挡更重要的弹窗。
    QTimer.singleShot(0, window.check_account_address)


if __name__ == "__main__":
    sys.exit(main())
