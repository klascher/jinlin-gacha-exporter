"""抽卡事件账本 — 整文件注释保留（2026-09-14）。
旧账本（以 timestamp 为唯一身份、内容+位置+方向判定）已被位置对齐方案取代，
产线无任何 import；本文件整体注释保存以便回滚旧账本。如要恢复：去掉每行行首的
`# ` 前缀，并一并取消 tests/test_event_ledger.py 的注释。设计见
docs/去重归档/去重架构重构设计.md。
"""


# """抽卡事件账本 — 以 timestamp 为唯一身份的去重账本。

# 背景、诊断与取舍见 `docs/去重架构重构设计.md`（§3.1 核心新件、§1.8 不变量、
# §15 「事件内有序合并」实施计划）。本模块是**统计层（stats）与持久层（history）
# 共用的同一套去重规则**——此前两处各自维护「单条 key 集合」，口径漂移正是
# 「十连丢抽」bug 的根因；合并为一处后物理上不可能再不一致。

# 核心定义
# --------
# - **身份 = timestamp**：一次抽卡（单抽/十连）= 一个事件。单抽 1 条记录，
#   十连 10 条记录**共享同一 timestamp**（实测事件大小 1/10 双峰）；账号内
#   timestamp 唯一（无 >10 组、无跨池 ts）。
# - **内容 = 有序 item 序列**：保序，**不比对重叠处的内容** —— 十连内抽到同一
#   角色两次是独立抽卡动作，必须按份数保留（拿内容比对会把它误当重发吞掉，
#   那正是原 bug 的翻版）。
# - **pool_id = 一致性属性**：同一事件内恒定（实测 0 例外）；出现不一致只打点，
#   不改数据。
# - **视图 = 第三信号（2026-09-12 新增）**：仅凭「内容 + 位置」无法同时正确处理
#   两个**完全同构**的场景（设计 §15.1 反例 A/B）——
#   A：同视图相邻两页，`[1,2,3,4,5]` + `[5,6,7,8,9]` → 真值 10（第 5 抽与第 6 抽
#      同角色，两次独立抽卡）；
#   B：跨视图位置错位，viewA `[X0..X4]` + viewB `[X4..X8]` → 真值两批指同一十连
#      的第 4~8 抽。
#   两者在（内容, 位置）上同构而真值相反，因此**必须**引入视图身份判断「两批的
#   位置是否在同一坐标系」。视图身份由 `pool_id@list_type` 给出（见 pool_view，
#   实测 ① 级可信）。

# 入账规则（跨视图 / 跨页 / 跨会话统一）
# --------------------------------------
# ==================  ==================================================  ==========
# 情形                 判定                                                新增记录
# ==================  ==================================================  ==========
# ``NEW``             该 ts 首次出现                                       全部
# ``SEEN``            事件**已收齐**且新观察是已有序列的连续子段            无
# ``SEEN*``           事件**已收齐**且观察段份数多重集被已有序列完全覆盖    无
# ``SLOT``            **同视图**且位置可比（本批严格递增、与已有槽位区间    位置为新槽的
#                     相接/相交）→ 逐槽判定：同槽同 item=复抓；新槽=追加；   记录
#                     同槽不同 item=冲突（打点 + 追加，宁重复勿漏抓）
# ``SEEN``(2)         跨视图（或视图未知）时，观察段是已有序列的连续子段    无
#                     → 复抓（保留步①语义，未收齐也生效）
# ``EXTEND``          已有序列是新观察的**连续子段**（更完整）→ 升级        新观察中未被
#                                                                         已有段覆盖部分
# ``OVERLAP``         **跨视图**且已有序列末尾 k 项 == 新观察开头 k 项        新观察的第 k
#                     （页边界错位造成的重叠）→ 只追加 k 之后的部分          项之后
# ``MERGED``          以上都不是 → **追加拼接**（宁重复勿漏抓）                全部
# ==================  ==================================================  ==========

# 判定顺序即上表顺序（``SLOT`` 必须排在 ``SEEN``(2) 之前 —— 否则「同视图相邻页
# 里携带的『十连内同名第二份』」会被当成连续子段吞掉，即 2026-09-11/12 实测
# 丢数据的路径）。

