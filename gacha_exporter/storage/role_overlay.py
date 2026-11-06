"""本地角色/卡池覆盖层（视觉识别确认结果）——data/role_overlay.json。

只存**远端 role.json / role_cache 里没有**的角色与卡池条目，玩家在「识别结果
确认」窗确认后写入；启动时在真实映射之上注入，远端后补同 id 时自动清理
（远端优先）。跨账号全局（id 的星级/池名是全局事实，不是账号数据）。

文件结构（version=1）::

    {
      "version": 1,
      "updated_at": "2026-10-21T12:00:00",
      "characters": {
        "90000024": {
          "name": "示例角色018", "rarity": 6, "limited": null,
          "source": {"kind": "vision", "page": 7, "total_page": 178,
                     "row": 2, "time": "...", "pool_id": "20009",
                     "new_tag": true, "ocr_name": "...", "confirmed_at": "..."}
        }
      },
      "pools": {
        "20009": {
          "name": "示例卡池001", "type": "UP", "rarity": 6,
          "up_character": "示例角色018", "pity_group": "shared:UP",
          "source": {"kind": "vision", "page": 7, "row": 1, "confirmed_at": "..."}
        }
      }
    }

边界：只写本机 ``data/``；不联网、不上传、不直接改远端 role.json。原始抽卡
历史永不修改——删除 overlay 条目即回到识别前的统计口径。
"""
import json
import os
import shutil
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..models import Character, Pool
from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

OVERLAY_FILENAME = "role_overlay.json"
OVERLAY_VERSION = 1

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def overlay_path() -> str:
    """覆盖层文件路径（全局单份）。"""
    return os.path.join(get_data_dir(), OVERLAY_FILENAME)


def _empty() -> dict:
    return {"version": OVERLAY_VERSION, "updated_at": "", "characters": {}, "pools": {}}


