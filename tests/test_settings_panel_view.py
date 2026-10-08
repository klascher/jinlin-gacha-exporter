"""保底面板视图偏好（pity_panel_view）读写测试 - 按卡池/按角色跨启动记忆。"""
import json

from gacha_exporter.storage import settings


def _path(tmp_path, monkeypatch):
    p = str(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_settings_path", lambda: p)
    return p


def test_default_pool_when_missing(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    assert settings.load_pity_panel_view() == "pool"


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    settings.save_pity_panel_view("flow")
    assert settings.load_pity_panel_view() == "flow"
    data = json.loads(open(p, encoding="utf-8").read())
    assert data["pity_panel_view"] == "flow"


def test_invalid_value_falls_back(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"pity_panel_view": "bogus"}, f)
    assert settings.load_pity_panel_view() == "pool"
    settings.save_pity_panel_view("whatever")  # 非法值不写入
    assert settings.load_pity_panel_view() == "pool"
