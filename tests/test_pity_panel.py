"""保底主面板 PityPanel 渲染测试（需 PySide6）。

PySide6 未安装时整个文件 skip（与 server 测试对 flask 的处理一致）。
覆盖：分组顺序（UP->遴选缺省->常驻->新手）、UP 池倒序、未出六星标记、池内命中
最近在上、[UP]/[歪] 着色、非 featured 池无标记、独立组头带池名、顶部摘要。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.gui.pity_panel import (
    _col_w,
    CollapsibleSection,
    FlowHitRow,
    HitRow,
    PityPanel,
    PoolRow,
)
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool

app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_view_setting(monkeypatch):
    """隔离视图偏好读写：面板测试一律从「按卡池」起跑，save 只记录不落盘。

    返回的列表记录 save_pity_panel_view 的调用参数，供持久化测试断言。
    """
    import gacha_exporter.gui.pity_panel as pp

    saved = []
    monkeypatch.setattr(pp, "load_pity_panel_view", lambda: "pool")
    monkeypatch.setattr(pp, "save_pity_panel_view", saved.append)
    yield saved

CHARS = {
    "u6_hit": Character("u6_hit", "命中角色", 6, limited=True),
    "u6_miss": Character("u6_miss", "歪角色", 6, limited=False),
    "u6_c": Character("u6_c", "池4六星", 6, limited=True),
    "u4": Character("u4", "四星垫", 4),
    "perm6": Character("perm6", "常驻六星", 6, limited=False),
}

POOLS = {
    "30001": Pool("30001", "UP池1", "UP", order=1, rarity=6, up_character="命中角色", pity_group="shared:UP"),
    "30002": Pool("30002", "UP池2", "UP", order=2, rarity=6, up_character="命中角色", pity_group="shared:UP"),
    "30003": Pool("30003", "UP池3", "UP", order=3, rarity=6, up_character="命中角色", pity_group="shared:UP"),
    "30004": Pool("30004", "UP池4", "UP", order=4, rarity=6, up_character="池4六星", pity_group="shared:UP"),
    "20001": Pool("20001", "示例卡池007", "常驻", pity_group="solo:20001"),
    "10001": Pool("10001", "新手池", "新手", pity_group="solo:10001"),
}

GROUPS = {
    "shared:UP": PityGroup("shared:UP", "UP池", 70, True),
    "solo:20001": PityGroup("solo:20001", "常驻池", 70, False),
    "solo:10001": PityGroup("solo:10001", "新手池", 30, False),
}

# timestamp 升序：30001歪 -> 30002命中 -> 30004两个六星(测最近在上) -> 常驻六星(非featured) -> 新手无六星
RECORDS = [
    GachaRecord("30001", "u4", 1000),
    GachaRecord("30001", "u6_miss", 2000),   # 歪
    GachaRecord("30002", "u6_hit", 3000),    # 命中UP
    GachaRecord("30004", "u4", 4000),
    GachaRecord("30003", "u4", 4500),         # 30003 有记录但无六星
    GachaRecord("30004", "u6_c", 5000),       # 30004 第1个六星
    GachaRecord("30004", "u6_c", 6000),       # 30004 第2个六星（更新）
    GachaRecord("20001", "perm6", 7000),      # 常驻池六星（非 featured）
    GachaRecord("10001", "u4", 8000),          # 新手池无六星
]


def _build_panel():
    stats = StatsEngine(CHARS, POOLS, GROUPS, set())
    stats.load_records(list(RECORDS))
    panel = PityPanel()
    panel.update_data(stats, CHARS, POOLS, "测试账号", "2026-08-12T22:44")
    return panel, stats


def _sections(panel):
    layout = panel._container_layout
    return [layout.itemAt(i).widget() for i in range(layout.count())
            if isinstance(layout.itemAt(i).widget(), CollapsibleSection)]


def _pool_rows(section):
    lay = section.body_layout
    return [lay.itemAt(i).widget() for i in range(lay.count())
            if isinstance(lay.itemAt(i).widget(), PoolRow)]


def _hit_rows(pool_row):
    """命中明细行：从 PoolRow 的 body 容器里找（头部行之外）。"""
    lay = pool_row.body_layout
    return [lay.itemAt(i).widget() for i in range(lay.count())
            if isinstance(lay.itemAt(i).widget(), HitRow)]


def test_group_order_up_then_perm_then_newbie():
    """分组顺序：UP -> 常驻 -> 新手（新手在最下）；UP 默认展开(▼)，其余折叠(▶)。"""
    panel, _ = _build_panel()
    titles = [s.header.text() for s in _sections(panel)]
    assert len(titles) == 3
    assert titles[0].startswith("▼ UP池")
    assert titles[1].startswith("▶ 常驻池")
    assert titles[2].startswith("▶ 新手池")


def test_up_pools_newest_first_order():
    """UP 池组内按 pool_id 号倒序，号最大（最新）卡池在最前（#1 = 最新）。"""

    panel, _ = _build_panel()
    up = _sections(panel)[0]
    assert [r.pool_id for r in _pool_rows(up)] == ["30004", "30003", "30002", "30001"]


