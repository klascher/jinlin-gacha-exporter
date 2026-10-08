"""手动补丁计算 - 推导可编辑槽 + 合成记录注入。

补丁描述「缺失边界」的手动补齐，用于恢复共享保底统计，但**不改动冻结的
shared_pity_tracking()**。思路与 §3.16 陌生卡池指派一致：把补丁数据合成为
原型 GachaRecord（含推进 pity 的「垫抽填充」与真正的六星命中），注入一个
**影子引擎**的 records 计算展示；真实引擎 records（会被 history.merge 持久化）
始终零污染。

可编辑槽（详见 compute_editable）：
- 锚点池 leading：组内「有真实记录、卡池最旧」的池，可补记录开始前垫的抽数。
- 补录池 items：组内「无真实记录、且比锚点更旧」的前缀池，可整段补六星序列。
- 仅前缀：只允许最旧的一批缺史前缀，中间断档不可补。

合成记录的时间戳全部落在该组最早真实 ts 之前，保证排序中手动记录最早；
「手」标记据此用 (hit.ts < 组最早真实ts) 判别。
"""
from collections import defaultdict
from typing import Dict, List, Tuple

from ..loaders.pool_groups import resolve_groups
from ..models import Character, GachaRecord, PityGroup, Pool
from ..storage.pool_patches import MANUAL_FILL_ID
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 补录池 slab 间距（ms）：前缀池之间
_SLAB_MS = 1_000_000
# 单池内两条抽取之间 / 锚点 leading 内
_INTRA_MS = 1_000
# position_in_batch 基础大值：手动记录按「小=新」语义排，见 _BIG_BASE 说明
_BIG_BASE = 10_000_000


def _pool_age(pool: Pool) -> tuple:
    """卡池新旧键：号小 = 池旧，越小越旧。

    按 pool_id 纯数字升序（与 pity_panel._pool_sort_key 同源、仅方向反转），
    不再读已废弃的 Pool.order 字段——role.json 里 order 曾用于排序但 2026-08-20
    弃用，容错差（新池漏改/乱改 order 会被判成最旧，如 30005 order=1 的反例），
    同组池 id 号已按先后分配，纯 id 序即可反映池子先后。非纯数字 pid 排最后。
    """
    pid = pool.id
    try:
        return (int(pid),)
    except (TypeError, ValueError):
        return (float("inf"), pid)


def _group_meta(
    pools: Dict[str, Pool],
    pity_groups: Dict[str, PityGroup],
    real_records: List[GachaRecord],
) -> Dict[str, dict]:
    """按分组推导元信息：锚点池、可补录池、最早真实 ts、组展示信息。"""
    pool_to_group, group_configs = resolve_groups(pools, pity_groups)

    # 组 -> 池集合（按新旧排序）
    group_pools: Dict[str, List[str]] = defaultdict(list)
    for pid in pools:
        group_pools[pool_to_group[pid]].append(pid)
    for gkey, pids in group_pools.items():
        group_pools[gkey] = sorted(pids, key=lambda pid: _pool_age(pools[pid]))

    # 组 -> 有真实记录的池集合；最早真实 ts
    group_recorded: Dict[str, set] = defaultdict(set)
    group_t0: Dict[str, int] = {}
    for rec in real_records:
        gkey = pool_to_group.get(rec.pool_id, "solo:" + rec.pool_id)
        group_recorded[gkey].add(rec.pool_id)
        group_t0[gkey] = min(group_t0.get(gkey, rec.timestamp), rec.timestamp)

    meta: Dict[str, dict] = {}
    for gkey, pids in group_pools.items():
        cfg = group_configs[gkey]
        recorded = group_recorded.get(gkey, set())
        # 锚点 = 有真实记录且池最旧者（无真实记录则无锚，不开放编辑）
        anchor = next((pid for pid in pids if pid in recorded), None)
        backfillable: List[str] = []
        if anchor is not None:
            anchor_age = _pool_age(pools[anchor])
            # 仅前缀：无真实记录且比锚点更旧的池
            backfillable = [
                pid for pid in pids
                if pid not in recorded and _pool_age(pools[pid]) < anchor_age
            ]
        meta[gkey] = {
            "has_record": anchor is not None,
            "anchor": anchor,                    # 可设 leading 的池（或 None）
            "backfillable": backfillable,        # 可整段补录的池
            "t0": group_t0.get(gkey),            # 组最早真实 ts（可能 None）
            "label": cfg.label,
            "hard_pity": cfg.hard_pity,
            "has_state": cfg.has_state,
        }
    return meta


