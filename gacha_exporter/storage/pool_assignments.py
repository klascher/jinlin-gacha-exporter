"""本地陌生卡池分组指派 - data/pool_assignments.json 读写。

陌生 pool_id（role.json 未收录）默认兜底为独立卡池（solo）。实际新卡池几乎
总是 UP 或遴选池（与其他池共享保底），故提供本地指派：把陌生池并入某个
has_state 共享组，统计时与组内其他池一起跑共享保底。

实现上不改统计算法：指派结果只是为陌生池合成一个带 pity_group 的 Pool，
注入传给 StatsEngine 的 pools 映射，分组解析（resolve_groups）自然生效。
仅对未收录的陌生池生效；元数据收录该池后真实配置自动覆盖，指派条目失效
（无害），启动时后台静默清掉已收录条目（cleanup_pool_assignments）。
跨账号全局，因为是卡池维度而非账号维度。
"""
import json
import os
from typing import Dict

from ..models import PityGroup, Pool
from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

ASSIGNMENTS_FILENAME = "pool_assignments.json"

# 合成陌生池的 order：排在组内所有已收录池之后（陌生池总是新池，展示在最下）
UNKNOWN_POOL_ORDER = 999


def _assignments_path() -> str:
    """指派文件路径（与 exe 同级的 data/ 目录）。"""
    return os.path.join(get_data_dir(), ASSIGNMENTS_FILENAME)


def load_pool_assignments() -> Dict[str, str]:
    """读取陌生池指派 {pool_id: group_id}。文件缺失/损坏返回空 dict（不抛错）。"""
    path = _assignments_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取 pool_assignments 失败，用空映射: {e}")
        return {}
    if isinstance(data, dict):
        raw = data.get("assignments", data)
    else:
        raw = {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(pid): str(gkey)
        for pid, gkey in raw.items()
        if isinstance(pid, (str, int)) and isinstance(gkey, str) and gkey
    }


def save_pool_assignments(assignments: Dict[str, str]) -> None:
    """写入陌生池指派。失败仅告警（不影响运行）。"""
    try:
        with open(_assignments_path(), "w", encoding="utf-8") as f:
            json.dump({"assignments": dict(sorted(assignments.items()))}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写 pool_assignments 失败（不影响运行）: {e}")


def apply_pool_assignments(
    pools: Dict[str, Pool],
    pity_groups: Dict[str, PityGroup],
    assignments: Dict[str, str],
) -> Dict[str, Pool]:
    """把指派合入 pools 映射，返回新的映射（不改动原 dict）。

    仅为「role.json 未收录 且 指向已定义 pity_group」的陌生池合成 Pool：
    - type 取该组内已收录池的 type（如 UP/遴选），决定 is_featured 等；
    - order 给大值，组内展示排在已收录池之后。
    指向未定义组 / 已被元数据收录的条目直接忽略（收录后真实配置优先）。
    """
    effective = dict(pools)
    for pid, gkey in assignments.items():
        if pid in effective or gkey not in pity_groups:
            continue
        ref = next((p for p in pools.values() if p.pity_group == gkey), None)
        pool_type = ref.type if ref else "UP"
        effective[pid] = Pool(
            id=pid,
            name=f"未知({pid})",
            type=pool_type,
            order=UNKNOWN_POOL_ORDER,
            pity_group=gkey,
        )
    return effective


def cleanup_pool_assignments(pools: Dict[str, Pool]) -> int:
    """启动清理：元数据（role.json/缓存）已收录的池，其指派条目清掉，返回清除数。

    指派是给「元数据暂未收录的陌生池」打的临时补丁，收录后 apply_pool_assignments
    本就会跳过（无害），这里做纯文件卫生：把已收录条目从文件里删掉，避免长期累积。
    - 只在确有可清条目时写盘，无变化零写入（文件不存在时 load 返回空 dict，同样零写入）；
    - 后台静默执行，仅日志，无用户可见反馈。
    """
    assignments = load_pool_assignments()
    if not assignments:
        return 0
    stale = [pid for pid in assignments if pid in pools]
    if not stale:
        return 0
    save_pool_assignments({pid: g for pid, g in assignments.items() if pid not in pools})
    logger.info(f"卡池指派清理：已移除 {len(stale)} 条已收录池的指派: {sorted(stale)}")
    return len(stale)
