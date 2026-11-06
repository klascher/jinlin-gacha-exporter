"""pool_view 单元测试 — 请求/响应解析 + 跨视图按份数配对去重。

场景全部来自 `卡池切换分析/卡池切换去重-开发方案.md` §4。
"""
import logging

from gacha_exporter.analysis.pool_view import (
    LIST_TYPE_ALL,
    PoolViewTracker,
    is_all_pools,
    make_view,
    parse_pool_list_type,
    parse_pool_request,
    parse_pool_response,
    split_view,
)
from gacha_exporter.models import GachaRecord


def _varint(value: int) -> bytes:
    """编码 protobuf varint。"""
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _build_request(seq: int, pool_id: int, offset: int,
                   list_type: int | None = None) -> bytes:
    """构造一条 C->S 切池/翻页请求帧体（不含长度头，与引擎传给回调的 body 一致）。

    真实布局：[4B 大端 seq][00 0c 08 00][1B counter=seq%127]
              [08 <pool> 10 <offset> 18 <list_type>]

    list_type=None 时不发 field 3（老协议），保持既有用例行为不变。
    """
    body = seq.to_bytes(4, "big")  # 前 4 字节大端 seq
    body += b"\x00\x0c\x08\x00"   # 信封
    body += bytes([seq % 127])     # counter 回显（seq%127，非低字节）
    body += b"\x08" + _varint(pool_id)
    body += b"\x10" + _varint(offset)
    if list_type is not None:
        body += b"\x18" + _varint(list_type)
    return body


def _build_response(seq: int, total: int = 0, offset: int = 0,
                    records: int = 0, simple_body: bool = True) -> bytes:
    """构造一条 S->C 切池/翻页响应帧体（不含长度头）。

    正常（simple_body=False）：[00 0c 08 00][00][flag=00][seq][ack][08 <offset> 10 <total> 1a <记录>...]
    被拒（simple_body=True 且 total/offset/records 都缺省）：8 字节截断帧
    [00 0c 08 00][00][flag=0x8e][seq][ack]，无 offset/total/记录。
    """
    ack = (seq + 1) & 0xFF
    if simple_body and total == 0 and offset == 0 and records == 0:
        # 被拒 8 字节截断帧
        body = b"\x00\x0c\x08\x00\x00\x8e" + bytes([seq % 127, ack])
        assert len(body) == 8
        return body
    body = b"\x00\x0c\x08\x00"          # 信封（无前置 seq）
    body += b"\x00\x00"                  # result=0 成功（大端 u16）
    body += bytes([seq % 127])          # seq 回显（=seq%127，非低字节）
    body += bytes([ack])                  # ack
    body += b"\x08" + _varint(offset)     # field1 = offset 回显
    body += b"\x10" + _varint(total)     # field2 = total
    for _ in range(records):
        body += b"\x1a\x06\x08\x01\x10\x02\x18\x03"
    return body


def _rec(pool_id: str, item_id: str, ts: int) -> GachaRecord:
    """构造一条记录。"""
    return GachaRecord(pool_id=pool_id, item_id=item_id, timestamp=ts)


# ---- 请求解析 ----

def test_parse_request_valid():
    """正常切池/翻页请求应解析出 (seq, pool_id, offset)。"""
    # 样本3：seq=0x3a 切常驻(20001) offset=0
    body = _build_request(0x3A, 20001, 0)
    assert parse_pool_request(body) == (0x3A, 20001, 0)


def test_parse_request_all_pool():
    """全部卡池 pool_id=0，帧短 2 字节。"""
    body = _build_request(0x3B, 0, 5)
    assert parse_pool_request(body) == (0x3B, 0, 5)


def test_parse_request_wrong_envelope():
    """信封特征不符（非切池/查询命令）应返回 None。"""
    body = b"\x00\x01\x02\x00\x3d\x08\x01\x10\x00"
    assert parse_pool_request(body) is None


def test_parse_request_too_short():
    """帧过短返回 None。"""
    assert parse_pool_request(b"\x00\x0c\x08\x00") is None


def test_parse_request_truncated_tail():
    """尾部缺 10 <offset> 标记返回 None。"""
    body = b"\x00\x0c\x08\x00\x3d\x08\x64"
    assert parse_pool_request(body) is None


# ---- 响应解析 ----

