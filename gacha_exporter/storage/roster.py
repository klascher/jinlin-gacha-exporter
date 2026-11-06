"""角色练度存储 - data/roster_<账号id>.json 读写与字段级合并（2026-10-13）。

每个账号一份练度文件（与 users/<id>.json 同 id 命名口径），结构：

    {
      "updated_at": "ISO 时刻（最近一次任一字段落盘）",
      "characters": {
        "90000010": {
          "level": 100,                  # 等级（null=未抓到）
          "stack": 0,                    # 叠卡（null=未抓到）
          "skills": {"501": 5, "101": 7},# 技能槽号->等级（槽号 501=普攻/101=被动/201=终结技）
          "image": [15, 15, 15],         # 映像三部分等级（[0,0,0] 是真实数据）
          "entries": [1, 2, 7],          # 词条库序多重集（原始序号，翻译在展示层——
                                         #   role.json lib 更新后旧数据无需重抓即生效）
          "sources": {                   # 各字段来源时刻（ISO），新值覆盖旧值的判据
            "level": "2026-10-13T09:00:00", ...
          }
        }
      },
      "ui_state": {"checked": [...], "display": "all", "star": "6"}   # 窗口状态，随账号记忆
    }

字段语义公理（设计稿 §9）：null = 没抓到（展示「—」）；空值/0 = 抓到的真实数据原样展示。
合并语义：同字段按 sources 时间戳「新覆盖旧」，缺失时间戳视为最旧；
skills 例外做键级并集（0300 按槽增量推送，一次只带一个技能，整组覆盖会丢其它槽）。
文件缺失/损坏回退空结构，不抛错；写盘失败仅告警（不影响抓包主流程）。
"""
import json
import os
import threading
from datetime import datetime
from typing import Dict, Optional

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

# 练度四项 + 词条：roster 里允许出现的字段名
ROSTER_FIELDS = ("level", "stack", "skills", "image", "entries")

_LOCK = threading.Lock()


def roster_path(profile_id: str) -> str:
    """某账号的练度文件路径。"""
    return os.path.join(get_data_dir(), f"roster_{profile_id}.json")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_roster(profile_id: str) -> dict:
    """读取账号练度。文件缺失/损坏返回空结构（不抛错）。"""
    path = roster_path(profile_id)
    if not os.path.isfile(path):
        return {"updated_at": "", "characters": {}, "ui_state": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取练度文件失败，用空结构: {path}: {e}")
        return {"updated_at": "", "characters": {}, "ui_state": {}}
    if not isinstance(data, dict):
        return {"updated_at": "", "characters": {}, "ui_state": {}}
    data.setdefault("updated_at", "")
    chars = data.setdefault("characters", {})
    if not isinstance(chars, dict):
        data["characters"] = {}
    data.setdefault("ui_state", {})
    return data


def save_roster(profile_id: str, data: dict) -> None:
    """整体写盘（调用方已持有合并逻辑）。失败仅告警。"""
    try:
        with open(roster_path(profile_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写练度文件失败（不影响运行）: {e}")


def _ts_key(iso: str) -> str:
    """时间戳比较键：缺失/非法视为最旧（空串 < 任何非空 ISO）。"""
    if not iso:
        return ""
    try:
        datetime.fromisoformat(iso)
        return iso
    except (ValueError, TypeError):
        return ""


def merge_character_fields(
    profile_id: str,
    cid: str,
    fields: Dict[str, object],
    source_ts: Optional[str] = None,
) -> bool:
    """把一批练度字段合并进某角色记录（字段级「新覆盖旧」）。

    Args:
        profile_id: 账号 id
        cid: 角色 cfg id（字符串）
        fields: {字段名: 新值}，只接受 ROSTER_FIELDS 里的键；值为 None 不写入
        source_ts: 本批数据的来源时刻（ISO），缺省取当前时刻

    Returns:
        是否有实际变化（有变化才写盘）。
    """
    ts = _now() if source_ts is None else source_ts
    clean = {k: v for k, v in fields.items() if k in ROSTER_FIELDS and v is not None}
    if not clean:
        return False
    with _LOCK:
        data = load_roster(profile_id)
        chars = data["characters"]
        rec = chars.setdefault(str(cid), {})
        sources = rec.setdefault("sources", {})
        changed = False
        for key, new_val in clean.items():
            old_ts = _ts_key(sources.get(key, ""))
            if _ts_key(ts) < old_ts:
                continue  # 旧数据，跳过
            if key == "skills" and isinstance(rec.get(key), dict) and isinstance(new_val, dict):
                new_val = {**rec[key], **new_val}  # 技能按槽增量（0300 一次一带）
            if rec.get(key) != new_val:
                rec[key] = new_val
                changed = True
            if sources.get(key) != ts:
                sources[key] = ts
                changed = True
        if changed:
            data["updated_at"] = ts
            save_roster(profile_id, data)
            logger.info(f"练度更新: 账号={profile_id} 角色={cid} 字段={sorted(clean)}")
        return changed


def load_ui_state(profile_id: str) -> dict:
    """读取窗口 UI 状态（勾选集/显示档位/星级筛选）。"""
    return load_roster(profile_id).get("ui_state", {}) or {}


def save_ui_state(profile_id: str, ui_state: dict) -> None:
    """写窗口 UI 状态（与练度数据同一文件，读-改-写按锁互斥）。"""
    with _LOCK:
        data = load_roster(profile_id)
        data["ui_state"] = ui_state
        save_roster(profile_id, data)