def test_unshipped_marker_only_when_no_six():
    """未出六星标记仅在该池 six_count==0 时出现。"""
    panel, _ = _build_panel()
    rows = {r.pool_id: r for r in _pool_rows(_sections(panel)[0])}
    assert rows["30003"].unshipped_label is not None
    assert rows["30003"].unshipped_label.text() == "未出六星"
    for pid in ("30001", "30002", "30004"):
        assert rows[pid].unshipped_label is None


def test_hits_newest_first_within_pool():
    """池内 6 星命中按 ts 倒序，最近一次在上。"""
    panel, _ = _build_panel()
    rows = {r.pool_id: r for r in _pool_rows(_sections(panel)[0])}
    hits = _hit_rows(rows["30004"])
    assert len(hits) == 2
    assert hits[0].ts > hits[1].ts


def test_same_tenpull_two_six_newest_on_top():
    """同一十连内两个 6 星：后出的（新）显示在上面。

    复现用户反馈：常驻池同一十连先出提线人(pity大)、后出东南西北(pity小)，
    两者 ts 相同，原 ts 降序稳定排序把先出的排上面了；修复后同 ts 内
    按时间新->旧展示（后出的在上）。
    """
    chars = dict(CHARS)
    chars["t6a"] = Character("t6a", "提线人", 6)
    chars["t6b"] = Character("t6b", "东南西北", 6)
    stats = StatsEngine(chars, POOLS, GROUPS, set())
    ts = 5000
    recs = []
    # 同 ts 十连拆两帧，游戏新->旧下发 -> 旧半帧 batch_seq 大
    # 新半帧 bseq=0：pos0=东南西北(最新一抽)
    recs.append(GachaRecord("20001", "t6b", ts, batch_seq=0, position_in_batch=0))
    for p in range(1, 5):
        recs.append(GachaRecord("20001", "u4", ts, batch_seq=0, position_in_batch=p))
    # 旧半帧 bseq=1：pos3=提线人(先出)
    for p in range(0, 3):
        recs.append(GachaRecord("20001", "u4", ts, batch_seq=1, position_in_batch=p))
    recs.append(GachaRecord("20001", "t6a", ts, batch_seq=1, position_in_batch=3))
    recs.append(GachaRecord("20001", "u4", ts, batch_seq=1, position_in_batch=4))
    stats.load_records(recs)

    panel = PityPanel()
    panel.update_data(stats, chars, POOLS, "测试账号", "")
    sections = _sections(panel)
    assert len(sections) == 1  # 只有常驻池一组
    hits = _hit_rows(_pool_rows(sections[0])[0])
    assert len(hits) == 2
    # 上面：东南西北(后出，十连内第10抽 pity=8)；下面：提线人(先出，pity=2)
    assert hits[0].pity == 8
    assert hits[1].pity == 2


