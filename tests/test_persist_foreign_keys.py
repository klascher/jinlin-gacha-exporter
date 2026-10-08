"""PC 写盘透传跨端键测试（T1~T8 + 纯函数用例）。

方案：`PC写盘保留跨端键-可行性与方案-2026-09-20.md` §4 / §7。
决策（2026-09-20 拍板）：**全透传 + 特判**（D1=C）；`last_capture_detail`
**清成 null**（D2）；测试同轮落（D3）。

约定跟随仓库既有测试风格（`tests/test_coord_migration.py` / `test_history.py`）：
pytest 函数式 + `tmp_path` 临时 `users/` + `patch("...get_users_dir")`。

⚠️ `gacha_exporter.storage.history` 与 `...coord_migration` 各自模块级导入了
`get_users_dir`，T8 必须**同时** patch 两个；只 patch 一个会让备份走到真实
data 目录。
"""
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from gacha_exporter.models import GachaRecord
from gacha_exporter.storage.coord_migration import migrate_one
from gacha_exporter.storage.history import HistoryService

_PID = "default"
_OWNED = {"records", "view_totals", "total"}
_FOREIGN_KEPT = ("scan_gaps", "source_packages")
_CLEARED = "last_capture_detail"


# ---------- 环境与工具 ----------

@contextmanager
def _env(tmp):
    """把两个模块的 `get_users_dir` 都指向临时 `users/`。"""
    users = Path(tmp, "users")
    users.mkdir(parents=True, exist_ok=True)
    with patch(
        "gacha_exporter.storage.history.get_users_dir",
        return_value=str(users),
    ), patch(
        "gacha_exporter.storage.coord_migration.get_users_dir",
        return_value=str(users),
    ):
        yield users