# 「已收齐」= 已有序列长度 ∈ {1,10} —— 仅作用于 ``SEEN``/``SEEN*``。``SEEN``(2)
# 不受此限（实测：给它加同样限制会让跨视图重复页无法去重，某事件由 11 条放大到
# 17 条，见 `tests/test_dedup_real_data.py`）。

# 「非嵌套即追加」是刻意的取舍：**宁可重复、不可漏抓**（用户明确要求），重复
# 由 `{1,10}` 不变量暴露。「追加」不比对重叠处内容，因此不会吞掉真实重复。

# **降级（视图未知 = 完全等同改动前行为）**：任一侧视图为 `None`（请求没被监听
# 到、或来自历史重建）时，`SLOT`/`OVERLAP` 都不启用，只会走 `SEEN`/`SEEN*`/
# `EXTEND`/`MERGED` —— 即步①/步②的既有行为，**身份判错时不会比改动前更糟**。

# 账本内部序列恒等于「历次追加的拼接」，所以**可从落库记录逐字重建**（无损、
# 可重放）——`rebuild()` 即利用这一点。
# """
# from typing import Dict, List, Optional, Tuple

# from ..models import GachaRecord
# from ..utils.logger import get_logger

# logger = get_logger(__name__)

# #: 事件的合法大小（§1.8）：单抽 = 1 条、十连 = 10 条。其余一律视为可疑。
# VALID_EVENT_SIZES: Tuple[int, ...] = (1, 10)


# def _contiguous_index(sub: List[str], full: List[str]) -> int:
#     """`sub` 作为 `full` 的**连续子序列**首次出现的起始下标；不存在返回 -1。

#     连续（contiguous）是刻意的：事件内顺序稳定（页内序 + 页 offset 可定位），
#     观察到的片段必是重建序列的连续一段。空 `sub` 视为在位置 0 命中。
#     """
#     n, m = len(sub), len(full)
#     if n == 0:
#         return 0
#     if n > m:
#         return -1
#     first = sub[0]
#     for i in range(m - n + 1):
#         if full[i] == first and full[i:i + n] == sub:
#             return i
#     return -1


# def _multiset_covered(sub: List[str], full: List[str]) -> bool:
#     """`sub` 的 item 份数是否被 `full` 完全覆盖（Counter 级 ≤）。

#     多重集兜底的判据（步②，见设计 §10.2）：同 ts = 同一事件，事件的真实
#     份数多重集是固定的；若观察段的每份 item 在已有序列里都已有对应份数，
#     则这段观察必然是复抓，追加只会产生重复。**含任一新份数即返回 False**
#     （交给 EXTEND/OVERLAP/MERGED），因此绝不吞新数据。
#     """
#     full_counts: Dict[str, int] = {}
#     for item in full:
#         full_counts[item] = full_counts.get(item, 0) + 1
#     for item in sub:
#         if full_counts.get(item, 0) <= 0:
#             return False
#         full_counts[item] -= 1
#     return True


# def _positions_usable(positions: List[int]) -> bool:
#     """本批位置是否可用于**位置模式**（SLOT）。

#     要求严格递增：位置来自同一视图列表的绝对序号（`offset + 帧内序号`），
#     同一页内必然递增；出现重复或回退说明位置不可比（如退化 fixture、
#     跨坐标系拼接），此时不得用位置做判定。
#     """
#     if not positions:
#         return False
#     return all(b > a for a, b in zip(positions, positions[1:]))


# def _ranges_touch(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
#     """两个位置区间是否**相交或紧邻**（差 1）。

#     仅在此条件下才认为「两批在同一坐标系里可比」——位置会随新抽卡整体平移
#     （§1.7），区间分离时自动退回内容模式，不会误配。
#     """
#     return a_lo <= b_hi + 1 and b_lo <= a_hi + 1


# def _forward_overlap_k(est_items: List[str], obs_items: List[str]) -> int:
#     """最大 k 使 `est` 末尾 k 项 == `obs` 开头 k 项（跨视图页边界错位的重叠量）。

#     用于 OVERLAP：同一事件在两个视图的页切分不同，新视图的一页可能以「已有
#     序列的末尾若干项」开头，此时只追加 k 之后的部分即可既补全又不重复。
#     """
#     limit = min(len(est_items), len(obs_items))
#     for k in range(limit, 0, -1):
#         if est_items[-k:] == obs_items[:k]:
#             return k
#     return 0