def test_featured_tag_present_and_colored():
    """featured 池命中行带 [UP·必出]/[歪] 标记并着色（绿/红）。

    30002 的命中前置是 30001 的歪（共享组跨池继承大保底）→ 必出，标 [UP·必出]。
    """
    panel, _ = _build_panel()
    rows = {r.pool_id: r for r in _pool_rows(_sections(panel)[0])}
    up_hit = _hit_rows(rows["30002"])[0]
    assert up_hit.tag_label is not None
    assert up_hit.tag_label.text() == "[UP·必出]"
    assert "#2e8b57" in up_hit.tag_label.styleSheet()  # 绿
    miss_hit = _hit_rows(rows["30001"])[0]
    assert miss_hit.tag_label.text() == "[歪]"
    assert "#c0392b" in miss_hit.tag_label.styleSheet()  # 红


def test_non_featured_pool_has_no_tag():
    """常驻池（非 featured）6 星命中行不带 [歪]/[UP] 标记。"""
    panel, _ = _build_panel()
    perm = _sections(panel)[1]  # 常驻池
    rows = _pool_rows(perm)
    assert len(rows) == 1
    hits = _hit_rows(rows[0])
    assert len(hits) == 1
    assert hits[0].tag_label is None
    assert hits[0].featured is False


def test_solo_header_includes_pool_name_when_different():
    """独立组池名与组标签不同时，池名带进组头（常驻池「示例卡池007」）。"""
    panel, _ = _build_panel()
    title = _sections(panel)[1].header.text()
    assert "「示例卡池007」" in title
    assert "独立保底" in title


def test_summary_and_account_header():
    """顶部摘要与账号/最近更新时间。"""
    panel, stats = _build_panel()
    summary = panel._summary_label.text()
    assert "总抽数 9" in summary
    assert "6星 5" in summary           # UP4 + 常驻1
    assert "出货池 4/6" in summary      # 30001/30002/30004/20001 出货
    acct = panel._account_label.text()
    upd = panel._update_label.text()
    assert "账号: 测试账号" in acct
    assert "最近更新: 2026-08-12 22:44" in upd


def test_summary_has_no_page_count():
    """「页数」提示已从摘要移除（无新增信息），标注不应再出现「共计…页」。"""
    recs = [
        GachaRecord("30001", "u4", 1000, batch_seq=0, position_in_batch=0),
        GachaRecord("30001", "u4", 2000, batch_seq=0, position_in_batch=1),
        GachaRecord("30001", "u6_hit", 3000, batch_seq=1, position_in_batch=0),
        GachaRecord("30002", "u4", 4000, batch_seq=1, position_in_batch=0),
    ]
    stats = StatsEngine(CHARS, POOLS, GROUPS, set())
    stats.load_records(recs)
    panel = PityPanel()
    panel.update_data(stats, CHARS, POOLS, "测试账号")
    assert "页" not in panel._summary_label.text()


def test_non_warp_rate_texts():
    """不歪率口径:小保底命中UP / 全部UP出货(含大保底必出),UP 出现在哪个池算哪个池。

    既有数据:30001歪(小保底) -> 30002必出UP(继承大保底) -> 30004胜 -> 30004胜。
    组=2/3≈66.7%;30002 唯一 UP 是必出 -> 0.0%;30004 全胜 -> 100%;
    30001 全歪无 UP 出货 -> 无数据,显示「-」。
    """
    panel, _ = _build_panel()
    summary = panel._summary_label.text()
    assert "不歪率 66.7%" in summary
    up = _sections(panel)[0]
    assert "不歪率 66.7%" in up.header.text()
    rows = {r.pool_id: r for r in _pool_rows(up)}
    assert rows["30001"].up_rate_label.text() == "不歪率 -"
    assert rows["30002"].up_rate_label.text() == "不歪率 0.0%"
    assert rows["30004"].up_rate_label.text() == "不歪率 100.0%"


