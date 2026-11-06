"""启动数据清理测试 - 公告已读失效条目 + 已收录池指派条目的静默清理。"""
import json
import os

from gacha_exporter.models import Pool
from gacha_exporter.storage import announcements, pool_assignments


# ---- cleanup_read_ids（公告已读清理）----

def _read_path(tmp_path, monkeypatch):
    p = str(tmp_path / "announcements_read.json")
    monkeypatch.setattr(announcements, "_read_path", lambda: p)
    return p


def test_cleanup_skips_when_live_empty(tmp_path, monkeypatch):
    """live_ids 为空集直接跳过（失败与真空列表无法区分），已读记录原样保留。"""
    p = _read_path(tmp_path, monkeypatch)
    announcements._save_read_ids({"1", "2"})
    assert announcements.cleanup_read_ids(set()) == 0
    assert announcements.load_read_ids() == {"1", "2"}


def test_cleanup_removes_stale_ids(tmp_path, monkeypatch):
    """公告列表已不存在的 id 清掉，仍在列表里的保留。"""
    p = _read_path(tmp_path, monkeypatch)
    announcements._save_read_ids({"1", "2", "3"})
    assert announcements.cleanup_read_ids({"2", "3", "4"}) == 1
    assert announcements.load_read_ids() == {"2", "3"}


def test_cleanup_noop_without_stale(tmp_path, monkeypatch):
    """无失效条目时零写入（不产生/不改动文件内容）。"""
    p = _read_path(tmp_path, monkeypatch)
    announcements._save_read_ids({"1"})
    before = open(p, encoding="utf-8").read()
    assert announcements.cleanup_read_ids({"1", "2"}) == 0
    assert open(p, encoding="utf-8").read() == before


def test_cleanup_noop_when_no_read_file(tmp_path, monkeypatch):
    """从未有已读记录：直接返回 0，不创建文件。"""
    p = _read_path(tmp_path, monkeypatch)
    assert announcements.cleanup_read_ids({"1"}) == 0
    assert not os.path.isfile(p)


def test_cleanup_int_ids_coerced(tmp_path, monkeypatch):
    """live_ids 混入数字 id 也按字符串对齐（与 load_read_ids 口径一致）。"""
    _read_path(tmp_path, monkeypatch)
    announcements._save_read_ids({"10"})
    assert announcements.cleanup_read_ids({10}) == 0


# ---- cleanup_pool_assignments（陌生池指派清理）----

def _assign_path(tmp_path, monkeypatch):
    p = str(tmp_path / "pool_assignments.json")
    monkeypatch.setattr(pool_assignments, "_assignments_path", lambda: p)
    return p


def _pools():
    return {
        "30001": Pool(id="30001", name="UP1", type="UP", order=1, up_character="UP角色",
                      pity_group="shared:UP"),
    }


def test_assignment_cleanup_removes_collected(tmp_path, monkeypatch):
    """元数据已收录的池指派清掉，未收录的保留。"""
    _assign_path(tmp_path, monkeypatch)
    pool_assignments.save_pool_assignments({"30001": "shared:UP", "30005": "shared:UP"})
    assert pool_assignments.cleanup_pool_assignments(_pools()) == 1
    assert pool_assignments.load_pool_assignments() == {"30005": "shared:UP"}


def test_assignment_cleanup_noop_without_stale(tmp_path, monkeypatch):
    """全部条目均指向未收录池：零写入（文件内容不变）。"""
    p = _assign_path(tmp_path, monkeypatch)
    pool_assignments.save_pool_assignments({"30005": "shared:UP"})
    before = open(p, encoding="utf-8").read()
    assert pool_assignments.cleanup_pool_assignments(_pools()) == 0
    assert open(p, encoding="utf-8").read() == before


def test_assignment_cleanup_noop_when_no_file(tmp_path, monkeypatch):
    """从未有指派文件：直接返回 0，不创建文件。"""
    _assign_path(tmp_path, monkeypatch)
    assert pool_assignments.cleanup_pool_assignments(_pools()) == 0
    assert not os.path.isfile(str(tmp_path / "pool_assignments.json"))


def test_assignment_cleanup_clears_all(tmp_path, monkeypatch):
    """全部条目都已收录：清成空 assignments。"""
    _assign_path(tmp_path, monkeypatch)
    pool_assignments.save_pool_assignments({"30001": "shared:UP"})
    assert pool_assignments.cleanup_pool_assignments(_pools()) == 1
    data = json.loads(open(pool_assignments._assignments_path(), encoding="utf-8").read())
    assert data == {"assignments": {}}
