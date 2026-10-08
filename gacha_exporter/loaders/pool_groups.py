"""保底分组解析 - 把 pool_id 解析到保底分组，并补齐兜底 solo 分组配置。

pool 的 pity_group 字段指向 role.json 顶层 pity_groups 段定义的组；
未配（字段空）或引用未定义组 -> 兜底 solo（仅该池自己一个组，hard_pity 70，无状态）。
这样即使 role.json 没有 pity_groups 段（旧数据/远程未更新）也能降级工作。
"""
from typing import Dict, Tuple

from ..models import PityGroup, Pool
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 兜底 solo 分组的默认配置
DEFAULT_HARD_PITY = 70
DEFAULT_HAS_STATE = False


def resolve_groups(
    pools: Dict[str, Pool],
    pity_groups: Dict[str, PityGroup],
) -> Tuple[Dict[str, str], Dict[str, PityGroup]]:
    """解析每个 pool 的保底分组归属。

    Args:
        pools: pool_id -> Pool（Pool.pity_group 指向组 key）
        pity_groups: group_id -> PityGroup（role.json 已定义的组）

    Returns:
        (pool_to_group, group_configs)
        - pool_to_group: pool_id -> group_id（每个 pool 都有归属，兜底 solo）
        - group_configs: group_id -> PityGroup（含兜底 solo 组的配置，可直接查）
    """
    pool_to_group: Dict[str, str] = {}
    group_configs: Dict[str, PityGroup] = dict(pity_groups)

    for pid, pool in pools.items():
        gkey = pool.pity_group
        if gkey and gkey in pity_groups:
            pool_to_group[pid] = gkey
        else:
            if gkey and gkey not in pity_groups:
                logger.warning(f"pool {pid} 引用未定义的 pity_group '{gkey}'，兜底 solo")
            solo_key = "solo:" + pid
            pool_to_group[pid] = solo_key
            if solo_key not in group_configs:
                group_configs[solo_key] = PityGroup(
                    id=solo_key,
                    label=pool.name,
                    hard_pity=DEFAULT_HARD_PITY,
                    has_state=DEFAULT_HAS_STATE,
                )
    return pool_to_group, group_configs
