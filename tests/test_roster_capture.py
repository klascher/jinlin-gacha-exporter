# -*- coding: utf-8 -*-
"""roster 采集解析器单测 - 真实帧夹具回放 + 合成帧 + 畸形帧容错（2026-10-13）。

真实夹具来源：
- 0x0038 概要：全部角色详情翻页.pcap 愚者帧（_out_详情翻页/s2c_0_type0038_seq64.bin）；
- 000c/0301 全量块：frame_debug.log 提线人 Lv80 帧（变长前缀 + LEN 超冲真实形态）。
"""
import json

from gacha_exporter.analysis import roster_capture as rc
from gacha_exporter.capture.frame_schema import (
    EXT_HERO_FULL,
    EXT_PLAYER_INFO,
    EXT_SKILL_UPDATE,
    MSG_HERO_SUMMARY,
)

# 愚者 90000010：映像 15/15/15，词条库序 {1,7,2}（决胜S/孤注一掷A/增伤B）
_SUMMARY_PAYLOAD = bytes.fromhex(
    "0a1e08b2c29906100f100f100f18011807180228ffffffffffffffffff013001"
)

# frame_debug.log 提线人帧（000c/0301）：前缀 8c01 f11c 0a b503（LEN 声明超冲真实形态）
# 内层 08 cbc29906（90000035）10 50（Lv80）18 00（F3）20 00（叠卡 0，指南三样本之一）...
_HERO_FULL_FRAME = bytes.fromhex(
    "000c0301000043468c01f11c0ab50308cbc2990610501800200028033080e1e0"
    "f791343ab8020a0b08011100000000800adb400a0b08020d00300000640d0011"
    "030d003080d4cd0d0012040d00202bc10d0011050d00300000590d0013060d00"
    "101c0d0012070d0020408f0d0012080d002028ae0d0012090d002088b30d0012"
    "0c0d0011d81a0012170d0020f8b11a0012180d002000790d00121b0d002075c2"
    "0d00121c0d002052a70d00121d0d0011340d00111f0d00308022e21a0011200d"
    "00300000540d0011210d0030c0b0d80d0011220d003000a8c70d0012230d0002"
    "c30013240d0001c30011250d00034e0011260d0003d00012450d00f10888c340"
    "40cdf9d0084a0d080110d6a806180520adf6ed170f0011e60f00219df30f0040"
    "02108ea91e002981f40f00031e00310110f02d00001e00f0075000580e600070"
    "007801800104"
)


def enc_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc_tag(fno: int, wt: int) -> bytes:
    return enc_varint((fno << 3) | wt)


def s2c(msg_type: int, ext: int, payload: bytes) -> bytes:
    """S->C 帧体：[类型2][ext2][result2][seq回显1][ack1][payload]。"""
    return (
        msg_type.to_bytes(2, "big")
        + ext.to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + b"\x01\x02"
        + payload
    )


def _patch_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rc.roster, "roster_path", lambda pid: str(tmp_path / f"roster_{pid}.json")
    )


def _load(tmp_path, pid="p1"):
    return rc.roster.load_roster(pid)["characters"]


# ---------- parse_* 纯函数 ----------

def test_parse_summary_real_fixture():
    parsed = rc.parse_summary(_SUMMARY_PAYLOAD)
    assert parsed["cid"] == "90000010"
    assert parsed["image"] == [15, 15, 15]
    assert parsed["entries"] == [1, 7, 2]


def test_parse_summary_empty_payload():
    assert rc.parse_summary(b"") is None


def test_parse_hero_full_real_frame():
    parsed = rc.parse_hero_full(_HERO_FULL_FRAME[8:])
    assert parsed["cid"] == "90000035"
    assert parsed["level"] == 80
    assert parsed["stack"] == 0  # 指南三样本：提线人叠卡 0


