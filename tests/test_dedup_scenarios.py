"""抽卡去重 —— 全场景回归测试（S1~S12）。

把 `docs/去重归档/去重架构重构设计.md` §11「场景清单」里的每一个失效场景固化成
可执行测试。每条用例名 `test_sNN_*` 与 docstring 都标注场景编号，
用于在后续任何去重改造中防止这些场景回归。

设计口径（见 `docs/去重归档/去重架构重构设计.md`）：
- 抽卡事件身份 = timestamp（账号内唯一）；一次十连 = 10 条共享同一 ts；
  「事件大小必须 ∈ {1,10}」是免费可执行的外部不变量（§1.8）。
- 本文件按「先破后立」编写：**目标语义**为准，尚复现缺陷的用例当前为红，
  即修复的验收标准；修好后应转绿。

用法：
    pytest tests/test_dedup_scenarios.py -v

场景 → 用例对照：
    S1  十连丢抽（前 5 已录、后 5 补录含同角色被吞）  test_s01_* / test_s01b_*
    S2  卡池视图重复（三汇总页 pool_id 撞车）        test_s02_*
    S3  S1+S2 互相掩盖（不变量同时标出漏/重）        test_s03_*
    S4  同一记录出现在 3 个视图                      test_s04_*
    S5  页码整体偏移                                  test_s05_*
    S6  十连内相同角色 id 保份数                      test_s06_*
    S7  十连不完整、各视图覆盖不同（嵌套前缀）        test_s07_*
    S8  只抓到前 5（不变量检出）                      test_s08_*
    S9  部分重叠 + 逆序到达（真重复不得被吞）        test_s09_*
    S10 跨会话接缝歧义（信息论极限，不静默）          test_s10_*
    S11 反向读取无害（到达方向不影响结果）            test_s11_*
    S12 跳页间隙（中间缺口可见 + 可补齐）             test_s12_*
    S15 未来新增卡池视图(pool_id 相同)仍只计一次      test_s15_*
    S16 跨视图拆分致 L1 误删时，不变量必须可见        test_s16_*
"""
import logging
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from gacha_exporter.analysis import pool_view
from gacha_exporter.analysis.pool_view import (
    PoolViewTracker,
    make_view,
    parse_pool_list_type,
)
from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.analysis.pos_align import merge_by_position
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool
from gacha_exporter.storage.history import HistoryService

# 一次十连的 10 抽共享同一 timestamp（设计 §1.8 实测：事件大小 1/10 双峰）
TEN_PULL_TS = 1786859992584
OTHER_TS = 1784345661004


# ---------- 公共构造与不变量 ----------


def rec(pool_id: str, item_id: str, ts: int, batch_seq: int = 0,
        position_in_batch: int = 0) -> GachaRecord:
    """构造一条抽卡记录（batch_seq/position_in_batch 仅为解析辅助，不参与去重）。"""
    return GachaRecord(pool_id, item_id, ts, batch_seq, position_in_batch)


def make_engine(shielded=None) -> StatsEngine:
    """最小统计引擎（与 tests/test_stats.py 同款，保证语义一致）。"""
    characters = {
        "101": Character(id="101", name="诺雅", rarity=6, limited=True),
        "102": Character(id="102", name="里奥", rarity=5, limited=False),
        "103": Character(id="103", name="艾米", rarity=4, limited=False),
        "104": Character(id="104", name="歪角", rarity=6, limited=False),
    }
    pools = {
        "30001": Pool(id="30001", name="示例卡池002", type="UP", order=1,
                      up_character="诺雅", pity_group="shared:UP"),
        "30003": Pool(id="30003", name="常驻池", type="常驻", order=0),
    }
    pity_groups = {
        "shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True),
    }
    return StatsEngine(characters, pools, pity_groups, shielded)


