"""roster 存储单测 - 字段级合并语义与文件读写（2026-10-13）。"""
import json

from gacha_exporter.storage import roster


def _patch_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(roster, "roster_path", lambda pid: str(tmp_path / f"roster_{pid}.json"))


def test_merge_creates_record(tmp_path, monkeypatch):
    _patch_dir(tmp_path, monkeypatch)
    changed = roster.merge_character_fields(
        "p1", "90000010", {"level": 100, "stack": 0}, source_ts="2026-10-13T09:00:00"
    )
    assert changed
    data = roster.load_roster("p1")
    rec = data["characters"]["90000010"]
    assert rec["level"] == 100
    assert rec["stack"] == 0
    assert rec["sources"]["level"] == "2026-10-13T09:00:00"
    assert data["updated_at"] == "2026-10-13T09:00:00"


def test_new_overwrites_old(tmp_path, monkeypatch):
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields("p1", "90000010", {"level": 80}, source_ts="2026-10-13T09:00:00")
    roster.merge_character_fields("p1", "90000010", {"level": 90}, source_ts="2026-10-13T10:00:00")
    assert roster.load_roster("p1")["characters"]["90000010"]["level"] == 90


def test_old_does_not_overwrite_new(tmp_path, monkeypatch):
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields("p1", "90000010", {"level": 90}, source_ts="2026-10-13T10:00:00")
    changed = roster.merge_character_fields(
        "p1", "90000010", {"level": 80}, source_ts="2026-10-13T09:00:00"
    )
    # 值被拒但 sources 时刻也不会回退
    assert not changed
    assert roster.load_roster("p1")["characters"]["90000010"]["level"] == 90


def test_missing_ts_treated_as_oldest(tmp_path, monkeypatch):
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields("p1", "90000010", {"level": 90}, source_ts="2026-10-13T10:00:00")
    roster.merge_character_fields("p1", "90000010", {"level": 80}, source_ts="")
    assert roster.load_roster("p1")["characters"]["90000010"]["level"] == 90


def test_per_field_independence(tmp_path, monkeypatch):
    """level 旧时刻不影响 image 新时刻（字段级独立判定）。"""
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields(
        "p1", "90000010", {"level": 80, "image": [15, 15, 15]}, source_ts="2026-10-13T09:00:00"
    )
    roster.merge_character_fields("p1", "90000010", {"level": 90}, source_ts="2026-10-13T10:00:00")
    rec = roster.load_roster("p1")["characters"]["90000010"]
    assert rec["level"] == 90
    assert rec["image"] == [15, 15, 15]  # image 保留旧值（没有新数据来覆盖）


def test_zero_and_empty_are_real_data(tmp_path, monkeypatch):
    """0 / 空列表是真实数据照写（null 公理：只有 None 被拒）。"""
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields(
        "p1", "90000004", {"image": [0, 0, 0], "entries": [], "stack": 0}
    )
    rec = roster.load_roster("p1")["characters"]["90000004"]
    assert rec["image"] == [0, 0, 0]
    assert rec["entries"] == []
    assert rec["stack"] == 0


def test_none_and_unknown_fields_rejected(tmp_path, monkeypatch):
    _patch_dir(tmp_path, monkeypatch)
    changed = roster.merge_character_fields(
        "p1", "90000010", {"level": None, "bogus": 1, "stack": 2}
    )
    assert changed  # stack 有效
    rec = roster.load_roster("p1")["characters"]["90000010"]
    assert "level" not in rec
    assert "bogus" not in rec


def test_skills_merge(tmp_path, monkeypatch):
    """skills 键级并集：0300 按槽增量推送，一次只带一个技能，不能整组覆盖。"""
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields("p1", "90000036", {"skills": {"501": 5}})
    roster.merge_character_fields("p1", "90000036", {"skills": {"101": 7}})
    rec = roster.load_roster("p1")["characters"]["90000036"]
    assert rec["skills"] == {"501": 5, "101": 7}
    # 同槽新值覆盖旧值
    roster.merge_character_fields("p1", "90000036", {"skills": {"501": 6}})
    assert roster.load_roster("p1")["characters"]["90000036"]["skills"] == {"501": 6, "101": 7}


def test_corrupt_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "roster_p1.json"
    p.write_text("not json{", encoding="utf-8")
    _patch_dir(tmp_path, monkeypatch)
    data = roster.load_roster("p1")
    assert data["characters"] == {}
    # 照常可写
    roster.merge_character_fields("p1", "90000010", {"level": 1})
    assert roster.load_roster("p1")["characters"]["90000010"]["level"] == 1


def test_ui_state_roundtrip(tmp_path, monkeypatch):
    _patch_dir(tmp_path, monkeypatch)
    roster.merge_character_fields("p1", "90000010", {"level": 100})
    roster.save_ui_state("p1", {"checked": ["90000010"], "display": "checked", "star": "6"})
    state = roster.load_ui_state("p1")
    assert state["checked"] == ["90000010"]
    # 练度数据不受影响
    assert roster.load_roster("p1")["characters"]["90000010"]["level"] == 100
    # 文件结构仍是合法 JSON
    raw = json.loads((tmp_path / "roster_p1.json").read_text(encoding="utf-8"))
    assert "characters" in raw and "ui_state" in raw
