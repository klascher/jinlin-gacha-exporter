# -*- coding: utf-8 -*-
"""跨端记录互通的 **IO 事务壳** 单测（A1~A12）。

被测：`gacha_exporter/storage/sync_apply.py`（整组原子落库）。
方案：`U1互通层-契约冻结与实现定案-2026-09-21.md` §6.2（整组原子的四条硬约束）+ §6.3。

约定跟随仓库既有风格（`tests/test_persist_foreign_keys.py` / `test_coord_migration.py`）：
pytest 函数式 + `tmp_path` + `patch("...get_data_dir")` 重定向账号注册表。

⚠️ 本组测试刻意**不**碰真实 `gacha_exporter/data/`：`ProfileManager` 的
`get_data_dir` 一律 patch 到 tmp（否则会往仓库里写 `profiles.json`）。
"""
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from gacha_exporter.storage import sync_apply
from gacha_exporter.storage.history import HistoryService
from gacha_exporter.storage.profiles import ProfileManager
from gacha_exporter.storage.sync_file import (
    CHOSEN_DEVICE,
    CHOSEN_FILE,
    MODE_MERGE,
    MODE_NEW,
    default_choices,
    parse,
    plan_import,
)

_FOREIGN_KEPT = ("scan_gaps", "source_packages")
_CLEARED = "last_capture_detail"


# ---------- 环境与工具 ----------

@contextmanager
def _env(tmp):
    """把账号注册表**与账号历史目录**一并重定向到 tmp，返回已建好「默认账号」的管理器。

    ⚠️ 必须**同时** patch 两个模块 —— 两条路径各自独立解析：
    - `ProfileManager` 走 `profiles.get_data_dir`；
    - `HistoryService` 走 `history.get_users_dir`（`history.py:106`）。

    只 patch 前者时，A7 里直接构造的 `HistoryService(<新账号 id>)` 会落到**真实**
    `gacha_exporter/data/users/`，写入 61 字节空账号文件（`records: []`）并在
    真实目录里留下垃圾（已实测复现）。
    """
    data = Path(tmp, "data")
    users = data / "users"
    users.mkdir(parents=True, exist_ok=True)
    with patch(
        "gacha_exporter.storage.profiles.get_data_dir",
        return_value=str(data),
    ), patch(
        "gacha_exporter.storage.history.get_users_dir",
        return_value=str(users),
    ):
        yield ProfileManager()


def _rec(ts, bs=0, pos=0, pool="30001", item="100"):
    return {
        "pool_id": pool,
        "item_id": item,
        "timestamp": ts,
        "batch_seq": bs,
        "position_in_batch": pos,
    }