def event_size_violations(records) -> list:
    """返回违反「抽卡事件大小 ∈ {1,10}」不变量的事件 [((pool,ts), size), ...]。

    不变量来源：设计 §1.8 —— 单抽事件恰 1 条、十连事件恰 10 条；任何 5/9/2
    之类的大小都对应一个已发生的缺陷（<10=漏抓，>10 或非 1/10 的重复=多算）。
    这里以测试侧实现复刻该判据，用于验证「异常可检出」。
    """
    sizes = Counter((r.pool_id, r.timestamp) for r in records)
    return [(key, n) for key, n in sizes.items() if n not in (1, 10)]


def _varint(value: int) -> bytes:
    """编码 protobuf varint。"""
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _request(seq: int, pool_id: int, offset: int, list_type=None) -> bytes:
    """构造一条 C->S 切池/翻页请求帧体（不含长度头，与 test_pool_view 一致）。"""
    body = seq.to_bytes(4, "big") + b"\x00\x0c\x08\x00" + bytes([seq % 127])
    body += b"\x08" + _varint(pool_id) + b"\x10" + _varint(offset)
    if list_type is not None:
        body += b"\x18" + _varint(list_type)
    return body


def _hist(pid: str):
    """在临时目录内打开一个 HistoryService（不碰用户真实数据）。"""
    tmp = tempfile.TemporaryDirectory()
    patcher = patch("gacha_exporter.storage.history.get_users_dir",
                    return_value=Path(tmp.name, "users"))
    patcher.start()
    return HistoryService(pid), patcher, tmp


# ---------- S1 十连丢抽（记录层·过度过滤） ----------


def test_s01_stats_ten_pull_lost_when_latter_half_recaptured():
    """场景 S1：十连前 5 已录、后 5 补录时，后 5 里与前 5 同角色那条被吞。

    复现用户原话：「十连前 5 抽拿到了，后 5 抽没录制，后面录制的时候后 5 抽
    里面有数据和前 5 抽一样导致被过滤了」。

    当前状态：**红**（现状 9 条 → 目标 10 条）。
    """
    stats = make_engine()
    pool, ts = "20001", TEN_PULL_TS
    first_half = [rec(pool, f"1300{n:04d}", ts) for n in (1, 2, 3, 4, 5)]
    assert stats.add(first_half) == 5
    stats.begin_new_session()  # 「停止抓包」→ 前半落库、快照刷新

    # 后半第 1 条与前半第 5 条是同一角色（同 pool/item/ts）：真·十连内第二次抽到
    second_half = [rec(pool, f"1300{n:04d}", ts) for n in (5, 6, 7, 8, 9)]
    stats.add(second_half)
    # 目标：一次十连 10 条全在（同角色算两份）
    assert stats.total == 10


def test_s01b_history_ten_pull_lost_when_latter_half_recaptured():
    """场景 S1（HistoryService 同源）：merge 用「key 集合即跳过」，后半同 key 同样被吞。

    当前状态：**红**（现状 9 条 → 目标 10 条）。
    """
    svc, patcher, tmp = _hist("scn01")
    try:
        pool, ts = "20001", TEN_PULL_TS
        svc.merge([rec(pool, f"1300{n:04d}", ts) for n in (1, 2, 3, 4, 5)])
        svc.merge([rec(pool, f"1300{n:04d}", ts) for n in (5, 6, 7, 8, 9)])
        assert len(svc.records) == 10
    finally:
        patcher.stop()
        tmp.cleanup()


# ---------- S2 卡池视图重复（视图层·漏过滤，已修） ----------


