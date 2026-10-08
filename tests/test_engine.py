"""抓包引擎 extract_frames 回归测试 -- 短帧（心跳/被拒响应）处理。

两层行为都要锁定：
1. 流对齐：任何短帧/越界帧不得破坏流对齐（曾出现的病根：对 <min_frame_len
   的短帧只前进 1 字节，一条 9 字节心跳帧出现后整条 C->S 流错位，后续切池/
   翻页请求帧全被吞进缓冲，视图配对失效、跨卡池去重失效、同批记录重复计入）。
2. 帧送达：已知合法短帧必须送到回调而不是被当非法帧丢弃（生产
   min_frame_len=8：9 字节心跳与 8 字节被拒响应都在范围内。曾设 10 导致
   被拒响应整帧被丢、note_response 永远收不到，见 models.CaptureConfig 注释）。
"""
import struct

from gacha_exporter.capture.engine import extract_frames

_MIN, _MAX = 8, 50000  # 与 CaptureConfig 缺省一致


def _frame(body: bytes) -> bytes:
    """按协议给一帧加 4 字节大端长度头。"""
    return struct.pack(">I", len(body)) + body


def test_short_frame_between_requests_keeps_stream_aligned():
    """9 字节心跳帧夹在两个池请求之间：正常送达，后续请求必须完整切出。"""
    req1 = bytes.fromhex("0000003a000c08003a08001000")   # 全部 offset=0
    heartbeat = bytes.fromhex("000000450001020045")      # 9 字节心跳帧
    req2 = bytes.fromhex("00000046000c08004608001037")   # 全部 offset=55
    data = _frame(req1) + _frame(heartbeat) + _frame(req2)

    frames, remaining = extract_frames(data, _MIN, _MAX)
    assert remaining == b""
    # 心跳帧必须送达（pool_view 按信封自然忽略），三个帧都在
    assert frames == [req1, heartbeat, req2]


def test_rejected_8byte_frame_delivered():
    """8 字节被拒响应（限流截断）必须送达回调供 note_response 识别。

    曾设 min_frame_len=10 时该帧被整帧丢弃，「频繁丢页无感知」的修复在
    上游失灵（replay_check.py 对真实 pcap 字节对账定位）。
    """
    rec = bytes.fromhex("000c08000000003a080010001a0508b4ea011000")
    rejected8 = bytes.fromhex("000c080000003d8e00")       # 8 字节截断
    data = _frame(rec) + _frame(rejected8)

    frames, remaining = extract_frames(data, _MIN, _MAX)
    assert remaining == b""
    assert frames == [rec, rejected8]


def test_out_of_range_frame_skipped_whole_keeps_alignment():
    """真越界帧（< min_len）整帧在则整帧跳过，保持流对齐（9068e5d 修复本体）。"""
    req = bytes.fromhex("0000003a000c08003a08001000")
    tiny = b"\x00\x00\x00\x00"   # 4 字节体 < min_len=8，越界
    data = _frame(tiny) + _frame(req)

    frames, remaining = extract_frames(data, _MIN, _MAX)
    assert remaining == b""
    assert frames == [req], "越界帧跳过后，后续合法帧必须完整切出"


def test_garbage_advances_one_byte_to_resync():
    """真正的错位字节仍走 1 字节重对齐路径，最终能找回合法帧。"""
    good = bytes.fromhex("0000003a000c08003a08001000")
    data = b"\x00\x01\x02" + _frame(good)

    frames, remaining = extract_frames(data, _MIN, _MAX)
    assert remaining == b""
    assert frames == [good]
