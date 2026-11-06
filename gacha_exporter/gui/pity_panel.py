"""保底主面板 - 按保底分组展示各卡池出货情况，6星第几抽/是否歪为主信息。

替代旧 StatsPanel 的纯文本保底区：每个保底分组一个可折叠区块，组内每个卡池一行
（抽了多少抽 / 是否出六星 / 出了哪些），6星命中行缩进显示「第N抽 [歪]/[UP] 角色名 时间」。
每抽明细另走 main_window 的「查看明细」弹窗，平时不占空间。

双视图（摘要栏右侧切换，2026-10-08 定稿）：「按卡池」= 上述现状；「按角色」=
流水视图——组内每条六星出货一行「[UP]/[歪] 第N抽 角色名 池名 时间」（2026-10-10 去必出标注），
不按池/角色聚合、无折叠层级，复刻（同角色复用 pool_id）的多期出货各成一行。
见 docs/已完成/展示设计说明 v0.2。

分组顺序由 StatsEngine.shared_pity_tracking() 的天然顺序决定（has_state 优先 + label
排序）-> UP池 / 遴选池 / 常驻池 / 新手池（新手池在最下面）。组内卡池按 pool_id 号
倒序，号大（新卡池）在前（#1 = 最新）；池内命中按 ts desc，最近一次 6 星在上。
"""
from datetime import datetime
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis.stats import StatsEngine
from ..models import Character, Pool
from ..storage.settings import load_pity_panel_view, save_pity_panel_view

# 折叠箭头位：所有卡池行（含无命中池）都固定占位，保证名称列对齐
_FOLD_ARROW_W = 16        # 箭头位固定宽度
_HEADER_SPACING = 10      # 头部各段间距
_FOLD_INDENT = _FOLD_ARROW_W + _HEADER_SPACING  # 命中明细缩进 = 对齐池名左缘

# 流水视图固定列宽（2026-10-08 用户定稿：各列同处一条垂直线）——
# 角色名按最长 15 字、池名按最长 10 字、抽数按两位数计算，超长裁切
_FLOW_NAME_CHARS = 15
_FLOW_POOL_CHARS = 10


def _col_w(sample: str, pad: int = 8) -> int:
    """固定列宽 = 样文本像素宽 + 余量（各行同列垂直对齐）。"""
    return QFontMetrics(QApplication.font()).horizontalAdvance(sample) + pad


def _fmt_hit_time(ts: int) -> str:
    """命中时间格式 YYYY-MM-DD HH:MM（跨年也能看清）。"""
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def _fmt_capture_time(iso_str: str) -> str:
    """最近更新时间：ISO 字符串 -> YYYY-MM-DD HH:MM，空/非法返回 '-'。"""
    if not iso_str:
        return "-"
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "-"


def _pool_sort_key(pid: str):
    """组内池排序键：按 pool_id 号倒序（号大=新卡池在前），序号 #1 起。

    order 字段曾用于排序，但维护时容易漏改（如最新池仍沿用旧值被排到末尾），
    改按 pool_id 号倒序后无需再维护 order 的递增性。非纯数字 pid 排最后。
    """
    try:
        return (-int(pid), 0)
    except ValueError:
        return (0, pid)


