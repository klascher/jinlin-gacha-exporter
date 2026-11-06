"""角色练度窗口 - 非模态卡片网格 + 三层交互 + 双导出（2026-10-13，设计稿 §5/§6/§9）。

交互三层（设计稿 §5.3，语义互不混用）：
- 勾选 = 选择集：跨筛选持久（存 ui_state），出图/导出 md 只看勾选集；
- 筛选 = 星级 chips + 搜索：只影响当前显示，不改变勾选；
- 显示开关 = 全部 | 仅看勾选：勾选的用法出口（想只看某几个角色时用）。

兜底（设计稿 §9）：列表只显示抓到过的角色；null 字段显示「—」；
陌生 id 显示「未知角色 #id」+ 星级「？」且仅「全部」档可见；role.json 无该角色
lib 词条库时词条显示 #序号 并标注「词条库待更新」。

无游戏美术资源：卡片为纯文本布局，截图友好（2 列、词条折行不截断）。
"""
import os
from datetime import date
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis.roster_capture import translate_entries
from ..models import Character
from ..storage import roster
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 技能槽号 -> 槽名（复合 id 500_XX_槽号，槽号 = id % 1000）
SKILL_SLOT_LABELS = {"501": "普攻", "101": "被动", "201": "终结技"}

_STAR_CHIPS = (("all", "全部"), ("6", "6★"), ("5", "5★"), ("4", "4★"))
# 名称/星级的按星级配色：6★红 / 5★黄 / 4★紫（白底上取可读的深色调）
_RARITY_COLORS = {6: "#d63031", 5: "#d4a017", 4: "#8e44ad"}
_DISPLAY_ALL = "all"
_DISPLAY_CHECKED = "checked"
_CARD_WIDTH = 460
_GRID_COLUMNS = 2


