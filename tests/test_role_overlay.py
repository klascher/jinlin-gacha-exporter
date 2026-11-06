"""本地角色/卡池覆盖层（role_overlay）测试。

覆盖：缺失/损坏回退、合法/非法写入、原子写 + .bak、远端优先清理、
注入生效、单条删除、条目列举。全部走 tmp_path，不碰真实 data/。
"""
import json
import os

import pytest

from gacha_exporter.models import Character, Pool
from gacha_exporter.storage import role_overlay as ro


@pytest.fixture()
def ov_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ro, "get_data_dir", lambda: str(tmp_path))
    return ro


def _char_entry(name="示例角色018", rarity=6, limited=None):
    return {
        "name": name,
        "rarity": rarity,
        "limited": limited,
        "source": {"kind": "vision", "page": 7, "row": 2},
    }


def _pool_entry(name="示例卡池001"):
    return {
        "name": name,
        "type": "UP",
        "rarity": 6,
        "up_character": "示例角色018",
        "pity_group": "shared:UP",
        "source": {"kind": "vision", "page": 7, "row": 1},
    }


def test_load_missing_returns_empty(ov_env):
    data = ov_env.load_overlay()
    assert data["characters"] == {} and data["pools"] == {}
    assert data["version"] == 1


def test_load_corrupted_returns_empty(ov_env, tmp_path):
    (tmp_path / "role_overlay.json").write_text("{not json", encoding="utf-8")
    assert ov_env.load_overlay()["characters"] == {}


def test_upsert_valid_and_reload(ov_env):
    assert ov_env.upsert_character("90000024", _char_entry())
    assert ov_env.upsert_pool("20009", _pool_entry())
    data = ov_env.load_overlay()
    assert data["characters"]["90000024"]["name"] == "示例角色018"
    assert data["pools"]["20009"]["type"] == "UP"
    assert data["characters"]["90000024"]["source"]["confirmed_at"]
    assert data["characters"]["90000024"]["source"]["anchor_trail"] == "p7.r2"
    assert data["pools"]["20009"]["source"]["anchor_trail"] == "p7.r1"


def test_upsert_rejects_invalid(ov_env):
    assert not ov_env.upsert_character("90000024", {"name": "", "rarity": 6})
    assert not ov_env.upsert_character("90000024", {"name": "x", "rarity": 3})
    assert not ov_env.upsert_pool("20009", {"name": "", "type": "UP"})
    assert ov_env.load_overlay()["characters"] == {}


def test_second_write_creates_backup(ov_env, tmp_path):
    ov_env.upsert_character("90000024", _char_entry())
    ov_env.upsert_character("90000024", _char_entry(name="示例角色0182"))
    assert os.path.isfile(tmp_path / "role_overlay.json.bak")
    bak = json.loads((tmp_path / "role_overlay.json.bak").read_text(encoding="utf-8"))
    assert bak["characters"]["90000024"]["name"] == "示例角色018"
    assert not os.path.exists(tmp_path / "role_overlay.json.tmp")


def test_merge_injects_and_prunes_remote(ov_env):
    ov_env.upsert_character("90000024", _char_entry())
    ov_env.upsert_character("13001099", _char_entry(name="已收录角色", rarity=5))
    ov_env.upsert_pool("20009", _pool_entry())
    ov_env.upsert_pool("20001", _pool_entry(name="已收录池"))

    chars = {"13001099": Character(id="13001099", name="远端名", rarity=5)}
    pools = {"20001": Pool(id="20001", name="远端池", type="常驻")}
    added = ov_env.merge_into(chars, pools)

    assert added == (1, 1)
    assert chars["90000024"].name == "示例角色018"
    assert chars["13001099"].name == "远端名"  # 远端优先
    assert pools["20009"].pity_group == "shared:UP"
    assert pools["20001"].name == "远端池"
    # 同 id 条目已从文件清理
    data = ov_env.load_overlay()
    assert "13001099" not in data["characters"] and "20001" not in data["pools"]
    assert "90000024" in data["characters"] and "20009" in data["pools"]


def test_merge_skips_invalid_entry(ov_env, tmp_path):
    path = tmp_path / "role_overlay.json"
    path.write_text(json.dumps({
        "characters": {"90000024": {"name": "", "rarity": 6}},
        "pools": {},
    }), encoding="utf-8")
    chars, pools = {}, {}
    assert ov_env.merge_into(chars, pools) == (0, 0)
    assert chars == {} and pools == {}


def test_remove_character_and_pool(ov_env):
    ov_env.upsert_character("90000024", _char_entry())
    ov_env.upsert_pool("20009", _pool_entry())
    assert ov_env.remove_character("90000024")
    assert not ov_env.remove_character("90000024")
    assert ov_env.remove_pool("20009")
    data = ov_env.load_overlay()
    assert data["characters"] == {} and data["pools"] == {}


def test_list_entries(ov_env):
    ov_env.upsert_character("90000024", _char_entry())
    ov_env.upsert_pool("20009", _pool_entry())
    items = ov_env.list_entries()
    assert [(i["kind"], i["id"]) for i in items] == [("character", "90000024"), ("pool", "20009")]
