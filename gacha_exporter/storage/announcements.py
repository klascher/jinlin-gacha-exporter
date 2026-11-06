"""公告已读状态 - data/announcements_read.json 读写。

记录用户点过“不再提示”的公告 id，下次启动不再弹这些公告。跨账号全局
（公告与账号无关，角色维度而非账号维度）。文件缺失/损坏回退空集，不抛错。
"""
import json
import os
import threading
from typing import Set

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

READ_FILENAME = "announcements_read.json"

# mark_read（GUI 线程）与 cleanup_read_ids（启动后台线程）都是读-改-写同一文件，
# 用模块级锁互斥，避免并发写互相覆盖。
_READ_LOCK = threading.Lock()


def _read_path() -> str:
    """已读文件路径（与 exe 同级的 data/ 目录）。"""
    return os.path.join(get_data_dir(), READ_FILENAME)


def load_read_ids() -> Set[str]:
    """读取已读公告 id 集合。文件缺失/损坏返回空集（不抛错）。

    兼容 {"read": [...]} 标准写法与裸数组 [...]；数字 id 强转字符串。
    """
    path = _read_path()
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取 announcements_read 失败，用空集: {e}")
        return set()
    if isinstance(data, dict):
        ids = data.get("read", [])
    elif isinstance(data, list):
        ids = data
    else:
        ids = []
    return {str(x) for x in ids if isinstance(x, (str, int))}


def _save_read_ids(read_ids: Set[str]) -> None:
    """写入已读 id 集合（排序保证 diff 稳定）。失败仅告警。"""
    try:
        with open(_read_path(), "w", encoding="utf-8") as f:
            json.dump({"read": sorted(read_ids)}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写 announcements_read 失败（不影响运行）: {e}")


def mark_read(aid: str) -> None:
    """标记单个公告为已读（不再提示）。去重后持久化。"""
    with _READ_LOCK:
        read_ids = load_read_ids()
        read_ids.add(str(aid))
        _save_read_ids(read_ids)


def cleanup_read_ids(live_ids: Set[str]) -> int:
    """启动清理：已读记录里「公告列表已不存在」的 id 清掉，返回清除数。

    - live_ids 为空集时直接跳过（load_announcements 失败与真·空列表无法区分，
      贸然清会把全部已读记录误删）；
    - 只在确有失效条目时写盘，无变化零写入；
    - 后台静默执行，仅日志，无用户可见反馈。
    """
    live = {str(x) for x in live_ids}
    if not live:
        return 0
    with _READ_LOCK:
        read_ids = load_read_ids()
        stale = read_ids - live
        if not stale:
            return 0
        _save_read_ids(read_ids - stale)
    logger.info(f"公告清理：已移除 {len(stale)} 条失效已读记录（公告已删除）: {sorted(stale)}")
    return len(stale)