class RosterPanel(QDialog):
    """角色练度窗口（非模态，show() 打开；数据来自 data/roster_<账号id>.json）。"""

    def __init__(
        self,
        parent,
        profile_id: str,
        profile_name: str,
        characters: Dict[str, Character],
    ):
        super().__init__(parent)
        self._profile_id = profile_id
        self._characters = characters
        self.setWindowTitle(f"角色练度 - {profile_name}")
        self.resize(1000, 720)
        self.setModal(False)

        state = roster.load_ui_state(profile_id)
        self._checked = set(state.get("checked") or [])
        self._display = state.get("display") if state.get("display") in (
            _DISPLAY_ALL, _DISPLAY_CHECKED) else _DISPLAY_ALL
        self._star = state.get("star") if state.get("star") in (
            "all", "6", "5", "4") else "6"

        root = QVBoxLayout(self)
        root.addLayout(self._build_toolbar())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._grid_host = QWidget()
        self._grid_host.setStyleSheet("background: #f5f5f5;")
        self._scroll.setWidget(self._grid_host)
        root.addWidget(self._scroll, 1)

        self._reload()

    # ---------- 顶栏 ----------

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self._star_group = QButtonGroup(self)
        self._star_group.setExclusive(True)
        for value, label in _STAR_CHIPS:
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            btn.setChecked(value == self._star)
            btn.clicked.connect(lambda _=False, v=value: self._set_star(v))
            self._star_group.addButton(btn)
            bar.addWidget(btn)

        self._chk_only_checked = QCheckBox("仅看勾选")
        self._chk_only_checked.setChecked(self._display == _DISPLAY_CHECKED)
        self._chk_only_checked.toggled.connect(self._set_display)
        bar.addWidget(self._chk_only_checked)

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索角色名 / id…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._reload())
        bar.addWidget(self._search, 1)

        sort_hint = QLabel("排序：星级↓ 等级↓ id↑")
        sort_hint.setStyleSheet("color: #999;")
        sort_hint.setToolTip("固定排序：星级降序 → 等级降序（未抓到等级的排所在星级末尾）→ id 升序")
        bar.addWidget(sort_hint)

        # 出图 = 当前展示（筛选后）的角色；导出 md = 勾选集
        self._btn_png = QPushButton("出图 PNG")
        self._btn_png.setToolTip("把当前展示的角色（筛选后的结果）导出为长图")
        self._btn_png.clicked.connect(self._export_png)
        bar.addWidget(self._btn_png)
        self._btn_md = QPushButton("导出 Markdown")
        self._btn_md.setToolTip("把勾选的角色导出为 Markdown 表格")
        self._btn_md.clicked.connect(self._export_md)
        bar.addWidget(self._btn_md)
        return bar

    # ---------- 列表构建 ----------

    def _roster_rows(self) -> List[Tuple[tuple, str, dict, Optional[Character]]]:
        """排序后的 (排序键, cid, 练度记录, 角色元数据|None) 列表。

        排序（设计稿 §9）：星级↓ → 等级↓（null 最后）→ id↑。
        """
        data = roster.load_roster(self._profile_id)["characters"]
        rows = []
        for cid, rec in data.items():
            ch = self._characters.get(cid)
            rarity = ch.rarity if ch is not None else 0
            level = rec.get("level")
            rows.append((
                (-rarity, -(level) if level is not None else float("inf"), cid),
                cid, rec, ch,
            ))
        rows.sort(key=lambda r: r[0])
        return rows

    def _visible_rows(self) -> List[Tuple[tuple, str, dict, Optional[Character]]]:
        query = self._search.text().strip().lower()
        out = []
        for row in self._roster_rows():
            _key, cid, rec, ch = row
            if self._display == _DISPLAY_CHECKED and cid not in self._checked:
                continue
            if self._star != "all":
                rarity = ch.rarity if ch is not None else 0
                if rarity != int(self._star):
                    continue  # 陌生 id（星级？）仅「全部」档可见
            if query:
                name = ch.name if ch is not None else f"未知角色 #{cid}"
                if query not in name.lower() and query not in cid.lower():
                    continue
            out.append(row)
        return out

    def _reload(self) -> None:
        # 布局常驻（对同一 host 重复建 QGridLayout 会警告且旧布局无法即时销毁），
        # 每次只清条目；行拉伸也一并复位，避免残留行数不一致的拉伸。
        if getattr(self, "_grid", None) is None:
            self._grid = QGridLayout(self._grid_host)
            self._grid.setContentsMargins(12, 12, 12, 12)
            self._grid.setSpacing(10)
        else:
            while self._grid.count():
                item = self._grid.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            for r in range(self._grid.rowCount()):
                self._grid.setRowStretch(r, 0)

        rows = self._visible_rows()
        if not rows:
            data = roster.load_roster(self._profile_id)["characters"]
            hint = QLabel(
                "暂无练度数据。\n\n抓包开启后，在游戏内翻阅「全部角色」页面即可自动采集\n"
                "映像与词条（每页自动推送）；等级 / 叠卡 / 技能在对应变化发生时自动记录。"
                if not data else
                "没有符合当前筛选的角色。"
            )
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #888; padding: 40px;")
            self._grid.addWidget(hint, 0, 0, 1, _GRID_COLUMNS)
        else:
            for i, (_key, cid, rec, ch) in enumerate(rows):
                self._grid.addWidget(
                    self._make_card(cid, rec, ch), i // _GRID_COLUMNS, i % _GRID_COLUMNS
                )
        self._grid.setRowStretch(self._grid.rowCount(), 1)

        self._btn_png.setEnabled(bool(rows))  # 出图 = 当前展示集
        has_checked = bool(self._checked & set(roster.load_roster(self._profile_id)["characters"]))
        self._btn_md.setEnabled(has_checked)  # 导出 md = 勾选集

    # ---------- 卡片 ----------

    def _make_card(self, cid: str, rec: dict, ch: Optional[Character]) -> QFrame:
        card = QFrame()
        card.setFixedWidth(_CARD_WIDTH)
        card.setStyleSheet(
            "QFrame { background: #fff; border: 1px solid #ddd; border-radius: 6px; }"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        # 行1：勾选 + 名字（按星级配色） + 星级数字
        head = QHBoxLayout()
        cb = QCheckBox()
        cb.setChecked(cid in self._checked)
        cb.toggled.connect(lambda on, c=cid: self._set_checked(c, on))
        head.addWidget(cb)
        rarity = ch.rarity if ch is not None else 0
        name = ch.name if ch is not None else f"未知角色 #{cid}"
        title = QLabel(name)
        f = title.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() + 1)
        title.setFont(f)
        if rarity in _RARITY_COLORS:
            title.setStyleSheet(f"color: {_RARITY_COLORS[rarity]};")
        head.addWidget(title)
        head.addStretch()
        stars = QLabel(f"{rarity}⭐" if rarity else "？")
        if rarity in _RARITY_COLORS:
            stars.setStyleSheet(f"color: {_RARITY_COLORS[rarity]}; font-weight: bold;")
        head.addWidget(stars)
        lay.addLayout(head)

        # 行2：等级 / 叠卡（null = 未抓到 → 「—」）
        meta = QLabel(
            f"等级：{rec['level'] if rec.get('level') is not None else '—'}    "
            f"叠卡：{rec['stack'] if rec.get('stack') is not None else '—'}"
        )
        lay.addWidget(meta)

        # 行3：技能（三槽带槽名；整组未抓到 → 「—」）
        skills = rec.get("skills")
        if isinstance(skills, dict) and skills:
            parts = []
            for slot, slot_name in SKILL_SLOT_LABELS.items():
                lv = skills.get(slot)
                parts.append(f"{slot_name} {lv if lv is not None else '—'}")
            skill_lbl = QLabel("技能：" + "　".join(parts))
        else:
            skill_lbl = QLabel("技能：—")
        lay.addWidget(skill_lbl)

        # 行4：映像（[0,0,0] 是真实数据，原样展示）
        image = rec.get("image")
        if isinstance(image, list) and image:
            lay.addWidget(QLabel(f"映像：{'/'.join(str(v) for v in image)}"))
        else:
            lay.addWidget(QLabel("映像：—"))

        # 行5：词条（折行不截断；无 lib 标「词条库待更新」）
        entries = rec.get("entries")
        if isinstance(entries, list) and entries:
            lib = ch.lib if ch is not None else None
            names = translate_entries(entries, lib)
            txt = "、".join(names)
            if lib is None:
                txt += "（词条库待更新）"
        elif entries == []:
            txt = "无"
        else:
            txt = "—"
        entry_lbl = QLabel(f"词条：{txt}")
        entry_lbl.setWordWrap(True)
        lay.addWidget(entry_lbl)
        return card

    # ---------- 状态 ----------

    def _save_state(self) -> None:
        roster.save_ui_state(self._profile_id, {
            "checked": sorted(self._checked),
            "display": self._display,
            "star": self._star,
        })

    def _set_checked(self, cid: str, on: bool) -> None:
        if on:
            self._checked.add(cid)
        else:
            self._checked.discard(cid)
        self._save_state()
        self._btn_md.setEnabled(bool(self._checked))  # 只有 md 走勾选集；出图随筛选

    def _set_display(self, on: bool) -> None:
        self._display = _DISPLAY_CHECKED if on else _DISPLAY_ALL
        self._save_state()
        self._reload()

    def _set_star(self, value: str) -> None:
        self._star = value
        self._save_state()
        self._reload()

    def refresh(self) -> None:
        """外部通知数据有更新时重读（保持勾选/筛选不变）。"""
        self._reload()

    # ---------- 导出（仅勾选集） ----------

    def _checked_rows(self) -> List[Tuple[str, dict, Optional[Character]]]:
        data = roster.load_roster(self._profile_id)["characters"]
        out = []
        for _key, cid, rec, ch in self._roster_rows():
            if cid in self._checked and cid in data:
                out.append((cid, rec, ch))
        return out

    def _entries_text(self, rec: dict, ch: Optional[Character]) -> str:
        entries = rec.get("entries")
        if not isinstance(entries, list) or not entries:
            return "无" if entries == [] else "—"
        lib = ch.lib if ch is not None else None
        names = translate_entries(entries, lib)
        txt = "、".join(names)
        if lib is None:
            txt += "（词条库待更新）"
        return txt

    def _skills_text(self, rec: dict) -> str:
        skills = rec.get("skills")
        if not isinstance(skills, dict) or not skills:
            return "—"
        parts = [
            f"{slot_name}{skills[slot]}" for slot, slot_name in SKILL_SLOT_LABELS.items()
            if skills.get(slot) is not None
        ]
        return "/".join(parts) if parts else "—"

    def _suggest_path(self, ext: str) -> str:
        return os.path.join(
            os.path.expanduser("~"), "Desktop",
            f"角色练度导出_{date.today().isoformat()}.{ext}",
        )

    def _export_png(self) -> None:
        # 出图 = 当前展示（筛选后）的角色，与页面所见一致
        rows = [(cid, rec, ch) for _key, cid, rec, ch in self._visible_rows()]
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "出图 PNG", self._suggest_path("png"), "PNG 图片 (*.png)")
        if not path:
            return
        # 勾选集卡片单独摆一张离屏画布再 grab，不受当前筛选/滚动位置影响
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(10)
        for i, (cid, rec, ch) in enumerate(rows):
            grid.addWidget(self._make_card(cid, rec, ch), i // _GRID_COLUMNS, i % _GRID_COLUMNS)
        host.setStyleSheet("background: #f5f5f5;")
        host.adjustSize()
        pix = host.grab()
        if pix.save(path):
            logger.info(f"练度出图: {path}")
        else:
            QMessageBox.warning(self, "出图失败", f"无法写入文件：\n{path}")

    def _export_md(self) -> None:
        rows = self._checked_rows()
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Markdown", self._suggest_path("md"), "Markdown (*.md)")
        if not path:
            return
        lines = [
            f"# 角色练度导出 · {date.today().isoformat()}",
            "",
            f"> 共 {len(rows)} 个勾选角色；「—」= 尚未抓到该数据。",
            "",
            "| 角色 | 星 | 等级 | 叠卡 | 技能 | 映像 | 词条 |",
            "|---|---|---|---|---|---|---|",
        ]
        for cid, rec, ch in rows:
            name = ch.name if ch is not None else f"未知角色 #{cid}"
            rarity = ch.rarity if ch is not None else 0
            image = rec.get("image")
            img_txt = "/".join(str(v) for v in image) if isinstance(image, list) and image else "—"
            star_txt = f"{rarity}⭐" if rarity else "？"
            lines.append(
                f"| {name} | {star_txt} "
                f"| {rec['level'] if rec.get('level') is not None else '—'} "
                f"| {rec['stack'] if rec.get('stack') is not None else '—'} "
                f"| {self._skills_text(rec)} | {img_txt} | {self._entries_text(rec, ch)} |"
            )
        try:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write("\n".join(lines) + "\n")
            logger.info(f"练度导出: {path}")
        except OSError as e:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：\n{path}\n\n{e}")