def compute_editable(
    pools: Dict[str, Pool],
    pity_groups: Dict[str, PityGroup],
    real_records: List[GachaRecord],
) -> Dict[str, dict]:
    """推导各组可编辑槽，供维护对话框展示。

    Returns:
        {group_id: {
            "label", "hard_pity", "has_state",
            "has_record": bool,
            "anchor": Optional[str],          # 可设 leading 的池
            "backfillable": [str, ...],       # 可整段补录的池
            "backfill_names": {pid: pool.name},
        }}
    """
    meta = _group_meta(pools, pity_groups or {}, real_records)
    out: Dict[str, dict] = {}
    for gkey, m in meta.items():
        out[gkey] = {
            "label": m["label"],
            "hard_pity": m["hard_pity"],
            "has_state": m["has_state"],
            "has_record": m["has_record"],
            "anchor": m["anchor"],
            "backfillable": m["backfillable"],
            "backfill_names": {pid: pools[pid].name for pid in m["backfillable"]},
        }
    return out


def synthesize_records(
    pools: Dict[str, Pool],
    characters: Dict[str, Character],
    pity_groups: Dict[str, PityGroup],
    patches: Dict[str, dict],
    real_records: List[GachaRecord],
) -> Tuple[List[GachaRecord], Dict[str, Character], Dict[str, int]]:
    """把补丁合成为 GachaRecord 列表，返回 (manual_records, aug_characters, bound)。

    - manual_records：合成的记录（垫抽填充 + 六星命中），不含任何真实记录。
    - aug_characters：characters 扩增 MANUAL_FILL_ID（rarity=4，非六星），
      供影子引擎把填充记录归为非六星、推进 pity 而不触发命中。
    - bound：{group_id: 组最早真实 ts}，面板据此把 hit.ts<bound 判为「手」。
    """
    meta = _group_meta(pools, pity_groups or {}, real_records)
    manual: List[GachaRecord] = []
    aug_chars = dict(characters)
    aug_chars[MANUAL_FILL_ID] = Character(
        id=MANUAL_FILL_ID, name="（手动补齐）", rarity=4, limited=False
    )
    bound: Dict[str, int] = {}
    position = _BIG_BASE  # 每发一条合成记录递减，保证「越小越新」且不相等

    for gkey, m in meta.items():
        if m["t0"] is None:
            continue
        bound[gkey] = m["t0"]
        # 补录池：按新旧升序（最旧在前），各占一个 slab，均严格落在 t0 之前
        backsort = sorted(
            [pid for pid in m["backfillable"]
             if pid in patches and patches[pid].get("items")],
            key=lambda pid: _pool_age(pools[pid]),
        )
        for rank, pid in enumerate(backsort):
            base_ts = m["t0"] - (len(backsort) - rank) * _SLAB_MS - _SLAB_MS
            manual, position = _synthesize_pool(
                pools[pid], patches[pid]["items"], base_ts, position, manual,
            )
        # 锚点 leading 填充：ts 落在补录 slab 之后、t0 之前
        anchor = m["anchor"]
        if anchor is not None and anchor in patches and patches[anchor].get("leading", 0):
            leading = patches[anchor]["leading"]
            # 最后一条填充落在 t0-_INTRA_MS（紧贴真实记录之前），逐条往前倒排
            for j in range(leading):
                ts = m["t0"] - (leading - j) * _INTRA_MS
                manual.append(GachaRecord(
                    pool_id=anchor, item_id=MANUAL_FILL_ID, timestamp=ts,
                    batch_seq=0, position_in_batch=position,
                ))
                position -= 1

    return manual, aug_chars, bound


def _synthesize_pool(pool: Pool, items: List[dict], base_ts: int,
                     position: int, manual: List[GachaRecord],
                     ) -> Tuple[List[GachaRecord], int]:
    """补录单池：按 items（draw 严格递增）生成填充 + 六星记录，追加到 manual。

    draw 为该池内绝对抽数：第 d 条填充/命中，直到该池累计 d 抽。每段（上一
    六星之后至本六星）抽数为 `draw - prev_draw - 1` 填充 + 1 六星。返回更新后
    的 (manual, position)。
    """
    prev_draw = 0
    ts = base_ts
    for item in items:
        draw = item["draw"]
        fills = draw - prev_draw - 1
        for _ in range(fills):
            manual.append(GachaRecord(
                pool_id=pool.id, item_id=MANUAL_FILL_ID, timestamp=ts,
                batch_seq=0, position_in_batch=position,
            ))
            position -= 1
            ts += _INTRA_MS
        manual.append(GachaRecord(
            pool_id=pool.id, item_id=item["char"], timestamp=ts,
            batch_seq=0, position_in_batch=position,
        ))
        position -= 1
        ts += _INTRA_MS
        prev_draw = draw
    return manual, position


def build_patched_engine(engine, manual_records: List[GachaRecord],
                         aug_characters: Dict[str, Character]):
    """构造影子引擎：记录 = 真实 + 合成，供展示计算；不改动原引擎。

    直接赋值 .records（不走 add，避开批次指纹/历史 key 去重/持久化副作用）。
    仅用于 shared_pity_tracking / total / non_warp_rates 等展示消费。
    """
    from ..analysis.stats import StatsEngine

    shadow = StatsEngine(
        aug_characters,
        engine.pools,
        engine.pity_groups,
        engine.shielded,
    )
    shadow.records = list(engine.records) + list(manual_records)
    return shadow