def test_s02_view_identity_includes_list_type():
    """场景 S2：三个汇总页 pool_id 都是 0，必须靠 list_type 区分。

    否则「活动契约 / 遴选契约」汇总页与「全部卡池」撞成同一视图 → 跨视图去重
    失效、记录重复计入（commit 2293afd 修复）。

    当前状态：**绿**（已修）。
    """
    # 视图标识必须含 list_type：三者互不相同
    assert make_view(0, 0) == "0@0"
    assert make_view(0, 3) == "0@3"
    assert make_view(0, 4) == "0@4"
    assert len({make_view(0, 0), make_view(0, 3), make_view(0, 4)}) == 3
    assert make_view(30005, 0) == "30005@0"
    # 老协议无 field 3 → 回退纯 pool_id（行为不变）
    assert make_view(0, None) == "0"
    # 请求帧 field 3 解析
    assert parse_pool_list_type(_request(1, 0, 0, list_type=0)) == 0
    assert parse_pool_list_type(_request(1, 0, 5, list_type=3)) == 3
    assert parse_pool_list_type(_request(1, 0, 10, list_type=4)) == 4
    # tracker 按 list_type 区分当前视图
    tracker = PoolViewTracker()
    tracker.track_request(_request(1, 0, 0, list_type=0))
    tracker.track_request(_request(2, 0, 5, list_type=3))
    tracker.track_request(_request(3, 0, 10, list_type=4))
    assert tracker.current_view == "0@4"


# ---------- S3 S1+S2 互相掩盖（不变量同时标出漏/重） ----------


def test_s03_invariant_flags_both_directions():
    """场景 S3：视图层重复(+N) 会掩盖记录层丢失(−M)，总数看起来「正常」。

    「事件大小 ∈ {1,10}」不变量能同时标出两类异常（<10=漏、>1 的异形=重），
    使其不再互相掩盖。

    当前状态：**绿**（检出逻辑正确）。
    """
    lost = [rec("20001", f"1300{n:04d}", TEN_PULL_TS) for n in range(9)]   # 十连只剩 9（漏）
    dup = [rec("30005", "90000041", OTHER_TS),
           rec("30005", "90000041", OTHER_TS)]                              # 同一条算两遍（重）
    sizes = sorted(n for _key, n in event_size_violations(lost + dup))
    assert sizes == [2, 9]


# ---------- S4 同一记录出现在 3 个视图 ----------


def test_s04_record_seen_in_three_views_counted_once():
    """场景 S4：同一条记录出现在 3 个页面（全部卡池 / 活动·遴选汇总 / 具体池）。

    步①后由「事件账本」在**统计层**保证只计 1 次：第 2、3 个视图到达时，事件
    （同 ts）已存在、观察段相同 → 幂等 SEEN 跳过。

    这里按真实流水线（L1 视图过滤 → stats）验证**端到端**效果，而非 L1 内部：
    L1 的成对配对仍会放行第 3 份（其 N 视图缺陷见设计 §1.6），但账本会拦住它
    （设计 §10.2：L1 收敛可后置，非修 bug 必需）。

    当前状态：**绿**（账本幂等，端到端只计 1 次）。
    """
    tracker = PoolViewTracker()
    stats = make_engine()
    record = rec("30005", "90000041", OTHER_TS)
    for view in ("0@0", "0@3", "30005@0"):
        kept, _ = tracker.filter_records(view, [record])
        stats.add(kept)
    assert stats.total == 1


# ---------- S5 页码整体偏移 ----------


def test_s05_page_boundary_shift_causes_no_duplicate():
    """场景 S5：新抽 3 抽后重抓同一批旧记录（页边界整体下移）→ 不得重复计入。

    **位置对齐方案**：抽卡让列表变长 3，旧记录位置整体 +3；Δ=3 归一化后与历史
    位置重合 → 全部跳过，只收真正新抽的 3 条。
    """
    pool, ts, ts2 = "30005", OTHER_TS, OTHER_TS + 5000
    old_page = [rec(pool, f"1300{n:04d}", ts, position_in_batch=n - 1)
                for n in range(1, 11)]              # 一次十连 10 条（位置 0~9）
    history = list(old_page)

    shift = 1                      # 两次抓包之间抽了 1 抽（单抽）
    resent = [rec(pool, f"1300{n:04d}", ts, position_in_batch=n - 1 + shift)
              for n in range(1, 11)]                # 同一批旧记录，位置整体 +1
    fresh = [rec(pool, f"1400{n:04d}", ts2, position_in_batch=n - 1)
             for n in (1,)]                         # 新的单抽事件
    report = merge_by_position(history, resent + fresh, shift=shift)
    history += report.added

    # 事件级判重后：历史十连完整（size=10）→ 整事件跳过（event_skipped=1），
    # 与逐槽命中跳过（skipped=10）结果等价：旧 10 条一条不加。
    assert report.event_skipped == 1 and report.skipped == 0, \
        "旧十连应整事件跳过"
    assert report.total_added == 1, "只应新增真正新抽的 1 条"
    assert len(history) == 11
    assert report.suspicious == []