def test_parse_response_valid():
    """正常响应应解析出 (seq, offset, total, rejected=False)。"""
    body = _build_response(seq=0x3C, total=260, offset=0, records=0,
                          simple_body=False)
    parsed = parse_pool_response(body)
    assert parsed is not None
    seq, offset, total, rejected = parsed
    assert seq == 0x3C
    assert rejected is False
    assert total == 260


def test_parse_response_rejected():
    """8 字节被拒响应应识别为 rejected=True。"""
    body = _build_response(seq=0x3D)  # simple_body 默认走被拒分支
    parsed = parse_pool_response(body)
    assert parsed is not None
    seq, offset, total, rejected = parsed
    assert seq == 0x3D
    assert rejected is True
    assert total == 0


def test_parse_response_wrong_envelope():
    """信封不符返回 None。"""
    body = b"\x00\x01\x02\x00\x00\x00\x3d\x08\x00"
    assert parse_pool_response(body) is None


def test_parse_response_multibyte_offset():
    """offset>=128 为双字节 varint（全部卡池 486 条翻到第 26 页起必然出现），
    必须正确解析 offset 与 total，不能因单字节假设失败回退 current_view。"""
    body = _build_response(seq=0x3C, total=486, offset=130, simple_body=False)
    parsed = parse_pool_response(body)
    assert parsed is not None
    seq, offset, total, rejected = parsed
    assert (seq, offset, total, rejected) == (0x3C, 130, 486, False)
    # 三字节 varint 深度（485 = 全部卡池 98 页的最后一页 offset）
    body = _build_response(seq=0x3D, total=486, offset=485, simple_body=False)
    seq, offset, total, rejected = parse_pool_response(body)
    assert (offset, total, rejected) == (485, 486, False)


# ---- 被拒响应识别（必须在无记录提前返回之前调用） ----

def test_note_response_rejected_returns_lost_page():
    """被拒响应应返回丢失页的 (view, offset)，并累计到 rejected_pages。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3C, 0, 10))  # 全部卡池 offset=10
    body = _build_response(seq=0x3C)  # 8 字节被拒截断帧
    assert tracker.note_response(body) == ("0", 10)
    assert tracker.rejected_pages == [("0", 10)]


def test_note_response_rejected_request_missed():
    """请求没被监听到：退回 (current_view, -1)。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 20001, 0))  # current_view=20001
    body = _build_response(seq=0xFF)
    assert tracker.note_response(body) == ("20001", -1)


def test_note_response_normal_returns_none():
    """正常响应/非切池响应返回 None，不计入 rejected_pages。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 20001, 0))
    normal = _build_response(seq=0x3A, total=260, simple_body=False)
    other = b"\x00\x01\x02\x00\x00\x00\x3d\x08\x00"  # 非切池信封
    assert tracker.note_response(normal) is None
    assert tracker.note_response(other) is None
    assert tracker.rejected_pages == []


def test_rejected_pages_reset():
    """reset 清空被拒页记录。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3C, 0, 5))
    tracker.note_response(_build_response(seq=0x3C))
    tracker.reset()
    assert tracker.rejected_pages == []


# ---- 服务器权威 total（完整性校验） ----

def test_note_response_records_total_per_view():
    """正常响应应把 total 按视图存入 view_totals（seq 反查视图）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x30, 0, 0))      # 全部卡池
    tracker.track_request(_build_request(0x31, 20001, 0))  # 具体池
    assert tracker.note_response(_build_response(seq=0x30, total=618, records=5)) is None
    assert tracker.note_response(_build_response(seq=0x31, total=260, records=5)) is None
    assert tracker.view_totals == {"0": 618, "20001": 260}


def test_note_response_total_fallback_current_view():
    """请求未监听到时，正常响应的 total 归到 current_view。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 20001, 0))  # 只为设置 current_view
    assert tracker.note_response(_build_response(seq=0xFF, total=260, records=5)) is None
    assert tracker.view_totals == {"20001": 260}


def test_note_response_rejected_not_record_total():
    """被拒响应无 total 字段，不应记入 view_totals。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3C, 0, 10))
    assert tracker.note_response(_build_response(seq=0x3C)) == ("0", 10)
    assert tracker.view_totals == {}


def test_view_totals_reset():
    """reset 清空 total 记录。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x30, 0, 0))
    tracker.note_response(_build_response(seq=0x30, total=618, records=5))
    tracker.reset()
    assert tracker.view_totals == {}


