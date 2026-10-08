"""陌生卡池分组指派测试 - 持久化读写 + 指派合入 pools 后的统计分组。"""
import json

from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool
from gacha_exporter.storage import pool_assignments
from gacha_exporter.storage.pool_assignments import apply_pool_assignments


def _path(tmp_path, monkeypatch):
    p = str(tmp_path / "pool_assignments.json")
    monkeypatch.setattr(pool_assignments, "_assignments_path", lambda: p)
    return p


def _pools():
    return {
        "30001": Pool(id="30001", name="UP1", type="UP", order=1, up_character="UP角色",
                      pity_group="shared:UP"),
        "30002": Pool(id="30002", name="遴选1", type="遴选", order=1,
                      pity_group="shared:遴选"),
    }


def _groups():
    return {
        "shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True),
        "shared:遴选": PityGroup(id="shared:遴选", label="遴选池", hard_pity=70, has_state=True),
    }


# ---- 持久化 ----

def test_load_empty_when_missing(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    assert pool_assignments.load_pool_assignments() == {}


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = _path(tmp_path, monkeypatch)
    pool_assignments.save_pool_assignments({"30005": "shared:UP"})
    assert pool_assignments.load_pool_assignments() == {"30005": "shared:UP"}
    data = json.loads(open(p, encoding="utf-8").read())
    assert data == {"assignments": {"30005": "shared:UP"}}


def test_corrupt_file_falls_back(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    open(_path(tmp_path, monkeypatch), "w", encoding="utf-8").write("not json{")
    assert pool_assignments.load_pool_assignments() == {}


def test_drop_invalid_entries(tmp_path, monkeypatch):
    """指向空串/非字符串值的条目被丢弃。"""
    _path(tmp_path, monkeypatch)
    pool_assignments.save_pool_assignments({"30005": "shared:UP"})
    data = json.loads(open(pool_assignments._assignments_path(), encoding="utf-8").read())
    data["assignments"] = {"30005": "shared:UP", "30006": "", "30007": None}
    open(pool_assignments._assignments_path(), "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False)
    )
    assert pool_assignments.load_pool_assignments() == {"30005": "shared:UP"}


# ---- apply_pool_assignments ----

def test_apply_synthesizes_pool_for_unknown_pid():
    eff = apply_pool_assignments(_pools(), _groups(), {"30005": "shared:UP"})
    assert "30005" in eff
    p = eff["30005"]
    assert p.pity_group == "shared:UP"
    assert p.type == "UP"  # 组内已收录池的 type
    assert p.is_featured
    assert p.name == "未知(30005)"
    assert p.order == pool_assignments.UNKNOWN_POOL_ORDER
    # 原 pools 不被改动
    assert "30005" not in _pools()


def test_apply_type_follows_target_group():
    """指派到遴选组 -> type 取遴选池的 type。"""
    eff = apply_pool_assignments(_pools(), _groups(), {"30006": "shared:遴选"})
    assert eff["30006"].type == "遴选"


def test_apply_ignores_known_pool_and_undefined_group():
    """已收录池与指向未定义组的条目被忽略（元数据优先）。"""
    pools = _pools()
    eff = apply_pool_assignments(
        pools, _groups(), {"30001": "shared:遴选", "30007": "shared:不存在"}
    )
    assert eff["30001"] is pools["30001"]  # 原对象原样保留
    assert "30007" not in eff


# ---- 统计分组集成（算法不改，靠注入合成 Pool 生效）----

def _engine_with_assignment(records):
    eff = apply_pool_assignments(_pools(), _groups(), {"30005": "shared:UP"})
    chars = {
        "90000001": Character(id="90000001", name="UP角色", rarity=6),
        "14001001": Character(id="14001001", name="四星角色", rarity=4),
    }
    eng = StatsEngine(chars, eff, _groups())
    eng.load_records(records)
    return eng


def test_assigned_unknown_pool_joins_shared_group():
    """陌生池指派到 shared:UP 后，其记录并入该组共享保底一起计数。"""
    base = 1700000000000
    recs = [
        # 30001（UP1）抽 5 下出 UP 六星
        *[GachaRecord(pool_id="30001", item_id="90000001", timestamp=base + i * 1000,
                      batch_seq=0, position_in_batch=0) for i in range(4)],
        GachaRecord(pool_id="30001", item_id="90000001", timestamp=base + 5000,
                    batch_seq=0, position_in_batch=0),
        # 30005（陌生池，已指派 UP 组）再抽 3 下
        *[GachaRecord(pool_id="30005", item_id="14001001", timestamp=base + 6000 + i * 1000,
                      batch_seq=0, position_in_batch=0) for i in range(3)],
    ]
    tracking = _engine_with_assignment(recs).shared_pity_tracking()
    assert "solo:30005" not in tracking  # 不再是独立组
    assert "shared:UP" in tracking
    info = tracking["shared:UP"]
    assert info["total"] == 8  # 5 + 3 跨池合并
    assert info["current_pity"] == 3  # 六星后重新从 0 起数 3
    assert set(info["per_pool"].keys()) == {"30001", "30005"}


def test_unassigned_unknown_pool_stays_solo():
    """未指派的陌生池保持独立卡池兜底行为。"""
    base = 1700000000000
    recs = [GachaRecord(pool_id="30005", item_id="14001001", timestamp=base,
                        batch_seq=0, position_in_batch=0)]
    chars = {"90000001": Character(id="90000001", name="UP角色", rarity=6)}
    eng = StatsEngine(chars, _pools(), _groups())
    eng.load_records(recs)
    tracking = eng.shared_pity_tracking()
    assert "solo:30005" in tracking
    assert tracking["solo:30005"]["total"] == 1