# ---------- S6 十连内相同角色 id 保份数 ----------


def test_s06_same_character_twice_in_one_event_keeps_both():
    """场景 S6：同一十连抽到同一角色两次 → 两条记录 key 完全相同，
    必须按份数保留两条（不能当重复删）。

    当前状态：**绿**（批内保留；跨会话保证见 S1）。
    """
    stats = make_engine()
    pool, ts = "20001", TEN_PULL_TS
    event = [rec(pool, "90000028", ts), rec(pool, "90000001", ts),
             rec(pool, "90000002", ts), rec(pool, "90000003", ts),
             rec(pool, "90000028", ts)]  # 90000028 出现两次
    assert stats.add(event) == 5
    assert sum(1 for r in stats.records if r.item_id == "90000028") == 2


# ---------- S7 十连不完整、各视图覆盖不同（嵌套前缀） ----------


def test_s07_nested_partial_observation_merges_to_full_event():
    """场景 S7：同一十连各次抓取覆盖不同（前 3 / 前 7 / 全 10）→ 并集恰 10 条。

    位置对齐方案：短的观察是长的「前缀」时，位置全部命中历史 → 幂等跳过，
    只补缺口。位置（而非内容）是判据。
    """
    pool, ts = "20001", TEN_PULL_TS
    full = [rec(pool, f"1300{n:04d}", ts, position_in_batch=n - 1)
            for n in range(1, 11)]
    history: list = []
    added_seq = []
    for part in (full[:3], full[:7], full):
        report = merge_by_position(history, part, shift=0)
        history += report.added
        added_seq.append(report.total_added)

    assert added_seq == [3, 4, 3], f"各次新增应为 3/4/3，实得 {added_seq}"
    assert len(history) == 10


# ---------- S8 只抓到前 5（不变量检出） ----------


def test_s08_only_first_half_captured_is_flagged():
    """场景 S8：只抓到前 5、后 5 没抓 → 事件大小 5。

    该异常必须可被不变量检出（不再静默）。

    当前状态：**绿**（检出逻辑正确）。
    """
    records = [rec("20001", f"1300{n:04d}", TEN_PULL_TS) for n in range(1, 6)]
    assert event_size_violations(records) == [(("20001", TEN_PULL_TS), 5)]


# ---------- S9 部分重叠 + 逆序到达 ----------


def test_s09_partial_overlap_reverse_arrival_keeps_real_duplicate():
    """场景 S9：十连被页边界切成的三段（非嵌套）到达时，真实重复不得被吞。

    真值（页 offset 拼接）= [28,08,21,18,19,07,13,01,24,28] = 10 条，
    其中接缝角色 90000028 真出现两次（首、尾各一次）。

    到达顺序：尾段（首元素 28）→ 中段 → 头段（末元素也是 28）。旧多重集规则会
    把第二次 28 当子集吞掉（只剩 9）；保序 + 追加规则下两段非嵌套 → 拼接保留。

    当前状态：**绿**（保序追加，28 两份都在）。
    """
    stats = make_engine()
    pool, ts = "20001", TEN_PULL_TS
    SHARED = "90000028"  # 接缝角色：首段与末段各出现一次（真·同角色重复）

    seg_tail = [rec(pool, SHARED, ts)]                              # 真值[0]   = 28
    seg_mid = [rec(pool, f"1300{n:04d}", ts) for n in (8, 21, 18, 19, 7)]   # 真值[1..5]
    seg_head = [rec(pool, f"1300{n:04d}", ts) for n in (13, 1, 24)] + [rec(pool, SHARED, ts)]  # 真值[6..9]

    stats.add(seg_tail); stats.begin_new_session()
    stats.add(seg_mid); stats.begin_new_session()
    stats.add(seg_head)  # 非嵌套 → 追加拼接：第二次 28 如实保留

    assert stats.total == 10
    assert sum(1 for r in stats.records if r.item_id == SHARED) == 2


