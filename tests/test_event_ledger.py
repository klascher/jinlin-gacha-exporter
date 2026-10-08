# tests/test_event_ledger.py — 整文件注释保留（2026-09-14）。
# 对应 gacha_exporter/analysis/event_ledger.py 的整文件注释（旧账本已退役，
# 被位置对齐方案取代）。恢复旧账本时连同 event_ledger.py 一并取消注释。
# """事件账本（EventLedger）单测——步①基础入账 + 步②多重集兜底 + §15 三模式判定。

# 背景与取舍见 `docs/去重架构重构设计.md`（§3.1 入账规则、§10.2 步②、§15 三模式）。

# 覆盖三类：
# 1. **基础/步②**（`view=None`，走保守路径）：NEW / SEEN / SEEN* / EXTEND / MERGED
#    ——这是「视图未知」时的行为，必须与改造前逐字一致。
# 2. **§15 三模式**：SLOT（同视图+位置可比）、OVERLAP（跨视图页边界错位）、
#    视图未知的保守回退。
# 3. **诊断**：`slot_conflicts`（同槽不同内容）与 `unresolved_events()`
#    （长度合法但中间缺一格 = 未定序）。

# 不变量：**绝不丢数据** —— 只有「已收齐的复抓」或「同视图同槽同内容」才跳过；
# 含新份数的观察一律接纳（重复由 `{1,10}` 不变量暴露）。
# """
# from gacha_exporter.analysis.event_ledger import EventLedger
# from gacha_exporter.models import GachaRecord
# import pytest


# TS = 1781066514587  # 任取的十连时间戳（同一次十连 10 条共享）


# def rec(item_id: str, ts: int = TS, pool_id: str = "30001",
#         position_in_batch: int = 0) -> GachaRecord:
#     return GachaRecord(pool_id, item_id, ts, 0, position_in_batch)


# # ---------- 基础入账（步①语义回归） ----------


# def test_new_then_full_reobservation_is_seen():
#     """整事件入账后，另一视图完整重观察（连续子段）→ SEEN 幂等。"""
#     ledger = EventLedger()
#     page1 = [rec(f"{i}") for i in range(5)]
#     page2 = [rec(f"{i}") for i in range(5, 10)]
#     added1, _ = ledger.add(page1)
#     added2, _ = ledger.add(page2)
#     assert len(added1) == 5 and len(added2) == 5
#     assert ledger.size_of(TS) == 10

#     # 另一视图：同两页再来一遍（页边界相同）
#     added3, seen3 = ledger.add([rec(f"{i}") for i in range(5)])
#     added4, seen4 = ledger.add([rec(f"{i}") for i in range(5, 10)])
#     assert len(added3) == 0 and seen3 == 5
#     assert len(added4) == 0 and seen4 == 5
#     assert ledger.size_of(TS) == 10


# def test_ten_pull_same_name_keeps_both_copies():
#     """十连内同名角色两份独立抽卡：按份数保留，绝不误吞（本次漏3 的反面）。"""
#     ledger = EventLedger()
#     # 事件内 item "41" 出现两次（位置 0 与 1）
#     items = ["41", "41", "7", "8", "15", "1", "15", "28", "24", "49"]
#     ledger.add([rec(x) for x in items])
#     assert ledger.size_of(TS) == 10


# # ---------- 步②：多重集 SEEN 兜底 ----------


# def test_fragment_reobservation_covered_by_multiset_is_seen():
#     """残片观察（实测 30001 多4 场景）：不连续但份数全被覆盖 → SEEN 不追加。

#     复刻 2026-09-11 测试123 实测：事件已完整入账后，单池视图的残缺片段
#     [028,049,032,013]（跨视图配对滤剩的窟窿，步②后为跨视图跳页残片）到达。
#     """
#     ledger = EventLedger()
#     full = ["30", "28", "49", "22", "32", "13", "18", "22", "19", "38"]
#     ledger.add([rec(x) for x in full])  # 完整事件（另一视图顺序翻页）
#     assert ledger.size_of(TS) == 10

#     fragment = ["28", "49", "32", "13"]  # 残片：份数全部已被覆盖
#     added, seen = ledger.add([rec(x) for x in fragment])
#     assert len(added) == 0 and seen == 4
#     assert ledger.size_of(TS) == 10  # 不再长成 14 条