# ---- 跨视图处理（步②后：L1 不再过滤，原样放行交账本） ----

def test_filter_same_view_no_drop():
    """同视图到达全部保留（十连同名 / 整批重发由账本处理）。"""
    tracker = PoolViewTracker()
    # 视图"20001"接连两批同 key 记录（同视图重发）应全部保留
    recs1 = [_rec("20001", "101", 1000), _rec("20001", "102", 1001)]
    recs2 = [_rec("20001", "101", 1000), _rec("20001", "102", 1001)]
    kept1, d1 = tracker.filter_records("20001", recs1)
    kept2, d2 = tracker.filter_records("20001", recs2)
    assert len(kept1) == 2 and d1 == 0
    assert len(kept2) == 2 and d2 == 0


def test_filter_cross_view_integre_retrans():
    """场景A（步②后）：跨视图重传不再由 L1 过滤，原样放行交账本 SEEN。"""
    tracker = PoolViewTracker()
    recs = [_rec("20001", str(i), 1000 + i) for i in range(5)]
    kept_pool, d_pool = tracker.filter_records("20001", recs)
    # 全部卡池视图为 "0"，记录自带 pool_id="20001"
    kept_all, d_all = tracker.filter_records("0", recs)
    assert len(kept_pool) == 5 and d_pool == 0
    assert len(kept_all) == 5 and d_all == 0
    assert tracker.filtered_count == 0


def test_filter_cross_view_scattered():
    """场景B（步②后）：打散到不同页也原样放行——L1 不再做任何判定。"""
    tracker = PoolViewTracker()
    # 黑哨池先抓 5 条
    pool_recs = [_rec("30004", str(i), 2000 + i) for i in range(5)]
    tracker.filter_records("30004", pool_recs)
    # 常驻新抽 1 条 n
    n = _rec("20001", "999", 1999)
    # 全部卡池页1: [n, k2, k3, k4, k5] -> 5 条全放行
    page1 = [n] + pool_recs[1:5]
    kept1, d1 = tracker.filter_records("0", page1)
    assert len(kept1) == 5 and d1 == 0
    # 全部卡池页2: [k1, 其他] -> 全放行
    other = _rec("20008", "555", 3000)
    page2 = [pool_recs[0], other]
    kept2, d2 = tracker.filter_records("0", page2)
    assert len(kept2) == 2 and d2 == 0


def test_filter_ten_pull_same_name_cross_view():
    """场景C（步②后）：十连同名份不再被 L1 配对丢弃（2026-09-11 实测漏3 的根），
    全部原样放行交账本；跨视图重复由账本 SEEN/多重集兜底处理。"""
    tracker = PoolViewTracker()
    a_key_ts = 5000
    A1 = _rec("30004", "A", a_key_ts)
    A2 = _rec("30004", "A", a_key_ts)
    A3 = _rec("30004", "A", a_key_ts)
    kept_pool, _ = tracker.filter_records("30004", [A1])
    assert len(kept_pool) == 1
    kept_all1, d1 = tracker.filter_records("0", [A1])
    kept_all2, d2 = tracker.filter_records("0", [A2])
    kept_all3, d3 = tracker.filter_records("0", [A3])
    assert len(kept_all1) == 1 and d1 == 0
    assert len(kept_all2) == 1 and d2 == 0
    assert len(kept_all3) == 1 and d3 == 0
    # L1 不再有丢弃；A 的份数完全由账本按事件份数决定
    assert tracker.filtered_count == 0


def test_filter_unknown_view_no_filter():
    """视图未知（请求没被监听到）-> 不过滤，全部接纳（宁重复不漏）。"""
    tracker = PoolViewTracker()
    recs_pool = [_rec("20001", "101", 1000)]
    recs_all = [_rec("20001", "101", 1000)]
    kept1, d1 = tracker.filter_records(None, recs_pool)
    kept2, d2 = tracker.filter_records(None, recs_all)
    assert len(kept1) == 1 and d1 == 0
    assert len(kept2) == 1 and d2 == 0
    assert tracker.filtered_count == 0


def test_filter_only_all_view_normally():
    """只翻全部卡池（规范流程）：全部接纳，行为不变。"""
    tracker = PoolViewTracker()
    recs = [_rec("20001", "101", 1000), _rec("30004", "102", 1001)]
    kept, d = tracker.filter_records("0", recs)
    # 再翻一遍（同视图）
    kept2, d2 = tracker.filter_records("0", recs)
    assert len(kept) == 2 and d == 0
    assert len(kept2) == 2 and d2 == 0
    assert tracker.filtered_count == 0