# def _longest_contiguous_run(positions: List[int]) -> int:
#     """位置集合的**最长连续区间**长度（识别「中间缺一格」）。"""
#     if not positions:
#         return 0
#     ordered = sorted(set(positions))
#     best = run = 1
#     for prev, cur in zip(ordered, ordered[1:]):
#         run = run + 1 if cur == prev + 1 else 1
#         best = max(best, run)
#     return best


# def _enum_alignments(base: Dict[int, str],
#                      batches: List[Tuple[Optional[str], List[GachaRecord]]],
#                      cap: int):
#     """枚举挂起批次在事件槽位（0..cap-1）上的**联合对齐**。

#     模型：事件占 ``cap`` 个连续槽位；每个挂起批次是该区间上的一个连续切片，
#     其对齐 ``a``（首条对应的槽位）待定。约束：

#     1. 同一 view 的批次（以及同一 view 的已确认槽位）互不重叠——同一视图里
#        一条记录只有一个位置；
#     2. 覆盖已确认槽位（base）时内容必须一致；
#     3. 对齐不越界（``a + n <= cap``）。

#     产出 ``(filled, matches, accepted)``：filled = {槽位: item}（联合覆盖），
#     matches = 与已确认槽位的内容命中数，accepted = 应新接纳的记录（按槽位序）。
#     """
#     results: List[Tuple[Dict[int, str], int, List[GachaRecord]]] = []
#     views = [v for v, _ in batches]
#     obs_list = [obs for _, obs in batches]

#     def walk(i: int, filled: Dict[int, str], slot_view: Dict[int, str],
#              matches: int, acc: List[Tuple[int, GachaRecord]]) -> None:
#         if i == len(obs_list):
#             ordered = [r for _, r in sorted(acc, key=lambda t: t[0])]
#             results.append((dict(filled), matches, ordered))
#             return
#         view, obs = views[i], obs_list[i]
#         n = len(obs)
#         for a in range(0, cap - n + 1):
#             f2 = dict(filled)
#             sv2 = dict(slot_view)
#             ok = True
#             m = matches
#             acc2 = list(acc)
#             for j, r in enumerate(obs):
#                 slot = a + j
#                 if slot in sv2:
#                     if sv2[slot] == view:        # 同视图双覆盖 → 不可能
#                         ok = False
#                         break
#                     if f2[slot] != r.item_id:    # 内容不一致 → 不可能
#                         ok = False
#                         break
#                     m += 1                       # 跨视图重复投递命中
#                     continue
#                 f2[slot] = r.item_id
#                 sv2[slot] = view
#                 if slot not in base:
#                     acc2.append((slot, r))
#                 else:
#                     m += 1                       # 命中已确认槽位
#             if ok:
#                 walk(i + 1, f2, sv2, m, acc2)

#     walk(0, dict(base), dict.fromkeys(base.keys(), "__est__"), 0, [])
#     return results


# class _EventState:
#     """账本内部的事件状态（可变）。

#     - `items`：保序 item 序列，恒等于「历次追加的拼接」（可逐字重建）。
#     - `view`：**首次观察时**的视图身份；`None` = 未知（历史重建 / 请求没被监听到）。
#     - `slots`：该事件在 `view` 坐标系下的 `{位置: item}`，仅在位置可比时维护；
#       用于同视图逐槽判定（区分「新份数」与「复抓」）。
#     """

#     __slots__ = ("pool_id", "items", "view", "slots", "pending", "direction")

#     def __init__(self, pool_id: str, items: List[str],
#                  view: Optional[str] = None,
#                  slots: Optional[Dict[int, str]] = None,
#                  direction: int = 0) -> None:
#         self.pool_id = pool_id
#         self.items: List[str] = items
#         self.view: Optional[str] = view
#         self.slots: Dict[int, str] = slots if slots is not None else {}
#         # 首个观察（est）所在视图的翻页方向：+1 往旧翻 / -1 往新翻 / 0 未知。
#         # 消歧用：决定 est 是事件的「开头段」还是「结尾段」（B6 端点规则）。
#         self.direction: int = direction
#         # 跨视图「内容接缝歧义」的观察：压缩与追加两种世界都自洽时挂起，
#         # 由 finalize() 用「事件大小 ∈ {1,10}」约束裁决（2026-09-12 桌面 791）
#         self.pending: List[GachaRecord] = []