def test_default_only_newest_pool_with_hits_expanded():
    """默认只展开最新有命中的池：30004 展开(▾)，旧池 30002/30001 折叠(▸)，
    未出六星的 30003 箭头留空占位（名称列对齐）。"""
    panel, _ = _build_panel()
    up = _sections(panel)[0]
    rows = {r.pool_id: r for r in _pool_rows(up)}
    assert rows["30004"]._expanded
    assert rows["30004"].arrow_label.text() == "▾"
    assert rows["30002"]._expanded is False
    assert rows["30002"].arrow_label.text() == "▸"
    assert rows["30001"]._expanded is False
    assert rows["30001"].arrow_label.text() == "▸"
    assert rows["30003"].arrow_label.text() == ""  # 未出六星，箭头空占位


def test_bulk_toggle_button_present_for_shared_groups():
    """一键展收按钮：UP 分组有；常驻/新手分组没有。"""
    panel, _ = _build_panel()
    panel.show()  # isVisible 需整条父链可见
    sections = _sections(panel)
    assert sections[0]._bulk_btn is not None
    assert sections[0]._bulk_btn.isVisible()
    assert sections[1]._bulk_btn is None  # 常驻
    assert sections[2]._bulk_btn is None  # 新手
    assert sections[0]._bulk_btn.text() == "展开"  # 初始未全展开


def test_bulk_toggle_expand_all_then_collapse_keep_first():
    """一键展收：点一次全部展开；再点收起只留第一个（最新）有命中的池。"""
    panel, _ = _build_panel()
    up = _sections(panel)[0]
    btn = up._bulk_btn
    rows = {r.pool_id: r for r in _pool_rows(up)}
    # 初始：默认只展开 30004（pid 最大=最新且有命中）
    assert rows["30004"]._expanded
    assert not rows["30002"]._expanded
    assert not rows["30001"]._expanded
    assert not rows["30003"]._expanded  # 无命中池恒折叠
    # 一键展开
    btn.click()
    assert rows["30004"]._expanded
    assert rows["30002"]._expanded
    assert rows["30001"]._expanded
    assert not rows["30003"]._expanded
    assert btn.text() == "收起"
    # 一键收起 -> 只留 30004
    btn.click()
    assert rows["30004"]._expanded
    assert not rows["30002"]._expanded
    assert not rows["30001"]._expanded
    assert not rows["30003"]._expanded
    assert btn.text() == "展开"


def test_pool_toggle_collapses_and_restores():
    """toggle() 折叠/展开：_expanded 翻转、箭头 ▾/▸ 切换，body 内容仍在。"""
    panel, _ = _build_panel()
    up = _sections(panel)[0]
    rows = {r.pool_id: r for r in _pool_rows(up)}
    row = rows["30004"]
    assert row._expanded and row.arrow_label.text() == "▾"
    row.toggle()
    assert not row._expanded
    assert row.arrow_label.text() == "▸"
    assert len(_hit_rows(row)) == 2  # 折叠只隐藏，不丢命中内容
    row.toggle()
    assert row._expanded
    assert row.arrow_label.text() == "▾"


def test_pool_collapse_state_preserved_across_refresh():
    """会话内记忆：折叠状态跨 update_data 重建保持。"""
    panel, stats = _build_panel()
    up = _sections(panel)[0]
    rows = {r.pool_id: r for r in _pool_rows(up)}
    rows["30004"].toggle()  # 手动折叠最新池
    panel.update_data(stats, CHARS, POOLS, "测试账号", "2026-08-12T22:44")
    up2 = _sections(panel)[0]
    rows2 = {r.pool_id: r for r in _pool_rows(up2)}
    assert not rows2["30004"]._expanded  # 记忆保持折叠
    assert rows2["30004"].arrow_label.text() == "▸"
    # 未手动动过的池仍走默认规则：旧池折叠、未出六星箭头空占位
    assert not rows2["30002"]._expanded
    assert rows2["30003"].arrow_label.text() == ""