class CollapsibleSection(QFrame):
    """可折叠区块：头部（▼/▶ 折叠控制器 + 可选「展开」短按钮）+ 可切换显隐 body 容器。

    show_bulk_toggle=True 时标题右侧紧挨「展开/收起」短按钮：点击后展开分组内全部
    卡池的命中明细，或收起只留第一个（最新）有命中的池展开。组内不足 2 个有命中池
    时按钮自动隐藏（无可批量操作）。标题保持紧凑靠左（原版观感）。
    """

    def __init__(self, title: str, expanded: bool, show_bulk_toggle: bool = False, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._title = title
        self._expanded = expanded
        self._pool_rows: List = []  # 已 add_row 的卡池行（供一键展收批量操作）

        # 头部条：左 紧凑标题（按内容宽靠左）+ 紧挨 展开/收起 短按钮
        header_bar = QWidget(self)
        hb = QHBoxLayout(header_bar)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(4)

        self.header = QToolButton(header_bar)
        self.header.setCheckable(True)
        # QToolButton 默认水平 Fixed：标题按内容宽紧凑靠左，不拉满整行
        self.header.setStyleSheet(
            "QToolButton { text-align: left; padding: 5px 8px; font-weight: bold; "
            "border: 1px solid #d0d0d0; border-radius: 3px; background: #f5f5f5; }"
        )
        self.header.toggled.connect(self._on_toggled)
        hb.addWidget(self.header, 0, Qt.AlignVCenter)

        self._bulk_btn = None
        if show_bulk_toggle:
            self._bulk_btn = QPushButton("展开", header_bar)
            self._bulk_btn.setCursor(Qt.PointingHandCursor)
            self._bulk_btn.setFixedSize(52, 26)  # 固定尺寸，防被布局拉宽（余宽会涌向可增长 widget）
            self._bulk_btn.setStyleSheet(
                "QPushButton { padding: 2px 8px; border: 1px solid #b0b0b0; "
                "border-radius: 3px; background: #fff; color: #333; font-weight: bold; }"
                "QPushButton:hover { background: #f0f0f0; }"
            )
            self._bulk_btn.clicked.connect(self._toggle_all)
            hb.addWidget(self._bulk_btn, 0, Qt.AlignVCenter)
            self._bulk_btn.setVisible(False)  # 组内满 2 个有命中池再显示
        # 末尾 stretch 吸收余宽：否则余宽会塞在标题与按钮之间把按钮推到最右
        hb.addStretch(1)

        outer.addWidget(header_bar)

        self.body = QFrame(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(14, 3, 4, 3)
        self.body_layout.setSpacing(1)

        outer.addWidget(self.body)
        self._apply()

    def _on_toggled(self, checked: bool) -> None:
        self._expanded = checked
        self._apply()

    def _apply(self) -> None:
        # setChecked 在状态未变时不发信号，故不会递归
        self.header.setChecked(self._expanded)
        self.header.setText(("▼ " if self._expanded else "▶ ") + self._title)
        self.body.setVisible(self._expanded)

    def add_row(self, widget: QWidget) -> None:
        """向 body 追加一行；满 2 个有命中池时显示一键展收按钮。"""
        self.body_layout.addWidget(widget)
        self._pool_rows.append(widget)
        if self._bulk_btn is not None:
            self._bulk_btn.setVisible(
                sum(1 for r in self._pool_rows if r._has_hits) >= 2
            )

    def _toggle_all(self) -> None:
        """一键展收：全部展开；或收起只留第一个（最新）有命中的池展开。"""
        rows = [r for r in self._pool_rows if r._has_hits]
        if not rows:
            return
        if all(r._expanded for r in rows):
            # 已全展开 -> 收起只留第一个有命中的池
            keep, rest = rows[0], rows[1:]
            for r in rest:
                if r._expanded:
                    r.toggle()
            self._set_bulk_text("展开")
        else:
            # 未全展开 -> 全部展开
            for r in rows:
                if not r._expanded:
                    r.toggle()
            self._set_bulk_text("收起")

    def _set_bulk_text(self, text: str) -> None:
        if self._bulk_btn is not None:
            self._bulk_btn.setText(text)


class HitRow(QFrame):
    """单条 6 星命中行：第N抽  [歪]/[UP]  角色名  时间（左缩进）。

    featured 池显示 [UP]/[歪] 着色标记；大保底必出不再单独标注（2026-10-10
    用户拍板去「必出」，引擎 guaranteed 输出保留仅展示不用）；
    非 featured（常驻/新手）不显示标记；
    manual=True 的角色名前加浅灰「手」标记（手动补齐），不抢 [UP]/[歪] 配色。
    """

    def __init__(self, pity: int, ts: int, name: str, is_up: bool, featured: bool,
                 manual: bool = False, parent=None):
        super().__init__(parent)
        self.pity = pity
        self.ts = ts
        self.is_up = is_up
        self.featured = featured
        self.manual = manual
        self.tag_label = None  # [UP]/[歪] QLabel（非 featured 池为 None）
        self.manual_label = None  # 「手」QLabel（非手动为 None）

        lay = QHBoxLayout(self)
        lay.setContentsMargins(_FOLD_INDENT, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"第{pity}抽"))
        if featured:
            tag_text = "[UP]" if is_up else "[歪]"
            tag = QLabel(tag_text)
            tag.setStyleSheet(
                "color: #2e8b57; font-weight: bold;" if is_up
                else "color: #c0392b; font-weight: bold;"
            )
            self.tag_label = tag
            lay.addWidget(tag)
        if manual:
            man = QLabel("手")
            man.setStyleSheet("color: #b0b0b0; font-weight: bold;")
            man.setToolTip("手动补齐")
            self.manual_label = man
            lay.addWidget(man)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("font-weight: bold;" if not manual else "color: #888; font-weight: bold;")
        lay.addWidget(name_lbl)
        lay.addStretch()
        time_lbl = QLabel(_fmt_hit_time(ts))
        time_lbl.setStyleSheet("color: #888;")
        lay.addWidget(time_lbl)


class FlowHitRow(QFrame):
    """「按角色」流水视图单条出货行：[UP]/[歪]  第N抽  角色名  池名  时间。

    与按卡池视图的 HitRow 区别：标签在最前（用户定稿行序）、行尾多池名标注
    （复刻=同角色复用 pool_id，同一角色的多期出货靠池名+时间区分）、
    无「手」标记（D10：手动补齐不进入流水视图）。
    各列固定宽度（标签列/第N抽列/角色名 15 字/池名 10 字，超长裁切），
    跨行同列垂直对齐；非 featured 池标签留空占位（同宽），后续列不左移。
    """

    def __init__(self, pity: int, ts: int, name: str, pool_name: str,
                 is_up: bool, featured: bool, parent=None):
        super().__init__(parent)
        self.pity = pity
        self.ts = ts
        self.name = name
        self.pool_name = pool_name
        self.is_up = is_up
        self.featured = featured
        self.tag_label = None  # [UP]/[歪] QLabel（非 featured 池为 None）

        lay = QHBoxLayout(self)
        lay.setContentsMargins(_FOLD_INDENT, 0, 0, 0)
        lay.setSpacing(10)
        # 标签列（固定宽）：featured 池显示 [UP]/[歪]，其余留空占位对齐
        if featured:
            tag_text = "[UP]" if is_up else "[歪]"
            tag = QLabel(tag_text)
            tag.setStyleSheet(
                "color: #2e8b57; font-weight: bold;" if is_up
                else "color: #c0392b; font-weight: bold;"
            )
            self.tag_label = tag
        else:
            tag = QLabel("")
        tag.setFixedWidth(_col_w("[UP]"))
        lay.addWidget(tag)
        pity_lbl = QLabel(f"第{pity}抽")
        pity_lbl.setFixedWidth(_col_w("第00抽"))
        lay.addWidget(pity_lbl)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("font-weight: bold;")
        name_lbl.setFixedWidth(_col_w("字" * _FLOW_NAME_CHARS))
        lay.addWidget(name_lbl)
        pool_lbl = QLabel(pool_name)
        pool_lbl.setStyleSheet("color: #888;")
        pool_lbl.setFixedWidth(_col_w("字" * _FLOW_POOL_CHARS))
        lay.addWidget(pool_lbl)
        lay.addStretch()
        time_lbl = QLabel(_fmt_hit_time(ts))
        time_lbl.setStyleSheet("color: #888;")
        lay.addWidget(time_lbl)


class _ClickableFrame(QFrame):
    """鼠标左键点击触发的容器：可折叠卡池的整行头部。

    QLabel 默认不吞鼠标事件，因此点击头部行内任意文字都会冒泡到这里触发折叠。
    """

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_click()
        super().mouseReleaseEvent(event)


class PoolRow(QFrame):
    """单卡池行：#序号 卡池名 [UP:角色] 抽X抽 [不歪率] [未出六星] + 可折叠命中明细。

    six_count==0 时行尾显示灰色「未出六星」，无命中明细、箭头留空占位（名称列对齐）；
    有命中时头部整行可点击切换展开/折叠（▾/▸），命中行缩进在 body 内按 ts 倒序
    （最近在上）。
    """

    def __init__(self, index: int, pool: Pool, total: int, six_count: int,
                 hits: List[dict], wins: int = 0, all_up: int = 0,
                 collapsed: bool = False,
                 manual_ts_bound: Optional[int] = None,
                 on_toggle: Optional[Callable[[str, bool], None]] = None,
                 parent=None):
        super().__init__(parent)
        self.pool_id = pool.id
        self.six_count = six_count
        self._has_hits = bool(hits)
        self._expanded = not collapsed
        self._on_toggle = on_toggle
        self.unshipped_label = None  # 「未出六星」QLabel（出了六星则为 None）
        self.up_rate_label = None  # 「不歪率」QLabel（非 featured 池为 None）
        self.arrow_label = None  # ▾/▸ 折叠箭头 QLabel（始终创建占位；无命中池文本为空）
        # 该池是否存在「手动补齐」的命中（hit.ts < 组最早真实ts）
        self.manual_ts_bound = manual_ts_bound
        self.has_manual = bool(
            manual_ts_bound is not None
            and any(h.get("ts", 0) < manual_ts_bound for h in hits)
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(1)

        # 头部行：有命中时整行可点击折叠；箭头位固定占位（无命中池留空），名称列对齐
        header = _ClickableFrame(self._on_header_click, self) if self._has_hits else QFrame(self)
        line = QHBoxLayout(header)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(_HEADER_SPACING)
        self.arrow_label = QLabel("")
        self.arrow_label.setFixedWidth(_FOLD_ARROW_W)
        self.arrow_label.setStyleSheet("color: #000; font-weight: bold;")
        line.addWidget(self.arrow_label)
        idx_lbl = QLabel(f"#{index}")
        idx_lbl.setStyleSheet("color: #999;")
        line.addWidget(idx_lbl)
        name_lbl = QLabel(pool.name)
        name_lbl.setStyleSheet("font-weight: bold;")
        line.addWidget(name_lbl)
        if pool.is_featured and pool.up_character:
            up_lbl = QLabel(f"UP:{pool.up_character}")
            up_lbl.setStyleSheet("color: #555;")
            line.addWidget(up_lbl)
        line.addWidget(QLabel(f"{total}抽"))
        if self.has_manual:
            man = QLabel("手")
            man.setStyleSheet("color: #b0b0b0; font-weight: bold;")
            man.setToolTip("含手动补齐的抽数")
            line.addWidget(man)
        if pool.is_featured:
            if all_up > 0:
                rate_lbl = QLabel(f"不歪率 {wins / all_up * 100:.1f}%")
            else:
                rate_lbl = QLabel("不歪率 -")
            rate_lbl.setStyleSheet("color: #555;")
            self.up_rate_label = rate_lbl
            line.addWidget(rate_lbl)
        if six_count == 0:
            unshipped = QLabel("未出六星")
            unshipped.setStyleSheet("color: #aaa;")
            self.unshipped_label = unshipped
            line.addWidget(unshipped)
        line.addStretch()
        outer.addWidget(header)

        # 命中明细 body（可折叠；无命中时为空容器）。缩进由 HitRow 的 _FOLD_INDENT 承担
        self.body = QFrame(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(1)
        if self._has_hits:
            # hits 由 stats 按时间旧->新追加；展示要新->旧：ts 降序为主，
            # 同 ts（同一十连内多个 6 星）按列表序倒排，后出的（新）在上。
            for _, h in sorted(
                enumerate(hits),
                key=lambda x: (x[1].get("ts", 0), x[0]),
                reverse=True,
            ):
                self.body_layout.addWidget(HitRow(
                    pity=h["pity"],
                    ts=h.get("ts", 0),
                    name=h["name"],
                    is_up=h["is_up"],
                    featured=pool.is_featured,
                    manual=(
                        self.manual_ts_bound is not None
                        and h.get("ts", 0) < self.manual_ts_bound
                    ),
                ))
        outer.addWidget(self.body)
        self._apply()

    def toggle(self) -> None:
        """切换展开/折叠，并回调记忆折叠状态。"""
        self._expanded = not self._expanded
        if self._on_toggle is not None:
            self._on_toggle(self.pool_id, not self._expanded)
        self._apply()

    def _on_header_click(self) -> None:
        if self._has_hits:
            self.toggle()

    def _apply(self) -> None:
        self.body.setVisible(self._expanded and self._has_hits)
        # 无命中池箭头留空占位（保持名称列对齐）；有命中池显示小三角（▾/▸ 同尺寸）
        if self._has_hits:
            self.arrow_label.setText("▾" if self._expanded else "▸")
        else:
            self.arrow_label.setText("")


class PityPanel(QWidget):
    """保底主面板。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # 顶部：账号(左) + 最近更新(右)
        self._account_label = QLabel("")
        self._account_label.setStyleSheet("font-weight: bold;")
        self._update_label = QLabel("")
        self._update_label.setStyleSheet("color: #555;")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.addWidget(self._account_label)
        header_layout.addStretch()
        header_layout.addWidget(self._update_label)
        header_bar = QFrame()
        header_bar.setStyleSheet("background: #fafafa; border: 1px solid #e5e5e5;")
        header_bar.setLayout(header_layout)
        root.addWidget(header_bar)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(
            "padding: 3px 8px; background: #f5f5f5; border: 1px solid #e5e5e5;"
        )
        root.addWidget(self._summary_label)

        # 视图切换（按卡池 | 按角色）：右对齐互斥按钮，切换即以最近一次数据重建。
        # 按卡池 = 现状视图；按角色 = 流水视图（组内每条六星出货一行，不聚合）。
        # 视图偏好持久化到 settings.json（pity_panel_view），跨启动记忆
        self._view_mode = load_pity_panel_view()
        self._last_update_args = None  # update_data 入参缓存，供切换重建
        toggle_bar = QFrame()
        tb = QHBoxLayout(toggle_bar)
        tb.setContentsMargins(8, 0, 8, 0)
        tb.addStretch(1)
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        self._btn_pool_view = QPushButton("按卡池", toggle_bar)
        self._btn_flow_view = QPushButton("按角色", toggle_bar)
        for i, btn in enumerate((self._btn_pool_view, self._btn_flow_view)):
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedSize(64, 26)
            btn.setStyleSheet(
                "QPushButton { padding: 2px 8px; border: 1px solid #b0b0b0; "
                "border-radius: 3px; background: #fff; color: #333; }"
                "QPushButton:checked { background: #e8e8e8; font-weight: bold; }"
            )
            self._view_group.addButton(btn, i)
            tb.addWidget(btn, 0, Qt.AlignVCenter)
        self._btn_pool_view.setChecked(self._view_mode == "pool")
        self._btn_flow_view.setChecked(self._view_mode == "flow")
        self._view_group.idClicked.connect(self._on_view_changed)
        root.addWidget(toggle_bar)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(6)
        self._container_layout.addStretch()
        self.scroll.setWidget(self._container)
        root.addWidget(self.scroll, 1)

        # 会话内记忆：各池手动折叠状态（pool_id -> 是否折叠），刷新重建时保留
        self._pool_collapsed: Dict[str, bool] = {}
        # 手动补齐边界：{group_id: 组最早真实 ts}，命中 ts<该值判「手」
        self._manual_bound: Dict[str, int] = {}

    def update_data(
        self,
        stats: StatsEngine,
        characters: Dict[str, Character],
        pools: Dict[str, Pool],
        account_name: str,
        last_capture_iso: str = "",
        manual_bound_by_group: Optional[Dict[str, int]] = None,
    ) -> None:
        """刷新全面板。

        Args:
            stats: 统计引擎（可为企业主窗口的影子引擎，含手动补齐记录）
            characters: 角色映射（预留，命中名已由 stats 解析）
            pools: 卡池映射
            account_name: 当前账号名（顶部展示）
            last_capture_iso: 上次抓包完成时间 ISO 字符串，空则显示 '-'
            manual_bound_by_group: {group_id: 组最早真实 ts}；命中 ts<该值判为
                「手」标记（补录记录）。缺省 None 表示无手动数据。
        """
        self._last_update_args = (
            stats, characters, pools, account_name,
            last_capture_iso, manual_bound_by_group,
        )
        tracking = stats.shared_pity_tracking()
        self._manual_bound = manual_bound_by_group or {}

        # 顶部摘要
        # 「页数」提示已移除（无意义）：最初打算显示「共计 N 页」，但 全部卡池
        # 页数 = ceil(总条数/5)，本就由总抽数唯一决定，单独显示没有新增信息；
        # 曾尝试按 (pool_id, batch_seq) 去重计数，但 batch_seq 是解析器按会话帧序
        # 累加的计数器（跨全部池/视图单调递增、每次抓包从 0 重计），不是可靠
        # 页序号，按真实池 id 一分会把常驻/UP 等记录拆散到全部卡池的帧号上，
        # 产生空洞虚高（曾把 124 页算成 131 页）。真实页数 = ceil(total/5)。
        six_total = sum(g["six_count"] for g in tracking.values())
        shipped = sum(
            1 for g in tracking.values()
            for pinfo in g["per_pool"].values() if pinfo["six_count"] > 0
        )
        total_pools = len(pools)
        rate = (six_total / stats.total * 100) if stats.total else 0.0
        # 总不歪率：小保底命中UP / 全部UP出货，仅 has_state 组（UP+遴选池），
        # 常驻/新手无 UP 概念不计入（non_warp_rates 内部已跳过非 state 组）
        rates = stats.non_warp_rates(tracking)
        tot_wins = sum(r["wins"] for r in rates.values())
        tot_up = sum(r["all_up"] for r in rates.values())
        up_rate_str = f"{tot_wins / tot_up * 100:.1f}%" if tot_up else "-"
        # 手动补齐命中数（hit.ts < 组最早真实 ts）——仅展示用。
        # 关键：只对「存在手动边界」的组计数；无边界(没做过手补)的组记 0 条，
        # 绝不能用一个大数兜底（否则所有真实命中都满足 ts<兜底 → 全都误算成手补）。
        manual_hits = 0
        for gkey, info in tracking.items():
            mb = self._manual_bound.get(gkey)
            if mb is not None:
                manual_hits += sum(
                    1 for h in info["hits"] if h.get("ts", 0) < mb
                )
        self._account_label.setText(f"账号: {account_name}")
        self._update_label.setText(f"最近更新: {_fmt_capture_time(last_capture_iso)}")
        manual_suffix = f"    手补 {manual_hits}" if manual_hits else ""
        self._summary_label.setText(
            f"总抽数 {stats.total}    6星 {six_total}    "
            f"出货池 {shipped}/{total_pools}    6星率 {rate:.1f}%    不歪率 {up_rate_str}"
            f"{manual_suffix}"
        )

        # 清掉旧分组（保留末尾 stretch）
        while self._container_layout.count() > 1:
            item = self._container_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not tracking:
            hint = QLabel("暂无抽卡记录，点击「开始抓包」后此处展示各卡池出货情况。")
            hint.setStyleSheet("color: #999; padding: 12px;")
            self._container_layout.insertWidget(self._container_layout.count() - 1, hint)
            return

        builder = (
            self._build_section_flow if self._view_mode == "flow" else self._build_section
        )
        for gkey, info in tracking.items():
            section = builder(
                info, pools, rates.get(gkey), self._manual_bound.get(gkey)
            )
            self._container_layout.insertWidget(self._container_layout.count() - 1, section)

    def _on_view_changed(self, btn_id: int) -> None:
        """切换按卡池/按角色视图：以最近一次 update_data 的入参整体重建并持久化。"""
        mode = "pool" if btn_id == 0 else "flow"
        if mode == self._view_mode or self._last_update_args is None:
            return
        self._view_mode = mode
        save_pity_panel_view(mode)
        self.update_data(*self._last_update_args)

    def _section_title(self, info: dict, pools: Dict[str, Pool],
                       rates: Optional[dict] = None) -> str:
        """组头标题（按卡池/按角色两种视图共用）。rates 为该组 non_warp_rates 输出。"""
        label = info["label"]
        if info["has_state"]:
            state_str = f"  {info['state']}" if info["state"] else ""
            six = info["six_count"]
            gr = rates["rate"] if rates else None
            up_rate_str = f"{gr * 100:.1f}%" if gr is not None else "-"
            return (
                f"{label}  共享保底 {info['current_pity']}/{info['hard_pity']}{state_str}"
                f"  距保底{info['distance_to_pity']}抽  │  组内{info['total']}抽 6星×{six}"
                f"  不歪率 {up_rate_str}"
            )
        # 独立组：若池名与组标签不同，把池名带进组头（如 常驻池「示例卡池007」）
        solo_pid = next(iter(info["per_pool"]), None)
        solo_pool = pools.get(solo_pid) if solo_pid else None
        if solo_pool and solo_pool.name and solo_pool.name != label:
            label_display = f"{label}「{solo_pool.name}」"
        else:
            label_display = label
        return (
            f"{label_display}  独立保底 {info['current_pity']}/{info['hard_pity']}"
            f"  距保底{info['distance_to_pity']}抽  │  {info['total']}抽 6星×{info['six_count']}"
        )

    def _build_section(self, info: dict, pools: Dict[str, Pool],
                       rates: Optional[dict] = None,
                       manual_ts_bound: Optional[int] = None) -> CollapsibleSection:
        """构建单个保底分组的可折叠区块（按卡池视图）。rates 为该组 non_warp_rates 输出。"""
        label = info["label"]

        # 一键展收：除常驻/新手外，其余类型分组都显示按钮（后续新增其他类型池自动带上）
        show_bulk = not any(k in label for k in ("常驻", "新手"))
        section = CollapsibleSection(
            self._section_title(info, pools, rates),
            expanded=(label == "UP池"), show_bulk_toggle=show_bulk
        )
        # 组内池按 pool_id 号倒序 -> 号大（新卡池）在前，#1 = 最新
        pids = sorted(
            info["per_pool"].keys(),
            key=_pool_sort_key,
        )
        # 默认只展开最新有命中的池，其余折叠；用户手动折叠过的池用会话内记忆值
        default_expanded_pid = next(
            (pid for pid in pids if info["per_pool"][pid]["six_count"] > 0), None
        )
        for i, pid in enumerate(pids, start=1):
            pool = pools.get(pid, Pool.default(pid))
            pinfo = info["per_pool"][pid]
            pr = rates["per_pool"].get(pid) if rates else None
            section.add_row(PoolRow(
                index=i,
                pool=pool,
                total=pinfo["total"],
                six_count=pinfo["six_count"],
                wins=pr["wins"] if pr else 0,
                all_up=pr["all_up"] if pr else 0,
                hits=pinfo["hits"],
                collapsed=self._pool_collapsed.get(pid, pid != default_expanded_pid),
                manual_ts_bound=manual_ts_bound,
                on_toggle=self._on_pool_toggled,
            ))
        return section

    def _build_section_flow(self, info: dict, pools: Dict[str, Pool],
                            rates: Optional[dict] = None,
                            manual_ts_bound: Optional[int] = None) -> CollapsibleSection:
        """构建「按角色」流水视图分组区块：组内每条 6 星出货一行，不聚合。

        行格式 [UP]/[歪] 第N抽 角色名 池名 时间（时间新->旧；2026-10-10 去必出标注）；
        手动补齐「手」标记不进入流水视图（D10，manual_ts_bound 仅保持
        与 _build_section 同签名，不使用）。
        """
        section = CollapsibleSection(
            self._section_title(info, pools, rates),
            expanded=(info["label"] == "UP池"),
            show_bulk_toggle=False,
        )
        hits = info["hits"]
        if not hits:
            hint = QLabel("未出六星")
            hint.setStyleSheet("color: #aaa;")
            hint.setContentsMargins(_FOLD_INDENT, 0, 0, 0)
            section.add_row(hint)
            return section
        # hits 为时间旧->新；展示新->旧：ts 降序为主，同 ts（同一十连）按列表序倒排
        for _, h in sorted(
            enumerate(hits),
            key=lambda x: (x[1].get("ts", 0), x[0]),
            reverse=True,
        ):
            pool = pools.get(h["pool_id"], Pool.default(h["pool_id"]))
            section.add_row(FlowHitRow(
                pity=h["pity"],
                ts=h.get("ts", 0),
                name=h["name"],
                pool_name=h["pool_name"],
                is_up=h["is_up"],
                featured=pool.is_featured,
            ))
        return section

    def _on_pool_toggled(self, pid: str, collapsed: bool) -> None:
        """记录某池手动折叠状态，跨刷新保留。"""
        self._pool_collapsed[pid] = collapsed
