"""char_shields 屏蔽ID持久化测试 - load/save/回退/兼容格式。"""
import json

from gacha_exporter.storage import char_shields


def _path(tmp_path, monkeypatch):
    p = str(tmp_path / "char_shields.json")
    monkeypatch.setattr(char_shields, "_shields_path", lambda: p)
    return p


def test_load_empty_when_missing(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    assert char_shields.load_shields() == set()


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    char_shields.save_shields({"999", "888"})
    assert char_shields.load_shields() == {"999", "888"}
    data = json.loads(open(p, encoding="utf-8").read())
    assert data == {"shielded": ["888", "999"]}  # sorted


def test_corrupt_file_falls_back(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    open(p, "w", encoding="utf-8").write("not json{")
    assert char_shields.load_shields() == set()


def test_legacy_bare_list_format(tmp_path, monkeypatch):
    """兼容裸数组格式。"""
    p = _path(tmp_path, monkeypatch)
    open(p, "w", encoding="utf-8").write(json.dumps(["111", "222"]))
    assert char_shields.load_shields() == {"111", "222"}


def test_non_string_ids_coerced(tmp_path, monkeypatch):
    """数字ID被转成字符串。"""
    p = _path(tmp_path, monkeypatch)
    open(p, "w", encoding="utf-8").write(json.dumps({"shielded": [111, "222"]}))
    assert char_shields.load_shields() == {"111", "222"}