def test_filter_reverse_order_all_then_pool():
    """反向场景（步②后）：先全部卡池后具体池，同样原样放行。"""
    tracker = PoolViewTracker()
    recs = [_rec("20001", "101", 1000)]
    kept_all, d_all = tracker.filter_records("0", recs)  # 全部卡池先计入
    kept_pool, d_pool = tracker.filter_records("20001", recs)  # 具体池重传 -> 交账本
    assert len(kept_all) == 1 and d_all == 0
    assert len(kept_pool) == 1 and d_pool == 0


def test_track_request_updates_current_view():
    """track_request 登记 seq->视图并更新 current_view。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 20001, 0))
    assert tracker.current_view == "20001"
    tracker.track_request(_build_request(0x3B, 0, 0))  # 切全部卡池 pool=0
    assert tracker.current_view == "0"


def test_resolve_view_by_response_seq():
    """resolve_view 通过响应回显 seq 反查视图，请求未抓到则回退当前视图。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 20001, 0))
    body = _build_response(seq=0x3A, total=10, simple_body=False)
    assert tracker.resolve_view(body) == "20001"


def test_resolve_view_fallback_to_current():
    """请求没被监听到（seq 未登记）-> resolve_view 回退到当前视图。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 20001, 0))  # current_view=20001
    # 未知 seq 的响应
    body = _build_response(seq=0xFF, total=10, simple_body=False)
    assert tracker.resolve_view(body) == "20001"


def test_resolve_view_none_when_no_request_seen():
    """从未抓到任何请求 -> resolve_view 返回 None。"""
    tracker = PoolViewTracker()
    body = _build_response(seq=0x3A, total=10, simple_body=False)
    assert tracker.resolve_view(body) is None


def test_latest_response_context_normal():
    """正常响应返回 (view, offset, total)，供卡池识别校验页号。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 0, 5))
    body = _build_response(seq=0x3A, total=887, offset=5, simple_body=False)
    assert tracker.latest_response_context(body) == ("0", 5, 887)


def test_latest_response_context_rejected_is_none():
    """被限流拒绝的响应没有 offset/total，不得产生识别任务。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3A, 0, 0))
    assert tracker.latest_response_context(_build_response(seq=0x3A)) is None


def test_latest_response_context_falls_back_current_view():
    """请求漏抓时回退当前视图；当前视图未知返回 None。"""
    tracker = PoolViewTracker()
    body = _build_response(seq=0x7F, total=10, offset=0, simple_body=False)
    assert tracker.latest_response_context(body) is None
    tracker.track_request(_build_request(0x3A, 20001, 0))
    assert tracker.latest_response_context(body) == ("20001", 0, 10)

# ---- 长会话 seq%%127 回绕配对（2026-08-18 结构化重构回归） ----
# capture_20260817_214949.pcap（seq 到 424）逐帧验证：counter/seq 回显 =
# seq %% 127（127->0x00、128->0x01、298->0x2c）。曾用 seq & 0xFF 配对，
# seq>=127 的请求配对全空，被拒响应 offset 全部 -1。

def test_pairing_long_session_seq_127():
    """seq=127 回显 0x00：请求/响应配对必须命中（旧 &0xFF 键 127 无回显可配）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(127, 0, 100))
    assert tracker.resolve_view(_build_response(seq=127, total=618,
                                                simple_body=False)) == "0"


def test_pairing_long_session_seq_128():
    """seq=128 回显 0x01（非低字节 0x80）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(128, 0, 100))
    assert tracker.resolve_view(_build_response(seq=128, total=618,
                                                simple_body=False)) == "0"


def test_pairing_long_session_seq_298_real_bytes():
    """真实字节回归：seq=0x12a(298) 全部卡池 offset=0x2f7? 样本 counter=0x2c。"""
    # 真实帧（214949.pcap）：0000012a 000c0800 2c 08 00 10 f702
    body = bytes.fromhex("0000012a" "000c0800" "2c" "08" "00" "10" "f702")
    assert parse_pool_request(body) == (298, 0, 375)  # f7 02 varint = 375
    tracker = PoolViewTracker()
    tracker.track_request(body)
    resp = _build_response(seq=298, total=618, simple_body=False)
    assert tracker.resolve_view(resp) == "0"


def test_rejected_page_located_in_long_session():
    """长会话被拒响应必须定位到 (view, offset)（旧版 offset 恒 -1）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(184, 0, 190))
    rejected = tracker.note_response(_build_response(seq=184))  # 8B 被拒帧
    assert rejected == ("0", 190)


