"""位置对齐去重（pos_align）单测 —— 对应设计稿 `docs/去重归档/去重方案-位置对齐.md`。

核心断言：**位置即身份**——同名记录 pos 不同，因此位置去重天然不会吞同名第二份，
也不会把跨会话重抓的重复计入。Δ（位置平移量）来自**权威 total 增量**（设计稿 §4.3），
不使用内容比对（十连同名使内容比对不可靠，见设计稿反例）。
"""
from gacha_exporter.analysis.pos_align import (
    VALID_EVENT_SIZES,
    group_by_ts,
    infer_shift_from_total,
    merge_by_position,
)
from gacha_exporter.models import GachaRecord

POOL = "30005"
TS = 1789000000000


def R(item: str, pos: int, ts: int = TS, pool: str = POOL) -> GachaRecord:
    """构造一条记录（batch_seq 恒 0，与 2026-08-18 后的真实数据一致）。"""
    return GachaRecord(pool, item, ts, 0, pos)


def ten(items, base: int, ts: int = TS):
    """按顺序构造一批记录，位置从 base 起连续递增。"""
    return [R(it, base + i, ts) for i, it in enumerate(items)]


# 十连（含同名：位置 0 与位置 5 都是 A）—— 同名第二份是历史三大缺陷的主角
ITEMS = ["A", "B", "C", "D", "E", "A", "F", "G", "H", "I"]


# ---------- Δ 来源：权威 total 增量 ----------

def test_infer_shift_from_total_growth():
    """Δ = 本次 total − 上次 total（列表头部插入的抽卡数）。"""
    assert infer_shift_from_total(551, 521) == 30
    assert infer_shift_from_total(521, 521) == 0


def test_infer_shift_from_total_handles_first_capture_and_reset():
    """缺任一端的 total（首次抓包 / 清空还原）→ 0（无历史可合并，Δ 不影响）。"""
    assert infer_shift_from_total(521, None) == 0
    assert infer_shift_from_total(None, 521) == 0


def test_infer_shift_from_total_never_negative():
    """total 变少（清空/还原后又抓到更少）→ 不返回负值，按 0 处理。"""
    assert infer_shift_from_total(100, 521) == 0


# ---------- 单次抓包：全收（不做事件内去重） ----------

def test_single_session_keeps_same_name_copies():
    """单次抓包内不做任何事件内去重：十连内同名两条必须都在。"""
    recs = ten(ITEMS, 100)
    report = merge_by_position([], recs, shift=0)
    assert len(report.added) == 10
    assert [r.item_id for r in report.added].count("A") == 2
    assert report.suspicious == []
    assert report.new_events == 1 and report.merged_events == 0


# ---------- 跨会话：位置去重 ----------

def test_merge_with_overlap_dedups_by_position():
    """有重叠（Δ=0）：已见过的位置跳过，新位置追加；同名第二份不受影响。"""
    hist = ten(ITEMS[:6], 100)          # pos 100~105（含位置 5 的 A）
    new = ten(ITEMS[4:], 104)           # pos 104~109（重叠 104/105）
    report = merge_by_position(hist, new, shift=0)
    assert report.skipped == 2 and len(report.added) == 4
    merged = hist + report.added
    assert len(merged) == 10
    assert [r.item_id for r in sorted(merged, key=lambda r: r.position_in_batch)] == ITEMS
    assert report.suspicious == []


def test_merge_complementary_halves_not_swallowed():
    """**关键**：两段互补（无重叠、未平移）→ 全收 10 条，同名第二份不得被吞。

    这正是「内容匹配推断 Δ」会误判形状：A 在两侧都出现（位置 0 与位置 5），
    内容比对会得出 Δ=5，把新的一整段判成重复 → 只剩 5 条。位置去重不会。
    """
    hist = ten(ITEMS[:5], 100)          # pos 100~104（位置 0~4）
    new = ten(ITEMS[5:], 105)           # pos 105~109（位置 5~9）
    report = merge_by_position(hist, new, shift=0)
    assert len(report.added) == 5 and report.skipped == 0
    merged = hist + report.added
    assert len(merged) == 10
    assert [r.item_id for r in sorted(merged, key=lambda r: r.position_in_batch)] == ITEMS


