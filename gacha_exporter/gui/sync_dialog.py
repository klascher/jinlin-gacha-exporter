# -*- coding: utf-8 -*-
"""跨端记录文件互通 —— PC 侧**导出 / 导入对话框**。

依据：`U1互通层-契约冻结与实现定案-2026-09-21.md`
- §5 「作为新用户导入」路径的 UI 语义（隐藏目标账号 / 使用哪一份 / 丢弃提示 / 三集合三行）
- §8 文案清单（与 Android **逐条同义**）
- §9 PC 导入对话框布局稿（单页：Step1 + Step2 并页，多账号用顶部切换器）

⚠️ 本模块**只收集用户选择**，不落库、不读文件、不判版本：
判据一律来自纯函数层（`sync_file.parse` 的 `ParsedFile`），落库一律交给事务壳
（`sync_apply.apply_plan`）。UI 里**不重写任何判据** —— 两端的「什么算冲突 / 谁更新」
必须与纯函数层逐字一致。
"""
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..storage.sync_file import (
    CHOSEN_DEVICE,
    CHOSEN_FILE,
    MODE_MERGE,
    MODE_NEW,
    MODE_SKIP,
    NEWER_FILE,
    plan_import,
)
from ..utils.helpers import safe_filename
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 导出范围（与「导出范围」单选项一一对应）
SCOPE_CURRENT = "current"
SCOPE_ALL = "all"

_EXPORT_TITLE = "导出记录文件"
_IMPORT_TITLE = "导入记录文件"
_JSON_FILTER = "记录文件 (*.json)"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _fmt_ts(ts: Any) -> str:
    """毫秒时间戳 → 「MM-DD HH:MM」；空值 / 脏值显示占位符（不抛）。"""
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _fmt_iso(text: Any) -> str:
    """ISO 时间串 → 「MM-DD HH:MM」；解析不出就原样返回（不抛）。"""
    if not isinstance(text, str) or not text:
        return "—"
    try:
        return datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        return text


def display_names(accounts: List[Dict[str, Any]]) -> List[str]:
    """账号展示名：**文件内同名是合法的**（§4 边界表）⇒ 重名加「(1)/(2)」后缀区分。"""
    counts: Dict[str, int] = {}
    for a in accounts:
        name = a.get("name") or ""
        counts[name] = counts.get(name, 0) + 1
    seen: Dict[str, int] = {}
    out: List[str] = []
    for a in accounts:
        name = a.get("name") or ""
        if counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            out.append(f"{name} ({seen[name]})")
        else:
            out.append(name)
    return out


def default_export_name(scope: str, account_name: str, stamp: str) -> str:
    """导出默认文件名。

    全部：`jinlin-records-all-<stamp>.json`
    当前：`jinlin-records-<账号名>-<stamp>.json`（账号名过 `safe_filename`）

    ⚠️ 前缀 = 产品名 `jinlin`（金鳞）。信封内部的 `format` 字段
    （`sync_file.FORMAT_NAME`）**同为 `jinlin-records`**（09-21 由 `example-key-records` 改名）
    —— 但两者是两码事：文件名给人看，`format` 给机器校验。
    """
    if scope == SCOPE_CURRENT:
        safe = safe_filename(account_name) or "account"
        return f"jinlin-records-{safe}-{stamp}.json"
    return f"jinlin-records-all-{stamp}.json"


# ---------------------------------------------------------------------------
# ① 导出对话框
# ---------------------------------------------------------------------------

