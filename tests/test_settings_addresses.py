"""抽卡地址按账号隔离测试 - 读写隔离 + 旧全局列表迁移。"""
import json

from gacha_exporter.storage import settings


def _path(tmp_path, monkeypatch):
    p = str(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_settings_path", lambda: p)
    return p


# ---- 读写 ----

def test_load_empty_when_missing(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    assert settings.load_discovered_addresses("acc1") == ()


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    settings.save_discovered_addresses("acc1", [("1.2.3.4", 12345)])
    assert settings.load_discovered_addresses("acc1") == (("1.2.3.4", 12345),)
    data = json.loads(open(p, encoding="utf-8").read())
    assert data["per_profile_discovered_addresses"] == {"acc1": [["1.2.3.4", 12345]]}


def test_profiles_isolated(tmp_path, monkeypatch):
    """账号间地址互不影响：各存各的，未存过的账号为空。"""
    _path(tmp_path, monkeypatch)
    settings.save_discovered_addresses("acc1", [("1.1.1.1", 1000)])
    settings.save_discovered_addresses("acc2", [("2.2.2.2", 2000)])
    assert settings.load_discovered_addresses("acc1") == (("1.1.1.1", 1000),)
    assert settings.load_discovered_addresses("acc2") == (("2.2.2.2", 2000),)
    assert settings.load_discovered_addresses("acc3") == ()
    assert settings.load_discovered_addresses("") == ()


def test_save_overwrite_per_profile(tmp_path, monkeypatch):
    """保存替换该账号的地址，不动其他账号。"""
    _path(tmp_path, monkeypatch)
    settings.save_discovered_addresses("acc1", [("1.1.1.1", 1000)])
    settings.save_discovered_addresses("acc2", [("2.2.2.2", 2000)])
    settings.save_discovered_addresses("acc1", [("3.3.3.3", 3000)])
    assert settings.load_discovered_addresses("acc1") == (("3.3.3.3", 3000),)
    assert settings.load_discovered_addresses("acc2") == (("2.2.2.2", 2000),)


def test_invalid_entries_filtered(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    data = {"per_profile_discovered_addresses": {
        "acc1": [["1.1.1.1", 1000], ["bad"], "x", [1, 2]],
    }}
    open(p, "w", encoding="utf-8").write(json.dumps(data))
    assert settings.load_discovered_addresses("acc1") == (("1.1.1.1", 1000),)


# ---- 旧全局列表迁移 ----

def test_migrate_legacy_global_list(tmp_path, monkeypatch):
    """旧全局列表复制给当时已存在的全部账号，且旧键删除。"""
    p = _path(tmp_path, monkeypatch)
    data = {"discovered_addresses": [["1.2.3.4", 12345]]}
    open(p, "w", encoding="utf-8").write(json.dumps(data))
    settings.migrate_global_discovered_addresses(["acc1", "acc2"])
    assert settings.load_discovered_addresses("acc1") == (("1.2.3.4", 12345),)
    assert settings.load_discovered_addresses("acc2") == (("1.2.3.4", 12345),)
    assert "discovered_addresses" not in json.loads(open(p, encoding="utf-8").read())
    # 迁移后再新建的账号无地址（由界面引导获取）
    assert settings.load_discovered_addresses("acc3") == ()


def test_migrate_idempotent(tmp_path, monkeypatch):
    """已是 dict 格式时不重复迁移，也不覆盖已有账号地址。"""
    p = _path(tmp_path, monkeypatch)
    settings.save_discovered_addresses("acc1", [("9.9.9.9", 9999)])
    settings.migrate_global_discovered_addresses(["acc1", "acc2"])
    assert settings.load_discovered_addresses("acc1") == (("9.9.9.9", 9999),)
    assert settings.load_discovered_addresses("acc2") == ()
    assert "discovered_addresses" not in json.loads(open(p, encoding="utf-8").read())


def test_migrate_no_legacy_noop(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    settings.migrate_global_discovered_addresses(["acc1"])  # 不应抛错
    assert settings.load_discovered_addresses("acc1") == ()
