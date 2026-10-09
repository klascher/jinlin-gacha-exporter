"""位置对齐去重 —— 单次抓包全收 + 跨会话按位置合并。

设计见 `docs/去重归档/去重方案-位置对齐.md`（v1）。核心一句话：
**把「内容模式去重」换成「位置模式去重」**——同一视图内 `position_in_batch` 是记录在
列表里的绝对位置，同名记录位置不同，所以位置即身份，天然不会吞同名第二份。

三件事：

1. **单次抓包全收**（`merge_by_position([], recs)` 或调用方直接 extend）：依据 B3
   （同一视图不同页互斥）+ C5（页缓存，翻过的页不会二次请求），单次会话内不会有重复。
   整页指纹（`stats._seen_batches`）作为廉价兜底保留在调用方。
2. **跨会话按位置去重**：归一化到历史坐标系后 `(ts, pos)` 相同即为重复。
3. **Δ（位置平移量）= 权威 total 增量**（`infer_shift_from_total`）：抽卡会让列表变长、
   历史记录的位置整体后移，后移量恰好等于新增抽卡数 = 本次 total − 上次 total。

> 为什么不用「重叠段内容匹配」推 Δ：十连内**允许同名重复**（A4），内容相同不代表
> 是同一条记录。反例见设计稿 §4.3——`AABCD` 与 `BCDAE` 两段互补（真值 Δ=0），
> 内容比对会得出 Δ=3（B/C/D 三项"一致"）→ 归一化后 3 条撞上历史 → **漏记 3 条**。

**安全阀**：历史里同一事件的位置若断裂（B8 之前混入多视图坐标系的脏数据），
该事件不做位置判重、退化为整段追加并登记 `fallback_events`（宁可重复，绝不用
不可比的坐标系去判重）；合并后事件大小 ∉ {1,10} 一律进 `suspicious` 报警。
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import GachaRecord

# 合法事件大小：1 = 单抽、10 = 十连（规则 A1；旧 event_ledger.VALID_EVENT_SIZES 同值，
# 该模块及其测试已于 2026-10-10 整体删除，历史见 docs/去重归档/）
VALID_EVENT_SIZES = (1, 10)


@dataclass
class MergeReport:
    """一次合并的结果与诊断（供调用方落库 + 报警）。"""

    added: List[GachaRecord] = field(default_factory=list)
    """应追加到记录列表的记录（调用方负责 extend + 落库）。"""

    shift: int = 0
    """本次使用的 Δ（位置平移量 = 权威 total 增量）。"""

    merged_events: int = 0
    """与历史有交集、按位置判重处理过的事件数。"""

    new_events: int = 0
    """历史里没有的新事件数（新抽卡）。"""

    skipped: int = 0
    """位置命中历史、判为重复而跳过的条数。"""

    event_skipped: int = 0
    """事件级判重跳过的**事件数**（旧格式历史兼容，见 docs/去重归档/去重方案-旧历史兼容-事件级判重.md）。

    历史中已存在的完整事件（size ∈ {1,10}）重抓时按 ts 整体跳过：同一次抽卡不会因
    坐标系不同（旧·页内坐标 vs 新·绝对坐标）变成两次抽卡，pos 不参与判定。"""

    replaced_events: List[int] = field(default_factory=list)
    """被新完整事件整体替换的历史**部分事件** ts（调用方负责移除历史旧记录）。

    部分事件（size ∉ {1,10}）的 pos 不可靠，与新记录逐槽比对既不可靠也无必要：
    新完整事件包含旧部分事件的全部信息（子集关系），整体替换无损失、绝不静默
    （由调用方写日志）。"""

    suspicious: List[Tuple[int, str, int]] = field(default_factory=list)
    """合并后大小 ∉ {1,10} 的事件 `[(ts, pool_id, size), ...]`（绝不静默）。"""

    fallback_events: List[int] = field(default_factory=list)
    """历史位置断裂、退化为整段追加的事件 ts 列表（脏数据信号）。"""

    @property
    def total_added(self) -> int:
        """新增条数。"""
        return len(self.added)


def group_by_ts(records: Sequence[GachaRecord]) -> Dict[int, List[GachaRecord]]:
    """按 timestamp 分组（一个 timestamp = 一次抽卡动作 = 一个事件）。"""
    groups: Dict[int, List[GachaRecord]] = defaultdict(list)
    for rec in records:
        groups[rec.timestamp].append(rec)
    return dict(groups)


def infer_shift_from_total(
    current_total: Optional[int], previous_total: Optional[int]
) -> int:
    """由权威 total 推断位置平移量 Δ（设计稿 §4.3）。

    Δ = 本次 total − 上次落库时的 total。列表头部每次新增抽卡都会让历史记录的位置
    整体后移同样的条数，所以这个差值就是归一化时要减掉的后移量。

    Args:
        current_total: 本次会话响应给出的权威总抽数（`view_totals`）
        previous_total: 上次落库时记录的权威总抽数（历史文件里的快照）

    Returns:
        非负整数。任一端缺失（首次抓包 / 清空或还原后重新起算）或 total 变少
        （清空/还原后抓得更少）→ 0：此时无历史可合并，Δ 取 0 最安全。
    """
    if current_total is None or previous_total is None:
        return 0
    return max(0, current_total - previous_total)


def _merge_by_slots(
    report: "MergeReport",
    hist_recs: Sequence[GachaRecord],
    new_recs: Sequence[GachaRecord],
    shift: int,
) -> List[GachaRecord]:
    """既有逐槽判定：`new.pos − shift` 命中历史位置 → 跳过；否则收下。

    同一事件内的位置集合就地扩充（`known.add`），同批内的重复位置也会被拦。
    """
    known = {r.position_in_batch for r in hist_recs}
    merged = list(hist_recs)
    for rec in new_recs:
        normalized = rec.position_in_batch - shift
        if normalized in known:
            report.skipped += 1
            continue
        known.add(normalized)
        report.added.append(rec)
        merged.append(rec)
    report.merged_events += 1
    return merged


def _positions_comparable(records: Sequence[GachaRecord]) -> bool:
    """判断同一事件内的位置是否处于**同一坐标系**（可以按位置判重）。

    两条判据（都来自规则文件）：

    1. **位置不重复**：同一坐标系下一条记录只有一个位置，出现重复说明混入了
       另一个坐标系的记录（B8 之前抓包混多视图时实测有 50 个 pos 值重复）。
    2. **跨度 ≤ 10**：一个事件最多 10 条、占 10 个连续位置（B1、A1），跨度超过
       10 必然是两个坐标系拼在一起。

    注意**不能要求位置连续**：历史可能只抓到事件的一部分（如十连只抓到第 1 抽
    与第 6 抽，pos 100 与 105），这种「有洞但同坐标系」是合法且常见的，
    强制连续会把它误判成脏数据、退化成整段追加而产生重复。
    """
    if len(records) <= 1:
        return True
    positions = [r.position_in_batch for r in records]
    if len(set(positions)) != len(positions):
        return False                      # 位置重复 -> 坐标系混合
    return max(positions) - min(positions) <= 10


def _check_event(ts: int, merged: Sequence[GachaRecord]) -> Optional[Tuple[int, str, int]]:
    """合并后事件大小是否合法；不合法返回 `(ts, pool_id, size)`。"""
    size = len(merged)
    if size in VALID_EVENT_SIZES:
        return None
    pool_id = merged[0].pool_id if merged else ""
    return (ts, pool_id, size)


def merge_by_position(
    history: Sequence[GachaRecord],
    new: Sequence[GachaRecord],
    shift: int = 0,
) -> MergeReport:
    """把本次抓到的 `new` 按位置并入 `history`，返回应新增的记录与诊断。

    判定规则（逐事件；`hits` = 新记录 `pos − shift` 命中历史位置集合的条数）：

    | 情形 | 处理 |
    |---|---|
    | 历史里没有该 ts | 全新事件 → 全部收下 |
    | **历史十连完整（size = 10）** | **事件级判重：ts 即身份 → 整事件跳过**。10 条已覆盖该事件全部可能记录，重抓同 ts（无论坐标是旧·页内还是新·绝对、命中几个）都没有可新增的东西——部分命中恰是两套坐标数值巧合，逐槽合并反而会产生 15 条怪事件 |
    | **历史单抽（size = 1）+ 新事件单条 + hits = 0** | **事件级判重**：同一次单抽、坐标系不可比 → 跳过（sample 的 21 个单抽事件靠这条保住） |
    | 历史单抽 + hits > 0 | 既有逐槽判定（同坐标系：命中跳过 / 未命中收下；跨会话碎片续传场景靠它到达收尾） |
    | 历史**部分事件** + 新事件完整 + **hits = 0** | **整体替换**：新完整事件必然包含旧部分事件的全部记录（同一次抽卡）却一条没对上 → 坐标系不可比 → 用新完整事件替换（子集关系，无损失），登记 `replaced_events` 交调用方移除旧记录，绝不静默 |
    | 历史部分事件、位置断裂（脏数据/坐标系不可比） | 整段追加 + 登记 `fallback_events` |
    | 其余（历史部分事件、位置可比） | 既有逐槽判定（前缀补全 / 重叠跳过 / 新增） |

    Args:
        history: 历史记录（已落库的全部记录）
        new: 本次抓到的记录（单次会话内已全收）
        shift: 位置平移量 Δ，见 `infer_shift_from_total`

    Returns:
        `MergeReport`；调用方应把 `added` 追加进记录列表（并按 `replaced_events`
        移除被替换的旧记录）后落库，并消费 `suspicious` / `fallback_events` 报警。
    """
    report = MergeReport(shift=shift)
    history_groups = group_by_ts(history)

    for ts, new_recs in group_by_ts(new).items():
        hist_recs = history_groups.get(ts)
        if not hist_recs:
            report.added.extend(new_recs)
            report.new_events += 1
            merged: List[GachaRecord] = list(new_recs)
        else:
            known = {r.position_in_batch for r in hist_recs}
            hits = sum(
                1 for rec in new_recs if rec.position_in_batch - shift in known
            )
            if len(hist_recs) == 10:
                # 事件级判重：历史十连完整，ts 即身份，整事件跳过（见上表）。
                report.event_skipped += 1
                merged = list(hist_recs)
            elif (len(hist_recs) == 1 and len(new_recs) == 1 and hits == 0):
                # 事件级判重：历史单抽 + 单条重抓 + 零命中（坐标系不可比）。
                report.event_skipped += 1
                merged = list(hist_recs)
            elif (len(hist_recs) not in VALID_EVENT_SIZES
                  and len(new_recs) in VALID_EVENT_SIZES and hits == 0):
                # 历史部分事件 + 新完整事件 + 零命中 → 坐标系不可比 → 整体替换。
                report.replaced_events.append(ts)
                report.added.extend(new_recs)
                report.merged_events += 1
                merged = list(new_recs)
            elif not _positions_comparable(hist_recs):
                # 历史坐标系不可比（B8 前混视图遗留：位置重复或跨度异常）
                # → 不做位置判重，宁重复勿漏抓
                report.fallback_events.append(ts)
                report.added.extend(new_recs)
                merged = list(hist_recs) + list(new_recs)
            else:
                merged = _merge_by_slots(report, hist_recs, new_recs, shift)

        bad = _check_event(ts, merged)
        if bad:
            report.suspicious.append(bad)

    return report
