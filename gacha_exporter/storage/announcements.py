"""公告已读状态 - data/announcements_read.json 读写。

记录用户点过“不再提示”的公告 id，下次启动不再弹这些公告。跨账号全局
（公告与账号无关，角色维度而非账号维度）。文件缺失/损坏回退空集，不抛错。
"""
import json
import os
from typing import Set

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

READ_FILENAME = "announcements_read.json"


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
    read_ids = load_read_ids()
    read_ids.add(str(aid))
    _save_read_ids(read_ids)