def test_pool_without_hits_has_no_toggle():
    """未出六星池（30003）箭头留空占位、不可折叠、无命中 body。"""
    panel, _ = _build_panel()
    up = _sections(panel)[0]
    rows = {r.pool_id: r for r in _pool_rows(up)}
    row = rows["30003"]
    assert row.arrow_label.text() == ""
    assert not row._has_hits
    # 头部非可点击容器（普通 QFrame）
    header = row.layout().itemAt(0).widget()
    assert header.__class__.__name__ == "QFrame"
    assert header.__class__.__name__ != "_ClickableFrame"


# ---------- 「按角色」流水视图（2026-10-08 定稿，见 展示设计说明 v0.2） ----------


def _flow_rows(section):
    """流水视图出货行：从分组 body 里找 FlowHitRow。"""
    lay = section.body_layout
    return [lay.itemAt(i).widget() for i in range(lay.count())
            if isinstance(lay.itemAt(i).widget(), FlowHitRow)]


def _body_labels(section):
    lay = section.body_layout
    return [lay.itemAt(i).widget() for i in range(lay.count())
            if isinstance(lay.itemAt(i).widget(), QLabel)]


def test_view_toggle_default_and_switch_to_flow():
    """默认按卡池；点「按角色」切到流水视图：分组结构不变、组内平铺出货行。"""
    panel, _ = _build_panel()
    panel.show()
    assert panel._btn_pool_view.isChecked()
    panel._btn_flow_view.click()
    assert panel._view_mode == "flow"
    sections = _sections(panel)
    assert len(sections) == 3  # UP / 常驻 / 新手 分组结构照旧
    up = sections[0]
    assert "UP池" in up.header.text()
    # UP 组 4 条六星平铺一行一条（无池行、无折叠层级），时间新->旧
    rows = _flow_rows(up)
    assert [r.ts for r in rows] == [6000, 5000, 3000, 2000]
    assert all(r.pool_name for r in rows)


def test_flow_view_tag_order_and_guaranteed():
    """流水行标签在最前：必出 UP=[UP·必出]、50/50 胜=[UP]、歪=[歪]；常驻无标签。

    时间序：2000 池1歪 -> 3000 池2命中（前置歪在 30001，大保底必出）
    -> 5000/6000 池4命中（50/50 胜）。展示倒序。
    """
    panel, _ = _build_panel()
    panel._btn_flow_view.click()
    rows = _flow_rows(_sections(panel)[0])
    assert rows[0].tag_label.text() == "[UP]"
    assert rows[1].tag_label.text() == "[UP]"
    assert rows[2].tag_label.text() == "[UP·必出]"
    assert "#2e8b57" in rows[2].tag_label.styleSheet()
    assert rows[3].tag_label.text() == "[歪]"
    assert "#c0392b" in rows[3].tag_label.styleSheet()
    # 常驻组（非 featured）流水行无标签
    perm_rows = _flow_rows(_sections(panel)[1])
    assert len(perm_rows) == 1
    assert perm_rows[0].name == "常驻六星"
    assert perm_rows[0].tag_label is None


def test_flow_view_row_format_fields():
    """流水行字段齐备：第N抽 + 角色名 + 池名 + 时间（无「手」标记字段）。"""
    panel, _ = _build_panel()
    panel._btn_flow_view.click()
    rows = _flow_rows(_sections(panel)[0])
    r = rows[3]  # 2000 池1歪（pity=2：前置 4 星垫 1 抽 + 本抽）
    assert r.pity == 2
    assert r.name == "歪角色"
    assert r.pool_name == "UP池1"
    assert not hasattr(r, "manual_label")  # D10：流水视图无「手」标记