class ExportSyncDialog(QDialog):
    """导出记录文件：选**范围**（当前账号 / 全部账号）+ 选**落点**。"""

    def __init__(self, device_accounts: List[Dict[str, Any]], active_pid: str,
                 stamp: str, parent=None):
        """初始化。

        Args:
            device_accounts: 设备侧全部账号快照（`sync_apply.load_device_accounts`）。
            active_pid: 当前激活账号 id（决定「当前账号」是哪一条）。
            stamp: 时间戳串（`YYYYMMDD_HHMM`），用于生成**随范围联动**的默认文件名。
        """
        super().__init__(parent)
        self._accounts = list(device_accounts or [])
        self._active_pid = active_pid
        self._stamp = stamp
        self._auto_name = ""   # 最近一次**自动生成**的文件名（判断用户是否手改过路径）
        self.setWindowTitle(_EXPORT_TITLE)
        self.resize(560, 250)
        self._setup_ui()
        self._refresh()

    # ---- UI ----
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        total_records = sum(len(a.get("records") or []) for a in self._accounts)
        active = self._active_account()
        active_count = len((active or {}).get("records") or [])

        self.radio_current = QRadioButton(
            f"当前账号（{active.get('name') if active else '—'} · {active_count} 条）"
        )
        self.radio_all = QRadioButton(
            f"全部账号（{len(self._accounts)} 个 · {total_records} 条）"
        )
        self._scope_group = QButtonGroup(self)
        self._scope_group.addButton(self.radio_current)
        self._scope_group.addButton(self.radio_all)
        self.radio_current.setChecked(True)
        layout.addWidget(self.radio_current)
        layout.addWidget(self.radio_all)

        layout.addWidget(QLabel("保存到："))
        path_row = QHBoxLayout()
        initial = default_export_name(SCOPE_CURRENT, self._active_name(), self._stamp)
        self._auto_name = initial
        self.edit_path = QLineEdit(initial)
        self.btn_browse = QPushButton("浏览…")
        path_row.addWidget(self.edit_path, 1)
        path_row.addWidget(self.btn_browse)
        layout.addLayout(path_row)

        self.lbl_hint = QLabel(
            "导出的文件可在另一端的「导入记录文件」里使用；本机原有记录不受影响。"
        )
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#888; font-size:11px;")
        layout.addWidget(self.lbl_hint)
        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_ok = QPushButton("导出")
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_ok)
        layout.addLayout(btn_row)

        self.btn_browse.clicked.connect(self._on_browse)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok.clicked.connect(self._on_ok)
        # 范围变化 ⇒ 默认文件名联动（见 `_refresh`）
        self.radio_current.toggled.connect(self._refresh)
        self.radio_all.toggled.connect(self._refresh)

    def _refresh(self) -> None:
        """范围变化时同步默认文件名（**用户手改过的路径不覆盖**，只换文件名部分）。

        `QButtonGroup` 里切换单选会让两个 `toggled` 各触发一次 ⇒ 本函数可能被调用
        两次；每次都由 `scope()` 现算，重复调用无害。判「是否手改过」的判据 =
        「当前文件名是否仍等于上次自动生成的名字」。
        """
        name = default_export_name(self.scope(), self._active_name(), self._stamp)
        current = self.edit_path.text().strip()
        if not current or os.path.basename(current) == self._auto_name:
            folder = os.path.dirname(current)
            self.edit_path.setText(os.path.join(folder, name) if folder else name)
        self._auto_name = name

    def _active_account(self) -> Optional[Dict[str, Any]]:
        for a in self._accounts:
            if a.get("id") == self._active_pid:
                return a
        return self._accounts[0] if self._accounts else None

    def _active_name(self) -> str:
        """当前账号展示名（无账号时退化为 `account`）。"""
        active = self._active_account()
        return (active or {}).get("name") or "account"

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, _EXPORT_TITLE, self.edit_path.text().strip() or "jinlin-records.json",
            _JSON_FILTER,
        )
        if path:
            self.edit_path.setText(path)

    def _on_ok(self) -> None:
        path = self.edit_path.text().strip()
        if not path:
            QMessageBox.warning(self, _EXPORT_TITLE, "请先选择保存位置。")
            return
        if not path.lower().endswith(".json"):
            path += ".json"
            self.edit_path.setText(path)
        self.accept()

    # ---- 结果 ----
    def scope(self) -> str:
        """导出范围：`SCOPE_CURRENT` / `SCOPE_ALL`。"""
        return SCOPE_CURRENT if self.radio_current.isChecked() else SCOPE_ALL

    def path(self) -> str:
        """目标文件绝对路径（已补 `.json` 后缀）。"""
        return self.edit_path.text().strip()

    def selected_accounts(self) -> List[Dict[str, Any]]:
        """按当前范围返回要导出的账号快照。"""
        if self.scope() == SCOPE_ALL:
            return list(self._accounts)
        active = self._active_account()
        return [active] if active else []


