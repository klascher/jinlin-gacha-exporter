# -*- coding: utf-8 -*-
"""role-image.json 词条库加载单测 - 容错降级与联动装配（2026-10-13）。"""
import json

from gacha_exporter.loaders import role_image


def _patch_path(tmp_path, monkeypatch):
    monkeypatch.setattr(role_image, "role_image_path", lambda: str(tmp_path / "role-image.json"))


def test_load_valid(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    lib = {"90000010": [{"seq": 1, "name": "决胜", "grade": "S"}]}
    (tmp_path / "role-image.json").write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
    assert role_image.load_role_image() == lib


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    assert role_image.load_role_image() == {}


def test_corrupt_file_returns_empty(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    (tmp_path / "role-image.json").write_text("not json{", encoding="utf-8")
    assert role_image.load_role_image() == {}


def test_top_level_not_dict(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    (tmp_path / "role-image.json").write_text("[]", encoding="utf-8")
    assert role_image.load_role_image() == {}


def test_invalid_entries_dropped(tmp_path, monkeypatch):
    """非数组角色整库丢弃；数组内非法元素剔除；seq 非数值剔除。"""
    _patch_path(tmp_path, monkeypatch)
    raw = {
        "90000010": [{"seq": 1, "name": "决胜", "grade": "S"}, {"bad": 1}, None, {"seq": "x"}],
        "90000011": "not-a-list",
        "90000012": [{"bad": 1}],
    }
    (tmp_path / "role-image.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    got = role_image.load_role_image()
    assert got["90000010"] == [{"seq": 1, "name": "决胜", "grade": "S"}]
    assert "90000011" not in got
    assert "90000012" not in got  # 全非法的角色不进结果


def test_seq_coerced_to_int(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    raw = {"90000010": [{"seq": "2", "name": "孤注一掷", "grade": "A"}]}
    (tmp_path / "role-image.json").write_text(json.dumps(raw), encoding="utf-8")
    assert role_image.load_role_image()["90000010"] == [
        {"seq": 2, "name": "孤注一掷", "grade": "A"}
    ]