def test_offset_varint_130_real_bytes():
    """offset>=130 双字节 varint 真实样本（20260817 深翻页抓包）。"""
    # 真实帧（frame_debug 21:30:40）：000c0800 0000006d 08 8201 10 e304 ...
    # offset=0x8201 varint = 130，total=0x0e3? e3 04 varint = 611
    body = bytes.fromhex("000c0800" "0000006d" "08" "8201" "10" "e304")
    assert parse_pool_response(body) == (0, 130, 611, False)  # S->C 无 4B seq，body[6]=seq回显


def test_problem3_seq_echo_reuse_no_misattribution():
    """问题3 回归（2026-08-17 晚 868323314e2d 复现 +16 重复的场景）。

    当晚误判为「响应 seq 独立计数器漂移 +2 + 低字节复用陈旧表项」；结构化
    重构后真相：回显 = seq % 127。seq=343(0x157) 的回显恰为 89(0x59)--
    343 % 127 = 89，无漂移。旧代码按 &0xFF 期待 0x57，反查命中 20 分钟前
    seq=89（全部卡池）的陈旧表项 -> 常驻池页错配视图 0 -> +16 条重复。
    新代码：请求 343 登记时覆写 key=89，响应 0x59 命中的是新鲜表项 -> 20001。
    """
    tracker = PoolViewTracker()
    # 21:53:35 陈旧登记：seq=89 -> 全部卡池
    tracker.track_request(_build_request(89, 0, 100))
    assert tracker._current_view == "0"
    # 21:55:02~10 常驻池翻页至致命请求 seq=343 off=45（真实 pcap 215402.pcap）
    for seq, off in [(340, 30), (341, 35), (342, 40), (343, 45)]:
        tracker.track_request(_build_request(seq, 20001, off))
    # 致命响应：echo=0x59(89)、off=45、total=260，含老十连首条 90000022
    rec = (b"\x08" + _varint(20001) + b"\x10" + _varint(90000022)
           + b"\x18" + _varint(1781363621528))
    resp = _build_response(seq=343, total=260, offset=45,
                           simple_body=False)
    resp = resp + b"\x1a" + bytes([len(rec)]) + rec
    # 响应归因必须是常驻池（旧代码此处错配 "0" -> 重复入库）
    assert tracker.resolve_view(resp) == "20001"
    # 跨视图过滤随视图正确而恢复拦截：先在全部卡池见过该 key，常驻页这份应被丢弃
    from gacha_exporter.analysis.pool_view import parse_pool_response
    seq_echo, off, total, rejected = parse_pool_response(resp)
    assert (seq_echo, off, total, rejected) == (343 % 127, 45, 260, False)


# ---- 列表类型（field 3）：区分同为 pool_id=0 的汇总页与全部卡池 ----

def test_parse_list_type_real_frames():
    """真实抓包逐字节回归：问题反馈/30006-遴选-活动-全部-20003.pcapng。

    五个页面的请求帧，pool_id 与 list_type（field 3 = 0x18）组合唯一确定视图。
    """
    real = [
        ("0000003c000c08003c08b6ea0110001800", 30006, 0),  # 单池 30006
        ("0000003d000c08003d080010001804", 0, 4),          # 遴选契约
        ("0000003e000c08003e080010001803", 0, 3),          # 活动契约
        ("0000003f000c08003f080010001800", 0, 0),          # 全部卡池
        ("00000040000c08004008a39c0110001800", 20003, 0),  # 单池 20003
    ]
    for hex_str, pool_id, list_type in real:
        body = bytes.fromhex(hex_str)
        assert parse_pool_request(body)[1] == pool_id
        assert parse_pool_list_type(body) == list_type