def load_overlay() -> dict:
    """读取覆盖层。文件缺失/损坏返回空结构（不抛错，不影响启动）。"""
    path = overlay_path()
    if not os.path.isfile(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取 role_overlay 失败，用空结构: {path}: {e}")
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data["version"] = OVERLAY_VERSION
    data.setdefault("updated_at", "")
    for key in ("characters", "pools"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def save_overlay(data: dict) -> bool:
    """原子写覆盖层：写临时文件 -> 备份旧文件为 .bak -> os.replace。

    先备份再替换；临时文件残留只影响本目录卫生，不影响正确性。
    返回是否写盘成功（失败只记日志，不抛错）。
    """
    path = overlay_path()
    tmp = path + ".tmp"
    try:
        data = dict(data)
        data["version"] = OVERLAY_VERSION
        data["updated_at"] = _now()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.path.isfile(path):
            try:
                shutil.copy2(path, path + ".bak")
            except OSError as e:
                logger.warning(f"备份 role_overlay 失败（继续写入）: {e}")
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning(f"写 role_overlay 失败（不影响运行）: {e}")
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _stamp_source(entry: dict) -> dict:
    """补齐来源元数据：kind、确认时间、页行锚点串 ``p{page}.r{row}``。"""
    entry = dict(entry)
    src = dict(entry.get("source") or {})
    src.setdefault("kind", "vision")
    page, row = src.get("page"), src.get("row")
    try:
        if page and row:
            # 锚点串保留「第几页第几行」的紧凑轨迹，供离线核对识别链路
            src.setdefault("anchor_trail", f"p{int(page)}.r{int(row)}")
    except (TypeError, ValueError):
        pass
    src["confirmed_at"] = _now()
    entry["source"] = src
    return entry


def _valid_character(item_id: str, entry: dict) -> bool:
    return (
        bool(item_id)
        and isinstance(entry, dict)
        and bool(str(entry.get("name") or "").strip())
        and entry.get("rarity") in (4, 5, 6)
    )


def _valid_pool(pool_id: str, entry: dict) -> bool:
    return (
        bool(pool_id)
        and isinstance(entry, dict)
        and bool(str(entry.get("name") or "").strip())
        and str(entry.get("type") or "").strip() != ""
    )


def upsert_character(item_id: str, entry: dict) -> bool:
    """写入/覆盖一条角色覆盖（确认窗「写入本地」逐条调用）。"""
    item_id = str(item_id)
    if not _valid_character(item_id, entry):
        logger.warning(f"拒绝写入非法角色覆盖: {item_id} {entry!r}")
        return False
    entry = _stamp_source(entry)
    with _LOCK:
        data = load_overlay()
        data["characters"][item_id] = entry
        return save_overlay(data)


def upsert_pool(pool_id: str, entry: dict) -> bool:
    """写入/覆盖一条卡池覆盖（确认窗「写入本地」逐条调用）。"""
    pool_id = str(pool_id)
    if not _valid_pool(pool_id, entry):
        logger.warning(f"拒绝写入非法卡池覆盖: {pool_id} {entry!r}")
        return False
    entry = _stamp_source(entry)
    with _LOCK:
        data = load_overlay()
        data["pools"][pool_id] = entry
        return save_overlay(data)


def remove_character(item_id: str) -> bool:
    """删除一条角色覆盖（可逆性入口）。返回是否确有删除。"""
    with _LOCK:
        data = load_overlay()
        if str(item_id) not in data["characters"]:
            return False
        del data["characters"][str(item_id)]
        save_overlay(data)
        return True


def remove_pool(pool_id: str) -> bool:
    """删除一条卡池覆盖（可逆性入口）。返回是否确有删除。"""
    with _LOCK:
        data = load_overlay()
        if str(pool_id) not in data["pools"]:
            return False
        del data["pools"][str(pool_id)]
        save_overlay(data)
        return True


def merge_into(
    characters: Dict[str, Character],
    pools: Dict[str, Pool],
) -> Tuple[int, int]:
    """把覆盖层注入生效映射，并清掉远端已收录的同 id 条目。

    - ``id in characters/pools``：远端（或缓存）已有 → 删除 overlay 条目，
      远端优先；有删除才写盘。
    - 其余合法条目：合成 Character/Pool 注入（调用方持有同一 dict 引用，
      统计/展示/导出全链生效）。
    - 非法条目跳过并记 warning，不阻断启动。

    Returns:
        (注入角色数, 注入卡池数)。
    """
    data = load_overlay()
    pruned_char = 0
    pruned_pool = 0
    added_char = 0
    added_pool = 0
    with _LOCK:
        for item_id in list(data["characters"]):
            if item_id in characters:
                data["characters"].pop(item_id, None)
                pruned_char += 1
                logger.info(f"覆盖层角色 {item_id} 已被远端收录，按远端优先清理")
                continue
            entry = data["characters"][item_id]
            if not _valid_character(item_id, entry):
                logger.warning(f"跳过非法角色覆盖条目: {item_id} {entry!r}")
                continue
            characters[item_id] = Character(
                id=item_id,
                name=str(entry["name"]).strip(),
                rarity=int(entry["rarity"]),
                limited=bool(entry.get("limited")),
            )
            added_char += 1
        for pool_id in list(data["pools"]):
            if pool_id in pools:
                data["pools"].pop(pool_id, None)
                pruned_pool += 1
                logger.info(f"覆盖层卡池 {pool_id} 已被远端收录，按远端优先清理")
                continue
            entry = data["pools"][pool_id]
            if not _valid_pool(pool_id, entry):
                logger.warning(f"跳过非法卡池覆盖条目: {pool_id} {entry!r}")
                continue
            pools[pool_id] = Pool(
                id=pool_id,
                name=str(entry["name"]).strip(),
                type=str(entry.get("type") or "未知"),
                order=int(entry.get("order") or 0),
                rarity=int(entry.get("rarity") or 0),
                up_character=str(entry.get("up_character") or ""),
                pity_group=str(entry.get("pity_group") or ""),
            )
            added_pool += 1
        if pruned_char or pruned_pool:
            save_overlay(data)
    if added_char or added_pool or pruned_char or pruned_pool:
        logger.info(
            f"覆盖层合并：注入角色 {added_char}、卡池 {added_pool}；"
            f"远端已收录清理角色 {pruned_char}、卡池 {pruned_pool}"
        )
    return added_char, added_pool


def list_entries() -> List[dict]:
    """列出覆盖层条目（确认窗/管理入口展示用），角色在前、卡池在后。"""
    data = load_overlay()
    out: List[dict] = []
    for item_id, entry in sorted(data["characters"].items()):
        out.append({"kind": "character", "id": item_id, **entry})
    for pool_id, entry in sorted(data["pools"].items()):
        out.append({"kind": "pool", "id": pool_id, **entry})
    return out