# def test_fragment_with_new_copies_still_appends():
#     """残片含新份数 → 不能跳过，照常 MERGED 追加（宁重复勿漏抓）。"""
#     ledger = EventLedger()
#     ledger.add([rec(x) for x in ["30", "28", "49", "22", "32"]])  # 前半 5 条
#     # 混合页：2 条已见（30,28）+ 3 条新（18,22,19）→ 不可整页跳过
#     mixed = ["30", "28", "18", "22", "19"]
#     added, seen = ledger.add([rec(x) for x in mixed])
#     assert seen == 0
#     assert len(added) == 5  # 全部追加（30,28 计重复，由不变量暴露）
#     assert ledger.size_of(TS) == 10


# def test_multiset_fallback_never_eats_new_copies_of_same_name():
#     """新份数绝不被兜底吞掉：事件差最后 1 条（49）时，另一视图补页
#     （该 ts 组仅 [49]）必须走 MERGED 接纳，事件收齐 10 条。"""
#     ledger = EventLedger()
#     truth = ["41", "41", "7", "8", "15", "1", "15", "28", "24", "49"]
#     ledger.add([rec(x) for x in truth[:5]])   # 第一视图前半页（5 条）
#     ledger.add([rec(x) for x in truth[5:9]])  # 第二视图补 4 条（缺 49）
#     assert ledger.size_of(TS) == 9
#     added, seen = ledger.add([rec(x) for x in ["49"]])  # 补页：新份数
#     assert seen == 0 and len(added) == 1 and added[0].item_id == "49"
#     assert ledger.size_of(TS) == 10  # 收齐，不丢不重


# def test_partial_event_single_record_page_is_not_swallowed():
#     """**实测丢数据回归**：事件未收齐时，同视图相邻页带来的「同名第二份」不得被吞。

#     2026-09-11/12 测试123 实测：20001 三个十连各缺「同名第二份」（缺格 100/153/194，
#     邻位都在）。真实形态是**同视图内连续位置**（页边界切开，缺的那条在相邻页里），
#     因此必须靠 **视图 + 位置** 判成「新槽」而不是「已见子段」——仅凭内容，这两者
#     完全同构（设计 §15.1）。
#     """
#     view = "20001@0"
#     ledger = EventLedger()
#     # 第一页 5 条（含 1 份 41），位置 95~99 —— 事件未收齐（size=5 ∉ {1,10}）
#     ledger.add([rec(x, position_in_batch=95 + i)
#                 for i, x in enumerate(["41", "7", "8", "15", "1"])], view)
#     assert ledger.size_of(TS) == 5

#     # 相邻页第一条：位置 100，正是「同名第二份」（新份数，不是复抓）
#     added, seen = ledger.add([rec("41", position_in_batch=100)], view)
#     assert seen == 0 and len(added) == 1 and added[0].item_id == "41"
#     assert ledger.size_of(TS) == 6  # 不再被吞

#     # 补齐剩余 4 条（位置 101~104）→ 事件收齐，槽位连续 95~104
#     added2, _ = ledger.add([rec(x, position_in_batch=101 + i)
#                             for i, x in enumerate(["15", "28", "24", "49"])], view)
#     assert len(added2) == 4 and ledger.size_of(TS) == 10
#     assert ledger.unresolved_events() == []

#     # 同位置复抓才算 SEEN（已收齐 + 连续子段）
#     added3, seen3 = ledger.add([rec("41", position_in_batch=100)], view)
#     assert len(added3) == 0 and seen3 == 1 and ledger.size_of(TS) == 10


# def test_multiset_fallback_scrambled_reobservation_is_seen():
#     """乱序重观察（先收后半、再收前半的全部卡池整页）：多重集覆盖 → SEEN。"""
#     ledger = EventLedger()
#     items = [str(i) for i in range(10)]
#     ledger.add([rec(x) for x in items[5:10]])  # 后半先到
#     ledger.add([rec(x) for x in items[0:5]])   # 前半后到
#     assert ledger.size_of(TS) == 10
#     # 第三个视图按 [0-4] 对齐重抓：连续子段不命中（est 已乱序），多重集兜底
#     added, seen = ledger.add([rec(x) for x in items[0:5]])
#     assert len(added) == 0 and seen == 5
#     assert ledger.size_of(TS) == 10


# # ---------- §15 三模式：SLOT（同视图·位置）/ OVERLAP（跨视图·重叠）/ 保守回退 ----------


# def test_same_view_adjacent_pages_keep_both_copies_of_same_name():
#     """反例 A（设计 §15.1）：同视图**相邻两页**，第 5 抽与第 6 抽同角色 → 真值 10。