def test_parse_skill_update_synthetic():
    # 前缀（varint+0a+超冲 LEN）+ 08 cid / 10 复合 id 50036501 / 18 新等级 5
    inner = enc_tag(1, 0) + enc_varint(90000036) + enc_tag(2, 0) + enc_varint(50036501) \
        + enc_tag(3, 0) + enc_varint(5)
    payload = b"\x8c\x01\xf1\x1c\x0a" + enc_varint(99999) + inner
    parsed = rc.parse_skill_update(payload)
    assert parsed == {"cid": "90000036", "skills": {"501": 5}}


def test_parse_player_info_synthetic():
    g1 = enc_tag(1, 0) + enc_varint(90000010) + enc_tag(2, 0) + enc_varint(100) \
        + enc_tag(4, 0) + enc_varint(4)
    g2 = enc_tag(1, 0) + enc_varint(90000004) + enc_tag(2, 0) + enc_varint(60)
    payload = (
        enc_tag(1, 0) + enc_varint(12345)
        + enc_tag(2, 2) + enc_varint(8) + b"klascher"
        + enc_tag(9, 2) + enc_varint(len(g1)) + g1
        + enc_tag(9, 2) + enc_varint(len(g2)) + g2
    )
    heroes = rc.parse_player_info(payload)
    assert heroes == [{"cid": "90000010", "level": 100}, {"cid": "90000004", "level": 60}]


# ---------- 翻译 ----------

def test_translate_entries_with_lib():
    lib = [
        {"seq": 2, "name": "孤注一掷", "grade": "A"},
        {"seq": 1, "name": "决胜", "grade": "S"},
        {"seq": 7, "name": "增伤", "grade": "B"},
    ]
    assert rc.translate_entries([1, 7, 2], lib) == ["决胜S", "孤注一掷A", "增伤B"]


def test_translate_entries_missing_seq_falls_back():
    lib = [{"seq": 1, "name": "决胜", "grade": "S"}]
    assert rc.translate_entries([1, 9], lib) == ["决胜S", "#9"]


def test_translate_entries_no_lib():
    assert rc.translate_entries([3, 1], None) == ["#1", "#3"]
    assert rc.translate_entries([], None) == []


def test_translate_entries_bad_lib_items_ignored():
    assert rc.translate_entries([2], [{"bad": 1}, None, {"seq": "x", "name": "a"}]) == ["#2"]


# ---------- RosterCapture 端到端（feed → 落盘 → 回调） ----------

def test_capture_summary_feeds_roster(tmp_path, monkeypatch):
    _patch_roster(tmp_path, monkeypatch)
    hits = []
    cap = rc.RosterCapture("p1", on_updated=lambda: hits.append(1))
    cap.feed(s2c(MSG_HERO_SUMMARY, 0, _SUMMARY_PAYLOAD))
    rec = _load(tmp_path)["90000010"]
    assert rec["image"] == [15, 15, 15]
    assert rec["entries"] == [1, 7, 2]
    assert len(hits) == 1


def test_capture_hero_full_feeds_roster(tmp_path, monkeypatch):
    _patch_roster(tmp_path, monkeypatch)
    cap = rc.RosterCapture("p1")
    cap.feed(_HERO_FULL_FRAME)
    rec = _load(tmp_path)["90000035"]
    assert rec["level"] == 80
    assert rec["stack"] == 0  # 指南三样本：提线人叠卡 0
    assert "image" not in rec  # 没抓到的字段不该被写


def test_capture_skill_updates_union(tmp_path, monkeypatch):
    """两次 0300 各带一个技能槽：并集落盘，不互相覆盖。"""
    _patch_roster(tmp_path, monkeypatch)
    cap = rc.RosterCapture("p1")

    def skill_frame(skill_id, level):
        inner = enc_tag(1, 0) + enc_varint(90000036) + enc_tag(2, 0) + enc_varint(skill_id) \
            + enc_tag(3, 0) + enc_varint(level)
        return s2c(0x000C, EXT_SKILL_UPDATE, b"\x8c\x01\xf1\x1c\x0a" + enc_varint(99999) + inner)

    cap.feed(skill_frame(50036501, 5))
    cap.feed(skill_frame(50036101, 7))
    assert _load(tmp_path)["90000036"]["skills"] == {"501": 5, "101": 7}