#     @property
#     def complete(self) -> bool:
#         """事件是否已收齐（序列长度 ∈ {1,10}）。"""
#         return len(self.items) in VALID_EVENT_SIZES


# class EventLedger:
#     """抽卡事件账本（保序、幂等、可重建）。

#     只存不可变的身份与内容，不持有 `GachaRecord` 对象——记录对象仍由调用方
#     （stats.records / history._records）保存，本账本只决定「哪些记录该被接纳」。
#     """

#     def __init__(self) -> None:
#         self._events: Dict[int, _EventState] = {}
#         # 同 ts 多池（理论不可能，实测 0 例）：出现即打点，供排查
#         self._pool_conflicts: List[Tuple[int, str, str]] = []
#         # 同视图同位置但内容冲突（位置坐标系判错 / 序号平移的信号）：打点不改数据
#         self._slot_conflicts: List[Tuple[int, Optional[str], int, str, str]] = []

#     # ---------- 查询 ----------

#     @property
#     def event_count(self) -> int:
#         """账本内的事件数。"""
#         return len(self._events)

#     def timestamps(self) -> set:
#         """账本内全部事件的时间戳集合（供事件级重叠判断，如 has_overlap）。"""
#         return set(self._events.keys())

#     def contains(self, timestamp: int) -> bool:
#         """该时间戳是否已有事件。"""
#         return timestamp in self._events

#     def event(self, timestamp: int) -> Optional[Tuple[str, List[str]]]:
#         """取某事件的 (pool_id, item 序列) 副本；不存在返回 None。"""
#         entry = self._events.get(timestamp)
#         if entry is None:
#             return None
#         return entry.pool_id, list(entry.items)

#     def size_of(self, timestamp: int) -> int:
#         """某事件的记录数；不存在返回 0。"""
#         entry = self._events.get(timestamp)
#         return len(entry.items) if entry else 0

#     def view_of(self, timestamp: int) -> Optional[str]:
#         """某事件记录到的视图身份（未知返回 None）。"""
#         entry = self._events.get(timestamp)
#         return entry.view if entry else None

#     def suspicious_events(self) -> List[Tuple[int, str, int]]:
#         """违反 `{1,10}` 不变量的事件 `[(ts, pool_id, size), ...]`（§1.8）。

#         在**会话收尾/合并时**调用（不要在每页 add 后调用：十连的首页天然只有
#         5 条、尚未收齐，会误报）。返回空列表表示全部事件大小合法。
#         """
#         return [
#             (ts, st.pool_id, len(st.items))
#             for ts, st in self._events.items()
#             if len(st.items) not in VALID_EVENT_SIZES
#         ]

#     def unresolved_events(self) -> List[Tuple[int, str, int]]:
#         """**未定序/未收齐**事件 `[(ts, pool_id, size), ...]`（计数与定序分离闸门）。

#         与 `suspicious_events()` 的区别：本方法优先看**槽位连续区间**，因此能抓出
#         「序列长度恰好 10 但中间缺一格、另有一格重复」这类大小合法却未定序的事件
#         （2026-09-11/12 实测三个老十连即「缺中格」形态）。无槽位（视图未知/历史
#         重建）时退回按长度判定。
#         """
#         out: List[Tuple[int, str, int]] = []
#         for ts, st in self._events.items():
#             if st.slots:
#                 if _longest_contiguous_run(list(st.slots.keys())) not in VALID_EVENT_SIZES:
#                     out.append((ts, st.pool_id, len(st.items)))
#             elif len(st.items) not in VALID_EVENT_SIZES:
#                 out.append((ts, st.pool_id, len(st.items)))
#         return out

#     @property
#     def pool_conflicts(self) -> List[Tuple[int, str, str]]:
#         """同 ts 多池的一致性冲突记录 `[(ts, 已有 pool, 新 pool), ...]`。"""
#         return list(self._pool_conflicts)

#     @property
#     def slot_conflicts(self) -> List[Tuple[int, Optional[str], int, str, str]]:
#         """同视图同槽位内容冲突 `[(ts, view, 位置, 已有 item, 新 item), ...]`。