def test_view_includes_list_type():
    """视图标识带列表类型：遴选 0@4 / 活动 0@3 / 全部 0@0 / 单池 30006@0。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3D, 0, 0, list_type=4))
    assert tracker.current_view == "0@4"
    tracker.track_request(_build_request(0x3E, 0, 0, list_type=3))
    assert tracker.current_view == "0@3"
    tracker.track_request(_build_request(0x3F, 0, 0, list_type=0))
    assert tracker.current_view == "0@0"
    tracker.track_request(_build_request(0x3C, 30006, 0, list_type=0))
    assert tracker.current_view == "30006@0"


def test_view_fallback_without_list_type():
    """协议没有 field 3（老版本）：视图回退为纯 pool_id，行为与改动前一致。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3C, 0, 0))
    assert tracker.current_view == "0"
    tracker.track_request(_build_request(0x3B, 30006, 0))
    assert tracker.current_view == "30006"


def test_cross_view_dedup_between_list_types():
    """回归：同为 pool_id=0 的汇总页与全部卡池，靠 list_type 区分后互相抵消。

    游戏新增按契约类型聚合的汇总页（遴选 type=4 / 活动 type=3），请求里
    pool_id 与「全部卡池」同为 0。早前只按 pool_id 判视图，同一批记录在两个
    页面各计入一次且互不过滤 -> 历史多出重复（实测 sample 524 而非 521）。
    """
    tracker = PoolViewTracker()
    recs = [
        _rec("30005", "90000037", 1787331759171),
        _rec("30005", "90000007", 1787547708224),
    ]
    # 先在活动契约汇总页（total=189）抓到
    tracker.track_request(_build_request(0x3E, 0, 0, list_type=3))
    view_a = tracker.resolve_view(
        _build_response(0x3E, total=189, offset=0, records=2, simple_body=False)
    )
    assert view_a == "0@3"
    kept_a, dropped_a = tracker.filter_records(view_a, recs)
    assert (kept_a, dropped_a) == (recs, 0)  # 首次到达全部接纳

    # 再在全部卡池页（total=521）遇到同一批
    tracker.track_request(_build_request(0x3F, 0, 0, list_type=0))
    view_b = tracker.resolve_view(
        _build_response(0x3F, total=521, offset=0, records=2, simple_body=False)
    )
    assert view_b == "0@0"
    kept_b, dropped_b = tracker.filter_records(view_b, recs)
    # 步②后 L1 不再过滤：视图仅作溯源标签，跨视图重复由账本 SEEN 处理
    assert (kept_b, dropped_b) == (recs, 0)


