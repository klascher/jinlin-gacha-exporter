"""本地角色屏蔽 - data/char_shields.json 读写。

被屏蔽的陌生 item_id 在保底计算中按 4/5 星（非 6 星）处理，不触发归零。
仅对未在 role.json 收录的陌生 ID 生效；元数据收录该 ID 后真实星级自动覆盖，
屏蔽条目自动失效（无害）。跨账号全局，因为是角色维度而非账号维度。
"""
import json
import os
from typing import Set

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

SHIELDS_FILENAME = "char_shields.json"


def _shields_path() -> str:
    """屏蔽文件路径（与 exe 同级的 data/ 目录）。"""
    return os.path.join(get_data_dir(), SHIELDS_FILENAME)


def load_shields() -> Set[str]:
    """读取屏蔽 ID 集合。文件缺失/损坏返回空集（不抛错）。"""
    path = _shields_path()
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取 char_shields 失败，用空集: {e}")
        return set()
    # 兼容 {"shielded": [...]} 与裸数组两种写法
    if isinstance(data, dict):
        ids = data.get("shielded", [])
    elif isinstance(data, list):
        ids = data
    else:
        ids = []
    return {str(x) for x in ids if isinstance(x, (str, int))}


def save_shields(shielded: Set[str]) -> None:
    """写入屏蔽 ID 集合。失败仅告警（不影响运行）。"""
    try:
        with open(_shields_path(), "w", encoding="utf-8") as f:
            json.dump({"shielded": sorted(shielded)}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写 char_shields 失败（不影响运行）: {e}")
