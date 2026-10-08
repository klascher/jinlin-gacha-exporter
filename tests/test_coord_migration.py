"""坐标统一启动迁移测试（T1~T14）。

方案：`PC端统一坐标迁移-落码方案-2026-09-20.md` §8。
验收不变量：G1~G11（见方案 §1）。

约定跟随仓库既有测试风格（`tests/test_history.py` / `test_legacy_history_compat.py`）：
pytest 函数式 + `tempfile`/`tmp_path` 临时 `users/` + `patch("...get_users_dir")`。

⚠️ 必须**同时** patch 两个模块的 `get_users_dir`：
`coord_migration` 用它找正式文件，`history` 用它给 `backup_account_history`
定位备份目录（`HistoryService.__init__`）。只 patch 一个会导致备份走到真实
data 目录并抛 FileNotFoundError。
"""
import hashlib
import json
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from gacha_exporter.analysis.coord_unify import apply_unify, plan_unify
from gacha_exporter.analysis.pos_align import merge_by_position
from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.models import GachaRecord
from gacha_exporter.storage.coord_migration import (
    is_official_file,
    migrate_coord_frame,
    migrate_one,
)
from gacha_exporter.storage.history import HistoryService

_PID = "default"
_FIELDS = {"pool_id", "item_id", "timestamp", "batch_seq", "position_in_batch"}
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_legacy.json"


# ---------- 环境与工具 ----------

@contextmanager
def _env(tmp):
    """把两个模块的 `get_users_dir` 都指向临时 `users/`。"""
    users = Path(tmp, "users")
    users.mkdir(parents=True, exist_ok=True)
    with patch(
        "gacha_exporter.storage.coord_migration.get_users_dir",
        return_value=str(users),
    ), patch(
        "gacha_exporter.storage.history.get_users_dir",
        return_value=str(users),
    ):
        yield users


