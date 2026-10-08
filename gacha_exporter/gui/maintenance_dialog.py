"""抽卡数据维护对话框 - 手动补齐缺失历史。

依据「缺失边界可补、已抓数据不可改」原则，仅对两类槽开放编辑：
- 锚点池（组内最早有真实记录的池）：补记录开始前垫的抽数（leading）。
- 补录池（组内无真实记录、比锚点更旧的严格前缀）：整段补六星序列（items，
  draw 为该池内绝对抽数、严格递增）。

编辑结果仅在展示层合成（见 analysis/manual_patches），保存到 data/pool_patches.json
（按账号），永不写回真实历史。

交互设计（2026-08-28 §3.26 之后的 UX 批）：
- 每池三态徽标：已保存 ✓ / 未保存 ● / 有误 ⚠（显示在左侧列表项）。
- 右侧「保存此池」就地落草稿；底部「保存」全校验提交。
- 非法输入就地红框 + 红字原因，不再只表现为「保存被禁」。
- 底部保存：有误 -> 弹窗列原因（可只存合法项）；有未保存项 -> 询问是否一并保存
  （全部保存 / 不保存这些 / 取消）。
- 切走不弹窗：当前池内容自动留在草稿，左栏持续显示「未保存 ●」。
严格校验（§3.26，角色必须来自候选名单、抽数 0-70）同样适用于已存档补丁。
"""
import copy
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 单池手动补齐的上限（抽）：补录池整段 / 锚点垫抽均不得超过硬保底周期内的一池抽数。
MAX_DRAWS = 70

# 状态徽标配色
_RED = QColor("#c0392b")
_RED_BG = QColor("#fdecea")

from ..analysis.manual_patches import build_patched_engine, synthesize_records
from ..analysis.stats import StatsEngine
from ..loaders.pool_groups import resolve_groups
from ..models import Character, GachaRecord, PityGroup, Pool
from ..utils.logger import get_logger

logger = get_logger(__name__)


def _resolve_char(chars: Dict[str, Character], text: str) -> str:
    """把用户输入（角色名或 id）解析为 item_id；无法识别则按未知 ID 处理。"""
    t = text.strip()
    if not t:
        return ""
    if t in chars:
        return t
    for cid, c in chars.items():
        if c.name == t:
            return cid
    return t  # 未知 ID（§3.11 兜底）


def _char_label(chars: Dict[str, Character], item_id: str) -> str:
    c = chars.get(item_id)
    return c.name if c else f"未知({item_id})"


def _pool_candidates(pool: Pool, chars: Dict[str, Character]) -> Dict[str, str]:
    """该补录池可选的「当期 UP + 常驻六星」候选，id -> 展示名。

    - 常驻：所有 rarity==6 且非限定（limited:false）的角色，任何池都可选。
    - 当期 UP：池元数据里的 up_character 回查（限定），排在最前，去重。
    返回按「UP 在前、其余按 id 稳定排」的顺序。严格只允许从这里选，名单外不放开。
    """
    out: Dict[str, str] = {}
    if pool.up_character:
        for cid, c in chars.items():
            if c.name == pool.up_character:
                out[cid] = c.name
                break
        else:
            # 池 UP 名字未能回查到具体角色（如未收录），仍保留文本项以便选中
            out[f"up:{pool.id}"] = pool.up_character
    for cid, c in chars.items():
        if c.rarity == 6 and not c.limited:
            out.setdefault(cid, c.name)
    return out