#         出现即说明「位置坐标系判错」或「序号随新抽卡平移」，用于排查；数据侧
#         按「宁重复勿漏抓」保留新记录，由 `{1,10}` 不变量暴露。
#         """
#         return list(self._slot_conflicts)

#     # ---------- 变更 ----------

#     def clear(self) -> None:
#         """清空账本。"""
#         self._events.clear()
#         self._pool_conflicts.clear()
#         self._slot_conflicts.clear()

#     def rebuild(self, records: List[GachaRecord]) -> None:
#         """按 `(pool_id, timestamp)` 分组、保序重建账本（无损）。

#         用于：加载历史、会话基线刷新。分成多个 ts 组时批内顺序即重建序列；
#         同一个 ts 的多条记录按其在 `records` 里的先后顺序拼成该事件的序列——
#         这与 `add()` 的「追加」语义一致，故重建结果与原账本逐字相同。

#         重建**不带视图信息**（落库记录里没有视图字段，刻意不引入第二个真值源，
#         见设计 §15.6）→ 视图为 `None`，后续该事件的判定走保守路径（= 改动前
#         行为），不会因「视图推断」出错。
#         """
#         self._events.clear()
#         self._pool_conflicts.clear()
#         self._slot_conflicts.clear()
#         for rec in records:
#             entry = self._events.get(rec.timestamp)
#             if entry is None:
#                 self._events[rec.timestamp] = _EventState(rec.pool_id, [rec.item_id])
#             else:
#                 entry.items.append(rec.item_id)

#     def add(self, records: List[GachaRecord],
#             view: Optional[str] = None,
#             direction: int = 0) -> Tuple[List[GachaRecord], int]:
#         """按 timestamp 分组后事件级入账，返回 `(新增记录, 已见跳过条数)`。

#         Args:
#             records: 本批新解析出的记录（页内有序）
#             view: 本批所属视图身份；`None` 表示未知（历史/请求未被监听）
#             direction: 该视图的翻页方向（**+1 往旧翻 / -1 往新翻 / 0 未知**）。
#                 仅首个观察（NEW）时记录，供 `finalize()` 消歧「est 是开头段还是
#                 结尾段」——仅凭内容+位置无法区分（两种对齐评分相同）。

#         Returns:
#             ``(new_records, seen_count)`` —— 应被追加到记录列表的记录，以及被
#             判定为「事件已完整见过」而跳过的条数（供调用方判「是否翻到历史边界」）。
#         """
#         if not records:
#             return [], 0

#         # 按 ts 分组，保持批内先后顺序（一次 add 通常是一页，一个事件最多跨两页）
#         groups: Dict[int, List[GachaRecord]] = {}
#         order: List[int] = []
#         for rec in records:
#             if rec.timestamp not in groups:
#                 groups[rec.timestamp] = []
#                 order.append(rec.timestamp)
#             groups[rec.timestamp].append(rec)

#         new_records: List[GachaRecord] = []
#         seen_count = 0
#         for ts in order:
#             obs = groups[ts]
#             obs_items = [r.item_id for r in obs]
#             obs_pos = [r.position_in_batch for r in obs]
#             est = self._events.get(ts)

#             # (1) NEW：首次见到该时间戳
#             if est is None:
#                 slots = (
#                     dict(zip(obs_pos, obs_items))
#                     if view is not None and _positions_usable(obs_pos)
#                     else {}
#                 )
#                 self._events[ts] = _EventState(
#                     obs[0].pool_id, list(obs_items), view, slots,
#                     direction if view is not None else 0,
#                 )
#                 new_records.extend(obs)
#                 continue

#             if obs[0].pool_id != est.pool_id:
#                 # 一致性校验：同 ts 应恒同池（实测 0 例外）。不改数据，只打点。
#                 self._pool_conflicts.append((ts, est.pool_id, obs[0].pool_id))
#                 logger.warning(
#                     f"事件账本异常：同 ts 出现不同 pool "
#                     f"(ts={ts}, 已有={est.pool_id}, 新={obs[0].pool_id})，按已有池保留"
#                 )

#             same_view = view is not None and est.view is not None and view == est.view
#             cross_view = view is not None and est.view is not None and view != est.view