# ---------- S10 跨会话接缝歧义（信息论极限） ----------


def test_s10_cross_session_seam_ambiguity_is_visible_not_silent():
    """场景 S10：十连切成两段、接缝处恰是同角色（X），且来自不同会话 →
    「重叠 1 条(=9)」还是「相邻两条(=10)」从内容无法判定（信息论极限）。

    目标设计：不静默 —— 不确定时必须让异常可见。

    当前状态：**绿**（输出落在 {9,10}；若为 9 则被不变量命中 → 可见）。
    """
    stats = make_engine()
    pool, ts = "20001", TEN_PULL_TS
    sess1 = [rec(pool, f"1300{n:04d}", ts) for n in (1, 2, 3, 4, 28)]  # 尾是 28
    sess2 = [rec(pool, f"1300{n:04d}", ts) for n in (28, 5, 6, 7, 8)]  # 头是 28
    stats.add(sess1); stats.begin_new_session()
    stats.add(sess2)

    n = sum(1 for r in stats.records if (r.pool_id, r.timestamp) == (pool, ts))
    assert n in (9, 10)  # 两种读法都「合法」
    if n != 10:
        # 未凑满即异常：必须可检出（不静默）
        assert ((pool, ts), n) in event_size_violations(stats.records)


# ---------- S11 反向读取无害（到达方向不影响结果） ----------


def test_s11_reading_direction_does_not_change_result():
    """场景 S11：反向读取（打开先载最新页、可跳末页从后往前翻）是真实用户习惯。

    方向本身无害：同一记录经两个视图、以相反到达顺序投递，结果必须一致。
    步②后 L1 直通，去重在账本层完成——同一事件第二次到达 = SEEN，
    两种顺序最终都只计 1 条。

    当前状态：**绿**。
    """
    a = rec("30005", "90000041", OTHER_TS)
    b = rec("30005", "90000041", OTHER_TS)

    def pipeline(order):
        tracker = PoolViewTracker()
        stats = make_engine()
        for view, r in order:
            kept, _ = tracker.filter_records(view, [r])
            stats.add(kept)
        return stats.total

    assert pipeline([("0@0", a), ("30005@0", b)]) == 1
    assert pipeline([("30005@0", b), ("0@0", a)]) == 1


# ---------- S12 跳页间隙（中间缺口可见 + 可补齐） ----------


def test_s12_backward_fast_paging_gap_is_visible_then_filled():
    """场景 S12：反向翻页过快可能一次跳过 5 页 → 观察出现「中间间隙」。

    后果是中间缺一段（**非**顺序反转）：必须可检出（事件大小 ∉ {1,10}），
    并能由其它视图/抓取补齐。

    当前状态：**绿**（间隙 → 事件大小异常 → 可检出）。
    """
    stats = make_engine()
    pool, ts = "20001", TEN_PULL_TS
    head = [rec(pool, f"1300{n:04d}", ts) for n in (1, 2, 3, 4, 5)]
    tail = [rec(pool, f"1300{n:04d}", ts) for n in (9, 10)]  # 反向翻页跳过中间段，只拿到尾
    stats.add(head); stats.begin_new_session()
    stats.add(tail)

    assert stats.total == 7
    assert event_size_violations(stats.records) == [((pool, ts), 7)]  # 中间缺口 → 异常可见

    # 另一视图/抓取补齐中间段后，恢复为完整 10 条
    stats.begin_new_session()
    stats.add([rec(pool, f"1300{n:04d}", ts) for n in (6, 7, 8)])
    assert stats.total == 10
    assert event_size_violations(stats.records) == []


# ---------- S15 未来新增卡池视图（pool_id 相同） ----------