# ---------------------------------------------------------------------------
# ② 导入对话框（§9 单页布局）
# ---------------------------------------------------------------------------

def parse_error_text(error: Any) -> str:
    """错误码 → 用户可读文案（§8 文案：与 Android **同句**）。

    `CORRUPT` 带上「第 N 个账号」 —— 纯函数层给的是 0 基下标，这里转 1 基。
    """
    code = (error or {}).get("code") if isinstance(error, dict) else None
    detail = str((error or {}).get("detail") or "") if isinstance(error, dict) else ""
    if code == "BAD_FORMAT":
        return "不是本工具导出的记录文件。"
    if code == "VERSION_TOO_HIGH":
        return "文件由更新版本导出，请先升级本工具。"
    if code == "NO_ACCOUNTS":
        return "文件里没有账号数据。"
    if code == "CORRUPT":
        idx = None
        if "account#" in detail:
            try:
                idx = int(detail.split("account#", 1)[1].split(" ", 1)[0].split(":", 1)[0])
            except ValueError:
                idx = None
        if idx is None:
            return "文件内容损坏。"
        return f"文件内容损坏（第 {idx + 1} 个账号）。"
    return "文件无法读取。"


class ImportSyncDialog(QDialog):
    """导入记录文件（单页 = Step1 + Step2 并页；多账号用顶部切换器）。

    用户选择由本对话框持有（`choices()`），**切换账号不清空任何已选配置**（定案稿 §6.2-c）。
    """

    def __init__(self, parsed: Dict[str, Any], device_accounts: List[Dict[str, Any]],
                 choices: List[Dict[str, Any]], file_name: str, parent=None):
        """初始化。

        Args:
            parsed: `sync_file.parse()` 的成功结果。
            device_accounts: `sync_apply.load_device_accounts()` 的快照。
            choices: `sync_file.default_choices()` 的初值（本对话框内可改）。
            file_name: 源文件名（仅展示）。
        """
        super().__init__(parent)
        self._parsed = parsed
        self._accounts = list(parsed.get("accounts") or [])
        self._device = list(device_accounts or [])
        self._choices = [dict(c) for c in choices]
        self._file_name = file_name
        self._idx = 0
        self._names = display_names(self._accounts)
        self.setWindowTitle(_IMPORT_TITLE)
        self.resize(620, 520)
        self._setup_ui()
        self._load_index(0)

    # ---- UI 构建 ----
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        total_records = sum(len(a.get("records") or []) for a in self._accounts)
        src = self._parsed.get("source") or {}
        platform = src.get("platform") if isinstance(src, dict) else None
        layout.addWidget(QLabel(f"文件　{self._file_name}"))
        from_lbl = QLabel(
            f"来自：{platform or '未知来源'} · {_fmt_iso(self._parsed.get('exported_at'))}"
            f" · 共 {len(self._accounts)} 个账号 · {total_records} 条"
        )
        from_lbl.setStyleSheet("color:#888; font-size:11px;")
        layout.addWidget(from_lbl)

        # —— 顶部账号切换器（切换不清空任何已选）——
        nav_row = QHBoxLayout()
        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        self.btn_prev.setFixedWidth(36)
        self.btn_next.setFixedWidth(36)
        self.lbl_nav = QLabel("")
        nav_row.addWidget(self.btn_prev)
        nav_row.addWidget(self.lbl_nav, 1)
        nav_row.addWidget(self.btn_next)
        layout.addLayout(nav_row)

        # —— 导入方式 ——
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("导入方式　"))
        self.radio_new = QRadioButton("作为新用户导入")
        self.radio_merge = QRadioButton("合并到已有用户")
        self.radio_skip = QRadioButton("不导入这个用户")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.radio_new)
        self._mode_group.addButton(self.radio_merge)
        self._mode_group.addButton(self.radio_skip)
        mode_row.addWidget(self.radio_new)
        mode_row.addWidget(self.radio_merge)
        mode_row.addWidget(self.radio_skip)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # —— 目标账号（仅合并路径显示）——
        self.target_row = QWidget()
        target_lay = QHBoxLayout(self.target_row)
        target_lay.setContentsMargins(0, 0, 0, 0)
        target_lay.addWidget(QLabel("目标账号　"))
        self.combo_target = QComboBox()
        for a in self._device:
            self.combo_target.addItem(f"{a.get('name')}（{len(a.get('records') or [])} 条）",
                                      a.get("id"))
        target_lay.addWidget(self.combo_target, 1)
        layout.addWidget(self.target_row)

        # —— 对照表（应用里 / 文件里）——
        self.cmp_box = QWidget()
        self.cmp_grid = QGridLayout(self.cmp_box)
        self.cmp_grid.setContentsMargins(0, 6, 0, 6)
        layout.addWidget(self.cmp_box)

        # —— 三集合拆解行已删（2026-09-22 用户裁定）——
        # 原「两边都有 X 条 ／ 仅手机有 Y 条 ／ 仅文件有 Z 条」与上方对照表的
        # 「最新抽卡 / 记录条数」重复，属噪音。三集合数值仍在纯函数层参与判定
        # （冲突判据用 `both`；丢弃提示用 `device_only` / `file_only`），只是不再单独展示。

        # —— 冲突提示（仅合并路径显示）——
        self.lbl_conflict = QLabel("⚠️ 两边数据不重合，存在冲突")
        self.lbl_conflict.setStyleSheet("color:#c0392b;")
        layout.addWidget(self.lbl_conflict)

        # —— 使用哪一份（仅合并路径显示）——
        self.choose_row = QWidget()
        choose_lay = QVBoxLayout(self.choose_row)
        choose_lay.setContentsMargins(0, 6, 0, 0)
        choose_lay.addWidget(QLabel("使用哪一份"))
        choose_line = QHBoxLayout()
        self.radio_file = QRadioButton("")
        self.radio_device = QRadioButton("")
        self._chosen_group = QButtonGroup(self)
        self._chosen_group.addButton(self.radio_file)
        self._chosen_group.addButton(self.radio_device)
        choose_line.addWidget(self.radio_file)
        choose_line.addWidget(self.radio_device)
        choose_line.addStretch(1)
        choose_lay.addLayout(choose_line)
        layout.addWidget(self.choose_row)

        # —— 丢弃提示（仅合并路径显示）——
        self.lbl_discard = QLabel("")
        self.lbl_discard.setWordWrap(True)
        self.lbl_discard.setStyleSheet("color:#c0392b;")
        layout.addWidget(self.lbl_discard)

        # —— 新建路径的单列说明 / 无基准提示 / 空账号提示 ——
        self.lbl_new_note = QLabel("")
        self.lbl_new_note.setWordWrap(True)
        layout.addWidget(self.lbl_new_note)

        self.lbl_anchor = QLabel(
            "⚠️ 该文件未提供总抽数基准，导入后首次抓包可能重复计入已在文件里的记录"
        )
        self.lbl_anchor.setWordWrap(True)
        self.lbl_anchor.setStyleSheet("color:#c0392b;")
        layout.addWidget(self.lbl_anchor)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_ok = QPushButton("确认导入")
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_ok)
        layout.addLayout(btn_row)

        # —— 接线 ——
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.radio_new.toggled.connect(self._on_mode_toggled)
        self.radio_merge.toggled.connect(self._on_mode_toggled)
        self.radio_skip.toggled.connect(self._on_mode_toggled)
        self.combo_target.currentIndexChanged.connect(self._on_target_changed)
        self.radio_file.toggled.connect(self._on_chosen_toggled)
        self.radio_device.toggled.connect(self._on_chosen_toggled)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok.clicked.connect(self._on_ok)

    # ---- 账号切换 ----
    def _step(self, delta: int) -> None:
        new_idx = self._idx + delta
        if not (0 <= new_idx < len(self._accounts)):
            return
        self._store_current()
        self._load_index(new_idx)

    def _load_index(self, idx: int) -> None:
        """载入第 idx 个账号的已存配置（**不清空**：配置一直放在 `self._choices` 里）。"""
        self._idx = idx
        choice = self._choices[idx]
        for w in (self.radio_new, self.radio_merge, self.radio_skip,
                  self.radio_file, self.radio_device):
            w.blockSignals(True)
        self.combo_target.blockSignals(True)

        if choice.get("mode") == MODE_MERGE:
            self.radio_merge.setChecked(True)
        elif choice.get("mode") == MODE_SKIP:
            self.radio_skip.setChecked(True)
        else:
            self.radio_new.setChecked(True)
        target = choice.get("target")
        if target is not None:
            pos = self.combo_target.findData(target)
            if pos >= 0:
                self.combo_target.setCurrentIndex(pos)
        if choice.get("chosen") == CHOSEN_DEVICE:
            self.radio_device.setChecked(True)
        else:
            self.radio_file.setChecked(True)

        for w in (self.radio_new, self.radio_merge, self.radio_skip,
                  self.radio_file, self.radio_device):
            w.blockSignals(False)
        self.combo_target.blockSignals(False)

        self.btn_prev.setEnabled(idx > 0)
        self.btn_next.setEnabled(idx < len(self._accounts) - 1)
        self._refresh()

    def _store_current(self) -> None:
        """把当前控件状态写回 `self._choices[self._idx]`（切换账号前调用）。"""
        idx = self._idx
        if self.radio_new.isChecked():
            mode, target = MODE_NEW, None
        elif self.radio_skip.isChecked():
            # 「不导入」：`target` / `chosen` 照存**不清空**（定案稿 §6.2-c）—— 用户改回
            # 「合并」时原目标必须还在。取控件现值（而不是写 `None`），这样
            # 「选 SKIP → 切到别的账号 → 再切回来」目标也不会丢。
            mode, target = MODE_SKIP, self.combo_target.currentData()
        else:
            mode, target = MODE_MERGE, self.combo_target.currentData()
        self._choices[idx] = {
            "index": idx,
            "mode": mode,
            "target": target,
            "chosen": CHOSEN_DEVICE if self.radio_device.isChecked() else CHOSEN_FILE,
        }

    # ---- 交互 ----
    def _on_mode_toggled(self, checked: bool) -> None:
        if not checked:
            return
        self._refresh()

    def _on_target_changed(self, _index: int) -> None:
        self._refresh()

    def _on_chosen_toggled(self, checked: bool) -> None:
        if not checked:
            return
        self._refresh()

    # ---- 渲染 ----
    def _device_account(self) -> Optional[Dict[str, Any]]:
        """合并目标对应的设备侧账号（**只有「合并」路径**才有；`NEW` / `SKIP` 返回 None）。"""
        if not self.radio_merge.isChecked():
            return None
        pid = self.combo_target.currentData()
        for a in self._device:
            if a.get("id") == pid:
                return a
        return None

    def _refresh(self) -> None:
        """按当前账号 + 当前模式重绘可见行与全部数字。**判据全部取自纯函数层结果。**"""
        account = self._accounts[self._idx]
        file_records = account.get("records") or []
        device = self._device_account()
        merge = device is not None
        skip = self.radio_skip.isChecked()

        self.lbl_nav.setText(
            f"账号 {self._idx + 1}/{len(self._accounts)} · "
            f"{self._names[self._idx]}（{len(file_records)} 条）"
        )

        # 行可见性：新建路径按 §5 隐藏「目标账号 / 使用哪一份 / 丢弃提示」；
        # 「不导入」路径隐藏同一批，且**连对照表一起收掉**，只留一行说明。
        self.target_row.setVisible(merge)
        self.choose_row.setVisible(merge)
        self.lbl_conflict.setVisible(merge)
        self.lbl_discard.setVisible(merge)
        self.cmp_box.setVisible(not skip)
        self.lbl_new_note.setVisible(skip or not merge)
        if skip:
            self.lbl_new_note.setText(
                f"不导入「{account.get('name')}」：文件里的 {len(file_records)} 条不写入，"
                f"应用里原有的也不变"
            )
        elif not merge:
            self.lbl_new_note.setText(
                f"将新建「{account.get('name')}」，共 {len(file_records)} 条"
            )

        # 无基准提示（与路径无关：`has_anchor=false` 就该提示）
        intg = account.get("integrity") or {}
        if "has_anchor" in intg:
            has_anchor = bool(intg.get("has_anchor"))
        else:
            has_anchor = account.get("total") is not None
        self.lbl_anchor.setVisible(not has_anchor)

        if merge:
            cmp = self._compare(device, account)
            self.radio_file.setText(f"文件里的（{cmp['file_count']} 条）")
            self.radio_device.setText(f"应用里的（{cmp['device_count']} 条）")
            # 仅一方非空不算冲突（§10-c）；两侧都非空且零重叠才提示
            conflict = (cmp["both"] == 0 and cmp["device_count"] > 0
                        and cmp["file_count"] > 0)
            self.lbl_conflict.setVisible(conflict)
            discard = cmp["device_only"] if self.radio_file.isChecked() else cmp["file_only"]
            self.lbl_discard.setText(
                f"⚠️ 未选择的那份将被丢弃（本次丢弃 {discard} 条）"
            )
            self._render_compare(device, account, cmp, single=False)
        elif not skip:
            self._render_compare(None, account, None, single=True)
        elif getattr(self, "_lbl_empty", None):
            # SKIP：对照表整块不渲染，顺带把「该账号没有记录」那行也收掉
            self._lbl_empty.setVisible(False)

    @staticmethod
    def _compare(device: Dict[str, Any], account: Dict[str, Any]) -> Dict[str, Any]:
        """复用纯函数层的 `compare()`（**不在这里重算三集合**）。"""
        from ..storage.sync_file import compare

        return compare(device.get("records") or [], account.get("records") or [])

    def _render_compare(self, device: Optional[Dict[str, Any]], account: Dict[str, Any],
                        cmp: Optional[Dict[str, Any]], single: bool) -> None:
        """重绘对照表。`single=True`（新建路径）只显示「文件里」一列。"""
        while self.cmp_grid.count():
            item = self.cmp_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        file_count = len(account.get("records") or [])
        file_max = max((r.get("timestamp") for r in (account.get("records") or [])),
                       default=None)

        if single:
            self.cmp_grid.addWidget(QLabel("文件里"), 0, 1)
            self.cmp_grid.addWidget(QLabel("最新抽卡"), 1, 0)
            self.cmp_grid.addWidget(QLabel(_fmt_ts(file_max)), 1, 1)
            self.cmp_grid.addWidget(QLabel("记录条数"), 2, 0)
            self.cmp_grid.addWidget(QLabel(str(file_count)), 2, 1)
        else:
            dev_records = (device or {}).get("records") or []
            dev_count = len(dev_records)
            dev_max = max((r.get("timestamp") for r in dev_records), default=None)
            header_file = "文件里"
            if cmp and cmp.get("newer") == NEWER_FILE:
                header_file = "文件里 ← 新"
            self.cmp_grid.addWidget(QLabel("应用里"), 0, 1)
            self.cmp_grid.addWidget(QLabel(header_file), 0, 2)
            self.cmp_grid.addWidget(QLabel("最新抽卡"), 1, 0)
            self.cmp_grid.addWidget(QLabel(_fmt_ts(dev_max)), 1, 1)
            self.cmp_grid.addWidget(QLabel(_fmt_ts(file_max)), 1, 2)
            self.cmp_grid.addWidget(QLabel("记录条数"), 2, 0)
            self.cmp_grid.addWidget(QLabel(str(dev_count)), 2, 1)
            self.cmp_grid.addWidget(QLabel(str(file_count)), 2, 2)

        empty = file_count == 0
        if empty and not getattr(self, "_lbl_empty", None):
            self._lbl_empty = QLabel("该账号没有记录")
            self._lbl_empty.setStyleSheet("color:#888;")
            self.layout().addWidget(self._lbl_empty)
        if getattr(self, "_lbl_empty", None):
            self._lbl_empty.setVisible(empty)

    # ---- 结果 ----
    def _on_ok(self) -> None:
        self._store_current()
        self.accept()

    def choices(self) -> List[Dict[str, Any]]:
        """当前全部账号的选择（与 `parsed.accounts` 一一对应）。"""
        self._store_current()
        return [dict(c) for c in self._choices]

    def plan(self) -> Dict[str, Any]:
        """产出落库执行单（`SyncPlan`）。**校验在纯函数层**，不合法会抛 `ValueError`。"""
        return plan_import(self._parsed, self._device, self.choices())