def _write(path, payload):
    """写一份 JSON（缩进与 `_write_payload` 一致，便于逐字节比对）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rec(ts, bs=0, pos=0, pool="30001", item="100"):
    return {
        "pool_id": pool,
        "item_id": item,
        "timestamp": ts,
        "batch_seq": bs,
        "position_in_batch": pos,
    }


def _android_payload(records=None, total=10, **extra):
    """模拟安卓 `HistoryStore.buildPayload()` 写出的文件（6 个业务键，无 `session`）。"""
    payload = {
        "records": records if records is not None else [_rec(1000)],
        "view_totals": {"0@0": total},
        "total": total,
        "scan_gaps": {"0@0": [0, 5]},
        "source_packages": ["com.bmystu.peng", "com.bmystu.peng.mi"],
        "last_capture_detail": {
            "started_at": "2026-08-17T21:00:00",
            "ended_at": "2026-08-17T21:52:43",
            "added": 601,
        },
    }
    payload.update(extra)
    return payload


# ---------- T1~T2：六键文件的写盘行为 ----------

def test_t1_no_new_records_keeps_keys_and_clears_detail(tmp_path):
    """T1：六键文件 + 无新增 merge ⇒ 未知键原样保留；`last_capture_detail` 清空。"""
    payload = _android_payload()
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, payload)

        added = HistoryService(_PID).merge([], {})

        after = _read(path)

    assert added == 0
    assert after["scan_gaps"] == payload["scan_gaps"]
    assert after["source_packages"] == payload["source_packages"]
    assert after[_CLEARED] is None, "陈旧明细必须清掉，否则手机「上次 N 条」会对不上"
    assert set(after) == _OWNED | set(_FOREIGN_KEPT) | {_CLEARED}


def test_t2_new_records_preserve_keys_and_update_total(tmp_path):
    """T2：六键文件 + 有新增 merge ⇒ 键照旧保留；记录正确追加；`total` 更新。"""
    payload = _android_payload(records=[_rec(1000, item="old")], total=1)
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, payload)

        new = [GachaRecord(**_rec(2000, item="new"))]
        added = HistoryService(_PID).merge(new, {"0@0": 2})

        after = _read(path)

    assert added == 1
    assert [r["item_id"] for r in after["records"]] == ["new", "old"]  # ts 降序
    assert after["total"] == 2
    assert after["view_totals"] == {"0@0": 2}
    assert after["scan_gaps"] == payload["scan_gaps"]
    assert after["source_packages"] == payload["source_packages"]
    assert after[_CLEARED] is None


# ---------- T3：幂等闸（唯一的真风险） ----------

def test_t3_idempotent_gate_prevents_backup_flood(tmp_path):
    """T3：同内容连跑两次 ⇒ 第二次不写盘、不新增备份。

    幂等闸必须用**并入跨端键之后**的 payload 比较：用朴素内存 payload 去比，
    磁盘有未知键时恒不等 ⇒ 每次收尾都写盘 + 造备份。
    """
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, _android_payload())

        svc = HistoryService(_PID)
        svc.merge([], {})  # 第一次：明细归一为 null ⇒ 真实写盘
        first_bytes = path.read_bytes()
        first_files = sorted(p.name for p in users.glob("*.json"))

        svc.merge([], {})  # 第二次：内容零变化

        assert path.read_bytes() == first_bytes, "幂等闸失效：重复写盘"
        assert sorted(p.name for p in users.glob("*.json")) == first_files, "备份洪水"


# ---------- T4：clear() 必须不透传 ----------

def test_t4_clear_drops_all_foreign_keys(tmp_path):
    """T4：`clear()` 不透传 —— 跨端三键与 PC 的 `session` 一并清掉（对齐安卓）。"""
    payload = _android_payload()
    payload["session"] = {
        "current_pool_id": "30001",
        "capture_start": "2026-08-17T21:00:00",
    }
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, payload)

        backup = HistoryService(_PID).clear()

        after = _read(path)

    assert backup is not None
    assert set(after) == _OWNED, f"清空后仍有残留键: {sorted(set(after) - _OWNED)}"
    assert after["records"] == []
    assert after["view_totals"] == {}
    assert after["total"] is None


# ---------- T5~T6：未知键与损坏件 ----------

def test_t5_future_unknown_key_is_carried(tmp_path):
    """T5：凭空多出的未知键也原样保留（将来安卓加键不会再丢）。"""
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, _android_payload(future_key={"v": [1, 2, 3]}))

        HistoryService(_PID).merge([], {})

        after = _read(path)

    assert after["future_key"] == {"v": [1, 2, 3]}


def test_t6_corrupt_file_salvaged_without_raise(tmp_path):
    """T6：文件损坏不可读 ⇒ 沿用既有 salvage 分支：不抛异常、为损坏件留一份备份。"""
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        path.write_text("{ this is not json", encoding="utf-8")

        svc = HistoryService(_PID)
        svc.merge([], {})  # 不得抛异常

        after = _read(path)
        infos = svc.backups()
        assert after == {"records": [], "view_totals": {}, "total": None}
        assert infos, "损坏件也必须 salvage 一份，不能直接覆盖丢弃"
        assert Path(infos[0].path).read_text(encoding="utf-8") == "{ this is not json"


# ---------- T7：restore 往返 ----------

def test_t7_restore_roundtrip_byte_identical(tmp_path):
    """T7：`restore()` 后正式文件与所选备份逐字节一致（跨端键随之回来）。"""
    snap_payload = _android_payload()
    with _env(tmp_path) as users:
        official = users / f"{_PID}.json"
        _write(official, {"records": [], "view_totals": {}, "total": None})

        snap = users / f"{_PID}_20260920_120000.json"
        _write(snap, snap_payload)
        snap_bytes = snap.read_bytes()

        HistoryService(_PID).restore(str(snap))

        assert official.read_bytes() == snap_bytes
        after = _read(official)

    assert after["source_packages"] == snap_payload["source_packages"]
    assert after[_CLEARED] == snap_payload[_CLEARED]
    assert after["scan_gaps"] == snap_payload["scan_gaps"]


# ---------- T8：与坐标迁移共存 ----------

def test_t8_coexists_with_coord_migration(tmp_path):
    """T8：先跑坐标迁移（非破坏式，三键原样留）→ 再 merge：未知键留住、明细被清。"""
    legacy = {
        "records": [
            _rec(3000, bs=3, pos=1, item="b"),
            _rec(3000, bs=4, pos=0, item="a"),
        ],
        "view_totals": {"0@0": 2},
        "total": 2,
        "scan_gaps": {"0@0": [0]},
        "source_packages": ["com.bmystu.peng"],
        "last_capture_detail": {"added": 2},
    }
    with _env(tmp_path) as users:
        path = users / f"{_PID}.json"
        _write(path, legacy)

        res = migrate_one(str(path))
        assert res.status == "migrated"

        mid = _read(path)
        assert mid[_CLEARED] == {"added": 2}, "迁移模块必须非破坏式（不替别的模块做决定）"
        assert mid["scan_gaps"] == {"0@0": [0]}
        assert all(r["batch_seq"] == 0 for r in mid["records"])

        HistoryService(_PID).merge([], {})
        after = _read(path)

    assert after["scan_gaps"] == {"0@0": [0]}
    assert after["source_packages"] == ["com.bmystu.peng"]
    assert after[_CLEARED] is None


# ---------- 纯函数：透传规则本身 ----------

def test_carry_foreign_keys_rules():
    """透传三规则：内存权威不被覆盖 / 未知键继承 / 清单键置 None。"""
    payload = {"records": [1], "view_totals": {}, "total": None}
    existing = {
        "records": [9, 9],
        "view_totals": {"0@0": 5},
        "total": 5,
        "scan_gaps": {"0@0": [1]},
        "last_capture_detail": {"added": 3},
    }

    out = HistoryService._carry_foreign_keys(dict(payload), existing)
    assert out["records"] == [1] and out["total"] is None     # 内存 payload 权威
    assert out["scan_gaps"] == {"0@0": [1]}                    # 未知键继承
    assert out["last_capture_detail"] is None                  # 清单键置空

    # 将来 PC 自己维护该键时：内存值不被磁盘旧值或 null 覆盖
    p2 = {"records": [], "total": None, "last_capture_detail": {"added": 42}}
    out2 = HistoryService._carry_foreign_keys(dict(p2), existing)
    assert out2["last_capture_detail"] == {"added": 42}