def test_flow_view_empty_group_shows_hint():
    """无六星分组（新手池）流水视图显示灰色「未出六星」提示。"""
    panel, _ = _build_panel()
    panel._btn_flow_view.click()
    newbie = _sections(panel)[2]
    assert _flow_rows(newbie) == []
    assert any(w.text() == "未出六星" for w in _body_labels(newbie))


def test_flow_view_no_manual_marker_even_with_bound():
    """传入手动补齐边界时，按卡池视图标「手」的命中在流水视图一律不标。"""
    panel, stats = _build_panel()
    panel._btn_flow_view.click()
    panel.update_data(stats, CHARS, POOLS, "测试账号", "",
                      manual_bound_by_group={"shared:UP": 2500})
    rows = _flow_rows(_sections(panel)[0])
    assert len(rows) == 4
    assert all(not hasattr(r, "manual_label") for r in rows)


def test_view_toggle_switch_back_to_pool():
    """切到流水再切回按卡池：恢复 PoolRow 结构，与初始一致。"""
    panel, _ = _build_panel()
    panel.show()
    panel._btn_flow_view.click()
    panel._btn_pool_view.click()
    assert panel._view_mode == "pool"
    up = _sections(panel)[0]
    pool_rows = _pool_rows(up)
    assert [r.pool_id for r in pool_rows] == ["30004", "30003", "30002", "30001"]
    assert _flow_rows(up) == []


def test_view_change_persisted_to_settings(_isolate_view_setting):
    """切换视图即调 save_pity_panel_view（跨启动记忆），往返各记一次。"""
    panel, _ = _build_panel()
    panel.show()
    panel._btn_flow_view.click()
    panel._btn_pool_view.click()
    assert _isolate_view_setting == ["flow", "pool"]


def test_view_mode_restored_from_settings(monkeypatch):
    """启动时从设置恢复：load 返回 flow → 初始即流水视图、按钮选中态一致。"""
    import gacha_exporter.gui.pity_panel as pp
    monkeypatch.setattr(pp, "load_pity_panel_view", lambda: "flow")
    stats = StatsEngine(CHARS, POOLS, GROUPS, set())
    stats.load_records(list(RECORDS))
    panel = PityPanel()
    assert panel._view_mode == "flow"
    assert panel._btn_flow_view.isChecked()
    assert not panel._btn_pool_view.isChecked()
    panel.update_data(stats, CHARS, POOLS, "测试账号", "")
    assert _flow_rows(_sections(panel)[0])  # 初始渲染即流水


def test_flow_view_columns_fixed_width():
    """流水行各列固定宽度（标签/第N抽/角色名15字/池名10字），跨行垂直对齐。

    非 featured 池无标签但标签列留空占位（同宽），后续列不左移。
    """
    panel, _ = _build_panel()
    panel.show()
    panel._btn_flow_view.click()
    rows = _flow_rows(_sections(panel)[0]) + _flow_rows(_sections(panel)[1])
    assert len(rows) == 5
    for r in rows:
        widgets = []
        lay = r.layout()
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                widgets.append(w)
        tag, pity_lbl, name_lbl, pool_lbl = widgets[0], widgets[1], widgets[2], widgets[3]
        # setFixedWidth = min/max 同值；与 _col_w 样文本计算一致
        assert tag.minimumWidth() == tag.maximumWidth() == _col_w("[UP·必出]")
        assert pity_lbl.minimumWidth() == _col_w("第00抽")
        assert name_lbl.minimumWidth() == _col_w("字" * 15)
        assert pool_lbl.minimumWidth() == _col_w("字" * 10)
        # 非 featured 行（常驻）tag_label 为 None，但占位 QLabel 同宽存在
        if not r.featured:
            assert r.tag_label is None
            assert tag.text() == ""