def _write(path, payload):
    """写一份 JSON（缩进与迁移写盘一致，便于逐字节比对）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _core_sha(payload):
    """三字段序列的哈希（G4：`pool_id`/`item_id`/`timestamp` 逐条不变）。"""
    core = [[r["pool_id"], r["item_id"], r["timestamp"]] for r in payload["records"]]
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _rec(ts, bs, pos, pool="30001", item="100"):
    return {
        "pool_id": pool,
        "item_id": item,
        "timestamp": ts,
        "batch_seq": bs,
        "position_in_batch": pos,
    }


def _legacy_payload(**extra):
    """旧格式 + 新格式混坐标；数组序 == 统一序（真实文件实测即如此）。

    形态仿真实旧格式（见 `test_legacy_history_compat.py` 的开篇说明）：
    十连跨两页（每页 5 条、页内序 0..4，两页 `batch_seq` 不同），
    单抽一页 1 条；另含一个只抓到一页的残缺事件与一条新格式单抽。
    """
    records = []
    records += [_rec(4000, 7, i, item=f"a{i}") for i in range(5)]   # 残缺：只有一页
    records += [_rec(3000, 0, 0, item="b0")]                        # 新格式单抽
    records += [_rec(2000, 3, i, item=f"c{i}") for i in range(5)]   # 旧十连上半页
    records += [_rec(2000, 4, i, item=f"d{i}") for i in range(5)]   # 旧十连下半页
    records += [_rec(1000, 1, 0, item="e0")]                        # 旧格式单抽
    payload = {
        "session": {
            "current_pool_id": "30001",
            "capture_start": "2026-08-01T00:00:00",
        },
        "records": records,
        "view_totals": {"0@0": 17},
        "total": 17,
    }
    payload.update(extra)
    return payload


def _unified_payload():
    """已统一形态：`batch_seq` 全 0、`position_in_batch` == 数组下标。"""
    payload = _legacy_payload()
    for idx, rec in enumerate(payload["records"]):
        rec["batch_seq"] = 0
        rec["position_in_batch"] = idx
    return payload


def _setup_and_migrate(tmp_path, payload=None):
    """写一份文件并迁移；返回 (users, path, before, before_bytes, result)。"""
    users = Path(tmp_path, "users")
    users.mkdir(parents=True, exist_ok=True)
    path = users / f"{_PID}.json"
    before = _legacy_payload() if payload is None else payload
    _write(path, before)
    before_bytes = path.read_bytes()
    return users, path, before, before_bytes, migrate_one(str(path))


def _pity_index(records):
    """`_order_records_for_pity`（旧->新）的结果映射回输入列表下标（G6）。

    用 `id()` 映射：`_order_within_event` 返回的是**原对象引用**（`sorted`
    不复制元素）。若将来它改成构造新对象，这里会抛 KeyError —— 显式失败，
    不会静默给出错误结论。
    """
    engine = StatsEngine({}, {}, {})
    ordered = engine._order_records_for_pity(records)
    idx_of = {id(r): i for i, r in enumerate(records)}
    return [idx_of[id(r)] for r in ordered]


# ---------- T1：已统一文件零写入 ----------

def test_t1_already_unified_skips_without_write(tmp_path):
    """已统一（全 bs=0）⇒ skipped；文件字节不变；无任何备份。"""
    payload = _unified_payload()
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, payload)
        sha_before = _sha(path)

        res = migrate_one(str(path))

        assert res.status == "skipped"
        assert res.count == len(payload["records"])
        assert _sha(path) == sha_before, "已统一文件被改写（违反幂等）"
        assert list(users.glob(f"{_PID}_*.json")) == [], "skipped 却产生了备份"
        assert not Path(str(path) + ".pre_unify").exists()


# ---------- T2~T5：迁移正确性与不变量 ----------

def test_t2_unifies_to_dense_frame_and_self_checks(tmp_path):
    """迁移后 bs 全 0、pos 稠密且 == 数组下标；条数不变；幂等自检 added=0。"""
    with _env(tmp_path):
        _, path, before, _, res = _setup_and_migrate(tmp_path)
        after = _read(path)

        assert res.status == "migrated"
        n = len(before["records"])
        assert len(after["records"]) == n                      # G3
        assert all(r["batch_seq"] == 0 for r in after["records"])   # G1
        assert [r["position_in_batch"] for r in after["records"]] == list(range(n))  # G2

        # G7：幂等自检 —— 统一后的数据再喂给判重算法，一条都不该新增
        recs = [GachaRecord(**r) for r in after["records"]]
        assert merge_by_position(recs, recs, shift=0).added == []


def test_t3_array_order_unchanged(tmp_path):
    """G5：数组顺序逐条不变（迁移只改两个辅助字段）。"""
    with _env(tmp_path):
        _, path, before, _, _ = _setup_and_migrate(tmp_path)
        after = _read(path)
        core = ("pool_id", "item_id", "timestamp")
        assert [tuple(r[k] for k in core) for r in after["records"]] == [
            tuple(r[k] for k in core) for r in before["records"]
        ]


def test_t4_core_three_fields_unchanged(tmp_path):
    """G4：pool_id / item_id / timestamp 逐条逐字段不变。"""
    with _env(tmp_path):
        _, path, before, _, _ = _setup_and_migrate(tmp_path)
        after = _read(path)
        assert _core_sha(after) == _core_sha(before)


def test_t5_pity_consumption_order_unchanged(tmp_path):
    """G6：保底消费序（`_order_records_for_pity`）的原始下标置换逐位不变。"""
    with _env(tmp_path):
        _, path, before, _, _ = _setup_and_migrate(tmp_path)
        after = _read(path)

        before_recs = [GachaRecord(**r) for r in before["records"]]
        after_recs = [GachaRecord(**r) for r in after["records"]]

        assert _pity_index(before_recs) == _pity_index(after_recs)


# ---------- T6~T7：加载无损与键集合 ----------

def test_t6_history_service_loads_losslessly(tmp_path):
    """G10：HistoryService 读回后条数、三字段、total、view_totals 全等。"""
    with _env(tmp_path):
        _, path, before, _, _ = _setup_and_migrate(tmp_path)
        svc = HistoryService(_PID)

        assert len(svc.records) == len(before["records"])
        assert [r.key() for r in svc.records] == [
            GachaRecord(**r).key() for r in before["records"]
        ]
        assert svc.last_total == 17
        assert all(r.batch_seq == 0 for r in svc.records)


def test_t7_every_record_has_exactly_five_keys(tmp_path):
    """G11：每条恰好 5 个键 —— 多一个键就会让 `GachaRecord(**r)` 抛 TypeError，
    被 `_load()` 吞掉 ⇒ 整份历史静默变空。"""
    with _env(tmp_path):
        _, path, _, _, _ = _setup_and_migrate(tmp_path)
        after = _read(path)
        assert all(set(r.keys()) == _FIELDS for r in after["records"])


# ---------- T8~T10：非破坏式、残缺、混层 ----------

def test_t8_preserves_unknown_top_level_keys(tmp_path):
    """G（§2.3）：`session` / Android 3 键 / 自造未知键一律原样保留。"""
    payload = _legacy_payload(
        scan_gaps=[1, 2, 3],
        source_packages=["x", "y"],
        last_capture_detail={"added": 5},
        zzz={"a": 1},
    )
    with _env(tmp_path):
        _, path, _, _, res = _setup_and_migrate(tmp_path, payload)
        assert res.status == "migrated"
        after = _read(path)

        assert after["scan_gaps"] == [1, 2, 3]
        assert after["source_packages"] == ["x", "y"]
        assert after["last_capture_detail"] == {"added": 5}
        assert after["zzz"] == {"a": 1}
        assert after["session"] == payload["session"]
        assert after["view_totals"] == {"0@0": 17}
        assert after["total"] == 17


def test_t9_missing_total_stays_none(tmp_path):
    """残缺事件存在时 `total` 保持 None —— 绝不用记录条数冒充权威总数。"""
    payload = _legacy_payload(total=None)
    payload["view_totals"] = {}
    with _env(tmp_path):
        _, path, _, _, res = _setup_and_migrate(tmp_path, payload)
        assert res.status == "migrated"
        after = _read(path)
        assert after["total"] is None
        assert after["view_totals"] == {}


def test_t10_mixed_event_ordered_by_convention_with_warning(tmp_path, caplog):
    """混层事件（同 ts 既旧又新）：不抛异常、按既有约定定序、留警告日志。"""
    payload = {
        "records": [
            _rec(5000, 0, 0, item="m0"),   # 新格式（batch_seq 小 = 更新）
            _rec(5000, 3, 1, item="m1"),   # 旧格式
        ],
        "total": None,
    }
    plan = plan_unify([GachaRecord(**r) for r in payload["records"]])
    assert plan.mixed_events == [5000]
    assert plan.warnings, "混层事件必须产生提示"

    with _env(tmp_path):
        path = Path(tmp_path, "users", f"{_PID}.json")
        _write(path, payload)
        with caplog.at_level(
            logging.WARNING, logger="gacha_exporter.storage.coord_migration"
        ):
            res = migrate_one(str(path))
        after = _read(path)

    assert res.status == "migrated"
    assert [r["item_id"] for r in after["records"]] == ["m0", "m1"]
    assert [r["position_in_batch"] for r in after["records"]] == [0, 1]
    assert any("混层" in rec.getMessage() for rec in caplog.records)


# ---------- T11~T13：备份与幂等 ----------

def test_t11_two_backups_pre_unify_is_byte_identical(tmp_path):
    """双份备份：既有规约快照（GUI 可还原）+ `.pre_unify`（防裁剪），
    且两者都与迁移前的文件逐字节相同。"""
    with _env(tmp_path) as users:
        _, path, _, before_bytes, res = _setup_and_migrate(tmp_path)

        assert res.status == "migrated"
        pre = Path(str(path) + ".pre_unify")
        assert pre.exists(), "缺少迁移专属快照"
        assert pre.read_bytes() == before_bytes

        assert res.snapshot is not None
        snapshots = list(users.glob(f"{_PID}_*.json"))
        assert len(snapshots) == 1, f"既有规约快照数量异常: {snapshots}"
        assert snapshots[0].read_bytes() == before_bytes

        # 既有规约快照必须能被 GUI 的「还原备份」列表识别（命名单源的意义）
        infos = HistoryService(_PID).backups()
        assert [i.path for i in infos] == [res.snapshot]


def test_t12_second_run_writes_nothing(tmp_path):
    """G9：第二次运行 skipped，文件哈希不变，且不再新增备份。"""
    with _env(tmp_path) as users:
        _, path, _, _, first = _setup_and_migrate(tmp_path)
        assert first.status == "migrated"
        sha_after_first = _sha(path)
        backups_after_first = sorted(p.name for p in users.glob("*.json"))

        second = migrate_one(str(path))

        assert second.status == "skipped"
        assert _sha(path) == sha_after_first
        assert sorted(p.name for p in users.glob("*.json")) == backups_after_first


def test_t13_backup_files_are_never_migrated(tmp_path):
    """备份快照必须排除：`<id>_<ts>.json` 与 `_N` 变体哈希不变。"""
    with _env(tmp_path) as users:
        official = users / f"{_PID}.json"
        _write(official, _legacy_payload())
        b1 = users / f"{_PID}_20260920_120000.json"
        b2 = users / f"{_PID}_20260920_120000_1.json"
        _write(b1, _legacy_payload())
        _write(b2, _legacy_payload())
        sha1, sha2 = _sha(b1), _sha(b2)

        results = migrate_coord_frame()

        assert _sha(b1) == sha1, "备份快照被当成正式文件迁移了"
        assert _sha(b2) == sha2
        assert {r.pid for r in results} == {_PID}
        assert results[0].status == "migrated"

    # 判据本身（含 `.pre_unify` / `.tmp` 的排除）
    assert is_official_file(f"{_PID}.json")
    assert not is_official_file(f"{_PID}_20260920_120000.json")
    assert not is_official_file(f"{_PID}_20260920_120000_1.json")
    assert not is_official_file(f"{_PID}.json.pre_unify")
    assert not is_official_file(f"{_PID}.json.tmp")
    assert not is_official_file("notes.txt")


# ---------- T14：真实旧格式样本 ----------

def test_t14_real_legacy_fixture_end_to_end(tmp_path):
    """用仓库真实旧格式样本（`tests/fixtures/sample_legacy.json`，521 条）跑全程。

    这是**真实数据**而非合成数据：含跨页十连、缺页（bs 4/40/79 缺号）、
    `total=None`。迁移后必须满足 G1~G5/G7/G11。
    """
    if not _FIXTURE.exists():
        pytest.skip(f"fixture 缺失: {_FIXTURE}")
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    n = len(payload["records"])
    before_recs = [GachaRecord(**r) for r in payload["records"]]

    plan = plan_unify(before_recs)
    assert plan.needed, "真实旧格式样本竟被认为无需迁移"
    assert plan.array_already_ordered, "真实样本的数组序与统一序不一致"
    assert len(plan.legacy_events) > 0

    with _env(tmp_path):
        _, path, _, _, res = _setup_and_migrate(tmp_path, payload)
        assert res.status == "migrated"
        after = _read(path)
        after_recs = [GachaRecord(**r) for r in after["records"]]

    assert len(after["records"]) == n                                   # G3
    assert all(r["batch_seq"] == 0 for r in after["records"])            # G1
    assert [r["position_in_batch"] for r in after["records"]] == list(range(n))  # G2
    assert [r["pool_id"] for r in after["records"]] == [r["pool_id"] for r in payload["records"]]
    assert [r["item_id"] for r in after["records"]] == [r["item_id"] for r in payload["records"]]
    assert [r["timestamp"] for r in after["records"]] == [r["timestamp"] for r in payload["records"]]  # G4/G5
    assert all(set(r.keys()) == _FIELDS for r in after["records"])       # G11
    assert after["total"] is None             # D7：不冒充
    assert after["view_totals"] == {}
    assert _pity_index(before_recs) == _pity_index(after_recs)           # G6
    assert merge_by_position(after_recs, after_recs, shift=0).added == []  # G7


# ---------- 纯函数层（快速回归） ----------

def test_pure_functions_on_empty_and_single():
    """空序列与单条序列的边界。"""
    assert plan_unify([]).needed is False
    assert plan_unify([]).order == []
    one = [_rec(1, 0, 0)]
    recs = [GachaRecord(**r) for r in one]
    assert plan_unify(recs).needed is False

    legacy_one = [GachaRecord(**_rec(1, 5, 2))]
    plan = plan_unify(legacy_one)
    assert plan.needed is True
    assert apply_unify(legacy_one, plan)[0].position_in_batch == 0
