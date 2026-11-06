"""角色练度帧采集 - 只读旁路解析器（2026-10-13，设计稿 §4）。

来源帧与字段口径（角色数据解析指南.md §3/§3.5/§3.6/§6，均为事件驱动推送）：

- ``0x0038`` 角色概要（全部角色翻页触发，逐页全量可达）：
  外层仅一个 F1 blob；内层 F1=角色 cfg id、F2×3=映像三等级、F3×N=词条库序号多重集。
- ``000c/0301`` 英雄全量块（升级/叠卡/洗练后推送）：F1=角色、F2=等级、F4=叠卡。
  payload 开头有 6-7B 变长前缀 + ``0a`` + LEN，**LEN 声明超冲约 27%**（服务器裁剪，
  已逐字节核验），不能按声明消费——用「定位 ``08 <cid>`` 起点 + 宽容遍历」绕开。
- ``000c/0300`` 技能更新（升级瞬间推新值非差值）：F1=角色、F2=技能复合 id
  （``500_XX_槽号`` 命名空间）、F3=新等级；槽号 = 复合 id % 1000
  （501=普攻 / 101=被动 / 201=终结技）。
- ``000c/0900`` 玩家信息（登录/切号推送）：F9 重复组 = 英雄概要（F1=角色、F2=等级）。

词条翻译：库序 → 名称+档位 查 role.json 该角色的 ``lib`` 字段（服务端随 role.json
下发，客户端零发版更新）；lib 缺失时回退 ``#序号`` 占位，展示层提示「词条库待更新」。

容错硬约束（设计稿 §4.3）：任何一帧解析失败只记日志，绝不影响抓包主流程；
未知帧型直接放行不碰。
"""
from typing import Callable, Dict, List, Optional, Tuple

from ..capture.frame_schema import (
    EXT_HERO_FULL,
    EXT_PLAYER_INFO,
    EXT_SKILL_UPDATE,
    MSG_GACHA,
    MSG_HERO_SUMMARY,
    parse_header,
)
from ..storage import roster
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 角色 cfg id 的合理区间（1300xxxx，全部样本 8 位）
_CID_MIN, _CID_MAX = 10000000, 99999999


def _read_varint(buf: bytes, i: int) -> Tuple[Optional[int], int]:
    """LEB128 varint。越界/超长返回 (None, i)，调用方据此中止。"""
    v, s = 0, 0
    while i < len(buf) and s <= 63:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << s
        if not b & 0x80:
            return v, i
        s += 7
    return None, i


def _find_hero_block(payload: bytes) -> int:
    """在 0301/0300 payload 前缀之后定位内层 ``08 <cid varint>`` 的 tag 偏移。

    前缀为 2-3 个 varint + ``0a`` + 超冲 LEN，长度不定；tag 0x08 后紧跟的
    varint 解码到合理 cid 区间即认为锚定。找不到返回 -1。
    """
    for i in range(min(len(payload) - 1, 16)):
        if payload[i] != 0x08:
            continue
        cid, _ = _read_varint(payload, i + 1)
        if cid is not None and _CID_MIN <= cid <= _CID_MAX:
            return i  # 返回 tag 偏移（调用方从 tag 起遍历）
    return -1


def _collect_varints(buf: bytes, i: int) -> Dict[int, List[object]]:
    """从 i 起宽容遍历 protobuf，收集各字段号取值（varint 与 blob 均收）。

    遇到结构不自洽（LEN 越界——0301 的 F7 超冲 LEN 正是如此、非法 wiretype、
    varint 截断）即停，已收集的字段照常返回。重复字段号按出现顺序保留。
    """
    out: Dict[int, List[object]] = {}
    while i < len(buf):
        key, j = _read_varint(buf, i)
        if key is None:
            break
        fno, wt = key >> 3, key & 7
        if fno == 0 or wt not in (0, 1, 2, 5):
            break
        if wt == 0:
            v, j2 = _read_varint(buf, j)
            if v is None:
                break
            out.setdefault(fno, []).append(v)
            i = j2
        elif wt == 2:
            ln, j2 = _read_varint(buf, j)
            if ln is None or j2 + ln > len(buf):
                break
            out.setdefault(fno, []).append(buf[j2:j2 + ln])
            i = j2 + ln
        else:  # wt 1 = fixed64 / wt 5 = fixed32，跳定长
            size = 8 if wt == 1 else 4
            if j + size > len(buf):
                break
            i = j + size
    return out


def _first_int(vals: List[object]) -> Optional[int]:
    for v in vals:
        if isinstance(v, int):
            return v
    return None


def parse_summary(payload: bytes) -> Optional[Dict[str, object]]:
    """``0x0038`` 概要帧 → {cid, image, entries}；无有效内层返回 None。

    image 缺失为 None（未推）；entries 恒为 list（帧到达即视为真实数据，
    空 = 无词条——设计稿 §9 null 公理）。
    """
    outer = _collect_varints(payload, 0)
    blob = next((v for v in outer.get(1, []) if isinstance(v, bytes)), None)
    if blob is None:
        return None
    inner = _collect_varints(blob, 0)
    cid = _first_int(inner.get(1, []))
    if cid is None or not _CID_MIN <= cid <= _CID_MAX:
        return None
    image = [v for v in inner.get(2, []) if isinstance(v, int)]
    entries = [v for v in inner.get(3, []) if isinstance(v, int)]
    return {"cid": str(cid), "image": image or None, "entries": entries}


