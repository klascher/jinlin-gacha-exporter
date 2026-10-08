"""解析器单元测试（结构化契约：帧须带合法 S->C 抽卡信封，见 frame_schema）。"""
from gacha_exporter.analysis.parser import GachaParser, extract_records
from gacha_exporter.models import GachaRecord

# S->C 抽卡响应固定头：类型 000c + extId 0800 + result 0000 + seq回显 00 + ack 00
_SC_HEADER = b"\x00\x0c\x08\x00\x00\x00\x00\x00"


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _build_raw(pool_id: int, item_id: int, timestamp: int) -> bytes:
    """构造一条带合法信封、含单条 field 3 记录的完整帧。"""
    record = b""
    record += b"\x08" + _varint(pool_id)
    record += b"\x10" + _varint(item_id)
    record += b"\x18" + _varint(timestamp)
    return _SC_HEADER + b"\x1a" + _varint(len(record)) + record


def _build_page(pool_id: int, offset: int, total: int, items: list) -> bytes:
    """构造一页带 offset 回显(field1)/total(field2) 的完整响应帧。

    items = [(item_id, timestamp), ...]，页内 pos0 = 最新（与真实协议一致）。
    """
    body = b"\x08" + _varint(offset) + b"\x10" + _varint(total)
    for item_id, ts in items:
        record = b"\x08" + _varint(pool_id) + b"\x10" + _varint(item_id) + b"\x18" + _varint(ts)
        body += b"\x1a" + _varint(len(record)) + record
    return _SC_HEADER + body


def test_parse_single_record():
    """解析单条记录。"""
    parser = GachaParser()
    raw = _build_raw(30001, 101, 123456789)
    records = parser.parse(raw)
    assert len(records) == 1
    assert records[0].pool_id == "30001"
    assert records[0].item_id == "101"
    assert records[0].timestamp == 123456789


def test_parse_multiple_records():
    """解析同帧多条记录。"""
    parser = GachaParser()
    body = _build_raw(30001, 101, 1)[len(_SC_HEADER):] + \
        _build_raw(30001, 102, 2)[len(_SC_HEADER):]
    records = parser.parse(_SC_HEADER + body)
    assert len(records) == 2
    assert records[0].item_id == "101"
    assert records[1].item_id == "102"


def test_parse_non_gacha_frame_returns_empty():
    """无合法抽卡信封的帧（心跳/未知模块/裸记录字节）结构化解析直接为空。

    旧版 0x1a 扫描对无信封帧做容忍解析（「帧前后带垃圾数据应仍能解析」），
    结构化重构后契约收紧：切帧层保证真线上帧必有信封，无信封=非抽卡帧。
    """
    parser = GachaParser()
    bare = _build_raw(30001, 101, 1)[len(_SC_HEADER):]
    assert parser.parse(b"\x00\x00" + bare + b"\xff") == []  # 垃圾+裸记录
    assert parser.parse(b"\x00\x01\x02\x00\x00\x00\x4f\x67\x08\x00") == []  # 心跳
    assert parser.parse(b"\x00\x0c\x08\x00\x00\x8e\x0a\x52") == []  # 被拒响应


def test_parse_frame_with_0x1a_in_header():
    """帧头包含 0x1a 字节时不应漏解析记录。

    复现 bug：帧头计数器递增到 0x1a 时，旧 parser 误判为记录标记，
    读取 false record 后 i 跳过真实记录标记，导致 5 条只解析出 4 条。
    修复后验证失败时 i 回退到 after_len 继续扫描。
    """
    parser = GachaParser()
    # Real frame from frame_debug.log seq=55, header counter = 0x03 0x1a
    raw = bytes.fromhex(
        "000c08000000031a08890210e103"
        "1a1008b1ea0110bbc2990618a1b190a5eb33"
        "1a1008b1ea0110d4c2990618a1b190a5eb33"
        "1a1008b1ea0110bec2990618a1b190a5eb33"
        "1a1008b1ea0110b0c2990618a1b190a5eb33"
        "1a1008b1ea0110a9c2990618a5ed8da5eb33"
    )
    records = parser.parse(raw)
    assert len(records) == 5


def test_offset_echo_normalizes_tiebreak_fields():
    """带 offset 回显的帧：tiebreaker 归一化为方向无关（batch_seq=0、pos=offset+i）。

    语义约定「越小越新」是 shared_pity_tracking 冻结排序的依赖；帧序赋值只在
    「新->旧」翻页时成立（2026-08-18 早期修复 旧->新案例实测错序），
    offset 回显归一化后与翻页方向无关。
    """
    frame = _build_page(30001, 130, 618, [(101, 1), (102, 1), (103, 1)])
    recs = extract_records(frame, batch_seq=42)
    assert [(r.batch_seq, r.position_in_batch) for r in recs] == [(0, 130), (0, 131), (0, 132)]


def test_no_offset_echo_falls_back_to_frame_seq():
    """无 offset/total 字段的帧（结构宽松）：回退帧序赋值（batch_seq/pos=帧内序）。"""
    recs = extract_records(_build_raw(30001, 101, 1), batch_seq=7)
    assert (recs[0].batch_seq, recs[0].position_in_batch) == (7, 0)


def test_same_tenpull_reconstruction_direction_independent():
    """同一十连（同 ts）无论新->旧还是旧->新翻页，冻结排序重建序必须一致。

    复现 bug：早期修复 旧->新翻页时帧序 tiebreaker 得到镜像序，6 星落点
    错位、各组 pity 全歪（新手池 0 vs 8）。数据取自 本人账号新手池首个十连
    （游戏内验证 90000038 为该十连最后一抽）。
    """
    ts = 1780667765876
    # 视图 listing 新->旧序（abs0 = 最新 = 90000038），页大小 5
    listing = [90000038, 90000032, 90000033, 90000015, 90000030,
               90000007, 90000016, 90000023, 90000023, 90000015]
    pages = [(0, listing[0:5]), (5, listing[5:10])]

    def reconstruct(order):
        recs = []
        for batch, (off, items) in enumerate(order):
            recs += extract_records(_build_page(10001, off, 30, [(i, ts) for i in items]), batch_seq=100 + batch)
        return sorted(recs, key=lambda r: (r.timestamp, -r.batch_seq, -r.position_in_batch))

    new_to_old = [r.item_id for r in reconstruct(pages)]
    old_to_new = [r.item_id for r in reconstruct(list(reversed(pages)))]
    assert new_to_old == old_to_new
    assert new_to_old[-1] == "90000038"  # 与游戏 UI 验证过的正确末位一致