def test_capture_player_info_batch(tmp_path, monkeypatch):
    _patch_roster(tmp_path, monkeypatch)
    hits = []
    cap = rc.RosterCapture("p1", on_updated=lambda: hits.append(1))
    g1 = enc_tag(1, 0) + enc_varint(90000010) + enc_tag(2, 0) + enc_varint(100)
    g2 = enc_tag(1, 0) + enc_varint(90000004) + enc_tag(2, 0) + enc_varint(60)
    payload = (
        enc_tag(9, 2) + enc_varint(len(g1)) + g1
        + enc_tag(9, 2) + enc_varint(len(g2)) + g2
    )
    cap.feed(s2c(0x000C, EXT_PLAYER_INFO, payload))
    chars = _load(tmp_path)
    assert chars["90000010"]["level"] == 100
    assert chars["90000004"]["level"] == 60
    assert len(hits) == 1  # 整帧合并完只回调一次


def test_repeated_summary_no_change_no_notify(tmp_path, monkeypatch):
    _patch_roster(tmp_path, monkeypatch)
    hits = []
    cap = rc.RosterCapture("p1", on_updated=lambda: hits.append(1))
    frame = s2c(MSG_HERO_SUMMARY, 0, _SUMMARY_PAYLOAD)
    cap.feed(frame)
    cap.feed(frame)
    assert len(hits) == 1  # 同值重复推送不触发回调


def test_unknown_frame_and_garbage_ignored(tmp_path, monkeypatch):
    _patch_roster(tmp_path, monkeypatch)
    hits = []
    cap = rc.RosterCapture("p1", on_updated=lambda: hits.append(1))
    for frame in (
        b"",                                   # 空帧
        b"\x00\x99\x00\x00\x00\x00\x00\x00",   # 未知类型
        b"\x00\x0c\x03\x01\x00\x00",           # 截断头（<8B）
        b"\xab\xcd" * 20,                      # 乱字节（未知类型）
        s2c(0x000C, 0x0800, b"\x01\x02\x03"),  # gacha 记录帧不归练度管
    ):
        cap.feed(frame)
    assert _load(tmp_path) == {}
    assert hits == []


def test_malformed_summary_payloads_no_exception(tmp_path, monkeypatch):
    """各种畸形 0x0038 payload 都不抛异常、不落脏数据。"""
    _patch_roster(tmp_path, monkeypatch)
    cap = rc.RosterCapture("p1")
    for payload in (
        b"\x0a\x05\x08\x01\x02",                        # blob LEN 超冲
        b"\x0a\x02\x08\x42",                            # 内层 cid=66 不在合理区间
        b"\x0a\x03\x08\xff",                            # 内层 varint 截断
        s2c(MSG_HERO_SUMMARY, 0, b"\x0a\x00")[:4],      # 截到只剩类型段
    ):
        cap.feed(payload)
    assert _load(tmp_path) == {}


def test_on_updated_exception_swallowed(tmp_path, monkeypatch):
    """UI 回调抛异常不得外泄（旁路功能不炸主流程）。"""
    _patch_roster(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("ui boom")

    cap = rc.RosterCapture("p1", on_updated=boom)
    cap.feed(s2c(MSG_HERO_SUMMARY, 0, _SUMMARY_PAYLOAD))
    assert _load(tmp_path)["90000010"]["image"] == [15, 15, 15]  # 数据照常落盘


def test_roster_file_content_roundtrip(tmp_path, monkeypatch):
    """落盘 JSON 结构可直接复核（sources 时间戳随帧更新）。"""
    _patch_roster(tmp_path, monkeypatch)
    cap = rc.RosterCapture("p1")
    cap.feed(s2c(MSG_HERO_SUMMARY, 0, _SUMMARY_PAYLOAD))
    raw = json.loads((tmp_path / "roster_p1.json").read_text(encoding="utf-8"))
    rec = raw["characters"]["90000010"]
    assert set(rec["sources"]) == {"image", "entries"}
    assert all(rec["sources"].values())