def test_merge_after_draw_uses_total_delta():
    """抽卡后重抓：列表头部插入 30 条 → 同一条记录 pos 整体 +30 → 用 Δ 归一化。"""
    hist = ten(ITEMS[:5], 100)                       # 上次：位置 0~4 在列表位置 100~104
    shift = infer_shift_from_total(551, 521)         # 本次 total 多了 30
    new = ten(ITEMS[3:], 103 + shift)                # 本次：位置 3~9 → 列表位置 133~139
    report = merge_by_position(hist, new, shift=shift)
    assert report.shift == 30
    # 位置 3、4 已在历史里（100+3 / 100+4）→ 只加 5 条
    assert report.skipped == 2 and len(report.added) == 5
    merged = hist + report.added
    assert len(merged) == 10
    assert report.suspicious == []


def test_merge_identical_reobservation_is_fully_skipped():
    """整段重抓（同位置同内容）→ 全部跳过，不重复计入。

    事件级判重后：历史十连完整（size=10）→ 整事件跳过（event_skipped），
    与逐槽跳过（skipped）结果等价：0 新增。
    """
    hist = ten(ITEMS, 100)
    new = ten(ITEMS, 100)
    report = merge_by_position(hist, new, shift=0)
    assert report.added == []
    assert report.event_skipped == 1 and report.skipped == 0
    assert report.suspicious == []


def test_merge_new_events_are_appended():
    """历史里没有的事件（新抽卡）→ 直接收下，不受 Δ 影响。"""
    hist = ten(ITEMS, 100)
    fresh = ten(["N1", "N2", "N3", "N4", "N5"], 95, ts=TS + 1)
    report = merge_by_position(hist, fresh, shift=0)
    assert len(report.added) == 5 and report.new_events == 1
    assert report.merged_events == 0


def test_merge_single_pull_event():
    """单抽事件（size=1）跨会话判重。

    事件级判重（旧格式历史兼容）后的语义：**ts 即事件身份**（A2：一个 ts =
    一次抽卡动作）——同 ts 的重抓记录无论坐标是否对得上都是同一次抽卡。
    旧断言「同 ts 位置不同 = 新的一份」是跨视图时代的产物（同一记录在两个
    视图各传一份、坐标系不同），B8 后不存在，且正是 sample 多算的机制，
    故随本设计稿收紧（docs/去重归档/去重方案-旧历史兼容-事件级判重.md §3.3）。
    """
    hist = [R("S1", 50, ts=TS + 2)]
    same = [R("S1", 50, ts=TS + 2)]
    rep = merge_by_position(hist, same, shift=0)     # 坐标命中 → 位置判重跳过
    assert rep.total_added == 0 and rep.skipped == 1
    other = [R("S1", 60, ts=TS + 2)]                 # 同 ts、坐标不命中 → 同一次抽卡
    rep2 = merge_by_position(hist, other, shift=0)   # 零命中 → 事件级跳过
    assert rep2.total_added == 0 and rep2.event_skipped == 1


# ---------- 异常与安全阀 ----------

def test_suspicious_events_reported_not_silent():
    """合并后事件大小 ∉ {1,10} → 必须报告（绝不静默）。"""
    hist = ten(ITEMS[:6], 100)
    new = ten(ITEMS[6:], 106)          # 只补 4 条 → 合并 10 条（合法）
    assert merge_by_position(hist, new, shift=0).suspicious == []

    new_partial = ten(ITEMS[6:8], 106)  # 只补 2 条 → 合并 8 条（非法）
    rep = merge_by_position(hist, new_partial, shift=0)
    assert rep.suspicious == [(TS, POOL, 8)]


def test_history_with_broken_positions_falls_back_to_append():
    """历史 pos 断裂（B8 前混视图留下的脏数据）→ 该事件退化为全追加并登记。

    宁可重复，绝不用不可比的坐标系去判重（否则可能漏记）。
    """
    hist = [R("A", 100), R("B", 130)]           # 位置断裂：100 与 130
    new = [R("A", 100), R("B", 101)]
    report = merge_by_position(hist, new, shift=0)
    assert report.fallback_events == [TS]
    assert len(report.added) == 2 and report.skipped == 0
    assert report.suspicious                  # 合并后 4 条 ∉ {1,10} → 报警


def test_valid_event_sizes_exported():
    """合法事件大小常量必须与账本保持一致（1 单抽 / 10 十连）。"""
    assert VALID_EVENT_SIZES == (1, 10)


def test_group_by_ts_splits_events():
    """按 timestamp 分组（事件 = 一次抽卡动作）。"""
    recs = ten(["x"], 1, ts=1) + ten(["y"], 1, ts=2)
    groups = group_by_ts(recs)
    assert set(groups) == {1, 2} and len(groups[1]) == 1