class MaintenanceDialog(QDialog):
    """手动补齐缺失历史的主对话框。"""

    def __init__(self, parent, pools: Dict[str, Pool], characters: Dict[str, Character],
                 pity_groups: Dict[str, PityGroup], real_records: List[GachaRecord],
                 editable: Dict[str, dict], existing: Dict[str, dict]):
        super().__init__(parent)
        self.pools = pools
        self.characters = characters
        self.pity_groups = pity_groups
        self.real_records = real_records
        self.editable = editable
        self._patches: Dict[str, dict] = {pid: dict(p) for pid, p in existing.items()}
        self._slots: List[dict] = []      # 可选中的编辑槽（锚点/补录）
        self._slot_map: Dict[str, dict] = {}
        self._current_slot: Optional[dict] = None
        # 每池「已保存」时的补丁快照：现状与快照一致 => 已保存；不一致 => 未保存。
        # 载入的既有补丁默认视为已保存；「保存此池」后落快照；后续任何改动即转未保存。
        self._committed_snap: Dict[str, dict] = {
            pid: copy.deepcopy(p) for pid, p in self._patches.items()
            if p.get("leading", 0) or p.get("items")
        }
        # 每池「有角色但『第几抽』非法（非正整数/解析失败）」的行，pid -> {行号: 原因}。
        # _gather_editor_into_patches 每次对当前池重建，其它池记录保留——这样切走后保存
        # 仍能拦截，避免非法行被静默丢弃、保存却成功（见 待办清单 §0.5 中项）。
        self._draw_errors: Dict[str, Dict[int, str]] = {}
        # 编辑部件引用（当前槽渲染期间有效）
        self._backfill_table: Optional[QTableWidget] = None
        self._backfill_candidates: Dict[str, str] = {}
        self._leading_spin: Optional[QSpinBox] = None
        self._anchor_err_label: Optional[QLabel] = None
        self._current_backfill_pool: Optional[str] = None
        self._current_anchor_pool: Optional[str] = None
        self._candidate_cache: Dict[str, Dict[str, str]] = {}
        self._ok_btn = None

        self.setWindowTitle("抽卡数据维护 - 手动补齐缺失历史")
        self.resize(820, 560)
        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "仅可「补齐缺失边界」：锚点池补开头垫抽、前缀缺史池整段补录；"
            "已抓到数据（首个六星之后）不可改。按账号保存。"
        ))

        self.left = QListWidget()
        self.left.setFixedWidth(330)
        self._stack = QStackedWidget()
        split = QHBoxLayout()
        split.addWidget(self.left, 0)
        split.addWidget(self._stack, 1)
        root.addLayout(split, 1)

        # ---- 底部状态行 + 保存（全校验） ----
        self._footer_label = QLabel("")
        self._footer_label.setWordWrap(True)
        root.addWidget(self._footer_label)
        self._build_footer_buttons(root)
        self._populate_slots()
        if self._slots:
            first_row = self.left.row(self._slots[0]["item"])
            self.left.setCurrentRow(first_row)
            self._select_slot(self._slots[0])
        else:
            self.preview_label.setText(
                "当前没有可编辑的缺失边界。\n\n可编辑需满足：某保底分组内有真实记录，"
                "且存在「比最早记录池更旧、完全无记录」的严格前缀池。")
            self._ok_btn.setEnabled(False)
        self.left.currentRowChanged.connect(self._on_list_row_changed)

    # ---- 底部按钮（保存/取消 + 状态） ----
    def _build_footer_buttons(self, root: QVBoxLayout) -> None:
        preview_title = QLabel("▶ 实时预览（受影响组的共享保底，编辑角色/抽数即刷新）")
        preview_title.setStyleSheet("font-weight:bold; color:#1f5f8f; margin-top:6px;")
        root.addWidget(preview_title)
        self.preview_label = QLabel(
            "在上方选择编辑槽、填入角色与抽数后，这里会实时显示受影响保底分组的"
            "共享保底顺延；无变更时显示提示。")
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(
            "color:#333; background:#fafafa; border:1px solid #e5e5e5; padding:6px;")
        root.addWidget(self.preview_label)

        btns = QDialogButtonBox()
        self._ok_btn = btns.addButton("保存", QDialogButtonBox.AcceptRole)
        cancel_btn = btns.addButton("取消", QDialogButtonBox.RejectRole)
        self._ok_btn.setEnabled(True)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ---- 槽位列表 ----
    def _populate_slots(self) -> None:
        self._slots.clear()
        self._slot_map.clear()
        for gkey, m in self.editable.items():
            if not m["has_record"]:
                continue
            head = QListWidgetItem(f"—— {m['label']} ——")
            head.setFlags(QListWidgetItem().flags() & ~Qt.ItemIsEnabled)  # 组头不可选
            self.left.addItem(head)
            if m["anchor"]:
                pid = m["anchor"]
                key = f"anchor:{pid}"
                it = QListWidgetItem("")
                self.left.addItem(it)
                self._slots.append({"key": key, "kind": "anchor", "gkey": gkey, "pool_id": pid, "item": it})
                self._slot_map[key] = self._slots[-1]
            for pid in m["backfillable"]:
                key = f"backfill:{pid}"
                it = QListWidgetItem("")
                self.left.addItem(it)
                self._slots.append({"key": key, "kind": "backfill", "gkey": gkey, "pool_id": pid, "item": it})
                self._slot_map[key] = self._slots[-1]
        self._refresh_slot_labels()

    def _slot_name(self, pid: str) -> str:
        pool = self.pools.get(pid)
        return pool.name if pool else pid

    # ---- 每池状态 ----
    def _pool_status(self, pid: str) -> str:
        """返回 empty / error / saved / unsaved 之一。"""
        p = self._patches.get(pid)
        if not p or not (p.get("leading", 0) or p.get("items")):
            return "empty"
        try:
            self._validate_pool(pid, p)
        except ValueError:
            return "error"
        return "saved" if self._committed_snap.get(pid) == p else "unsaved"

    _BADGE = {"saved": "·已保存✓", "unsaved": "·未保存●", "error": "·有误⚠"}

    def _refresh_slot_labels(self) -> None:
        for s in self._slots:
            pid = s["pool_id"]
            p = self._patches.get(pid)
            name = self._slot_name(pid)
            badge = self._BADGE.get(self._pool_status(pid), "")
            if s["kind"] == "anchor":
                led = p.get("leading", 0) if p else 0
                s["item"].setText(f"【锚点】{name} 开头垫抽{led}{badge}")
            else:
                n = len(p.get("items", [])) if p else 0
                s["item"].setText(f"【补录】{name}（已填{n}个六星）{badge}")

    # ---- 槽切换 ----
    def _row_to_slot(self, row: int) -> Optional[dict]:
        if not (0 <= row < self.left.count()):
            return None
        for s in self._slots:
            if s["item"] is self.left.item(row):
                return s
        return None

    def _on_list_row_changed(self, row: int) -> None:
        slot = self._row_to_slot(row)
        if slot:
            self._select_slot(slot)

    def _select_slot(self, slot: dict) -> None:
        if self._current_slot:  # 先落盘离开槽的内容（切走不弹窗、保留在草稿）
            self._gather_editor_into_patches()
        self._current_slot = slot
        while self._stack.count():
            w = self._stack.widget(0)
            self._stack.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        if slot["kind"] == "anchor":
            self._stack.addWidget(self._build_anchor_editor(slot))
        else:
            self._stack.addWidget(self._build_backfill_editor(slot))
        self._refresh()

    # ---- 编辑器构建 ----
    def _build_anchor_editor(self, slot: dict) -> QWidget:
        pid = slot["pool_id"]
        self._current_anchor_pool = pid
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(f"卡池：{self._slot_name(pid)}"))
        v.addWidget(QLabel(
            f"「记录开始前垫了多少抽」（≤{MAX_DRAWS}）：该池第一条真实记录之前的垫抽数，"
            "即记录缺失的边界抽数。它会把该池首个六星的顺位/整组共享保底顺延。"))
        self._leading_spin = QSpinBox()
        self._leading_spin.setRange(0, MAX_DRAWS)
        self._leading_spin.setValue(self._patches.get(pid, {}).get("leading", 0))
        row = QHBoxLayout()
        row.addWidget(QLabel("开头垫抽数"))
        row.addWidget(self._leading_spin)
        row.addStretch()
        v.addLayout(row)
        self._anchor_err_label = QLabel("")
        self._anchor_err_label.setStyleSheet(f"color:{_RED.name()};")
        self._anchor_err_label.setWordWrap(True)
        v.addWidget(self._anchor_err_label)
        v.addStretch()
        v.addLayout(self._pool_action_row(pid))
        self._leading_spin.valueChanged.connect(self._refresh)
        return w

    def _build_backfill_editor(self, slot: dict) -> QWidget:
        pid = slot["pool_id"]
        self._current_backfill_pool = pid
        self._backfill_candidates = _pool_candidates(self.pools[pid], self.characters)
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(f"卡池：{self._slot_name(pid)}"))
        v.addWidget(QLabel(
            f"逐条填「第 N 抽出 角色」：N 为该池内绝对抽数、严格递增（≤{MAX_DRAWS} 抽）。"
            "角色从下拉选，仅限当期 UP 与常驻六星，可输入角色名过滤。"))
        self._backfill_table = QTableWidget(0, 3)
        self._backfill_table.setHorizontalHeaderLabels(["角色", "第几抽", "问题"])
        self._backfill_table.horizontalHeader().setStretchLastSection(True)
        self._backfill_table.setColumnWidth(0, 220)
        self._backfill_table.setColumnWidth(1, 70)
        v.addWidget(self._backfill_table)
        for item in self._patches.get(pid, {}).get("items", []):
            self._add_backfill_row(_char_label(self.characters, item["char"]), item["draw"])
        btn = QHBoxLayout()
        b_add = QPushButton("＋ 加一条")
        b_del = QPushButton("－ 删选中")
        b_add.clicked.connect(lambda: self._add_backfill_row("", 1))
        b_del.clicked.connect(self._del_backfill_row)
        btn.addWidget(b_add)
        btn.addWidget(b_del)
        btn.addStretch()
        v.addLayout(btn)
        v.addStretch()
        v.addLayout(self._pool_action_row(pid))
        self._backfill_table.itemChanged.connect(self._refresh)
        return w

    def _pool_action_row(self, pid: str) -> QHBoxLayout:
        """该池就地操作：保存此池 / 还原此池。"""
        lay = QHBoxLayout()
        save = QPushButton("保存此池 ✓")
        save.setStyleSheet("font-weight:bold; color:#1f5f8f;")
        save.setToolTip("把当前填写内容落为「已保存」草稿；有误项需先修正才能保存。")
        save.clicked.connect(self._save_current_pool)
        reset = QPushButton("还原此池")
        reset.setStyleSheet(f"color:{_RED.name()};")
        reset.setToolTip("删除此池的补丁（回到未填写）。")
        reset.clicked.connect(lambda: self._reset_pool(pid))
        lay.addWidget(save)
        lay.addStretch()
        lay.addWidget(reset)
        return lay

    def _save_current_pool(self) -> None:
        cur = self._current_slot
        if cur is None:
            return
        pid = cur["pool_id"]
        self._gather_editor_into_patches()
        p = self._patches.get(pid)
        if p is None or not (p.get("leading", 0) or p.get("items")):
            QMessageBox.information(self, "保存此池", "该池尚未填写任何内容。")
            return
        try:
            self._validate_pool(pid, p)
        except ValueError as e:
            self._refresh()  # 确保红框/红字可见
            QMessageBox.warning(self, "此池有误", str(e))
            return
        self._committed_snap[pid] = copy.deepcopy(p)
        self._refresh_slot_labels()
        self._refresh_footer()
        self._update_preview()

    def _make_role_combo(self, char_text: str) -> QComboBox:
        """建一个「当期 UP + 常驻六星」候选、可输入过滤的模糊角色下拉。"""
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)  # 只允许选现成项，输入仅用于过滤
        combo.addItems(list(self._backfill_candidates.values()))
        # 已载入的补丁可能引用名单外的旧/未知角色：作为额外项保留，不强制丢
        if char_text and combo.findText(char_text) < 0:
            combo.addItem(char_text)
        # 模糊过滤：含子串 + 大小写不敏感
        completer = QCompleter(combo.model(), combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        combo.setCompleter(completer)
        if char_text:
            combo.setCurrentText(char_text)
        combo.editTextChanged.connect(self._refresh)
        return combo

    def _add_backfill_row(self, char_text: str, draw: int) -> None:
        r = self._backfill_table.rowCount()
        self._backfill_table.insertRow(r)
        combo = self._make_role_combo(char_text)
        self._backfill_table.setCellWidget(r, 0, combo)
        di = QTableWidgetItem(str(draw))
        di.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        di.setFlags(di.flags() | Qt.ItemIsEditable)
        self._backfill_table.setItem(r, 1, di)
        tip = QTableWidgetItem("")   # 「问题」列，就地红字原因
        tip.setFlags(Qt.ItemIsEnabled)
        self._backfill_table.setItem(r, 2, tip)

    def _del_backfill_row(self) -> None:
        sel = self._backfill_table.currentRow()
        if sel >= 0:
            self._backfill_table.removeRow(sel)
            self._refresh()

    def _reset_pool(self, pid: str) -> None:
        """删除此池补丁回到未填写。必须先清空当前编辑器控件再删补丁——否则 _refresh 第一步
        _gather_editor_into_patches 会把编辑器残留内容重新写回补丁，还原失效（还原后原样回来）。
        blockSignals 防止清空动作触发 valueChanged/itemChanged 回环再次 _refresh。"""
        if self._current_slot and self._current_slot["pool_id"] == pid:
            if self._current_slot["kind"] == "anchor":
                if self._leading_spin is not None:
                    self._leading_spin.blockSignals(True)
                    self._leading_spin.setValue(0)
                    self._leading_spin.blockSignals(False)
                if self._anchor_err_label:
                    self._anchor_err_label.setText("")
            elif self._backfill_table is not None:
                self._backfill_table.blockSignals(True)
                for r in reversed(range(self._backfill_table.rowCount())):
                    self._backfill_table.removeRow(r)
                self._backfill_table.blockSignals(False)
        self._patches.pop(pid, None)
        self._committed_snap.pop(pid, None)
        self._draw_errors.pop(pid, None)
        self._refresh()

    # ---- 就地错误标记（红框 + 红字） ----
    def _backfill_row_errors(self) -> Dict[int, str]:
        """返回当前补录表 {行号: 错误原因}。空白行（未填角色）不计错。"""
        if self._backfill_table is None:
            return {}
        cands = self._backfill_candidates
        errs: Dict[int, str] = {}
        draws: Dict[int, int] = {}   # draw -> 首个占用它的行（判重）
        for r in range(self._backfill_table.rowCount()):
            combo = self._backfill_table.cellWidget(r, 0)
            d = self._backfill_table.item(r, 1)
            text = combo.currentText().strip() if combo else ""
            try:
                draw = int(d.text().strip()) if d else 0
            except (ValueError, OverflowError):
                draw = -1
            if not text:          # 空白占位行，忽略
                continue
            cid = _resolve_char(self.characters, text)
            if cid not in cands:
                errs[r] = f"「{_char_label(self.characters, cid)}」不在该池可选范围（仅当期UP+常驻六星）"
            if draw <= 0:
                errs[r] = "第几抽须为正整数"
            elif draw > MAX_DRAWS:
                errs[r] = f"第 {draw} 抽超出单池上限 {MAX_DRAWS}"
            else:
                # 仅对合法 draw 判重——非法 draw（解析失败→-1、或 0/负数）若也登记进 draws，
                # 两行同类非法输入会把「须为正整数」误标成「第几抽 … 与另一条重复」。
                if draw in draws:
                    errs.setdefault(draws[draw], "第几抽不能重复")
                    errs[r] = f"第几抽 {draw} 与另一条重复"
                else:
                    draws[draw] = r
        return errs

    def _apply_backfill_errors(self, errs: Dict[int, str]) -> None:
        t = self._backfill_table
        if t is None:
            return
        for r in range(t.rowCount()):
            msg = errs.get(r, "")
            tip = t.item(r, 2)
            di = t.item(r, 1)
            combo = t.cellWidget(r, 0)
            if tip:
                tip.setText(msg)
                tip.setForeground(QBrush(_RED) if msg else QBrush(QColor("#666666")))
            if di:
                di.setBackground(QBrush(_RED_BG) if msg else QBrush(QColor("#ffffff")))
            if combo:
                combo.setStyleSheet("QComboBox{ background:#fdecea; }" if msg else "")
        self._table_has_errors = bool(errs)

    def _anchor_error(self) -> Optional[str]:
        pid = self._current_anchor_pool
        p = self._patches.get(pid) if pid else None
        if not p:
            return None
        led = p.get("leading", 0)
        if led > MAX_DRAWS:
            return f"开头垫抽 {led} 超过上限 {MAX_DRAWS}"
        if led < 0:
            return "开头垫抽不能为负"
        return None

    # ---- 汇总与预览 ----
    def _current_pool_id(self) -> Optional[str]:
        return self._current_slot["pool_id"] if self._current_slot else None

    def _refresh_footer(self) -> None:
        counts = {"saved": 0, "unsaved": 0, "error": 0}
        for s in self._slots:
            st = self._pool_status(s["pool_id"])
            if st in counts:
                counts[st] += 1
        if not self._slots:
            self._footer_label.setText("")
        elif sum(counts.values()) == 0:
            self._footer_label.setText("尚未填写任何待保存内容。")
        else:
            txt = (f"已保存 {counts['saved']} · 待保存 {counts['unsaved']} · 有误 {counts['error']}")
            self._footer_label.setText(txt)
            self._footer_label.setStyleSheet(
                f"color:{_RED.name()};" if counts["error"] else "color:#333;")
        self._ok_btn.setEnabled(bool(self._slots))

    def _refresh(self) -> None:
        cur = self._current_slot
        if cur is None:
            self._update_preview()
            self._refresh_footer()
            return
        self._gather_editor_into_patches()
        if cur["kind"] == "backfill":
            self._apply_backfill_errors(self._backfill_row_errors())
        elif cur["kind"] == "anchor":
            err = self._anchor_error()
            if self._anchor_err_label:
                self._anchor_err_label.setText(f"⚠ {err}" if err else "")
        self._update_preview()
        self._refresh_slot_labels()
        self._refresh_footer()

    def _gather_editor_into_patches(self) -> None:
        """把当前可见编辑器内容回写到 self._patches（用于预览、保存、落草稿）。"""
        cur = self._current_slot
        if cur is None:
            return
        pid = cur["pool_id"]
        if cur["kind"] == "anchor":
            if self._leading_spin is not None:
                self._patches[pid] = {
                    "leading": self._leading_spin.value(),
                    "items": self._patches.get(pid, {}).get("items", []),
                }
        elif cur["kind"] == "backfill":
            items = []
            draw_errs: Dict[int, str] = {}
            for r in range(self._backfill_table.rowCount()):
                combo = self._backfill_table.cellWidget(r, 0)
                d = self._backfill_table.item(r, 1)
                if combo is None or d is None:
                    continue
                text = combo.currentText().strip()
                raw = d.text().strip()
                if not text:
                    continue  # 空白行（未填角色）：忽略，也不留错
                try:
                    draw = int(raw)
                except (ValueError, OverflowError):
                    draw = 0
                if draw <= 0:
                    # 有角色但「第几抽」非法：not 进 items（避免污染算法），但记录供保存
                    # 拦截，否则该行被静默丢弃、点保存仍成功（见 待办清单 §0.5 中项）。
                    draw_errs[r] = f"第几抽「{raw}」须为正整数"
                    continue
                items.append((_resolve_char(self.characters, text), draw))
            items.sort(key=lambda x: x[1])
            self._draw_errors[pid] = draw_errs
            self._patches[pid] = {
                "leading": 0,
                "items": [{"char": cid, "draw": d} for cid, d in items],
            }

    def _update_preview(self) -> None:
        active = {pid: p for pid, p in self._patches.items()
                  if p.get("leading", 0) or p.get("items")}
        try:
            manual, aug, bound = synthesize_records(
                self.pools, self.characters, self.pity_groups, active, self.real_records,
            )
            base = StatsEngine(self.characters, self.pools, self.pity_groups)
            base.records = list(self.real_records)
            leaf = build_patched_engine(base, manual, aug) if manual else base
            tracking = leaf.shared_pity_tracking()
            pool_to_group, _ = resolve_groups(self.pools, self.pity_groups)
            lines = []
            affected_groups = {pool_to_group.get(pid, "solo:" + pid) for pid in active}
            for gkey, info in tracking.items():
                gbound = bound.get(gkey)
                mh = sum(1 for h in info["hits"] if gbound is not None and h.get("ts", 0) < gbound)
                if gkey in affected_groups or mh:
                    state = f"  {info['state']}" if info["state"] else ""
                    lines.append(
                        f"{info['label']}: 共享保底 {info['current_pity']}/{info['hard_pity']}{state}"
                        f"  距保底{info['distance_to_pity']}抽  组内{info['total']}抽 6星×{info['six_count']}"
                        + (f"  ·手补{mh}" if mh else "")
                    )
            self.preview_label.setText("\n".join(lines) if lines else "（无有效变更）")
        except Exception as e:
            logger.warning(f"维护预览失败: {e}")
            self.preview_label.setText(f"预览出错：{e}")

    # ---- 保存 ----
    def result_patches(self) -> Dict[str, dict]:
        return {pid: p for pid, p in self._patches.items()
                if p.get("leading", 0) or p.get("items")}

    def _on_accept(self) -> None:
        """底部「保存」：全校验 -> 有误拦截（可只存合法）-> 未保存项询问 -> 落库。"""
        self._gather_editor_into_patches()
        active = self.result_patches()
        valid: Dict[str, dict] = {}
        errors: Dict[str, str] = {}
        for pid, p in active.items():
            try:
                self._validate_pool(pid, p)
                valid[pid] = p
            except ValueError as e:
                errors[pid] = str(e)

        # 非法「第几抽」行已被 gather 记为 _draw_errors（这些行不进 items，_validate_pool
        # 看不到）——必须显式合并进 errors 拦截，否则静默丢行、保存仍成功。
        for pid, rows in self._draw_errors.items():
            if not rows:
                continue
            detail = "；".join(
                f"第{r + 1}行:{err}" for r, err in sorted(rows.items()))
            errors[pid] = (errors[pid] + "；" if pid in errors else "") + detail
            valid.pop(pid, None)

        if errors:
            detail = "\n".join(f"· {self._slot_name(pid)}：{m}" for pid, m in errors.items())
            box = QMessageBox(self)
            box.setWindowTitle("有补录有误")
            box.setIcon(QMessageBox.Warning)
            box.setText("以下补录有误，无法保存：\n" + detail)
            only_valid = box.addButton("只保存合法项", QMessageBox.AcceptRole)
            go_back = box.addButton("返回修改", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is only_valid:
                if not valid:
                    QMessageBox.information(self, "没有可保存项", "没有合法的补录可保存。")
                    return
                active = valid
            else:
                return
        else:
            active = valid

        unsaved = [pid for pid in active if self._committed_snap.get(pid) != active[pid]]
        if unsaved:
            names = "、".join(self._slot_name(pid) for pid in unsaved)
            qbox = QMessageBox(self)
            qbox.setWindowTitle("未保存的补录")
            qbox.setIcon(QMessageBox.Question)
            qbox.setText(f"以下 {len(unsaved)} 项已编辑但未点「保存此池」，是否一并保存？\n{names}")
            b_all = qbox.addButton("全部保存", QMessageBox.AcceptRole)
            b_skip = qbox.addButton("不保存这些", QMessageBox.DestructiveRole)
            b_cancel = qbox.addButton("取消", QMessageBox.RejectRole)
            qbox.exec()
            if qbox.clickedButton() is b_all:
                pass  # 全部保存
            elif qbox.clickedButton() is b_skip:
                active = {pid: p for pid, p in active.items() if pid not in unsaved}
            else:
                return

        self._patches = active
        self.accept()

    def _validate_pool(self, pid: str, patch: dict) -> None:
        if pid not in self.pools:
            raise ValueError(f"卡池 {pid} 不在元数据中")
        # 该池必须是当前仍有效的编辑槽
        slot = self._slot_map.get(f"anchor:{pid}") or self._slot_map.get(f"backfill:{pid}")
        if slot is None:
            raise ValueError(f"卡池 {pid} 不允许补录（仅允许比最早记录池更旧的严格前缀）")
        leading = patch.get("leading", 0)
        if leading < 0:
            raise ValueError(f"卡池 {pid} 的开头垫抽不能为负")
        if leading > MAX_DRAWS:
            raise ValueError(f"卡池 {pid} 的开头垫抽不能超过 {MAX_DRAWS} 抽")
        items = patch.get("items", [])
        draws = [it["draw"] for it in items]
        if draws != sorted(draws) or len(set(draws)) != len(draws) or any(d <= 0 for d in draws):
            raise ValueError(f"卡池 {pid} 的「第几抽」必须严格递增且为正")
        if any(d > MAX_DRAWS for d in draws):
            raise ValueError(f"卡池 {pid} 的单池补录不能超过 {MAX_DRAWS} 抽")
        cands = self._candidate_cache.get(pid)
        if cands is None:
            cands = _pool_candidates(self.pools[pid], self.characters)
            self._candidate_cache[pid] = cands
        if items:
            for it in items:
                if it["char"] not in cands:
                    raise ValueError(
                        f"「{_char_label(self.characters, it['char'])}」不在该池可选范围"
                        f"（仅当期 UP 与常驻六星）")
        if leading == 0 and not items:
            raise ValueError(f"卡池 {pid} 未填入任何有效内容")