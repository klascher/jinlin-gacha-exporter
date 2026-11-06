"""角色词条库加载 - data/role-image.json（2026-10-13）。

角色专属词条库（库序 -> 词条名+档位）独立于 role.json 存放，避免每次词条库
更新都要动服务端权威角色数据；展示时与 role.json **联动**：role 出名字/星级，
role-image 出词条翻译（见 analysis/roster_capture.translate_entries）。

文件结构（由 example-key-dump-research 仓 材料/角色数据解析脚本/词条库全表.md（2026-10-18 迁移） 生成）：

    {
      "90000010": [ {"seq": 1, "name": "决胜", "grade": "S"}, ...共 12 条 ],
      ...
    }

缺失/损坏返回空 dict（不抛错）——对应角色词条回退 #序号 + 「词条库待更新」。
"""
import json
import os
from typing import Dict, List

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

ROLE_IMAGE_FILENAME = "role-image.json"


def role_image_path() -> str:
    """词条库文件路径（data/role-image.json）。"""
    return os.path.join(get_data_dir(), ROLE_IMAGE_FILENAME)


def load_role_image() -> Dict[str, List[dict]]:
    """读取角色词条库。文件缺失/损坏/条目非法时容错降级（不抛错）。

    单角色条目非法（非 list / 元素缺 seq）丢弃该角色（整库回退），
    元素 seq 非法丢弃该元素（部分回退）——词条库是纯展示翻译表，
    任何缺损都不值得让启动失败。
    """
    path = role_image_path()
    if not os.path.isfile(path):
        logger.info(f"词条库文件不存在（词条将显示 #序号）: {path}")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"词条库文件损坏，按无词条库处理: {path}: {e}")
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"词条库文件格式错误（顶层应为对象）: {path}")
        return {}
    out: Dict[str, List[dict]] = {}
    for cid, lib in raw.items():
        if not isinstance(lib, list):
            logger.warning(f"词条库条目非法（应为数组），丢弃角色 {cid}")
            continue
        entries: List[dict] = []
        for item in lib:
            if not isinstance(item, dict) or "seq" not in item:
                continue
            try:
                entries.append({
                    "seq": int(item["seq"]),
                    "name": str(item.get("name", "?")),
                    "grade": str(item.get("grade", "?")),
                })
            except (TypeError, ValueError):
                continue
        if entries:
            out[str(cid)] = entries
        else:
            logger.warning(f"词条库条目全非法，丢弃角色 {cid}")
    logger.info(f"词条库加载 {len(out)} 个角色: {path}")
    return out
