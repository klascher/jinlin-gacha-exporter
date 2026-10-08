"""手动卡池补丁 - data/pool_patches.json 按账号读写。

补丁描述「缺失边界」的手动补齐，用于恢复共享保底统计：
- leading：锚点池（组内最早有真实记录、order 最小的池）记录开始前垫的抽数。
- items：补录池（无真实记录、order 小于锚点的前缀池）的六星命中序列，
  draw 为该池内绝对抽数（第 N 抽出角色X，严格递增）。

补丁与真实抓包数据（data/users/<id>.json）分离存储，绝不覆盖原历史。
仅账号维度隔离（对齐 HistoryService 的 profile）。语义校验（仅前缀 / 锚点）
由 analysis/manual_patches 与维护对话框负责，本模块只校验结构。
"""
import json
import os
from typing import Dict

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

PATCHES_FILENAME = "pool_patches.json"

# 填充记录占位 item_id：引擎中映射为 rarity=4 的非六星，推进 pity 用
MANUAL_FILL_ID = "__MANUAL_FILL__"


def _patches_path() -> str:
    """补丁文件路径（与 exe 同级的 data/ 目录）。"""
    return os.path.join(get_data_dir(), PATCHES_FILENAME)


def _validate_pool_patch(patch: dict) -> dict:
    """校验单个池的补丁结构；非法抛 ValueError。补齐缺失键、强转类型。"""
    if not isinstance(patch, dict):
        raise ValueError("卡池补丁必须是对象")
    out: Dict[str, object] = {}
    # leading：可省略 -> 0
    raw_leading = patch.get("leading", 0)
    try:
        leading = int(raw_leading)
    except (TypeError, ValueError):
        raise ValueError("leading 必须为整数")
    if leading < 0:
        raise ValueError("leading 不能为负")
    out["leading"] = leading

    # items：六星序列，draw 严格递增
    raw_items = patch.get("items", [])
    if not isinstance(raw_items, list):
        raise ValueError("items 必须是数组")
    items: list = []
    last_draw = -1
    for it in raw_items:
        if not isinstance(it, dict):
            raise ValueError("items 项必须是对象")
        char = it.get("char")
        if not isinstance(char, str) or not char:
            raise ValueError("items 项缺少角色 id")
        try:
            draw = int(it.get("draw", 0))
        except (TypeError, ValueError):
            raise ValueError("draw 必须为整数")
        if draw <= 0:
            raise ValueError("draw 必须为正整数")
        if draw <= last_draw:
            raise ValueError("draw 必须严格递增")
        last_draw = draw
        items.append({"char": char, "draw": draw})
    out["items"] = items
    return out


def load_pool_patches(profile_id: str) -> Dict[str, dict]:
    """读取某账号的卡池补丁 {pool_id: patch}。文件缺失/损坏/账号无记录返回空 dict。

    单池结构非法时丢弃该池（其余保留）；整体文件损坏回退空。
    """
    path = _patches_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取 pool_patches 失败，用空补丁: {e}")
        return {}
    if not isinstance(data, dict):
        return {}
    raw_profile = data.get("pool_patches", data)
    if not isinstance(raw_profile, dict):
        return {}
    raw_for_acc = raw_profile.get(profile_id)
    if not isinstance(raw_for_acc, dict):
        return {}
    result: Dict[str, dict] = {}
    for pid, patch in raw_for_acc.items():
        try:
            result[str(pid)] = _validate_pool_patch(patch)
        except Exception as e:
            logger.warning(f"丢弃非法卡池补丁 {pid}: {e}")
    return result


def save_pool_patches(profile_id: str, patches: Dict[str, dict]) -> None:
    """写入某账号的卡池补丁；失败仅告警（不影响运行）。

    单个池补丁结构非法会抛 ValueError（由调用方在持久化前校验拒绝保存）。
    """
    for pid, patch in patches.items():
        _validate_pool_patch(patch)  # 保存前强制校验
    try:
        existing: Dict[str, dict] = {}
        if os.path.isfile(_patches_path()):
            with open(_patches_path(), "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        if not isinstance(existing, dict):
            existing = {}
        profiles = existing.get("pool_patches", existing)
        if not isinstance(profiles, dict):
            profiles = {}

        if not patches:
            profiles.pop(profile_id, None)
        else:
            profiles[profile_id] = {"%s" % pid: p for pid, p in patches.items()}

        with open(_patches_path(), "w", encoding="utf-8") as f:
            json.dump({"pool_patches": profiles}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写 pool_patches 失败（不影响运行）: {e}")


def delete_pool_patch(profile_id: str, pool_id: str) -> None:
    """删除某账号某池的补丁（还原该池）。"""
    patches = load_pool_patches(profile_id)
    if pool_id in patches:
        del patches[pool_id]
        save_pool_patches(profile_id, patches)