#             # (2) SEEN：事件**已收齐**且新观察是已有序列的连续子段 → 复抓，幂等跳过
#             if est.complete and _contiguous_index(obs_items, est.items) >= 0:
#                 seen_count += len(obs)
#                 continue

#             # (3) SEEN*：事件**已收齐**且份数多重集被完全覆盖 → 复抓（步②兜底）。
#             #     （已收齐事件的多重集已固定，含新份数只可能是多算 → 跳过安全。）
#             if est.complete and _multiset_covered(obs_items, est.items):
#                 seen_count += len(obs)
#                 continue

#             # (4) SLOT：同视图 + 位置可比 → 逐槽判定（**必须早于 (5)**，否则同视图
#             #     相邻页携带的「十连内同名第二份」会被连续子段规则吞掉）
#             if (same_view and _positions_usable(obs_pos) and est.slots
#                     and _ranges_touch(obs_pos[0], obs_pos[-1],
#                                       min(est.slots), max(est.slots))):
#                 accepted, seen = self._slotwise(ts, est, obs, obs_pos)
#                 est.items.extend(r.item_id for r in accepted)
#                 new_records.extend(accepted)
#                 seen_count += seen
#                 continue

#             # (5) SEEN：跨视图 / 视图未知时的连续子段复抓（保留步①语义，未收齐
#             #     也生效——实测给它加「已收齐」限制会放大重复，见模块 docstring）
#             if _contiguous_index(obs_items, est.items) >= 0:
#                 seen_count += len(obs)
#                 continue

#             # (6) EXTEND：已有序列是新观察的连续子段 → 用更完整的新观察升级
#             k = _contiguous_index(est.items, obs_items)
#             if k >= 0:
#                 new_part = obs[:k] + obs[k + len(est.items):]
#                 # 账本序列 = 「已有 + 本批新增」的拼接，与调用方记录列表的追加
#                 # 顺序保持一致 → 可从记录逐字重建（见 rebuild）。
#                 est.items.extend(r.item_id for r in new_part)
#                 new_records.extend(new_part)
#                 continue

#             # (7) 跨视图 + 未收齐 → **对齐无法在单批内唯一确定，一律挂起**，
#             #     交给 finalize() 联立求解（此时已掌握该事件的全部观察）。
#             #     唯一例外：len(est)+n > 事件容量 → 总量强制存在重叠，且内容接缝
#             #     能定位时立即压缩（2026-09-12 实测事件 A/B 即此形态）。
#             #     背景：跨视图两批可能是「互补的两半」（2026-09-12 桌面实测 791：
#             #     up 池第 7/8 页同一十连、第 5/6 抽同名、翻完第 7 页切全部卡池），
#             #     也可能是「部分重叠」—— 仅凭内容接缝无法区分（§15.1 反例 A/B）。
#             if cross_view:
#                 n = len(obs_items)
#                 cap = 10 if (len(est.items) > 1 or n > 1) else 1
#                 if n > cap:
#                     # 单批记录数超过事件容量 → 结构不可能 → 保守追加 + 报警
#                     logger.warning(
#                         f"事件账本异常：跨视图批次 {n} 条超过事件容量 {cap} "
#                         f"(ts={ts}, view={view})，按宁重复勿漏抓整段追加"
#                     )
#                     est.items.extend(obs_items)
#                     new_records.extend(obs)
#                     continue
#                 if len(est.items) + n > cap:
#                     k = _forward_overlap_k(est.items, obs_items)
#                     if k > 0:
#                         new_part = obs[k:]
#                         est.items.extend(r.item_id for r in new_part)
#                         new_records.extend(new_part)
#                         continue
#                 # 对齐不唯一 → 挂起，收尾联立求解
#                 est.pending.append((view, list(obs)))
#                 continue

#             # (8) MERGED：非嵌套 → 追加拼接，不比对重叠处内容（宁重复勿漏抓）
#             est.items.extend(obs_items)
#             new_records.extend(obs)

#         return new_records, seen_count

#     def finalize(self) -> List[GachaRecord]:
#         """收尾裁决：联立求解挂起批次的对齐，返回应补接纳的记录。

#         在**会话收尾 / 历史合并前**调用。挂起的观察是「互补的两半」与「部分
#         重叠」两种世界都自洽的批次——单批无法定案（§15.1 反例 A/B 同构），
#         但收尾时已掌握该事件的**全部**观察，可联立求解：

