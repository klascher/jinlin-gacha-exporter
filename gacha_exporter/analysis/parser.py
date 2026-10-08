"""Protobuf 抽卡记录解析 - 按已验证帧结构顺序读字段（2026-08 结构化重构）。

响应帧结构见 capture.frame_schema（抓包样本逐字节核对）：
S->C 抽卡响应 = 10B 固定头（类型/extId/result/seq回显/ack，含 4B 长度头）
+ 业务体 `[08 offset回显][10 total][1a 记录]...`。本模块从业务体起按字段
顺序解析（field1/field2 跳过、field3+ 为记录，记录 = 08 pool / 10 item /
18 timestamp，字段序号由抓包样本逐字节回归推断），替代此前的
全帧 0x1a 扫描。不依赖 blackboxprotobuf，直接按 varint 解码。

非抽卡 S->C 帧（心跳等）/被拒响应/结构不符帧直接返回空列表。
"""
from datetime import datetime
from typing import List, Optional, Tuple

from ..capture.frame_schema import MSG_GACHA, RESULT_OK, parse_header
from ..models import GachaRecord
from ..utils.logger import get_logger
from .pool_view import parse_pool_response

logger = get_logger(__name__)


def _read_varint(data: bytes, offset: int):
    """读取 protobuf varint。

    Returns:
        (value, next_offset)
    """
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if (byte & 0x80) == 0:
            break
        shift += 7
    return result, offset


def _parse_record_fields(record_data: bytes) -> Optional[Tuple[str, str, int]]:
    """解析一条记录的三个 varint 字段：1=pool_id / 2=item_id / 3=timestamp。

    Returns:
        (pool_id, item_id, timestamp) 或 None（字段不全/结构不符）
    """
    pool_id = ""
    item_id = ""
    timestamp = 0
    j = 0
    while j < len(record_data):
        tag = record_data[j]
        field_num = tag >> 3
        wire_type = tag & 0x07
        j += 1
        if wire_type != 0:
            break
        val, j = _read_varint(record_data, j)
        if field_num == 1:
            pool_id = str(val)
        elif field_num == 2:
            item_id = str(val)
        elif field_num == 3:
            timestamp = val
    if pool_id and item_id and timestamp:
        return pool_id, item_id, timestamp
    return None


def extract_records(raw: bytes, batch_seq: int = 0) -> List[GachaRecord]:
    """解析一帧 S->C 抽卡响应，按字段顺序提取记录（纯函数，无递增/日志副作用）。

    batch_seq/position_in_batch 为解析辅助字段（不参与去重），语义约定
    「越小越新」（stats.shared_pity_tracking 冻结排序的 tiebreaker 依赖此约定）。
    优先用响应 field1 的 offset 回显归一化为方向无关：batch_seq=0（常量）、
    position_in_batch=offset+帧内序号（视图内绝对序号，0=最新一条）。
    否则回退帧序赋值（batch_seq 越小越新仅当用户「新->旧」翻页时成立，
    「旧->新」翻页会得到镜像序，见 2026-08-18 早期修复 案例）。

    Args:
        raw: 帧体（不含 4 字节长度头）
        batch_seq: 批次序号（仅 offset 回显缺失的回退路径写入 batch_seq 字段）

    Returns:
        GachaRecord 列表（按帧内顺序）
    """
    header = parse_header(raw, server_to_client=True)
    if header is None or not header.known or header.msg_type != MSG_GACHA:
        # 非抽卡帧（心跳/未知模块）或结构不符：无记录可提取
        return []
    if header.result != RESULT_OK:
        # 被拒（限流）等非成功响应：无业务体
        return []
    resp = parse_pool_response(raw)
    page_offset = resp[1] if resp is not None else None
    payload = raw[header.payload_offset:]
    records: List[GachaRecord] = []
    i = 0
    while i < len(payload):
        tag = payload[i]
        field_num = tag >> 3
        wire_type = tag & 0x07
        i += 1
        if field_num == 3 and wire_type == 2:
            # 记录字段（repeated）：长度前缀 + 子消息
            length, i = _read_varint(payload, i)
            if i + length > len(payload):
                break
            parsed = _parse_record_fields(payload[i : i + length])
            if parsed is not None:
                pool_id, item_id, timestamp = parsed
                records.append(
                    GachaRecord(
                        pool_id=pool_id,
                        item_id=item_id,
                        timestamp=timestamp,
                        batch_seq=0 if page_offset is not None else batch_seq,
                        position_in_batch=(
                            page_offset + len(records)
                            if page_offset is not None
                            else len(records)
                        ),
                    )
                )
            i += length
        elif wire_type == 0:
            # field1(offset回显)/field2(total) 等已知 varint 字段：跳过
            _, i = _read_varint(payload, i)
        elif wire_type == 2:
            # 其他长度前缀字段：跳过内容
            length, i = _read_varint(payload, i)
            i += length
        else:
            # 不认识的 wire type：停止解析（保留已提取记录，与旧扫描一致）
            break
    return records


def looks_like_gacha(frame: bytes) -> bool:
    """判定一帧是否像抽卡响应（含至少 1 条可解析记录）。供地址发现用。"""
    return len(extract_records(frame)) > 0


class GachaParser:
    """抽卡协议解析器。"""

    def __init__(self):
        """初始化解析器。"""
        self._batch_seq = 0

    def parse(self, raw: bytes) -> List[GachaRecord]:
        """解析一帧原始数据，返回抽卡记录列表。

        Args:
            raw: 抓包得到的 protobuf 帧内容（不含 4 字节长度头）

        Returns:
            GachaRecord 列表（按帧内顺序）
        """
        records = extract_records(raw, self._batch_seq)
        found = len(records)
        if found:
            from datetime import datetime
            detail_parts = []
            for r in records:
                dt = datetime.fromtimestamp(r.timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
                detail_parts.append(f"  pool={r.pool_id} item={r.item_id} ts={r.timestamp}({dt})")
            logger.info(f"解析到 {found} 条记录 (batch_seq={self._batch_seq}):")
            for part in detail_parts:
                logger.info(part)
        else:
            logger.info(f"本帧未解析到记录, raw_len={len(raw)}")
        self._batch_seq += 1
        return records