def test_view_totals_separated_by_list_type():
    """各汇总页的权威 total 分别记录，不再互相覆盖（此前同为 "0" 会互相顶掉）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x3E, 0, 0, list_type=3))
    tracker.note_response(_build_response(0x3E, total=189, records=1, simple_body=False))
    tracker.track_request(_build_request(0x3F, 0, 0, list_type=0))
    tracker.note_response(_build_response(0x3F, total=521, records=1, simple_body=False))
    assert tracker.view_totals == {"0@3": 189, "0@0": 521}


# ---------- 翻页方向（B6 消歧信号，见 协议结构说明 缺口 3） ----------
# ---- 旧账本 B6 方向信号已注释（2026-09-14），方向测试随之停用保留 ----
# （direction_of 链路见 pool_view.__init__ 注释；如需恢复旧账本一并取消整块注释）
# def test_direction_forward_paging_is_positive():
#     """同视图 offset 递增（0→5→10）= 由新往旧翻 → +1。"""
#     tracker = PoolViewTracker()
#     for off in (0, 5, 10):
#         tracker.track_request(_build_request(1, 0, off, list_type=0))
#     assert tracker.direction_of("0@0") == 1
#
#
# def test_direction_backward_paging_is_negative():
#     """同视图 offset 递减（510→505→500）= 跳末页由旧往新翻 → -1。"""
#     tracker = PoolViewTracker()
#     for off in (510, 505, 500):
#         tracker.track_request(_build_request(1, 0, off, list_type=0))
#     assert tracker.direction_of("0@0") == -1
#
#
# def test_direction_back_to_zero_is_forward_not_backward():
#     """从末页切回第一页（500→0）不算「往新翻」→ 记 +1。"""
#     tracker = PoolViewTracker()
#     for off in (510, 505, 500, 0):
#         tracker.track_request(_build_request(1, 0, off, list_type=0))
#     assert tracker.direction_of("0@0") == 1
#
#
# def test_direction_is_per_view_and_unknown_by_default():
#     """方向按视图独立记录；未翻过的视图 / 未知视图返回 0。"""
#     tracker = PoolViewTracker()
#     tracker.track_request(_build_request(1, 0, 0, list_type=0))
#     tracker.track_request(_build_request(2, 0, 5, list_type=0))
#     tracker.track_request(_build_request(3, 0, 500, list_type=3))
#     tracker.track_request(_build_request(4, 0, 495, list_type=3))
#     assert tracker.direction_of("0@0") == 1      # 递增
#     assert tracker.direction_of("0@3") == -1     # 递减
#     assert tracker.direction_of("0@4") == 0      # 未翻过
#     assert tracker.direction_of(None) == 0       # 视图未知
#
#
# def test_reset_clears_direction():
#     """reset 后方向状态一并清空。"""
#     tracker = PoolViewTracker()
#     tracker.track_request(_build_request(1, 0, 0, list_type=0))
#     tracker.track_request(_build_request(2, 0, 5, list_type=0))
#     assert tracker.direction_of("0@0") == 1
#     tracker.reset()
#     assert tracker.direction_of("0@0") == 0


def test_view_ident_helpers_roundtrip():
    """make_view / split_view 往返与无类型回退。"""
    assert make_view(0, 4) == "0@4"
    assert make_view(0, None) == "0"
    assert split_view("0@4") == ("0", 4)
    assert split_view("30006@0") == ("30006", 0)
    assert split_view("0") == ("0", None)
    assert split_view(None) == ("", None)


# ---- 「仅采纳全部卡池数据」的视图判定（2026-09-13，见 协议结构说明 B8） ----


def test_is_all_pools_accepts_only_all_pools_view():
    """只有「全部卡池」(0@0 或老协议裸 "0") 为真；单池与汇总页一律为假。"""
    assert is_all_pools("0@0") is True          # 全部卡池
    assert is_all_pools("0") is True            # 老协议无 field3，同为全部卡池
    assert is_all_pools("0@3") is False         # 活动契约汇总页
    assert is_all_pools("0@4") is False         # 遴选契约汇总页
    assert is_all_pools("20001@0") is False     # 单池
    assert is_all_pools("30006@0") is False     # 单池
    assert is_all_pools(None) is False          # 视图未知（调用方按超集语义放行）


def test_is_all_pools_survives_rebuild_of_view_ident():
    """判定与 make_view 保持一致（防止将来视图标识格式变动时静默失配）。"""
    assert is_all_pools(make_view(0, LIST_TYPE_ALL)) is True


# ---- H1 视图塌缩预警（pool_id=0 且缺 field 3）：2026-09-12 补，此前零覆盖 ----
#
# 背景：§16.5-A 实测协议自 2026-09-10 21:04 起恒带 field 3，因此**真实流量不会
# 走到这条回退路径**——H1 的有效性无法靠运行日志证实，只能用单测构造保证。
# 设计：设计文档 §13.5 / §16.2。

def _reset_h1_flag() -> None:
    """重置 H1「同进程只报一次」的模块级标志，保证用例与执行顺序无关。"""
    from gacha_exporter.analysis import pool_view
    pool_view._VIEW_COLLAPSE_WARNED = False


def _h1_warnings(caplog) -> list:
    """取出本轮捕获到的 H1 预警记录。"""
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "视图标识回退" in r.getMessage()]


def test_h1_warns_on_missing_field3_for_pool_zero(caplog):
    """缺 field3 且 pool_id=0 → 视图退化为 "0"，并打出一条 WARNING。"""
    _reset_h1_flag()
    body = _build_request(0x3C, 0, 0)          # 不带 0x18（老协议帧）
    assert parse_pool_list_type(body) is None   # 前提：field 3 确实缺失
    with caplog.at_level(logging.WARNING):
        assert make_view(0, None) == "0"
    warns = _h1_warnings(caplog)
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]
    assert "field3" in warns[0].getMessage()


def test_h1_warns_only_once_per_process(caplog):
    """同类警告一个进程只报一次（避免逐帧刷屏）。"""
    _reset_h1_flag()
    with caplog.at_level(logging.WARNING):
        make_view(0, None)
        make_view(0, None)
        make_view(0, None)
    assert len(_h1_warnings(caplog)) == 1


def test_h1_silent_when_field3_present_or_pool_nonzero(caplog):
    """带 field3、或 pool_id≠0（自带区分度）时都不应报警。"""
    _reset_h1_flag()
    with caplog.at_level(logging.WARNING):
        assert make_view(0, 3) == "0@3"          # 活动契约：带类型
        assert make_view(0, 4) == "0@4"          # 遴选契约：带类型
        assert make_view(30006, 0) == "30006@0"  # 单池：带类型
        assert make_view(20001, None) == "20001"  # 缺类型但 pool_id≠0
    assert _h1_warnings(caplog) == []


def test_h1_warns_via_track_request_pipeline(caplog):
    """走真实调用链（track_request → make_view）也必须报警，而非只在直调时报警。"""
    _reset_h1_flag()
    tracker = PoolViewTracker()
    with caplog.at_level(logging.WARNING):
        tracker.track_request(_build_request(0x3C, 0, 0))   # 老协议帧，pool_id=0
    assert tracker.current_view == "0"
    assert len(_h1_warnings(caplog)) == 1


# ---- 收尾缺口报告（B：已见页 + 缺页推算） ----

def _page_offsets(total: int, size: int = 5):
    """由权威 total 推算应有页的 offset 列表（每页 size 条，末页可少）。"""
    full = total // size
    offsets = [i * size for i in range(full)]
    if total % size:
        offsets.append(full * size)
    return offsets


def test_seen_pages_tracked_and_missing_report():
    """成功响应的页被登记；缺页报告按权威 total 推算缺哪些页。"""
    tracker = PoolViewTracker()
    for i, off in enumerate((0, 5, 10)):
        tracker.track_request(_build_request(0x10 + i, 0, off, list_type=0))
        assert tracker.note_response(
            _build_response(0x10 + i, total=521, offset=off, records=5, simple_body=False)
        ) is None
    assert tracker.seen_pages == {("0@0", 0), ("0@0", 5), ("0@0", 10)}

    report = tracker.missing_pages()
    assert len(report) == 1
    view, missing, n_seen, n_expected = report[0]
    assert (view, n_seen, n_expected) == ("0@0", 3, 105)   # 521 条 = 104×5 + 1
    assert missing == _page_offsets(521)[3:]
    assert len(missing) == 102


def test_missing_pages_counts_partial_last_page():
    """末页不足 5 条时 offset 仍是 5 的倍数（521 → 105 页，末页 offset=520）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x30, 0, 790, list_type=0))
    tracker.note_response(
        _build_response(0x30, total=792, offset=790, records=2, simple_body=False)
    )
    view, missing, n_seen, n_expected = tracker.missing_pages()[0]
    assert (view, n_seen, n_expected) == ("0@0", 1, 159)   # 792 = 158×5 + 2
    assert 790 not in missing and 785 in missing