#     两批位置同坐标系（view 相同 + 位置相接）→ SLOT 逐槽判定：位置不同就是新抽卡，
#     绝不按内容把「同名两份」压成 9 条（这是漏 3 的同型陷阱）。
#     """
#     view = "30005@0"
#     ledger = EventLedger()
#     ledger.add([rec(x, position_in_batch=i)
#                 for i, x in enumerate(["1", "2", "3", "4", "5"])], view)
#     added, seen = ledger.add([rec(x, position_in_batch=5 + i)
#                               for i, x in enumerate(["5", "6", "7", "8", "9"])], view)
#     assert seen == 0 and len(added) == 5
#     assert ledger.size_of(TS) == 10
#     assert [r.item_id for r in added][0] == "5"  # 同名第二条被完整保留


# def test_cross_view_page_boundary_overlap_is_compressed():
#     """反例 B（设计 §15.1）：跨视图位置错位（坐标系不可比）→ 挂起后由
#     finalize 联立求解，只接纳未覆盖的部分。

#     viewA 先给 `[X0..X4]`（位置 0~4）；viewB 的页给出 `[X4..X8]`（位置 5~9）——
#     两批位置不能比，但内容上后者的开头与前者末尾重叠 1 项 → 该批挂起，
#     finalize 裁决为对齐 a=4 → 只接纳 X5..X8。
#     """
#     view_a, view_b = "30005@0", "0@0"
#     ledger = EventLedger()
#     items = [f"X{i}" for i in range(10)]
#     ledger.add([rec(it, position_in_batch=i) for i, it in enumerate(items[0:5])], view_a)
#     added, seen = ledger.add([rec(it, position_in_batch=5 + i)
#                               for i, it in enumerate(items[4:9])], view_b)
#     assert seen == 0 and len(added) == 0          # 歧义 → 挂起，不当场裁决
#     added2, _ = ledger.add([rec(items[9], position_in_batch=10)], view_b)
#     assert len(added2) == 0                       # 单条跨视图 → 同样挂起
#     accepted = ledger.finalize()                  # 联立求解：p2 对齐 a=4、X9 对齐 9
#     assert len(accepted) == 5                     # 只接纳 X5..X9（X4 是重叠项）
#     assert ledger.size_of(TS) == 10
#     assert ledger.event(TS)[1] == items           # 顺序 == 真序（保底数学的输入）


# def test_view_unknown_keeps_legacy_conservative_behavior():
#     """视图未知（历史重建 / 请求没被监听到）→ SLOT/OVERLAP 都不启用 = 改动前行为。

#     这是「身份判错不会更糟」的保障：跨视图错位那批无法压缩，仍按内容模式整段
#     追加（X4 计两份、X9 尚缺），由 `{1,10}` 不变量与后续补页处理——**不劣化**。
#     """
#     ledger = EventLedger()
#     items = [f"X{i}" for i in range(10)]
#     ledger.add([rec(it, position_in_batch=i) for i, it in enumerate(items[0:5])])
#     added, seen = ledger.add([rec(it, position_in_batch=5 + i)
#                               for i, it in enumerate(items[4:9])])
#     assert seen == 0 and len(added) == 5
#     assert ledger.event(TS)[1] == items[0:5] + items[4:9]


# # ---------- V6 根因回归（2026-09-12 定位） ----------


# def test_same_name_landing_at_any_page_offset_is_not_dropped():
#     """**V6 根因回归**：前一页只有 1 条，下一页里**任意位置**出现同名 item → 都不得丢。

#     旧 `EXTEND` 只按内容判「est 是否 obs 的连续子段」：est=[X] 命中 `obs[k]==X` 时，
#     旧代码认定 obs[k] 就是已见过的那条 → 用 `obs[:k] + obs[k+1:]` 把该位置丢掉。
#     实测三个老十连（`20001` ts 1782541588797 / 1781752322041 / 1781154955186）缺的
#     pos 100 / 153 / 194 即命中下标 k = 0 / 3 / 4 —— 缺格位置随「同名项落在页内哪儿」
#     而变，这解释了「位置看似随机、四个代码版本表现一致」。

#     新 `SLOT` 模式按「同视图 + 位置」逐槽判定，与页内位置无关。
#     """
#     view = "20001@0"
#     for base, k in ((99, 0), (149, 3), (189, 4)):
#         head = "90000040"
#         mid = ["90000001", "90000007", "90000008", "90000015", "90000019"]
#         mid[k] = head                      # 同名项落在下一页的页内下标 k
#         tail = ["90000016", "90000001", "90000008", "90000011"]
#         truth = [head] + mid + tail