def _write(path, payload):
    """写一份账号文件（缩进与 `_write_payload` / `sync_apply` 一致，便于逐字节比对）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _users_files(users):
    return sorted(p.name for p in Path(users).iterdir() if p.is_file())


def _plan_entry(pid, records, total, view_totals=None, mode=MODE_MERGE, name=None):
    """构造一条 `SyncPlan.accounts` 项（与 `plan_import` 输出同形）。"""
    return {
        "index": 0,
        "name": name,
        "mode": mode,
        "target": pid,
        "chosen": CHOSEN_FILE,
        "write": {
            "records": records,
            "total": total,
            "view_totals": view_totals if view_totals is not None else {},
        },
        "discard": 0,
    }


def _plan(*entries):
    return {"ok": True, "error": None, "accounts": list(entries)}


# ---------- A1：合并到已有账号 ----------

def test_a1_merge_writes_file_and_keeps_cross_device_keys(tmp_path):
    """A1：合并落库 —— 记录/total 按计划写；跨端键透传；`last_capture_detail` 置 null。"""
    with _env(tmp_path) as pm:
        pid = pm.list_profiles()[0].id
        users = pm.users_dir
        path = Path(users, f"{pid}.json")
        _write(path, {
            "records": [_rec(1000)],
            "view_totals": {"0@0": 1},
            "total": 1,
            "scan_gaps": {"0@0": [0]},
            "source_packages": ["com.bmystu.peng"],
            _CLEARED: {"added": 7},
        })
        before = _read(path)

        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(pid, [_rec(1000), _rec(2000)], 2, {"0@0": 2})
        ))

        after = _read(path)
        backed_up = [n for n in _users_files(users) if n.startswith(f"{pid}_")]

    assert res.ok, res.error
    assert len(res.written) == 1 and res.written[0]["changed"] is True
    assert len(after["records"]) == 2 and after["total"] == 2
    assert after["view_totals"] == {"0@0": 2}
    assert after["scan_gaps"] == {"0@0": [0]}, "跨端键必须透传"
    assert after["source_packages"] == ["com.bmystu.peng"]
    assert after[_CLEARED] is None, "陈旧明细必须清掉（与既有 _write_payload 同语义）"
    assert backed_up, "有非空历史时应留一份用户可见备份"
    assert _read(Path(users, backed_up[0])) == before, "备份内容应为写前原文"


# ---------- A2：作为新用户导入 ----------

def test_a2_new_account_creates_profile_and_file(tmp_path):
    """A2：新建路径 —— 注册表多一个账号，其文件按计划写好。"""
    with _env(tmp_path) as pm:
        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(None, [_rec(1000), _rec(2000)], 2, {"0@0": 2}, mode=MODE_NEW)
        ))
        profiles = pm.list_profiles()
        new_pid = res.created[0] if res.created else None
        body = _read(Path(pm.users_dir, f"{new_pid}.json")) if new_pid else None

    assert res.ok, res.error
    assert len(profiles) == 2, "应新建出第二个账号"
    assert len(res.created) == 1
    assert res.written[0]["mode"] == MODE_NEW
    assert body["total"] == 2 and len(body["records"]) == 2
    assert res.written[0]["backup"] is None, "新建无历史，不产生备份"
    # 新建账号应追加在末尾，且不改动激活账号
    assert [p.id for p in profiles][-1] == new_pid


# ---------- A3：幂等闸（「选中应用里的那一份」必须是 no-op） ----------

def test_a3_device_chosen_is_noop(tmp_path):
    """A3：`chosen=DEVICE` 的账号写回原值 ⇒ 幂等闸拦住，不写盘、不建备份。

    这正是「整组原子要求 `chosen=DEVICE` 也进计划」的代价控制点：若幂等闸失效，
    每次导入都会给每个未改动的账号白造一个备份。
    """
    with _env(tmp_path) as pm:
        pid = pm.list_profiles()[0].id
        users = pm.users_dir
        path = Path(users, f"{pid}.json")
        current = {
            "records": [_rec(1000)],
            "view_totals": {"0@0": 1},
            "total": 1,
        }
        _write(path, current)
        before_bytes = path.read_bytes()
        before_files = _users_files(users)

        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(pid, [_rec(1000)], 1, {"0@0": 1})
        ))

        assert res.ok, res.error
        assert res.written[0]["changed"] is False
        assert path.read_bytes() == before_bytes, "幂等闸失效：重复写盘"
        assert _users_files(users) == before_files, "幂等闸失效：备份洪水"


# ---------- A4/A5：整组原子（中途失败 ⇒ 全部回滚） ----------

def test_a4_failure_rolls_back_written_account(tmp_path, monkeypatch):
    """A4：第 2 个账号写失败 ⇒ 第 1 个已写账号**原样还原**，`written` 为空。"""
    with _env(tmp_path) as pm:
        pid = pm.list_profiles()[0].id
        users = pm.users_dir
        path = Path(users, f"{pid}.json")
        _write(path, {
            "records": [_rec(1000)],
            "view_totals": {"0@0": 1},
            "total": 1,
        })
        original = path.read_text(encoding="utf-8")

        real = sync_apply._atomic_write_text
        calls = {"n": 0}

        def flaky(target, text, translate_newlines=True):
            calls["n"] += 1
            if calls["n"] == 2:          # 只炸第 2 次正向写；回滚（第 3 次起）放行
                raise OSError("注入的写入失败")
            return real(target, text, translate_newlines=translate_newlines)

        monkeypatch.setattr(sync_apply, "_atomic_write_text", flaky)

        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(pid, [_rec(1000), _rec(2000)], 2),
            _plan_entry(pm.list_profiles()[0].id, [_rec(3000)], 1),
        ))

        assert path.read_text(encoding="utf-8") == original, "已写账号必须回滚到写前原文"
        leftovers = [n for n in _users_files(users) if n.endswith(sync_apply._TMP_SUFFIX)]
        remaining = _users_files(users)

    assert res.ok is False
    assert res.written == [], "失败时不得返回半份结果"
    assert "回滚" in (res.error or "")
    assert leftovers == [], "不得留下 .sync-tmp 垃圾"
    assert remaining == [f"{pid}.json"], f"回滚后目录应回到初始态，实际={remaining}"


def test_a5_failure_removes_new_account_without_archive(tmp_path, monkeypatch):
    """A5：本批新建的账号在回滚时被撤掉，且不留下归档副本。"""
    with _env(tmp_path) as pm:
        users = pm.users_dir
        pid = pm.list_profiles()[0].id
        real = sync_apply._atomic_write_text
        calls = {"n": 0}

        def flaky(target, text, translate_newlines=True):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("注入的写入失败")
            return real(target, text, translate_newlines=translate_newlines)

        monkeypatch.setattr(sync_apply, "_atomic_write_text", flaky)

        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(pid, [_rec(1000)], 1),
            _plan_entry(None, [_rec(2000)], 1, mode=MODE_NEW, name="手机账号"),
        ))

        names = _users_files(users)
        count = len(pm.list_profiles())

    assert res.ok is False
    assert count == 1, "新建账号必须被撤销"
    # 写前 users/ 是空的（`_env` 只建注册表，账号文件要等首次落库才出现）⇒ 回滚后应回到空
    assert names == [], f"不该留下归档副本或备份: {names}"
    assert not any(n.endswith(sync_apply._TMP_SUFFIX) for n in names)


def test_a6_corrupt_existing_restored_verbatim_on_rollback(tmp_path, monkeypatch):
    """A6：写前文件是损坏 JSON ⇒ 回滚必须**逐字节**还原（不能被当成「原本不存在」删掉）。"""
    with _env(tmp_path) as pm:
        pid = pm.list_profiles()[0].id
        path = Path(pm.users_dir, f"{pid}.json")
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        original = path.read_text(encoding="utf-8")

        real = sync_apply._atomic_write_text
        calls = {"n": 0}

        def flaky(target, text, translate_newlines=True):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("注入的写入失败")
            return real(target, text, translate_newlines=translate_newlines)

        monkeypatch.setattr(sync_apply, "_atomic_write_text", flaky)

        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(pid, [_rec(1000)], 1),
            _plan_entry(pid, [_rec(2000)], 1),
        ))

        assert path.read_text(encoding="utf-8") == original

    assert res.ok is False


# ---------- A7：与既有落库路径互不打架 ----------

def test_a7_apply_result_is_idempotent_for_history_service(tmp_path):
    """A7：导入落库后，`HistoryService.merge([], {})` 必须**无新增、无新备份**。

    这是「导入写出的文件格式与既有 `_write_payload` 完全兼容」的实证：
    若字段名/类型/跨端键有一处不同，幂等闸会立刻失效并多造一个备份。
    """
    with _env(tmp_path) as pm:
        pid = pm.list_profiles()[0].id
        users = pm.users_dir
        path = Path(users, f"{pid}.json")
        _write(path, {
            "records": [_rec(1000)],
            "view_totals": {"0@0": 1},
            "total": 1,
            "scan_gaps": {"0@0": [0]},
            "session": {"current_pool_id": "all", "capture_start": "2026-09-21T00:00:00"},
        })

        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(pid, [_rec(1000), _rec(2000)], 2, {"0@0": 2})
        ))
        assert res.ok, res.error
        after_apply = path.read_bytes()
        files_after_apply = _users_files(users)

        added = HistoryService(pid).merge([], {})

        assert added == 0
        assert path.read_bytes() == after_apply, "既有落库路径认为导入结果「内容已变」"
        assert _users_files(users) == files_after_apply, "多造了备份 ⇒ 幂等闸失效"
        assert _read(path)["session"]["current_pool_id"] == "all", "PC 自己的键必须留住"


# ---------- A8/A9：纯函数与只读快照 ----------

def test_a8_build_payload_passthrough_and_copy(tmp_path):
    """A8：payload 三件套来自计划；磁盘未知键继承；清单键置 None；不与入参共享引用。"""
    write = {"records": [_rec(1000)], "total": 1, "view_totals": {"0@0": 1}}
    existing = {
        "records": [_rec(99)],
        "view_totals": {"0@0": 9},
        "total": 9,
        "scan_gaps": {"0@0": [1]},
        _CLEARED: {"added": 3},
    }

    payload = sync_apply.build_payload(write, existing)

    assert payload["records"] == [_rec(1000)] and payload["total"] == 1   # 计划权威
    assert payload["scan_gaps"] == {"0@0": [1]}                            # 未知键继承
    assert payload[_CLEARED] is None                                       # 清单键置空

    payload["records"][0]["item_id"] = "mutated"
    payload["view_totals"]["0@0"] = 999
    assert write["records"][0]["item_id"] == "100" and write["view_totals"]["0@0"] == 1, \
        "payload 必须与入参解耦（副本）"


def test_a9_load_device_accounts_covers_every_profile(tmp_path):
    """A9：设备侧快照必须与账号注册表**一一对应**；缺失/损坏文件按空记录返回，不抛。"""
    with _env(tmp_path) as pm:
        a = pm.list_profiles()[0]
        b = pm.create("B")
        c = pm.create("C")
        _write(Path(pm.users_dir, f"{a.id}.json"), {
            "records": [_rec(1000)], "view_totals": {"0@0": 1}, "total": 1,
        })
        # b：文件缺失；c：文件损坏
        Path(pm.users_dir, f"{c.id}.json").write_text("{坏", encoding="utf-8")

        snap = sync_apply.load_device_accounts(pm)

    assert [s["id"] for s in snap] == [a.id, b.id, c.id]
    assert len(snap[0]["records"]) == 1 and snap[0]["total"] == 1
    assert snap[0]["view_totals"] == {"0@0": 1}
    assert snap[1]["records"] == [] and snap[1]["name"] == "B"
    assert snap[2]["records"] == [] and snap[2]["total"] is None


def test_a10_empty_or_invalid_plan_rejected(tmp_path):
    """A10：空计划 / 未成功产出的计划一律拒绝，且不碰磁盘。"""
    with _env(tmp_path) as pm:
        users_before = _users_files(pm.users_dir)

        r1 = sync_apply.apply_plan(pm, _plan())
        r2 = sync_apply.apply_plan(pm, {"ok": False, "error": {"code": "CORRUPT"}})
        r3 = sync_apply.apply_plan(pm, None)

        assert (r1.ok, r2.ok, r3.ok) == (False, False, False)
        assert _users_files(pm.users_dir) == users_before
        assert len(pm.list_profiles()) == 1, "拒绝的计划不得改注册表"


def test_a11_merge_target_must_exist(tmp_path):
    """A11：合并目标不存在 ⇒ 拒绝；本批已建账号要撤掉（不留半成品）。"""
    with _env(tmp_path) as pm:
        res = sync_apply.apply_plan(pm, _plan(
            _plan_entry(None, [_rec(1000)], 1, mode=MODE_NEW, name="先建后败"),
            _plan_entry("不存在的id", [_rec(2000)], 1),
        ))
        assert res.ok is False
        assert len(pm.list_profiles()) == 1, "准备阶段失败也要撤掉本批新建账号"
        assert _users_files(pm.users_dir) == []


# ---------- A12：导出文件落盘（换行不翻译） ----------

def test_a12_export_file_written_verbatim_lf(tmp_path):
    """A12：导出文件按 `dumps()` 原文写（LF，不做平台换行翻译）⇒ 跨平台逐字节相同。"""
    out = tmp_path / "out.json"
    text = "{\n  \"format\": \"jinlin-records\"\n}\n"

    sync_apply.write_export_file(str(out), text)

    assert out.read_bytes() == text.encode("utf-8")
    assert b"\r" not in out.read_bytes(), "导出文件不得混入 CRLF"


# ---------- A13：黄金样本端到端（parse → default_choices → plan_import → apply_plan） ----------

def test_a13_golden_sample_end_to_end(tmp_path):
    """A13：用两端共用的黄金样本走完整导入链，三个账号全部落到设备上。"""
    golden = Path(__file__).parent / "fixtures" / "golden-jinlin-records-v1.json"
    text = golden.read_text(encoding="utf-8")

    with _env(tmp_path) as pm:
        parsed = parse(text)
        assert parsed["ok"], parsed["error"]

        device = sync_apply.load_device_accounts(pm)
        choices = default_choices(parsed, device)
        assert len(choices) == len(parsed["accounts"]) == 3
        assert all(c["mode"] == MODE_NEW for c in choices), "黄金样本账号名与设备账号不重名"

        plan = plan_import(parsed, device, choices)
        res = sync_apply.apply_plan(pm, plan)

        assert res.ok, res.error
        after = sync_apply.load_device_accounts(pm)
        by_name = {a["name"]: a for a in after}

    assert len(pm.list_profiles()) == 4, "默认账号 + 样本 3 个"
    for src in parsed["accounts"]:
        got = by_name[src["name"]]
        assert got["records"] == src["records"], f"{src['name']} 记录未被逐字搬运"
        assert got["total"] == src["total"]
        assert got["view_totals"] == src["view_totals"]