def test_rejected_page_not_counted_as_seen():
    """被限流拒绝的页无数据 → 不得计入已见（否则缺口会漏报）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x20, 0, 50, list_type=0))
    assert tracker.note_response(_build_response(0x20)) == ("0@0", 50)
    assert tracker.seen_pages == set()


def test_missing_pages_can_target_single_view():
    """可只针对指定视图报告（B8 后只有「全部卡池」入账，其余视图不入报告）。"""
    tracker = PoolViewTracker()
    for i, (pool, lt) in enumerate(((0, 0), (20001, 0))):
        tracker.track_request(_build_request(0x40 + i, pool, 0, list_type=lt))
        tracker.note_response(
            _build_response(0x40 + i, total=10, offset=0, records=5, simple_body=False)
        )
    assert len(tracker.missing_pages()) == 2                 # 不指定则报全部
    only = tracker.missing_pages("0@0")
    assert len(only) == 1 and only[0][0] == "0@0"
    assert tracker.missing_pages("99999@0") == []            # 无该视图 → 空


def test_missing_pages_skips_unknown_total():
    """没有权威 total 的视图无法推算页数 → 不报（宁缺勿误）。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x50, 0, 0, list_type=0))
    tracker.note_response(_build_response(0x50))             # 被拒：无 total
    assert tracker.missing_pages() == []


def test_reset_clears_seen_pages():
    """reset（切账号/清空）必须清掉已见页，否则新账号会看到旧账号的缺口。"""
    tracker = PoolViewTracker()
    tracker.track_request(_build_request(0x60, 0, 0, list_type=0))
    tracker.note_response(
        _build_response(0x60, total=10, offset=0, records=5, simple_body=False)
    )
    assert tracker.seen_pages
    tracker.reset()
    assert tracker.seen_pages == set() and tracker.missing_pages() == []
