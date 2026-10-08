"""历史坐标系归一 —— 纯函数，不碰 IO。

`users/<id>.json` 的 `records[]` 里混着两代坐标：

- 旧格式（<=2026-08-10）：`batch_seq` = 帧到达序号 / 页号，`position_in_batch`
  = 页内序号；
- 新格式（>=2026-08-11，B8 起）：`batch_seq` = 0，`position_in_batch` = 视图内
  绝对序号。

本模块把两者重编号为**单一连续坐标系**：`batch_seq` 全 0、`position_in_batch`
= 记录在「新->旧」序列里的下标（0 = 最新），形态与 Android 一次抓全的文件一致。

不变量（迁移前后必须完全相同）：

1. `pool_id` / `item_id` / `timestamp` 逐条不动；
2. 记录条数与**数组顺序**不动（实测 5 份正式文件数组序已等于统一序）；
3. 保底消费序（先 `timestamp`、后事件内 `(batch_seq, position_in_batch)`）
   逐位不变。

定序规则**沿用两端既有约定**（`analysis/stats._order_records_for_pity` /
Android `StatsEngine.orderWithinEvent`），不发明新规则：

- 事件之间：按 `timestamp` **降序**（新在前）；
- 事件内部：按 `(batch_seq, position_in_batch)` **升序**；
- 末尾「原下标升序」是保底 tiebreaker，保证排序结果唯一、可重复。

⚠️ **不要**改成「全局 `(batch_seq, position_in_batch)` 排序」—— 那会把不同事件
放进同一把尺子量，正是设计稿 §1.3/§1.5 已经否掉的做法。
"""
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Dict, List, Sequence

from ..models import GachaRecord

__all__ = [
    "UnifyPlan",
    "apply_unify",
    "group_order",
    "needs_unify",
    "plan_unify",
]


@dataclass(frozen=True)
class UnifyPlan:
    """一次归一计划（纯数据，不含 IO）。"""

    needed: bool
    """是否需要写盘：存在 `batch_seq != 0`，或数组序与统一序不一致。"""

    count: int
    """记录条数。"""

    changed: int
    """实际会被改动的记录条数（`batch_seq` 或 `position_in_batch` 与目标不符者）。"""

    order: List[int]
    """「新->旧」统一序列的**原数组下标**序列。"""

    array_already_ordered: bool
    """数组顺序是否已等于统一序列（True 时写盘不改变任何行序）。"""

    legacy_events: List[int]
    """含旧格式记录（`batch_seq != 0`）的事件 ts（升序）。"""

    mixed_events: List[int]
    """同一 ts 内新旧格式混层的事件 ts（升序）。"""

    warnings: List[str]
    """需要写日志的提示（非致命）。"""


def group_order(records: Sequence[GachaRecord]) -> List[int]:
    """返回「新->旧」统一序列的原数组下标序列。

    排序键：① 事件间 `timestamp` 降序；② 事件内 `(batch_seq,
    position_in_batch)` 升序；③ 原下标升序（tiebreaker，保证结果唯一可重复）。

    Args:
        records: 待定序的记录序列（只读，不修改）。

    Returns:
        原数组下标列表，长度恒等于 `len(records)`。
    """
    groups: Dict[int, List[int]] = defaultdict(list)
    for idx, rec in enumerate(records):
        groups[rec.timestamp].append(idx)

    order: List[int] = []
    for ts in sorted(groups, reverse=True):
        bucket = groups[ts]
        bucket.sort(
            key=lambda i: (
                records[i].batch_seq,
                records[i].position_in_batch,
                i,
            )
        )
        order.extend(bucket)
    return order


def needs_unify(records: Sequence[GachaRecord]) -> bool:
    """幂等判据：存在 `batch_seq != 0` 的记录才需要迁移。

    跨端安全：Android 落库恒写 `batch_seq = 0`（`HistoryStore.buildPayload`），
    即便用户在手机上重写整份文件，判据依然成立 —— 这是**刻意不采用
    「新增版本键」**的原因：Android 会丢弃未知键，用版本键会导致重复迁移。

    依据 `models.GachaRecord` 的字段约定：新数据（2026-08-18 起）
    `batch_seq` 恒为 0，旧数据是「帧到达序号」⇒ `batch_seq != 0` 等价于
    「文件里还混着旧格式坐标」。
    """
    return any(rec.batch_seq != 0 for rec in records)


def plan_unify(records: Sequence[GachaRecord]) -> UnifyPlan:
    """只读地算出一份归一计划（不修改任何输入）。

    Args:
        records: 待归一的记录序列。

    Returns:
        `UnifyPlan`；空序列返回 `needed=False` 的空计划。
    """
    count = len(records)
    if count == 0:
        return UnifyPlan(
            needed=False,
            count=0,
            changed=0,
            order=[],
            array_already_ordered=True,
            legacy_events=[],
            mixed_events=[],
            warnings=[],
        )

    order = group_order(records)
    array_ordered = order == list(range(count))
    # pos_of[原下标] = 该记录在统一序列里的新位置
    pos_of: Dict[int, int] = {old: new for new, old in enumerate(order)}

    changed = sum(
        1
        for idx, rec in enumerate(records)
        if rec.batch_seq != 0 or rec.position_in_batch != pos_of[idx]
    )

    legacy_events: List[int] = []
    mixed_events: List[int] = []
    buckets: Dict[int, List[GachaRecord]] = defaultdict(list)
    for rec in records:
        buckets[rec.timestamp].append(rec)
    for ts in sorted(buckets):
        has_legacy = any(r.batch_seq != 0 for r in buckets[ts])
        has_new = any(r.batch_seq == 0 for r in buckets[ts])
        if has_legacy:
            legacy_events.append(ts)
        if has_legacy and has_new:
            mixed_events.append(ts)

    warnings: List[str] = []
    if not array_ordered:
        warnings.append(
            "数组顺序与统一序列不一致（写盘会重排数组；Excel 明细行序随之变化）"
        )
    if mixed_events:
        warnings.append(
            f"{len(mixed_events)} 个事件新旧格式混层，按既有约定"
            f"（batch_seq 小者更新）定序: {mixed_events[:5]}"
        )

    needed = needs_unify(records) or not array_ordered
    return UnifyPlan(
        needed=needed,
        count=count,
        changed=changed,
        order=order,
        array_already_ordered=array_ordered,
        legacy_events=legacy_events,
        mixed_events=mixed_events,
        warnings=warnings,
    )


def apply_unify(
    records: Sequence[GachaRecord], plan: UnifyPlan
) -> List[GachaRecord]:
    """按计划重编号，返回新列表（顺序 = `plan.order`）。

    `GachaRecord` 是 frozen dataclass，用 `dataclasses.replace` 生成新实例，
    **不就地修改**输入。

    Args:
        records: 原始记录序列（须与生成 `plan` 时是同一份数据）。
        plan: `plan_unify(records)` 的产物。

    Returns:
        重编号后的记录列表（`batch_seq` 全 0，`position_in_batch` = 统一序下标）。

    Raises:
        ValueError: `plan.order` 不是 `records` 的合法置换（计划与数据不匹配）。
    """
    n = len(records)
    if len(plan.order) != n or sorted(plan.order) != list(range(n)):
        raise ValueError("plan.order 不是 records 的合法置换，拒绝重编号")

    pos_of: Dict[int, int] = {old: new for new, old in enumerate(plan.order)}
    out: List[GachaRecord] = []
    for old_idx in plan.order:
        out.append(
            replace(records[old_idx], batch_seq=0, position_in_batch=pos_of[old_idx])
        )
    return out