def parse_hero_full(payload: bytes) -> Optional[Dict[str, object]]:
    """``000c/0301`` 全量块 → {cid, level, stack}（缺的字段不进 dict）。"""
    start = _find_hero_block(payload)
    if start < 0:
        return None
    fields = _collect_varints(payload, start)
    cid = _first_int(fields.get(1, []))
    if cid is None or not _CID_MIN <= cid <= _CID_MAX:
        return None
    out: Dict[str, object] = {"cid": str(cid)}
    level = _first_int(fields.get(2, []))
    if level is not None:
        out["level"] = level
    stack = _first_int(fields.get(4, []))
    if stack is not None:
        out["stack"] = stack
    return out


def parse_skill_update(payload: bytes) -> Optional[Dict[str, object]]:
    """``000c/0300`` 技能更新 → {cid, skills: {槽号: 等级}}；无法定位返回 None。"""
    start = _find_hero_block(payload)
    if start < 0:
        return None
    fields = _collect_varints(payload, start)
    cid = _first_int(fields.get(1, []))
    skill_id = _first_int(fields.get(2, []))
    level = _first_int(fields.get(3, []))
    if cid is None or not _CID_MIN <= cid <= _CID_MAX or skill_id is None or level is None:
        return None
    return {"cid": str(cid), "skills": {str(skill_id % 1000): level}}


def parse_player_info(payload: bytes) -> List[Dict[str, object]]:
    """``000c/0900`` 玩家信息 → [{cid, level}, ...]（F9 重复组，无组返回 []）。"""
    heroes: List[Dict[str, object]] = []
    i = 0
    while i < len(payload):
        key, j = _read_varint(payload, i)
        if key is None:
            break
        fno, wt = key >> 3, key & 7
        if fno == 0 or wt not in (0, 1, 2, 5):
            break
        if wt == 0:
            v, i = _read_varint(payload, j)
            if v is None:
                break
            continue
        if wt == 2:
            ln, j2 = _read_varint(payload, j)
            if ln is None or j2 + ln > len(payload):
                break
            if fno == 9:
                group = _collect_varints(payload[j2:j2 + ln], 0)
                cid = _first_int(group.get(1, []))
                level = _first_int(group.get(2, []))
                if cid is not None and _CID_MIN <= cid <= _CID_MAX:
                    hero: Dict[str, object] = {"cid": str(cid)}
                    if level is not None:
                        hero["level"] = level
                    heroes.append(hero)
            i = j2 + ln
        else:
            size = 8 if wt == 1 else 4
            if j + size > len(payload):
                break
            i = j + size
    return heroes


def translate_entries(seqs: List[int], lib: Optional[List[dict]]) -> List[str]:
    """词条库序号 → 「名称+档位」列表（按序号升序）。

    lib 为 role.json 该角色的 ``lib`` 字段，元素形如
    ``{"seq": 1, "name": "决胜", "grade": "S"}``；lib 缺失/该序号不在库里
    回退 ``#序号``（展示层对整库缺失提示「词条库待更新」）。
    """
    table: Dict[int, Tuple[str, str]] = {}
    for item in lib or []:
        if not isinstance(item, dict):
            continue
        try:
            table[int(item.get("seq"))] = (str(item.get("name", "?")), str(item.get("grade", "?")))
        except (TypeError, ValueError):
            continue
    out = []
    for s in sorted(seqs):
        name, grade = table.get(s, (f"#{s}", ""))
        out.append(f"{name}{grade}")
    return out


class RosterCapture:
    """练度帧观察者：feed 一帧 S->C 帧体，解析到练度字段即合并落盘。

    只读旁路：不注册进业务分派、不改主解析链；每帧独立吞异常（仅日志），
    保证练度采集的任何问题都不影响抽卡抓包主流程。
    """

    def __init__(self, profile_id: str, on_updated: Optional[Callable[[], None]] = None):
        self.profile_id = profile_id
        self.on_updated = on_updated

    def _notify(self) -> None:
        if self.on_updated is None:
            return
        try:
            self.on_updated()
        except Exception as e:  # noqa: BLE001 - UI 回调异常同样不外泄
            logger.warning(f"练度更新回调异常（忽略）: {e}")

    def feed(self, frame: bytes) -> None:
        """喂一帧完整 S->C 帧体（含 8B 帧头）。任何异常只记日志。"""
        try:
            self._feed(frame)
        except Exception as e:  # noqa: BLE001 - 容错降级是硬约束
            logger.warning(f"练度帧解析异常（忽略该帧）: {e}")

    def _feed(self, frame: bytes) -> None:
        header = parse_header(frame, server_to_client=True)
        if header is None:
            return
        payload = frame[header.payload_offset:]
        changed = False
        if header.msg_type == MSG_HERO_SUMMARY:
            parsed = parse_summary(payload)
            if parsed is None:
                return
            fields: Dict[str, object] = {"entries": parsed["entries"]}
            if parsed.get("image") is not None:
                fields["image"] = parsed["image"]
            changed = self._merge(parsed["cid"], fields)
        elif header.msg_type == MSG_GACHA and header.ext_id == EXT_HERO_FULL:
            parsed = parse_hero_full(payload)
            if parsed is None:
                return
            cid = parsed.pop("cid")
            changed = self._merge(cid, parsed)
        elif header.msg_type == MSG_GACHA and header.ext_id == EXT_SKILL_UPDATE:
            parsed = parse_skill_update(payload)
            if parsed is None:
                return
            cid = parsed.pop("cid")
            changed = self._merge(cid, parsed)
        elif header.msg_type == MSG_GACHA and header.ext_id == EXT_PLAYER_INFO:
            # 0900 一帧带几十个角色概要：整帧合并完只回调一次，避免连环刷 UI
            for hero in parse_player_info(payload):
                cid = hero.pop("cid")
                if self._merge(cid, hero):
                    changed = True
        if changed:
            self._notify()

    def _merge(self, cid: str, fields: Dict[str, object]) -> bool:
        if not fields:
            return False
        return roster.merge_character_fields(self.profile_id, cid, fields)