def _ten_with_dup_tail(pool: str, ts: int) -> list:
    """构造一次十连：9 个不同角色 + 再抽一次首个角色（头尾同角色，真重复份数）。"""
    items = [f"1300{n:04d}" for n in range(1, 10)]
    items.append(items[0])  # 尾 = 首（同一 item_id，真·重复份数）
    return [rec(pool, i, ts) for i in items]


def test_s15_future_new_view_sharing_pool_id_counted_once():
    """场景 S15：**自适应**——未来游戏再新增一个汇总视图、pool_id 与「全部
    卡池」相同（与当年 活动/遴选 同类），同一十连从 4 个视图到达时，端到端
    必须仍然只计 1 次（10 条）。

    两种子情形：
      (a) 协议带 field3 → 视图可区分（make_view 对**任意** list_type 通用，
          新增类型无需改代码）；
      (b) 协议缺 field3 → 视图撞车、L1 无法区分 → 由**事件账本**按 ts 幂等兜住。

    关键：去重身份已从「视图/卡池」（随版本变）挪到「抽卡事件 = ts」（数据本身
    的属性），故新增视图**不可能**让计数变错。

    当前状态：**绿**（两种情况 total 均 10、无不变量违规）。
    """
    pool, ts = "20001", TEN_PULL_TS

    # (a) 新视图可区分：新增 list_type=5
    tracker = PoolViewTracker()
    stats = make_engine()
    for view in ("0@0", "0@3", "0@4", "0@5"):
        kept, _ = tracker.filter_records(view, _ten_with_dup_tail(pool, ts))
        stats.add(kept)
    assert stats.total == 10
    assert event_size_violations(stats.records) == []

    # (b) 新视图撞车：无 field3 → 回退纯 pool_id "0"，与既有汇总页混同
    tracker = PoolViewTracker()
    stats = make_engine()
    for view in ("0@0", "0@3", "0@4", "0"):
        kept, _ = tracker.filter_records(view, _ten_with_dup_tail(pool, ts))
        stats.add(kept)
    assert stats.total == 10  # L1 即使放行重复，账本按 ts 幂等拦住
    assert event_size_violations(stats.records) == []


# ---------- S16 跨视图拆分（L1 残余风险，必须可见） ----------


def test_s16_cross_view_split_no_longer_loses():
    """场景 S16（步②后转正）：同一 key 在同一十连内合法出现两次，且这两份
    恰好被拆到**不同视图**到达。

    步②前：L1 的 key 级成对配对会把第二份当成「他视图重传」误删 → 总数 9
    （2026-09-11 测试123 实测漏 3 的同款机理）。步②后 L1 直通、账本统一
    入账：后半 5 条与已有序列构成 MERGED 拼接 → 总数 10、不变量零命中。

    当前状态：**绿**（总数 10，无不变量异常）。
    """
    pool, ts = "20001", TEN_PULL_TS
    ten = _ten_with_dup_tail(pool, ts)  # 头尾同角色 90000001

    tracker = PoolViewTracker()
    stats = make_engine()
    # 前半（含第一份 90000001）来自视图 A；后半（含第二份）来自视图 B
    for view, part in (("0@0", ten[:5]), ("0@3", ten[5:])):
        kept, _ = tracker.filter_records(view, part)
        stats.add(kept)

    assert stats.total == 10  # 两份同名独立抽卡都保留，不再误删
    assert event_size_violations(stats.records) == []  # 事件大小合法


# ---------- S17 撞车预警（H1：视图回退必须可见） ----------


