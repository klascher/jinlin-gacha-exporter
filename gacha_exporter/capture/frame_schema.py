"""帧结构层 - 按已验证的线上帧结构解析帧头（2026-08 结构化切帧重构）。

帧结构由 7 个真实 pcap（C->S 1085 帧 / S->C 1074 帧）逐字节核对确定：

    帧 = [4B totalLength 大端] + 帧体（长度头由切帧层消费，本模块只看帧体）
    C->S 帧体 = [4B seq 大端][2B 类型][2B extId][1B counter][protobuf 业务体]
    S->C 帧体 = [2B 类型][2B extId][2B result][1B seq回显][1B ack][protobuf 业务体]

counter / seq 回显 = seq % SEQ_MOD（127，非低字节：seq 127->0x00、128->0x01、
298->0x2c 逐帧验证；短会话 seq<127 时与低字节相等所以此前未暴露）。

设计约束：
- 类型/extId 白名单外**不判非法**（known=False 照常返回）--以实际观测到的
  消息类型为准，未知消息类型必须能按长度正常切帧放行，只是不解析；
- 白名单仅用于失步重对齐评分（envelope_plausible）与业务分派，帧边界永远由
  4B 长度头决定。
"""
from dataclasses import dataclass
from typing import Optional

# 消息类型（帧体 2B 大端）
MSG_GACHA = 0x000C      # 抽卡记录请求/响应
MSG_HEARTBEAT = 0x0001  # 心跳（C->S 9B / S->C 15B）

# 扩展 ID / 模块分类（帧体 2B 大端）
EXT_GACHA = 0x0800
EXT_HEARTBEAT = 0x0200

# S->C result（2B 大端）
RESULT_OK = 0
RESULT_REJECTED = 142  # 0x008e：请求被限流拒绝（无业务体）

# counter / seq 回显的回绕模数
SEQ_MOD = 127

# 已知 (类型, extId) 组合
_KNOWN_ENVELOPES = {(MSG_GACHA, EXT_GACHA), (MSG_HEARTBEAT, EXT_HEARTBEAT)}


@dataclass(frozen=True)
class FrameHeader:
    """帧头解析结果（不含 4B 长度头与业务体）。

    seq/counter 仅 C->S 有；result/ack 仅 S->C 有。known=False 表示类型/extId
    不在已知白名单（帧本身仍合法，只是我们不解析其业务体）。
    """

    server_to_client: bool
    msg_type: int
    ext_id: int
    known: bool
    payload_offset: int  # 业务体（protobuf）在帧体内的起始偏移
    seq: int = 0        # C->S：完整 4B 大端 seq（心跳/抽卡共享计数器）
    counter: int = -1   # C->S：seq % SEQ_MOD 回显（-1 表示 S->C 无此字段）
    result: int = -1    # S->C：0 成功 / 142 被拒（-1 表示 C->S 无此字段）
    ack: int = -1       # S->C：应答字节（语义未完全确认，仅透传）
    seq_echo: int = -1  # S->C：请求 seq % SEQ_MOD 的回显


def parse_header(body: bytes, server_to_client: bool) -> Optional[FrameHeader]:
    """解析帧头。帧体过短/结构不自洽返回 None。

    Args:
        body: 帧体（不含 4B 长度头）
        server_to_client: True 为 S->C 布局，False 为 C->S 布局
    """
    if server_to_client:
        # S->C：[类型2][extId2][result2][seq回显1][ack1][业务体]
        if len(body) < 8:
            return None
        msg_type = int.from_bytes(body[0:2], "big")
        ext_id = int.from_bytes(body[2:4], "big")
        result = int.from_bytes(body[4:6], "big")
        return FrameHeader(
            server_to_client=True,
            msg_type=msg_type,
            ext_id=ext_id,
            known=(msg_type, ext_id) in _KNOWN_ENVELOPES,
            result=result,
            seq_echo=body[6],
            ack=body[7],
            payload_offset=8,
        )
    # C->S：[seq4][类型2][extId2][counter1][业务体]
    if len(body) < 9:
        return None
    seq = int.from_bytes(body[0:4], "big")
    msg_type = int.from_bytes(body[4:6], "big")
    ext_id = int.from_bytes(body[6:8], "big")
    counter = body[8]
    known = (msg_type, ext_id) in _KNOWN_ENVELOPES
    if known and counter != seq % SEQ_MOD:
        # 已知类型但 counter 不自洽 -> 大概率不是帧边界（错位/脏数据）
        known = False
    return FrameHeader(
        server_to_client=False,
        msg_type=msg_type,
        ext_id=ext_id,
        known=known,
        seq=seq,
        counter=counter,
        payload_offset=9,
    )


def envelope_plausible(body: bytes, server_to_client: bool) -> bool:
    """帧体开头是否像指定方向的一个合法帧头（仅看已知信封）。

    供切帧失步重对齐扫描评分用：字节尚不足以读出完整头时返回 False
    （调用方按半包处理，不据此跳字节）。
    """
    header = parse_header(body, server_to_client)
    return header is not None and header.known