#         ledger = EventLedger()
#         ledger.add([rec(head, position_in_batch=base)], view)      # 前一页只有 1 条
#         ledger.add([rec(it, position_in_batch=base + 1 + i)
#                     for i, it in enumerate(mid)], view)
#         ledger.add([rec(it, position_in_batch=base + 6 + i)
#                     for i, it in enumerate(tail)], view)

#         assert ledger.size_of(TS) == 10, f"base={base} k={k}: 实得 {ledger.size_of(TS)} 条"
#         assert ledger.event(TS)[1] == truth, f"base={base} k={k}: 事件内顺序错"


# # ---------- 桌面分发版实测（2026-09-12 21:27，账号 27e99c91ef75）----------

# TS_DESK = 1789007614050
# POOL_DESK = "30006"
# VIEW_POOL, VIEW_ALL = "30006@0", "0@0"
# # 十连占 pos 30~39；其中 90000016 有三份（pos 34/35/38），90000001 两份（pos 31/33）
# PAGE_A = [(30, "90000040"), (31, "90000001"), (32, "90000028"),
#           (33, "90000001"), (34, "90000016")]            # 示例卡池016 off=30
# PAGE_A2 = PAGE_A                                         # 全部卡池 off=30（同内容重发）
# PAGE_B = [(35, "90000016"), (36, "90000034"), (37, "90000024"),
#           (38, "90000016"), (39, "90000011")]            # 全部卡池 off=35（限流重发成功）
# TRUTH_DESK = [it for _, it in PAGE_A + PAGE_B]


# def _desk(rows):
#     return [rec(it, ts=TS_DESK, pool_id=POOL_DESK, position_in_batch=pos)
#             for pos, it in rows]


# def test_desktop_seam_same_name_second_copy_is_not_swallowed():
#     """**桌面分发版实测（用户复现）**：up 池第 7/8 页是同一十连，第 5/6 抽同名。

#     翻完第 7 页（示例卡池016 off=30）就切到全部卡池，第 8 页（off=35，**页首与
#     第 7 页页尾同名**）从跨视图到达 → OVERLAP 的 k=1 内容巧合把它当成
#     「已覆盖的重叠」吞掉 → 事件 9 条（真值 10），总数 791（应 792）。

#     到达顺序取自桌面 app.log 21:27:28 / 21:27:35 / 21:27:36（第三页是限流被拒后
#     自动重发成功的，日志铁证「本批 5 条: 新增 4, 已见跳过 0」）。
#     """
#     ledger = EventLedger()
#     added1, _ = ledger.add(_desk(PAGE_A), VIEW_POOL)
#     assert len(added1) == 5 and ledger.size_of(TS_DESK) == 5

#     added2, seen2 = ledger.add(_desk(PAGE_A2), VIEW_ALL)   # 同内容跨视图重发
#     assert len(added2) == 0 and seen2 == 5

#     added3, seen3 = ledger.add(_desk(PAGE_B), VIEW_ALL)    # 后半页（页首同名）
#     assert seen3 == 0
#     ledger.finalize()                                      # 收尾裁决（挂起批次定案）
#     assert len(added3) == 0 or len(added3) == 5            # 挂起批不在 add 时接纳
#     assert ledger.size_of(TS_DESK) == 10
#     assert ledger.event(TS_DESK)[1] == TRUTH_DESK          # 顺序也必须正确


# def test_seam_same_name_at_any_next_page_offset_is_not_dropped():
#     """泛化：前页末条与后页第 k 条同名（k=0~4）→ 后页整页 5 条都必须保留。

#     旧 OVERLAP 只会误吞 k=0（页首）这一种；本用例把 5 种落点全部钉死，
#     防止修复时只堵页首、漏掉其他落点。
#     """
#     view_a, view_b = "30006@0", "0@0"
#     head_items = ["90000040", "90000001", "90000028", "90000001", "13001090"]
#     for k in range(5):
#         ledger = EventLedger()
#         first = [rec(it, ts=TS_DESK, pool_id=POOL_DESK, position_in_batch=30 + i)
#                  for i, it in enumerate(head_items)]       # 页尾 = 13001090
#         second_items = ["13001091", "13001092", "13001093",
#                         "13001094", "13001095"]
#         second_items[k] = "13001090"                       # 同名项落在后页下标 k
#         second = [rec(it, ts=TS_DESK, pool_id=POOL_DESK, position_in_batch=35 + i)
#                   for i, it in enumerate(second_items)]
#         ledger.add(first, view_a)
#         ledger.add(second, view_b)
#         ledger.finalize()                                  # 收尾裁决（挂起批次定案）
#         assert ledger.size_of(TS_DESK) == 10, f"k={k}: 实得 {ledger.size_of(TS_DESK)} 条"
#         assert ledger.event(TS_DESK)[1] == [r.item_id for r in first + second], (
#             f"k={k}: 事件内顺序错")