def test_s17_view_collapse_fallback_emits_warning_once(caplog):
    """场景 S17（H1 加固）：协议不带 field3 时，pool_id=0 的多个汇总页会折叠成
    同一个视图标识 "0" → L1 跨视图去重失效（与本次 bug 同类）。

    返回语义不变（仍回退 "0"），但必须打一次 warning，把「未来再新增 pool_id=0
    的视图」从静默变可见；同类警告只报一次，不逐帧刷屏；带 list_type 或非 0
    pool_id 时不自带撞车风险，不预警。

    当前状态：**绿**（H1 已实施）。
    """
    with patch.object(pool_view, "_VIEW_COLLAPSE_WARNED", False):
        caplog.set_level(logging.WARNING)

        # pool_id=0 且无类型 → 撞车风险：回退 "0" 且预警
        assert make_view(0, None) == "0"
        assert any("list_type" in r.getMessage() for r in caplog.records)

        # 同类警告只报一次（不逐帧刷屏）
        caplog.clear()
        assert make_view(0, None) == "0"
        assert not [r for r in caplog.records if "list_type" in r.getMessage()]

        # 带 list_type 或非 0 pool_id → 视图自带区分度，不预警
        caplog.clear()
        assert make_view(0, 0) == "0@0"
        assert make_view(30005, None) == "30005"
        assert not [r for r in caplog.records if "list_type" in r.getMessage()]

    # 端到端：真实 track_request 路径（请求缺 field3）同样触发预警且视图退化为 "0"
    with patch.object(pool_view, "_VIEW_COLLAPSE_WARNED", False):
        caplog.set_level(logging.WARNING)
        tracker = PoolViewTracker()
        tracker.track_request(_request(1, 0, 0))  # 不带 list_type → 无 field3
        assert tracker.current_view == "0"
        assert any("list_type" in r.getMessage() for r in caplog.records)


# ---------- S18/S19 2026-09-11 测试123 实测场景（步②回归钉死） ----------


def test_s18_cross_view_ten_pull_no_more_loss():
    """场景 S18（漏 3 实测回归）：同一十连经两个视图各完整到达一遍。

    步②前：L1 按单条 key 配对，跨视图轮换时把「十连内同名第二份」当跨视图
    重复滤掉（实测 20001 三个十连各丢 1 条，518 < 521）。步②后 L1 直通，
    账本按事件份数入账：第二视图整页重观察 = 连续子段 SEEN，不丢不重。
    """
    pool, ts = "20001", TEN_PULL_TS
    # 十连：位置 3 与 7 同角色 90000007（与 sample 真值同构）
    items = ["1", "2", "3", "90000007", "5", "6", "7", "90000007", "9", "10"]
    ten = [rec(pool, x, ts) for x in items]

    tracker = PoolViewTracker()
    stats = make_engine()
    for view, part in (("20001@0", ten[:5]), ("20001@0", ten[5:]),
                       ("0@0", ten[:5]), ("0@0", ten[5:])):  # 两视图各完整一遍
        kept, _ = tracker.filter_records(view, part)
        stats.add(kept)

    assert stats.total == 10
    assert event_size_violations(stats.records) == []


def test_s19_fragment_reobservation_no_more_duplicate():
    """场景 S19（多 4 实测回归）：完整事件入账后，**同一片段再次到达** → 不得重复。

    旧实现（内容模式）会把残片判为新观察整段追加（实测该事件长成 14 条）；
    位置对齐方案下残片的位置全部命中历史 → 全部跳过（位置即身份，不看内容）。
    """
    pool, ts = "30001", 1781066514587
    full = ["30", "28", "49", "22", "32", "13", "18", "22", "19", "38"]

    # 会话 1：两页收齐 10 条（位置 0~9）
    history = [rec(pool, x, ts, position_in_batch=i) for i, x in enumerate(full)]
    assert len(history) == 10

    # 会话 2：同一批里的残片再次到达（位置 1/2/4/5 —— 与历史完全重合）
    fragment = [rec(pool, x, ts, position_in_batch=i)
                for i, x in zip((1, 2, 4, 5), ["28", "49", "32", "13"])]
    report = merge_by_position(history, fragment, shift=0)
    history += report.added

    # 事件级判重后：历史十连完整（size=10）→ 残片整批随事件跳过
    # （event_skipped=1），与逐槽命中跳过结果等价：0 新增。
    assert report.total_added == 0 and report.event_skipped == 1, \
        "残片随完整事件整体跳过"
    assert len(history) == 10  # 不再 +4
    assert event_size_violations(history) == []
