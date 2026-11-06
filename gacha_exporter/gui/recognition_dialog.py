"""识别结果确认窗 + 本机覆盖层管理（卡池识别 P4）。

确认窗数据全部来自本会话内存中的 `PageValidation`：

- 自动配对通过的页：角色/卡池默认勾选，名字、星级、池元数据均可改；
- 校验未通过但页号一致的页：进入「待人工配对」，选行后才写入；
- 页号不一致/截图失败的页不提供配对（行属于别的页，绑了就是错配）。

写入只落 `data/role_overlay.json`（本机、可逆），不碰 role.json、不改历史。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..analysis.gacha_history_ocr import normalize_separators
from ..analysis.unknown_trigger import (
    PageValidation,
    RecognitionTask,
    RowBinding,
    name_similarity,
)
from ..models import Character, Pool
from ..storage.role_overlay import (
    list_entries,
    remove_character,
    remove_pool,
    upsert_character,
    upsert_pool,
)
from ..utils.logger import get_logger

logger = get_logger(__name__)

POOL_TYPES = ("UP", "遴选", "常驻", "新手", "其他")
RARITIES = (4, 5, 6)


def _default_pool_type(pool_id: str, has_new_six: bool) -> str:
    """§7.5 弱推断：id 段 → 类型；出现新 6★ 时按 UP。用户可改。"""
    pid = str(pool_id)
    if pid.startswith("300"):
        ptype = "UP"
    elif pid.startswith("200"):
        ptype = "遴选"
    else:
        ptype = "常驻"
    return "UP" if has_new_six else ptype


def _default_pity_group(ptype: str, pool_id: str) -> str:
    if ptype == "UP":
        return "shared:UP"
    if ptype == "遴选":
        return "shared:遴选"
    return f"solo:{pool_id}"


# 名字尾部的星级角标（「（6♛）」「（64）」）。角色名本身不含括号（role.json
# 全表核对过），可以安全剥离：v5 rec 偶尔把 ♛ 读成数字，角标就会粘在名字上。
_TRAILING_BADGE_RE = re.compile(r"[（(][^）)]{0,12}[）)]?\s*$")
# UP 名字模糊匹配阈值：OCR 错 1~2 个字时仍能对上已知 6★ 名
UP_NAME_FUZZY_THRESHOLD = 0.85


def clean_character_name(text: str) -> str:
    """去掉名字尾部的星级角标残留并统一间隔号。

    2026-10-23 实机：``示例角色039（6♛）`` 被 v5 读成
    ``示例角色039（64）``，旧实现把这串当正名写进 overlay，UP 下拉也
    因此认不出人。解析层已放宽星级正则；这里再兜一层，避免历史数据/其他路径
    把角标带进确认窗。
    """
    name = normalize_separators(str(text or "")).strip()
    stripped = _TRAILING_BADGE_RE.sub("", name).strip()
    return stripped or name


def _check_item(checked: bool) -> QTableWidgetItem:
    item = QTableWidgetItem()
    item.setFlags(
        Qt.ItemFlag.ItemIsUserCheckable
        | Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsSelectable
    )
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    return item


def _ro(text: object) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    return item


def _widen_combo(
    combo: QComboBox, min_width: int, popup_width: Optional[int] = None
) -> None:
    """把下拉框加宽并给悬浮提示。

    2026-10-22 实机反馈：角色名下拉框被列宽压成 `x...x`，看不出是谁。
    Qt 的表格列宽不会自动跟随 cellWidget 的 wanted width，所以除了
    minimumWidth，还要在 resizeColumnsToContents 后强制列宽（见调用处）。
    """
    combo.setMinimumWidth(min_width)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    if popup_width:
        combo.view().setMinimumWidth(popup_width)
    combo.setToolTip(combo.currentText())
    combo.currentTextChanged.connect(combo.setToolTip)


class RecognitionDialog(QDialog):
    """本批次识别结果确认窗。写入成功后 `written_characters/pools` 带条目。"""

    def __init__(
        self,
        parent,
        validations: Sequence[PageValidation],
        manual: Sequence[PageValidation],
        pending_tasks: Sequence[RecognitionTask],
        known_characters: Mapping[str, Character],
        known_pools: Mapping[str, Pool],
        failed_pages: Optional[Mapping[int, str]] = None,
        pool_items: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("识别结果确认")
        self.resize(1120, 680)
        self._validations = list(validations)
        self._manual = list(manual)
        self._pending_tasks = list(pending_tasks)
        self._failed_pages = dict(failed_pages or {})
        self._known_characters = known_characters
        self._known_pools = known_pools
        # 未知池 -> 本会话抓到的角色 id（含未 OCR 的页）：UP 6★ 推断用
        self._pool_items: Dict[str, set] = {
            str(pid): set(items) for pid, items in (pool_items or {}).items()
        }
        self.written_characters: Dict[str, dict] = {}
        self.written_pools: Dict[str, dict] = {}
        self._char_rows: List[dict] = []
        self._pool_rows: List[dict] = []
        self._manual_rows: List[dict] = []
        self._pool_manual_rows: List[dict] = []
        self._pool_manual_table: Optional[QTableWidget] = None
        self._setup_ui()

    # ---- 构建 ----

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        n_char = len(self._collect_character_drafts())
        n_pool = len(self._collect_pool_drafts())
        n_manual = sum(
            len(v.task.unknown_item_ids) + len(v.task.unknown_pool_ids)
            for v in self._manual
            if v.task
        )
        layout.addWidget(
            QLabel(
                f"本批次未知角色 {n_char} / 未知卡池 {n_pool} / 待人工配对 {n_manual}"
                + (f" / 未完成 {len(self._pending_tasks)} 页" if self._pending_tasks else "")
            )
        )

        # 抓包时用户通常不看工具：未识别的页统一在停止汇总里交代清楚，
        # 并给出补识别入口（主窗口「重新识别当前页」）。
        if self._failed_pages:
            items = sorted(self._failed_pages.items())[:6]
            text = "；".join(f"第 {page} 页（{reason}）" for page, reason in items)
            if len(self._failed_pages) > 6:
                text += f" 等 {len(self._failed_pages)} 页"
            tip = QLabel(
                "⚠ 以下页面本机识别未完成：" + text
                + "\n如需补识别：关闭本窗后回到游戏对应页，点主窗口顶部提示条上的"
                "「重新识别当前页」。未识别不影响抓包与统计。"
            )
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#a02020;")
            layout.addWidget(tip)

        tabs = QTabWidget()
        tabs.addTab(self._build_character_tab(), f"角色 ({n_char})")
        tabs.addTab(self._build_pool_tab(), f"卡池 ({n_pool})")
        tabs.addTab(self._build_manual_tab(), f"待人工配对 ({n_manual})")
        layout.addWidget(tabs, 1)

        warn = QLabel("⚠ 6★ 角色将参与保底统计（含历史记录重算）；写入后可在「更多 → 本机识别结果」删除。")
        warn.setStyleSheet("color: #a06000;")
        layout.addWidget(warn)

        buttons = QHBoxLayout()
        buttons.addStretch()
        btn_cancel = QPushButton("取消")
        btn_write = QPushButton("写入本地")
        btn_write.setDefault(True)
        buttons.addWidget(btn_cancel)
        buttons.addWidget(btn_write)
        layout.addLayout(buttons)
        btn_cancel.clicked.connect(self.reject)
        btn_write.clicked.connect(self._on_write)

    def _collect_character_drafts(self) -> List[dict]:
        if self._char_rows:
            return self._char_rows
        seen: Dict[str, dict] = {}
        for v in self._validations:
            if not v.ok:
                continue
            for b in v.unknown_item_bindings:
                iid = b.record.item_id
                if iid in seen:
                    continue
                seen[iid] = {
                    "binding": b,
                    "task": v.task,
                    "name": clean_character_name(b.row.name),
                    "rarity": b.row.rarity,
                    "pool_id": b.record.pool_id,
                    "pool_name": b.row.pool_name,
                    "time": b.row.time_norm,
                    "new_tag": b.row.new_tag,
                    "ocr_name": b.row.ocr_name,
                    "name_v4": b.row.name_v4,
                    "pool_v4": b.row.pool_v4,
                    "rarity_v4": b.row.rarity_v4,
                    "name_diverged": b.row.name_diverged,
                    "pool_diverged": b.row.pool_diverged,
                    "rarity_diverged": b.row.rarity_diverged,
                    "page": v.task.page if v.task else 0,
                    "total_page": v.task.total_page if v.task else 0,
                    "row": b.row_index + 1,
                }
        self._char_rows = list(seen.values())
        return self._char_rows

    def _known_six_names(self) -> List[str]:
        """当前映射里所有已知 6★ 的名字（UP 候选，含 role.json + overlay）。"""
        names: List[str] = []
        for ch in self._known_characters.values():
            try:
                rarity = int(getattr(ch, "rarity", 0) or 0)
            except (TypeError, ValueError):
                continue
            name = str(getattr(ch, "name", "") or "")
            if rarity >= 6 and name and not name.startswith("未知"):
                names.append(name)
        return list(dict.fromkeys(names))

    def _up_name_options(self) -> List[str]:
        """UP 下拉候选：已知 6★ + 本批全部未知角色。

        未知角色不按星级过滤——2026-10-23 实机监狱长的角标被 v5 读坏、星级
        误判成 4★，结果下拉里根本找不到人。候选只是「可选项」，默认值仍由
        `_infer_up`/唯一陌生角色规则给出，混进个别 4★ 不会造成误绑。
        """
        drafts = [
            self._canonical_up_name(d.get("name") or "")
            for d in self._collect_character_drafts()
            if d.get("name")
        ]
        return list(
            dict.fromkeys(self._known_six_names() + [n for n in drafts if n])
        )

    def _canonical_up_name(self, name: str) -> str:
        """名字清理 + 对已知 6★ 名做模糊匹配。

        OCR 偶发错 1~2 个字（如 ``白镰刀`` → ``自镰刀``）时，用相似度最高的
        已知 6★ 规范名代替；低于阈值则保留清理后的原 OCR 名，不硬改。
        """
        cleaned = clean_character_name(name)
        if not cleaned:
            return ""
        best, best_ratio = "", 0.0
        for cand in self._known_six_names():
            ratio = name_similarity(cleaned, cand)
            if ratio > best_ratio:
                best, best_ratio = cand, ratio
        return best if best_ratio >= UP_NAME_FUZZY_THRESHOLD else cleaned

    def _resolved_six_stars(self) -> Dict[str, str]:
        """本批可解析的 6★：id -> 名字（远端映射 + 本批已识别的未知角色草稿）。

        监狱长（90000049）这类新角色在写入 overlay 前不在 `_known_characters`
        里，但它的名字就在本批识别草稿中——不并进来，`_infer_up` 永远认不出它。
        """
        out: Dict[str, str] = {}
        for iid, ch in self._known_characters.items():
            try:
                rarity = int(getattr(ch, "rarity", 0) or 0)
            except (TypeError, ValueError):
                continue
            name = str(getattr(ch, "name", "") or "")
            if rarity >= 6 and name and not name.startswith("未知"):
                out[str(iid)] = name
        for d in self._collect_character_drafts():
            iid = str(d["binding"].record.item_id)
            try:
                rarity = int(d.get("rarity") or 0)
            except (TypeError, ValueError):
                continue
            name = str(d.get("name") or "")
            if rarity >= 6 and name and not name.startswith("未知"):
                out.setdefault(iid, name)
        return out

    def _infer_up(self, pool_id: str) -> Tuple[str, bool]:
        """从本池抓到的记录反推 UP 6★。

        触发器的 attempted 机制只会 OCR 未知池的第一页；UP 6★ 常出现在后面
        的页（骑士在第 7 页、30007 第一页在第 6 页），但那些页的**响应记录**
        仍然抓到了。取本池记录里出现的 6★，恰好一个才作为默认值，
        多个（有歪出的 6★）时不猜。返回 (名字, 是否推断)。
        """
        six: List[str] = []
        resolved = self._resolved_six_stars()
        for iid in self._pool_items.get(str(pool_id), set()):
            name = resolved.get(str(iid), "")
            if name and name not in six:
                six.append(name)
        if len(six) == 1:
            return six[0], True
        return "", False

    def _solo_unknown_draft(self, pool_id: str) -> Optional[dict]:
        """唯一陌生角色 + 它出现在该陌生池 → 返回该角色草稿，否则 None。

        2026-10-23 实机反馈：本批只有一个陌生 id（监狱长），它就在陌生池
        30008 里；这已经足够判定「该陌生池 = 该陌生角色的 UP 池」。旧实现只认
        「本池唯一 6★」，星级一旦被 OCR 误判成 4★ 就既不填 UP、下拉里也找不到
        人。唯一对应时直接带出，证据列注明「请核对」。
        """
        drafts = self._collect_character_drafts()
        ids = {str(d["binding"].record.item_id) for d in drafts}
        if len(ids) != 1:
            return None
        iid = next(iter(ids))
        if iid not in {str(x) for x in self._pool_items.get(str(pool_id), ())}:
            return None
        return drafts[0]

    def _resolve_pool_up(
        self, pool_id: str, new_six_name: str = "", new_six_page: int = 0
    ) -> Tuple[str, str, int]:
        """按强弱顺序给出 (UP 名字, 来源, 页号)。

        来源：``new``（跨页汇总的「新」6★）> ``solo``（本批唯一陌生角色且在
        池内）> ``inferred``（本池唯一的已知 6★）。solo 优先于 inferred——陌生
        角色才是新池 UP 的本体；但若 OCR 把陌生角色判成 4★ 以下、而本池又恰好
        只有一个已知 6★，保守地沿用后者，免得把歪出的 4★ 当 UP。
        """
        if new_six_name:
            return self._canonical_up_name(new_six_name), "new", new_six_page
        solo = self._solo_unknown_draft(pool_id)
        inferred, inferred_flag = self._infer_up(pool_id)
        if solo is not None:
            name = clean_character_name(solo.get("name") or "")
            try:
                solo_rarity = int(solo.get("rarity") or 0)
            except (TypeError, ValueError):
                solo_rarity = 0
            if solo_rarity >= 6 or not inferred:
                return self._canonical_up_name(name), "solo", 0
        if inferred and inferred_flag:
            return self._canonical_up_name(inferred), "inferred", 0
        return "", "", 0

    def _batch_new_six(self) -> Dict[str, List[Tuple[str, int]]]:
        """跨页汇总本批自动识别页里每池的「新」6★：(名字, 页号)。

        池 30008 的识别任务只在第一页，监狱长却抽在后面的页——那页的
        `unknown_pool_ids` 已不包含 30008（attempted 去重），按页取新 6★
        永远取不到。必须按池跨全部已识别页汇总，UP 才能自动带出。
        """
        out: Dict[str, List[Tuple[str, int]]] = {}
        for v in self._validations:
            if not v.ok or v.task is None:
                continue
            for b in v.bindings:
                try:
                    rarity = int(b.row.rarity or 0)
                except (TypeError, ValueError):
                    continue
                if not (b.row.new_tag and rarity >= 6 and b.row.name):
                    continue
                name = clean_character_name(b.row.name)
                lst = out.setdefault(str(b.record.pool_id), [])
                if all(n != name for n, _page in lst):
                    lst.append((name, v.task.page))
        return out

    @staticmethod
    def _pick_new_six(
        found: Sequence[Tuple[str, int]],
    ) -> Tuple[str, int, bool]:
        """同一池只有一个新 6★ 才能直接当 UP；多个时不猜。"""
        if len(found) == 1:
            return found[0][0], found[0][1], True
        return "", 0, bool(found)

    def _collect_pool_drafts(self) -> List[dict]:
        if self._pool_rows:
            return self._pool_rows
        seen: Dict[str, dict] = {}
        batch_six = self._batch_new_six()
        for v in self._validations:
            if not v.ok or v.task is None:
                continue
            for pid in v.task.unknown_pool_ids:
                names = v.unknown_pool_names.get(pid, [])
                if not names:
                    names = [
                        b.row.pool_name
                        for b in v.bindings
                        if b.pool_unknown and b.row.pool_name
                    ]
                entries = list(dict.fromkeys(names))
                row = next((b for b in v.bindings if b.record.pool_id == pid), None)
                # C6 分歧：本池行里 v4/v5 池名不一致的 v4 原文
                pool_v4 = list(dict.fromkeys(
                    b.row.pool_v4
                    for b in v.bindings
                    if str(b.record.pool_id) == str(pid) and b.row.pool_diverged
                ))
                new_six_name, new_six_page, has_new_six = self._pick_new_six(
                    batch_six.get(str(pid), [])
                )
                draft = seen.get(pid)
                if draft is None:
                    ptype = _default_pool_type(pid, has_new_six)
                    up_name, up_src, up_page = self._resolve_pool_up(
                        pid, new_six_name, new_six_page
                    )
                    seen[pid] = {
                        "pool_id": str(pid),
                        "names": entries,
                        "name": entries[0] if entries else "",
                        "type": ptype,
                        "up_character": up_name,
                        "up_inferred": up_src == "inferred",
                        "up_solo": up_src == "solo",
                        "up_page": up_page,
                        "pity_group": _default_pity_group(ptype, pid),
                        "page": v.task.page,
                        "row": (row.row_index + 1) if row else 1,
                        "ambiguous": len(entries) > 1,
                        "pool_v4": pool_v4,
                    }
                else:
                    for n in entries:
                        if n and n not in draft["names"]:
                            draft["names"].append(n)
                    for n in pool_v4:
                        if n not in draft["pool_v4"]:
                            draft["pool_v4"].append(n)
                    draft["ambiguous"] = len(draft["names"]) > 1
                    # 跨页汇总到「新」6★ 时优先用它；否则保留已有值/补推断
                    if new_six_name or not draft.get("up_character"):
                        up_name, up_src, up_page = self._resolve_pool_up(
                            pid, new_six_name, new_six_page
                        )
                        if up_name:
                            draft["up_character"] = up_name
                            draft["up_inferred"] = up_src == "inferred"
                            draft["up_solo"] = up_src == "solo"
                            if up_src == "new":
                                draft["up_page"] = up_page
        self._pool_rows = list(seen.values())
        return self._pool_rows

    def _build_character_tab(self) -> QWidget:
        drafts = self._collect_character_drafts()
        table = QTableWidget(len(drafts), 9)
        table.setHorizontalHeaderLabels(
            ["写入", "角色 ID", "名字（可改）", "星级", "卡池", "时间", "新",
             "OCR 原文", "重点核对"]
        )
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for r, d in enumerate(drafts):
            table.setItem(r, 0, _check_item(True))
            table.setItem(r, 1, _ro(d["binding"].record.item_id))
            name_item = QTableWidgetItem(d["name"])
            table.setItem(r, 2, name_item)
            combo = QComboBox()
            for rr in RARITIES:
                combo.addItem(f"{rr}★", rr)
            combo.setCurrentIndex(max(0, RARITIES.index(int(d["rarity"]))))
            table.setCellWidget(r, 3, combo)
            # 卡池列同时给出池名与 id：未知池只显示名字时看不出对应关系
            pool_label = str(d["pool_name"] or "")
            if d["pool_id"]:
                pool_label = (
                    f"{pool_label}（{d['pool_id']}）" if pool_label
                    else str(d["pool_id"])
                )
            pool_item = _ro(pool_label)
            pool_item.setToolTip(pool_label)
            table.setItem(r, 4, pool_item)
            table.setItem(r, 5, _ro(d["time"]))
            table.setItem(r, 6, _ro("新" if d["new_tag"] else ""))
            table.setItem(r, 7, _ro(d["ocr_name"]))
            # C6 分歧：v4/v5 结果不同时把 v4 原文摊出来，提示人工重点核对
            hints = []
            if d["name_diverged"]:
                hints.append(f"名字 v4：{d['name_v4']}")
            if d["rarity_diverged"]:
                hints.append(f"星级 v4：{d['rarity_v4']}★")
            if d["pool_diverged"]:
                hints.append(f"池名 v4：{d['pool_v4']}")
            check = _ro("⚠ " + "；".join(hints) if hints else "")
            if hints:
                check.setForeground(QColor("#a02020"))
            table.setItem(r, 8, check)
        table.resizeColumnsToContents()
        self._char_table = table
        return _wrap(table)

    def _build_pool_tab(self) -> QWidget:
        drafts = self._collect_pool_drafts()
        table = QTableWidget(len(drafts), 8)
        table.setHorizontalHeaderLabels(
            ["写入", "卡池 ID", "池名（可改）", "类型", "UP 角色", "保底组", "证据", "池名候选"]
        )
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        up_names = list(
            dict.fromkeys(
                self._up_name_options()
                + [d["up_character"] for d in drafts if d["up_character"]]
            )
        )
        for r, d in enumerate(drafts):
            table.setItem(r, 0, _check_item(not d["ambiguous"]))
            table.setItem(r, 1, _ro(d["pool_id"]))
            table.setItem(r, 2, QTableWidgetItem(d["name"]))
            type_combo = QComboBox()
            type_combo.addItems(POOL_TYPES)
            if d["type"] in POOL_TYPES:
                type_combo.setCurrentText(d["type"])
            else:
                type_combo.addItem(d["type"])
                type_combo.setCurrentText(d["type"])
            table.setCellWidget(r, 3, type_combo)
            up_combo = QComboBox()
            up_combo.setEditable(True)
            up_combo.addItem("")
            for n in up_names:
                up_combo.addItem(n)
            up_combo.setCurrentText(d["up_character"])
            _widen_combo(up_combo, 260, 360)
            table.setCellWidget(r, 4, up_combo)
            group_combo = QComboBox()
            group_combo.setEditable(True)
            group_combo.addItems(["shared:UP", "shared:遴选", f"solo:{d['pool_id']}"])
            group_combo.setCurrentText(d["pity_group"])
            _widen_combo(group_combo, 130, 180)
            table.setCellWidget(r, 5, group_combo)
            evidence = f"第 {d['page']} 页 第 {d['row']} 行"
            if d.get("up_inferred"):
                evidence += "；UP 按本池 6★ 记录推断，请核对"
            if d.get("up_solo"):
                evidence += "；UP 按「本批唯一陌生角色且在池内」自动匹配，请核对"
            if d.get("up_page") and d["up_page"] != d["page"]:
                evidence += f"；UP 取自第 {d['up_page']} 页的新 6★，请核对"
            if d.get("pool_v4"):
                evidence += (
                    "；池名 v4/v5 不一致（v4："
                    + " / ".join(d["pool_v4"]) + "），请核对"
                )
            evidence_item = _ro(evidence)
            evidence_item.setToolTip(evidence)
            table.setItem(r, 6, evidence_item)
            cand = " / ".join(d["names"])
            cand_item = _ro(cand)
            cand_item.setToolTip(cand)
            table.setItem(r, 7, cand_item)
        table.resizeColumnsToContents()
        # 列宽不会自动跟随 cellWidget 的 minimumWidth，这里强制兜住，
        # 否则角色名仍会被省略成 x...x（2026-10-22 实机反馈）。
        table.setColumnWidth(4, max(table.columnWidth(4), 270))
        table.setColumnWidth(5, max(table.columnWidth(5), 150))
        table.setColumnWidth(6, max(table.columnWidth(6), 180))
        self._pool_table = table
        return _wrap(table)

    def _build_manual_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        entries: List[dict] = []
        for v in self._manual:
            task = v.task
            if task is None or v.page is None or not v.page.rows:
                continue
            # 只有页号一致才允许人工配对（行属于该页）；页号不一致直接排除
            if v.page.page != task.page or v.page.total_page != task.total_page:
                continue
            records = task.records
            for iid in task.unknown_item_ids:
                ridx = next(
                    (i for i, rec in enumerate(records) if rec.item_id == iid), None
                )
                if ridx is None:
                    continue
                entries.append(
                    {
                        "validation": v,
                        "item_id": iid,
                        "record_index": ridx,
                        "record": records[ridx],
                        "page": task.page,
                        "reason": v.reason,
                    }
                )
        layout.addWidget(QLabel("未知角色：选择与 id 对应的行（页号已一致）"))
        table = QTableWidget(len(entries), 6)
        table.setHorizontalHeaderLabels(
            ["写入", "未知 ID", "选择行", "名字（可改）", "星级", "未自动配对原因"]
        )
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for r, e in enumerate(entries):
            e["row"] = r
            table.setItem(r, 0, _check_item(False))
            table.setItem(r, 1, _ro(e["item_id"]))
            row_combo = QComboBox()
            row_combo.addItem("不配对", -1)
            for i, row in enumerate(e["validation"].page.rows):
                # C6 分歧行在选项里直接带出 v4 原文/星级，选中前就能看到差异
                hint = f"  ⚠ v4：{row.name_v4}" if row.name_diverged else ""
                if row.rarity_diverged:
                    hint += f"  ⚠ 星级 v4：{row.rarity_v4}★"
                row_combo.addItem(
                    f"第 {i + 1} 行  {clean_character_name(row.name)}"
                    f"（{row.rarity}★）{hint}",
                    i,
                )
            _widen_combo(row_combo, 320, 480)
            table.setCellWidget(r, 2, row_combo)
            name_item = QTableWidgetItem("")
            table.setItem(r, 3, name_item)
            rarity_combo = QComboBox()
            for rr in RARITIES:
                rarity_combo.addItem(f"{rr}★", rr)
            table.setCellWidget(r, 4, rarity_combo)
            table.setItem(r, 5, _ro(e["reason"]))
            e["row_combo"] = row_combo
            e["name_item"] = name_item
            e["rarity_combo"] = rarity_combo
            row_combo.currentIndexChanged.connect(
                lambda _idx, ent=e: self._on_manual_row_changed(ent)
            )
        table.resizeColumnsToContents()
        table.setColumnWidth(2, max(table.columnWidth(2), 330))
        table.setColumnWidth(5, max(table.columnWidth(5), 200))
        self._manual_table = table
        self._manual_rows = entries
        layout.addWidget(_wrap(table), 1)

        # 未知卡池人工配对（2026-10-21 P5 实机补）：只有池未知、没有未知角色的
        # 页失败时旧实现完全无入口（main_window 曾以 unknown_item_ids 为条件）。
        pool_entries = self._collect_manual_pool_entries()
        self._pool_manual_rows = pool_entries
        self._pool_manual_table: Optional[QTableWidget] = None
        if pool_entries:
            layout.addWidget(QLabel("未知卡池：选择用于取池名的一行（页号已一致）"))
            ptable = QTableWidget(len(pool_entries), 8)
            ptable.setHorizontalHeaderLabels(
                ["写入", "未知卡池 ID", "选择行（取池名）", "池名（可改）",
                 "类型", "UP 角色", "保底组", "未自动配对原因"]
            )
            ptable.verticalHeader().setVisible(False)
            ptable.horizontalHeader().setSectionResizeMode(
                7, QHeaderView.ResizeMode.Stretch
            )
            for r, e in enumerate(pool_entries):
                e["row"] = r
                ptable.setItem(r, 0, _check_item(False))
                ptable.setItem(r, 1, _ro(e["pool_id"]))
                row_combo = QComboBox()
                row_combo.addItem("不配对", -1)
                for i, row in enumerate(e["validation"].page.rows):
                    hint = f"  ⚠ v4：{row.pool_v4}" if row.pool_diverged else ""
                    row_combo.addItem(
                        f"第 {i + 1} 行  {row.pool_name or '（空）'}{hint}", i
                    )
                _widen_combo(row_combo, 270, 420)
                ptable.setCellWidget(r, 2, row_combo)
                name_item = QTableWidgetItem("")
                ptable.setItem(r, 3, name_item)
                type_combo = QComboBox()
                type_combo.addItems(POOL_TYPES)
                type_combo.setCurrentText(e["type"])
                ptable.setCellWidget(r, 4, type_combo)
                up_combo = QComboBox()
                up_combo.setEditable(True)
                up_combo.addItem("")
                for n in self._up_name_options():
                    up_combo.addItem(n)
                if e["up_character"]:
                    up_combo.setCurrentText(e["up_character"])
                _widen_combo(up_combo, 260, 360)
                ptable.setCellWidget(r, 5, up_combo)
                group_combo = QComboBox()
                group_combo.setEditable(True)
                group_combo.addItems(
                    ["shared:UP", "shared:遴选", f"solo:{e['pool_id']}"]
                )
                group_combo.setCurrentText(e["pity_group"])
                _widen_combo(group_combo, 130, 180)
                ptable.setCellWidget(r, 6, group_combo)
                reason_item = _ro(e["reason"])
                reason_item.setToolTip(e["reason"])
                ptable.setItem(r, 7, reason_item)
                e["row_combo"] = row_combo
                e["name_item"] = name_item
                e["type_combo"] = type_combo
                e["up_combo"] = up_combo
                e["group_combo"] = group_combo
                row_combo.currentIndexChanged.connect(
                    lambda _idx, ent=e: self._on_manual_pool_row_changed(ent)
                )
            ptable.resizeColumnsToContents()
            ptable.setColumnWidth(2, max(ptable.columnWidth(2), 280))
            ptable.setColumnWidth(5, max(ptable.columnWidth(5), 270))
            ptable.setColumnWidth(6, max(ptable.columnWidth(6), 150))
            self._pool_manual_table = ptable
            layout.addWidget(_wrap(ptable), 1)
        return _wrap(container)

    def _collect_manual_pool_entries(self) -> List[dict]:
        """池未知、需人工选行取池名的条目（只收页号一致的页）。"""
        entries: List[dict] = []
        batch_six = self._batch_new_six()
        for v in self._manual:
            task = v.task
            if task is None or v.page is None or not v.page.rows:
                continue
            if v.page.page != task.page or v.page.total_page != task.total_page:
                continue
            for pid in task.unknown_pool_ids:
                ridx = next(
                    (i for i, rec in enumerate(task.records) if rec.pool_id == pid),
                    None,
                )
                if ridx is None:
                    continue
                new_six, new_six_page, has_new_six = self._pick_new_six(
                    batch_six.get(str(pid), [])
                )
                up, up_src, up_page = self._resolve_pool_up(
                    pid, new_six, new_six_page
                )
                ptype = _default_pool_type(
                    pid, has_new_six or up_src == "inferred"
                )
                reason = v.reason
                if up_src == "new" and up_page and up_page != task.page:
                    reason += f"；UP 取自第 {up_page} 页的新 6★，请核对"
                elif up_src == "inferred":
                    reason += "；UP 按本池 6★ 记录推断，请核对"
                elif up_src == "solo":
                    reason += "；UP 按「本批唯一陌生角色且在池内」自动匹配，请核对"
                entries.append(
                    {
                        "validation": v,
                        "pool_id": str(pid),
                        "record": task.records[ridx],
                        "page": task.page,
                        "type": ptype,
                        "up_character": up,
                        "up_inferred": up_src == "inferred",
                        "up_solo": up_src == "solo",
                        "up_page": up_page,
                        "pity_group": _default_pity_group(ptype, pid),
                        "reason": reason,
                    }
                )
        return entries

    @staticmethod
    def _on_manual_row_changed(entry: dict) -> None:
        idx = entry["row_combo"].currentData()
        if idx is None or idx < 0:
            return
        row = entry["validation"].page.rows[int(idx)]
        entry["name_item"].setText(clean_character_name(row.name))
        entry["rarity_combo"].setCurrentIndex(
            max(0, RARITIES.index(int(row.rarity)))
        )

    @staticmethod
    def _on_manual_pool_row_changed(entry: dict) -> None:
        idx = entry["row_combo"].currentData()
        if idx is None or idx < 0:
            return
        row = entry["validation"].page.rows[int(idx)]
        entry["name_item"].setText(row.pool_name or "")

    # ---- 写入 ----

    def _collect_checked(self) -> Tuple[List[dict], List[dict]]:
        chars: List[dict] = []
        pools: List[dict] = []
        for r, d in enumerate(self._collect_character_drafts()):
            if self._char_table.item(r, 0).checkState() != Qt.CheckState.Checked:
                continue
            name = self._char_table.item(r, 2).text().strip()
            rarity = self._char_table.cellWidget(r, 3).currentData()
            if not name or rarity not in RARITIES:
                continue
            chars.append(
                {
                    "item_id": d["binding"].record.item_id,
                    "entry": {
                        "name": name,
                        "rarity": int(rarity),
                        "limited": None,
                        "source": {
                            "kind": "vision",
                            "page": d["page"],
                            "total_page": d["total_page"],
                            "row": d["row"],
                            "time": d["time"],
                            "pool_id": d["pool_id"],
                            "new_tag": d["new_tag"],
                            "ocr_name": d["ocr_name"],
                        },
                    },
                }
            )
        for r, d in enumerate(self._collect_pool_drafts()):
            if self._pool_table.item(r, 0).checkState() != Qt.CheckState.Checked:
                continue
            name = self._pool_table.item(r, 2).text().strip()
            ptype = self._pool_table.cellWidget(r, 3).currentText().strip()
            up = self._pool_table.cellWidget(r, 4).currentText().strip()
            group = self._pool_table.cellWidget(r, 5).currentText().strip()
            if not name or not ptype or not group:
                continue
            pools.append(
                {
                    "pool_id": d["pool_id"],
                    "entry": {
                        "name": name,
                        "type": ptype,
                        "rarity": 6,
                        "up_character": up,
                        "pity_group": group,
                        "source": {
                            "kind": "vision",
                            "page": d["page"],
                            "row": d["row"],
                        },
                    },
                }
            )
        # 人工配对：勾选且选了行才写，且同一行不得被两个 id 占用
        used_rows: Dict[Tuple[int, int], str] = {}
        for e in self._manual_rows:
            r = e["row"]
            if self._manual_table.item(r, 0).checkState() != Qt.CheckState.Checked:
                continue
            idx = e["row_combo"].currentData()
            if idx is None or int(idx) < 0:
                continue
            key = (id(e["validation"]), int(idx))
            if key in used_rows:
                QMessageBox.warning(
                    self,
                    "重复配对",
                    f"第 {e['page']} 页同一行被 {used_rows[key]} 与 {e['item_id']} 同时选中，"
                    "请只保留一个。",
                )
                return [], []
            used_rows[key] = e["item_id"]
            row = e["validation"].page.rows[int(idx)]
            name = e["name_item"].text().strip() or clean_character_name(row.name)
            rarity = e["rarity_combo"].currentData()
            chars.append(
                {
                    "item_id": e["item_id"],
                    "entry": {
                        "name": name,
                        "rarity": int(rarity),
                        "limited": None,
                        "source": {
                            "kind": "vision",
                            "page": e["page"],
                            "total_page": e["validation"].task.total_page,
                            "row": int(idx) + 1,
                            "time": row.time_norm,
                            "pool_id": e["record"].pool_id,
                            "new_tag": row.new_tag,
                            "ocr_name": row.ocr_name,
                        },
                    },
                }
            )
        # 人工配对：未知卡池（页号一致但自动校验未过；只有池未知也能写）
        auto_pool_ids = {d["pool_id"] for d in self._collect_pool_drafts()}
        used_pool_rows: Dict[Tuple[int, int], str] = {}
        for e in self._pool_manual_rows:
            if self._pool_manual_table is None:
                break
            r = e["row"]
            if (
                self._pool_manual_table.item(r, 0).checkState()
                != Qt.CheckState.Checked
            ):
                continue
            idx = e["row_combo"].currentData()
            if idx is None or int(idx) < 0:
                continue
            key = (id(e["validation"]), int(idx))
            if key in used_pool_rows:
                QMessageBox.warning(
                    self,
                    "重复配对",
                    f"第 {e['page']} 页同一行被卡池 {used_pool_rows[key]} 与 "
                    f"{e['pool_id']} 同时选中，请只保留一个。",
                )
                return [], []
            used_pool_rows[key] = e["pool_id"]
            row = e["validation"].page.rows[int(idx)]
            name = e["name_item"].text().strip() or row.pool_name
            ptype = e["type_combo"].currentText().strip()
            up = e["up_combo"].currentText().strip()
            group = e["group_combo"].currentText().strip()
            if not name or not ptype or not group:
                continue
            if e["pool_id"] in auto_pool_ids:
                continue  # 自动配对已生成同 id 条目，避免重复写
            pools.append(
                {
                    "pool_id": e["pool_id"],
                    "entry": {
                        "name": name,
                        "type": ptype,
                        "rarity": 6,
                        "up_character": up,
                        "pity_group": group,
                        "source": {
                            "kind": "vision",
                            "page": e["page"],
                            "row": int(idx) + 1,
                        },
                    },
                }
            )
        return chars, pools

    def _on_write(self) -> None:
        chars, pools = self._collect_checked()
        if not chars and not pools:
            QMessageBox.information(self, "没有可写入项", "请先勾选至少一条并选择配对行。")
            return
        warnings = []
        for c in chars:
            if c["item_id"] in self._known_characters:
                warnings.append(f"角色 {c['item_id']} 远端已收录，将跳过（远端优先）")
        for p in pools:
            if p["pool_id"] in self._known_pools:
                warnings.append(f"卡池 {p['pool_id']} 远端已收录，将跳过（远端优先）")
            if p["entry"]["type"] in ("UP", "遴选") and not p["entry"]["up_character"]:
                warnings.append(
                    f"卡池 {p['pool_id']} 未填 UP 角色：该池 6★ 会被统计为「歪」"
                )
        if warnings:
            msg = QMessageBox(self)
            msg.setWindowTitle("写入前确认")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText("\n".join(warnings))
            msg.setInformativeText("是否继续写入其余条目？")
            btn_go = msg.addButton("继续写入", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("返回修改", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() is not btn_go:
                return

        ok_chars: Dict[str, dict] = {}
        ok_pools: Dict[str, dict] = {}
        failed = []
        for c in chars:
            if c["item_id"] in self._known_characters:
                continue
            if upsert_character(c["item_id"], c["entry"]):
                ok_chars[c["item_id"]] = c["entry"]
            else:
                failed.append(f"角色 {c['item_id']}")
        for p in pools:
            if p["pool_id"] in self._known_pools:
                continue
            if upsert_pool(p["pool_id"], p["entry"]):
                ok_pools[p["pool_id"]] = p["entry"]
            else:
                failed.append(f"卡池 {p['pool_id']}")
        if failed:
            QMessageBox.warning(
                self,
                "部分写入失败",
                "以下条目写盘失败（权限/磁盘？），请检查 data 目录：\n"
                + "\n".join(failed),
            )
        self.written_characters = ok_chars
        self.written_pools = ok_pools
        logger.info(
            f"识别结果写入 overlay：角色 {len(ok_chars)}、卡池 {len(ok_pools)}"
        )
        self.accept()


class OverlayManagerDialog(QDialog):
    """本机识别结果管理：查看/删除 overlay 条目（可逆性入口）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("本机识别结果")
        self.resize(760, 460)
        self.removed: List[Tuple[str, str]] = []
        self._entries = list_entries()
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "这些条目只存在于本机 data/role_overlay.json；远端 role.json 收录同 id 后"
                "会自动清理。删除后统计回到识别前口径，历史记录不受影响。"
            )
        )
        table = QTableWidget(len(self._entries), 5)
        table.setHorizontalHeaderLabels(["类型", "ID", "名称", "详情", "来源"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for r, e in enumerate(self._entries):
            kind = "角色" if e.get("kind") == "character" else "卡池"
            if kind == "角色":
                detail = f"{e.get('rarity')}★" + (" 新" if (e.get("source") or {}).get("new_tag") else "")
            else:
                detail = f"{e.get('type')} / {e.get('pity_group')}"
            src = e.get("source") or {}
            evidence = f"第 {src.get('page', '?')} 页 第 {src.get('row', '?')} 行"
            table.setItem(r, 0, _ro(kind))
            table.setItem(r, 1, _ro(e.get("id", "")))
            table.setItem(r, 2, _ro(e.get("name", "")))
            table.setItem(r, 3, _ro(detail))
            table.setItem(r, 4, _ro(evidence))
        table.resizeColumnsToContents()
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table = table
        layout.addWidget(table, 1)

        row = QHBoxLayout()
        row.addStretch()
        btn_delete = QPushButton("删除选中")
        btn_close = QPushButton("关闭")
        row.addWidget(btn_delete)
        row.addWidget(btn_close)
        layout.addLayout(row)
        btn_delete.clicked.connect(self._on_delete)
        btn_close.clicked.connect(self.accept)

    def _on_delete(self) -> None:
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "未选择", "请先选中要删除的条目。")
            return
        if QMessageBox.question(
            self, "删除确认", f"删除选中的 {len(rows)} 条识别结果？统计将回到识别前口径。"
        ) != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            e = self._entries[r]
            kind = e.get("kind")
            oid = str(e.get("id", ""))
            if kind == "character":
                remove_character(oid)
            else:
                remove_pool(oid)
            self.removed.append((kind, oid))
        logger.info(f"删除本机识别结果 {len(rows)} 条")
        for r in reversed(rows):
            self._table.removeRow(r)
            del self._entries[r]


def _wrap(table: QTableWidget) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(table)
    return widget