# # ---------- 诊断接口 ----------


# def test_slot_conflict_is_reported_not_swallowed():
#     """同视图同位置但内容不同（坐标系判错 / 序号平移）→ 打点 + 追加，不静默。"""
#     view = "20001@0"
#     ledger = EventLedger()
#     ledger.add([rec("A", position_in_batch=10), rec("B", position_in_batch=11)], view)
#     added, seen = ledger.add([rec("C", position_in_batch=10)], view)  # 同槽不同 item
#     assert seen == 0 and len(added) == 1          # 宁重复勿漏抓
#     assert ledger.slot_conflicts == [(TS, view, 10, "A", "C")]


# def test_unresolved_events_detects_middle_hole_with_legal_size():
#     """长度恰好 10 但**中间缺一格** → `suspicious_events` 看不出，`unresolved_events` 能抓。

#     2026-09-11/12 实测三个老十连正是此形态（缺格 100/153/194，邻位都在）：大小合法
#     却**未定序**，不能进保底/导出（计数与定序分离，设计 §14.4）。
#     """
#     view = "20001@0"
#     ledger = EventLedger()
#     ledger.add([rec(f"i{p}", position_in_batch=p) for p in (99, 101, 102, 103, 104)], view)
#     ledger.add([rec(f"i{p}", position_in_batch=p) for p in (105, 106, 107, 108)], view)
#     assert ledger.size_of(TS) == 9

#     # 第 10 条落在已占位置 99 且内容不同 → 冲突追加；大小凑成 10，但 100 仍缺
#     added, _ = ledger.add([rec("dup99", position_in_batch=99)], view)
#     assert len(added) == 1 and ledger.size_of(TS) == 10
#     assert ledger.suspicious_events() == []                    # 大小合法 → 看不出
#     assert ledger.unresolved_events() == [(TS, "30001", 10)]   # 未定序 → 被抓出


# # ---------- 事件内顺序（B6 端点规则，见 协议结构说明） ----------


# @pytest.mark.xfail(
#     strict=True,
#     reason="已知不修（2026-09-13 决策）：finalize 起点固定为槽位 0，反向翻页时 est 实为"
#           "「结尾段」→ 事件内顺序错位。"
#           "曾实现「枚举起点 + B7 翻页方向消歧」修好它，但实测引入更严重的重复追加"
#           "（测试123 的 1781752322041 由 9 条变 14 条），故回退。"
#           "顺序错的影响已被 stats._order_within_event() 的保守策略挡住：pos 连续时用"
#           "既有键，保底计数不受影响；真正的跨视图混合才用账本序列。",
# )
# def test_reverse_paging_event_order_matches_truth():
#     """先抓到较旧的一半、跨视图补较新的一半 → 事件序列必须等于真序。

#     B6：单向连续翻页时首个观察必然是事件的一端。反向翻页（跳到末页往新翻）
#     先遇到**较旧的一端**，此时 `est` 是「结尾段」；而 `finalize()` 的 base
#     用 `enumerate(st.items)` 把它钉死在槽位 0 → 整个事件顺序错位。

#     实测（2026-09-12 审查）：实得 `[X6..X10, X1..X5]`，真值 `[X1..X10]`。
#     保底数学不消费本序列（按 `position_in_batch` 自排序），但账本序列会被
#     `finalize` 之后的重建持久化，且是排序修正的输入源，故必须正确。
#     """
#     items = [f"X{i}" for i in range(1, 11)]          # X1 最新 … X10 最旧
#     led = EventLedger()
#     # 反向翻页（direction=-1：由旧往新翻）：先抓到较旧的一半
#     # （第 6~10 抽，up 池视图 pos 35~39）
#     led.add(
#         [rec(items[i], position_in_batch=35 + j) for j, i in enumerate(range(5, 10))],
#         "30005@0",
#         direction=-1,
#     )
#     # 跨视图补较新的一半（第 1~5 抽，全部卡池视图 pos 100~104）
#     led.add(
#         [rec(items[i], position_in_batch=100 + j) for j, i in enumerate(range(0, 5))],
#         "0@0",
#         direction=1,
#     )
#     led.finalize()
#     assert led.size_of(TS) == 10
#     assert led.event(TS)[1] == items, (
#         f"事件内顺序错：实得 {led.event(TS)[1]}"
#     )