#         - 事件 = ``cap ∈ {1,10}`` 个连续槽位（0..cap-1），已确认内容 = est；
#         - 每个挂起批次是槽位上的一个连续区间（对齐 a 待定）；
#         - 约束：同一 view 的批次/槽位互不重叠（同视图一条记录只有一个位置）；
#           覆盖已确认槽位时内容必须一致；槽位不越界；
#         - 评分：优先「cap 个槽位全部填满（事件完整）」，其次内容命中数。

#         唯一/最优解 → 按解接纳**新槽位**上的记录（含按槽位排序）；无解 →
#         全部接纳（宁重复勿漏抓）+ 报警，由 ``suspicious_events`` 暴露。

#         Returns:
#             补接纳的记录（调用方应追加到自己的记录列表）。
#         """
#         new_records: List[GachaRecord] = []
#         for ts, st in self._events.items():
#             if not st.pending:
#                 continue
#             cap = 10 if (len(st.items) > 1
#                          or any(len(obs) > 1 for _, obs in st.pending)) else 1
#             batches = [(view, obs) for view, obs in st.pending]
#             # ⚠️ 起点**固定为槽位 0**（不上线「枚举起点」）：2026-09-13 实测该改动
#             # 会让部分事件的 est 内部顺序选错，后续批次因此识别不出重复而整段追加
#             # （测试123 的 1781752322041 由 9 条 → 14 条）。顺序错的影响已被
#             # `stats._order_within_event()` 的保守策略挡住（pos 连续时用既有键），
#             # 故此处保留旧行为以杜绝重复。B7 翻页方向信号仍采集（供将来用）。
#             base = {i: it for i, it in enumerate(st.items)}
#             cands = _enum_alignments(base, batches, cap)
#             if not cands:
#                 logger.warning(
#                     f"事件账本收尾：ts={ts} 挂起观察 {len(batches)} 批无自洽对齐 "
#                     f"→ 宁重复勿漏抓，全部接纳"
#                 )
#                 for _, obs in batches:
#                     st.items.extend(r.item_id for r in obs)
#                     new_records.extend(obs)
#             else:
#                 # 评分：①槽位全填满（事件完整）优先；②内容命中数；③少接纳重复
#                 cands.sort(key=lambda c: (0 if len(c[0]) >= cap else 1,
#                                           -c[1], len(c[2])))
#                 filled, _, accepted = cands[0]
#                 st.items = [filled[i] for i in sorted(filled)]
#                 new_records.extend(accepted)
#                 if len(st.items) not in VALID_EVENT_SIZES:
#                     logger.warning(
#                         f"事件账本收尾：ts={ts} 裁决后大小 {len(st.items)} "
#                         f"∉ {VALID_EVENT_SIZES}（槽位 {len(filled)}/{cap}）"
#                         f"→ 存在缺口，由 suspicious_events 暴露"
#                     )
#             st.pending.clear()
#         return new_records

#     # ---------- 内部 ----------

#     def _slotwise(self, ts: int, est: _EventState,
#                   obs: List[GachaRecord],
#                   obs_pos: List[int]) -> Tuple[List[GachaRecord], int]:
#         """同视图位置模式：逐槽判定「复抓 / 新槽 / 冲突」。

#         前提由调用方保证：本批位置严格递增（无重复槽），且与已有槽位区间相接/相交。
#         """
#         accepted: List[GachaRecord] = []
#         seen = 0
#         for rec, pos in zip(obs, obs_pos):
#             known = est.slots.get(pos)
#             if known is None:
#                 est.slots[pos] = rec.item_id
#                 accepted.append(rec)
#             elif known == rec.item_id:
#                 seen += 1
#             else:
#                 # 同槽不同内容：位置坐标系可能判错 / 序号平移 → 打点 + 追加（宁重复）
#                 self._slot_conflicts.append((ts, est.view, pos, known, rec.item_id))
#                 logger.warning(
#                     f"事件账本异常：同视图同位置内容冲突"
#                     f"(ts={ts}, view={est.view}, pos={pos}, 已有={known}, 新={rec.item_id})，"
#                     f"按宁重复勿漏抓追加"
#                 )
#                 accepted.append(rec)
#         return accepted, seen
