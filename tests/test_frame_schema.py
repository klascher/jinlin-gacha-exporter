"""frame_schema 帧头解析测试 - 字节样本取自真实抓包样本。

样本来源：
- C->S 请求 / S->C 响应：capture_20260817_*.pcap（20260817 晚真实抓包）
- 长会话 seq>=127：capture_20260817_214949.pcap（seq 到 424，counter=seq%127）
- 心跳：C->S 9B / S->C 15B（所有 pcap 均有）
"""
from gacha_exporter.capture.frame_schema import (
    EXT_GACHA,
    EXT_HEARTBEAT,
    MSG_GACHA,
    MSG_HEARTBEAT,
    RESULT_REJECTED,
    SEQ_MOD,
    envelope_plausible,
    parse_header,
)

# seq=0x3a=58, counter=58, pool=0(全部卡池), offset=0（13B 请求）
_CS_REQUEST = bytes.fromhex("0000003a" "000c" "0800" "3a" "08" "00" "10" "00")
# seq=0x0129=297, counter=297%127=43=0x2b, pool=0, offset=0x234(564)? 实际样本
# 00000129 000c0800 2b 08 00 10 e704：offset=0x234？e7 04 = 0x263+... 按字节原样
_CS_REQUEST_LONG = bytes.fromhex("00000129" "000c" "0800" "2b" "080010e704")
# 心跳：seq=0x4f=79, counter=79（9B）
_CS_HEARTBEAT = bytes.fromhex("0000004f" "0001" "0200" "4f")
# 正常抽卡响应开头（103B 帧前 16B）：result=0, seq回显=0x64, ack=0x52
_SC_RESPONSE = bytes.fromhex("000c" "0800" "0000" "64" "52") + bytes.fromhex(
    "080010e3041a10"
)
# 被拒（限流）响应：result=142, seq回显=0x0a, ack=0x52（8B 整帧）
_SC_REJECTED = bytes.fromhex("000c" "0800" "008e" "0a" "52")
# 心跳响应（15B 整帧）：result=0, seq回显=0x4f, ack=0x67, 业务体=08 <ts varint>
_SC_HEARTBEAT = bytes.fromhex("0001" "0200" "0000" "4f" "67" "08add5ebff8034")
# 未知信封：类型 0x0002 不在白名单
_CS_UNKNOWN = bytes.fromhex("00000005" "0002" "0800" "05" "08" "00")
# 已知信封但 counter 不自洽（seq=0x3a, counter=0x3b）
_CS_BAD_COUNTER = bytes.fromhex("0000003a" "000c" "0800" "3b" "08" "00" "10" "00")


class TestParseHeaderCS:
    def test_request(self):
        h = parse_header(_CS_REQUEST, server_to_client=False)
        assert h is not None and h.known
        assert h.seq == 0x3A
        assert h.msg_type == MSG_GACHA
        assert h.ext_id == EXT_GACHA
        assert h.counter == 0x3A
        assert h.payload_offset == 9

    def test_long_session_seq_mod_127(self):
        """seq=297 时 counter=297%127=43（长会话回绕，非低字节）。"""
        h = parse_header(_CS_REQUEST_LONG, server_to_client=False)
        assert h is not None and h.known
        assert h.seq == 297
        assert h.counter == 297 % SEQ_MOD == 0x2B

    def test_heartbeat(self):
        h = parse_header(_CS_HEARTBEAT, server_to_client=False)
        assert h is not None and h.known
        assert h.msg_type == MSG_HEARTBEAT
        assert h.ext_id == EXT_HEARTBEAT
        assert h.seq == 0x4F and h.counter == 0x4F

    def test_unknown_envelope_not_rejected(self):
        """未知类型/extId 必须照常解析（known=False），不能判非法。"""
        h = parse_header(_CS_UNKNOWN, server_to_client=False)
        assert h is not None
        assert not h.known
        assert h.seq == 5 and h.counter == 5

    def test_counter_mismatch_marks_unknown(self):
        h = parse_header(_CS_BAD_COUNTER, server_to_client=False)
        assert h is not None
        assert not h.known

    def test_too_short(self):
        assert parse_header(b"\x00\x00\x00\x3a\x00\x0c\x08", False) is None


class TestParseHeaderSC:
    def test_response(self):
        h = parse_header(_SC_RESPONSE, server_to_client=True)
        assert h is not None and h.known
        assert h.msg_type == MSG_GACHA and h.ext_id == EXT_GACHA
        assert h.result == 0
        assert h.seq_echo == 0x64 and h.ack == 0x52
        assert h.payload_offset == 8

    def test_rejected(self):
        h = parse_header(_SC_REJECTED, server_to_client=True)
        assert h is not None and h.known
        assert h.result == RESULT_REJECTED
        assert h.seq_echo == 0x0A

    def test_heartbeat(self):
        h = parse_header(_SC_HEARTBEAT, server_to_client=True)
        assert h is not None and h.known
        assert h.msg_type == MSG_HEARTBEAT and h.ext_id == EXT_HEARTBEAT
        assert h.result == 0 and h.seq_echo == 0x4F and h.ack == 0x67

    def test_too_short(self):
        assert parse_header(b"\x00\x0c\x08\x00\x00\x00\x64", True) is None


class TestEnvelopePlausible:
    def test_known(self):
        assert envelope_plausible(_CS_REQUEST, False)
        assert envelope_plausible(_SC_RESPONSE, True)
        assert envelope_plausible(_CS_HEARTBEAT, False)
        assert envelope_plausible(_SC_HEARTBEAT, True)

    def test_unknown(self):
        assert not envelope_plausible(_CS_UNKNOWN, False)
        assert not envelope_plausible(_CS_BAD_COUNTER, False)

    def test_short_body(self):
        assert not envelope_plausible(b"\x00\x0c", True)
