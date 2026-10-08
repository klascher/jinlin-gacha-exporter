# -*- coding: utf-8 -*-
"""U1 跨端记录文件互通 —— PC 侧 UI 单测（D1~D18）。

被测：
- `gacha_exporter/gui/sync_dialog.py`（导出 / 导入对话框）
- `gacha_exporter/gui/main_window.py` 的菜单接线与「抓包中整组置灰」

方案：`U1互通层-契约冻结与实现定案-2026-09-21.md` §5 / §8.2 / §9。
只做「离屏冒烟 + 行为断言」：断言**行为**（行可见性 / 已选是否保留 / 计划可产出），
**不钉实现**（不校验内部控件对象图、不复制文案渲染逻辑——数字一律与纯函数层
`compare()` 的结果比对，避免两边各写一份判据后漂移）。

约定跟随仓库既有 GUI 测试（`test_discovery_dialog.py` / `test_pity_panel.py`）：
`pytest.importorskip("PySide6")` + 模块级 `QApplication`；数据目录一律重定向到
`tmp_path`，绝不碰真实 `gacha_exporter/data/`。
"""
import json
import os
import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from gacha_exporter.gui import sync_dialog as sd  # noqa: E402
from gacha_exporter.storage import sync_file  # noqa: E402


# ---------- 工具 ----------

def _rec(ts, pos=0, bs=0, pool="30001", item="100"):
    return {"pool_id": pool, "item_id": item, "timestamp": ts,
            "batch_seq": bs, "position_in_batch": pos}


def _file_account(name, records, total=None, view_totals=None, account_id=None):
    """文件内账号（顶层信封 `accounts[]` 的一项）。"""
    return {"account_id": account_id, "name": name, "records": records,
            "total": total, "view_totals": view_totals or {}}


def _device(pid, name, records, total=None, view_totals=None):
    """设备侧账号（`sync_apply.load_device_accounts` 的一项）。"""
    return {"id": pid, "name": name, "records": records,
            "total": total, "view_totals": view_totals or {}}


def _envelope_text(accounts, exported_at="2026-09-21T10:00:00"):
    """构造一份合法互通文件文本（模拟来自 Android 端）。"""
    return json.dumps({
        "format": sync_file.FORMAT_NAME,
        "version": sync_file.FORMAT_VERSION,
        "exported_at": exported_at,
        "source": {"platform": "android", "app": "gacha-assistant",
                   "app_version": "0.1.0"},
        "accounts": accounts,
    }, ensure_ascii=False)


def _parsed(accounts):
    result = sync_file.parse(_envelope_text(accounts))
    assert result["ok"] is True, result.get("error")
    return result


def _visible(widget, dialog):
    """对话框尚未 show()，故用 `isVisibleTo(dialog)` 判「若显示则可见」。"""
    return widget.isVisibleTo(dialog)


# ===========================================================================
# D1~D3 纯展示小工具
# ===========================================================================

def test_d1_parse_error_text_maps_all_codes():
    """四类错误码 → 用户可读文案；`CORRUPT` 的 0 基下标转 1 基。"""
    assert sd.parse_error_text({"code": "BAD_FORMAT"}) == "不是本工具导出的记录文件。"
    assert sd.parse_error_text({"code": "VERSION_TOO_HIGH"}) == "文件由更新版本导出，请先升级本工具。"
    assert sd.parse_error_text({"code": "NO_ACCOUNTS"}) == "文件里没有账号数据。"
    assert (sd.parse_error_text({"code": "CORRUPT", "detail": "account#1: no-records"})
            == "文件内容损坏（第 2 个账号）。")
    assert sd.parse_error_text(None) == "文件无法读取。"


def test_d2_display_names_marks_duplicates():
    """文件内同名合法（§4）；重名加 (1)/(2) 以便区分。"""
    assert sd.display_names([{"name": "甲"}, {"name": "甲"}, {"name": "乙"}]) == [
        "甲 (1)", "甲 (2)", "乙"]


def test_d3_default_export_name_scope():
    assert (sd.default_export_name(sd.SCOPE_ALL, "x", "20260921_1130")
            == "jinlin-records-all-20260921_1130.json")
    assert (sd.default_export_name(sd.SCOPE_CURRENT, "默认账号", "20260921_1130")
            == "jinlin-records-默认账号-20260921_1130.json")


# ===========================================================================
# D4 导出对话框
# ===========================================================================

def test_d4_export_dialog_scope_default_and_pick():
    """默认「当前账号」；切到「全部账号」后取材变为全部。"""
    accounts = [
        _device("a", "甲", [_rec(1000), _rec(2000)]),
        _device("b", "乙", [_rec(3000)]),
    ]
    dlg = sd.ExportSyncDialog(accounts, "b", "20260921_1130")
    assert dlg.scope() == sd.SCOPE_CURRENT
    assert [a["id"] for a in dlg.selected_accounts()] == ["b"]
    assert dlg.path() == "jinlin-records-乙-20260921_1130.json"

    dlg.radio_all.setChecked(True)
    assert dlg.scope() == sd.SCOPE_ALL
    assert [a["id"] for a in dlg.selected_accounts()] == ["a", "b"]


# ===========================================================================
# D5~D6 导入对话框：两条路径的行可见性
# ===========================================================================

def test_d5_new_path_hides_merge_only_rows():
    """§5：新建路径隐藏「目标账号 / 使用哪一份 / 冲突 / 丢弃」，改出单列说明。"""
    parsed = _parsed([_file_account("丙", [_rec(1000)])])
    device = [_device("a", "甲", [_rec(5000)])]
    choices = sync_file.default_choices(parsed, device)
    assert choices[0]["mode"] == sync_file.MODE_NEW

    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    assert not _visible(dlg.target_row, dlg)
    assert not _visible(dlg.choose_row, dlg)
    assert not _visible(dlg.lbl_conflict, dlg)
    assert not _visible(dlg.lbl_discard, dlg)
    assert _visible(dlg.lbl_new_note, dlg)
    assert dlg.lbl_new_note.text() == "将新建「丙」，共 1 条"


def test_d6_merge_path_shows_rows_and_numbers_from_compare():
    """§9：合并路径显示全部行；两份条数一律取自纯函数层 `compare()`。"""
    file_records = [_rec(1000, 0), _rec(2000, 1), _rec(3000, 2)]
    dev_records = [_rec(1000, 0), _rec(4000, 1)]
    parsed = _parsed([_file_account("甲", file_records, total=3)])
    device = [_device("a", "甲", dev_records, total=4)]
    choices = sync_file.default_choices(parsed, device)
    assert choices[0]["mode"] == sync_file.MODE_MERGE

    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    assert _visible(dlg.target_row, dlg)
    assert _visible(dlg.choose_row, dlg)
    assert not _visible(dlg.lbl_new_note, dlg)

    cmp = sync_file.compare(dev_records, file_records)
    assert cmp["both"] == 1 and cmp["device_only"] == 1 and cmp["file_only"] == 2
    # 2026-09-22 用户裁定：三集合拆解行**整体删除**（与对照表的条数/最新抽卡重复）
    assert not hasattr(dlg, "lbl_sets")
    assert f"（{cmp['file_count']} 条）" in dlg.radio_file.text()
    assert f"（{cmp['device_count']} 条）" in dlg.radio_device.text()


def test_d6b_conflict_only_when_both_sides_nonempty_and_zero_overlap():
    """§10-c：仅一方非空不算冲突；两侧都非空且零重叠才提示。"""
    # 零重叠：文件 [1000]，设备 [9000]
    parsed = _parsed([_file_account("甲", [_rec(1000)], total=1)])
    device = [_device("a", "甲", [_rec(9000)], total=1)]
    choices = sync_file.default_choices(parsed, device)
    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    assert _visible(dlg.lbl_conflict, dlg)

    # 仅文件非空（设备空）⇒ 不报冲突
    parsed2 = _parsed([_file_account("甲", [_rec(1000)], total=1)])
    device2 = [_device("a", "甲", [])]
    choices2 = sync_file.default_choices(parsed2, device2)
    dlg2 = sd.ImportSyncDialog(parsed2, device2, choices2, "f.json")
    assert not _visible(dlg2.lbl_conflict, dlg2)


# ===========================================================================
# D7 切换账号不清空已选（§6.2-c）
# ===========================================================================

def test_d7_account_switch_preserves_choices():
    parsed = _parsed([
        _file_account("甲", [_rec(1000)]),
        _file_account("乙", [_rec(2000)]),
    ])
    device = [_device("a", "甲", [_rec(5000)]), _device("b", "乙", [_rec(6000)])]
    choices = sync_file.default_choices(parsed, device)
    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    assert len(dlg.choices()) == 2

    # 账号 1 改为「新建」→ 切到账号 2 改成「用应用里的」→ 来回切换应各自保留
    dlg.radio_new.setChecked(True)
    dlg._step(1)
    dlg.radio_device.setChecked(True)
    dlg._step(-1)
    assert dlg.radio_new.isChecked()
    dlg._step(1)
    assert dlg.radio_merge.isChecked() and dlg.radio_device.isChecked()

    out = dlg.choices()
    assert out[0]["mode"] == sync_file.MODE_NEW
    assert out[1]["mode"] == sync_file.MODE_MERGE
    assert out[1]["chosen"] == sync_file.CHOSEN_DEVICE


def test_d7b_switch_does_not_touch_other_account_config():
    """切到账号 2 只改账号 2：账号 1 的配置逐字段不变。"""
    parsed = _parsed([
        _file_account("甲", [_rec(1000)]),
        _file_account("乙", [_rec(2000)]),
    ])
    device = [_device("a", "甲", [_rec(5000)]), _device("b", "乙", [_rec(6000)])]
    choices = sync_file.default_choices(parsed, device)
    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    dlg._step(1)              # 载入账号 2
    dlg.radio_new.setChecked(True)
    dlg._step(-1)             # 回账号 1
    assert dlg.radio_merge.isChecked()     # 账号 1 仍是初始的 MERGE
    dlg._step(1)
    assert dlg.radio_new.isChecked()       # 账号 2 的改动保留


# ===========================================================================
# D8 「确认导入」产出可无条件执行的计划（§6.2-a）
# ===========================================================================

def test_d8_dialog_plan_is_executable():
    parsed = _parsed([_file_account("甲", [_rec(1000)], total=1)])
    device = [_device("a", "甲", [_rec(1000)], total=1)]
    choices = sync_file.default_choices(parsed, device)
    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    dlg.radio_device.setChecked(True)      # 用应用里的（对等值，实为 no-op）

    plan = dlg.plan()
    assert plan["ok"] is True
    assert len(plan["accounts"]) == 1
    entry = plan["accounts"][0]
    assert entry["mode"] == sync_file.MODE_MERGE
    assert entry["chosen"] == sync_file.CHOSEN_DEVICE
    assert entry["write"]["total"] == 1
    assert len(entry["write"]["records"]) == 1


# ===========================================================================
# D9 无基准提示（R3）：与路径无关，看 has_anchor
# ===========================================================================

def test_d9_no_anchor_warning_follows_total():
    no_anchor = _parsed([_file_account("丙", [_rec(1000)], total=None)])
    dlg_no = sd.ImportSyncDialog(no_anchor, [], sync_file.default_choices(no_anchor, []),
                                 "f.json")
    assert _visible(dlg_no.lbl_anchor, dlg_no)

    anchored = _parsed([_file_account("丁", [_rec(1000)], total=1)])
    dlg_yes = sd.ImportSyncDialog(anchored, [], sync_file.default_choices(anchored, []),
                                  "f.json")
    assert not _visible(dlg_yes.lbl_anchor, dlg_yes)


# ===========================================================================
# D10~D11 主窗口菜单接线 + 抓包中整组置灰
# ===========================================================================

@pytest.fixture()
def _main_window(tmp_path, monkeypatch):
    """在临时数据目录下构造 MainWindow（绝不碰真实 data/）。"""
    from gacha_exporter.gui.main_window import MainWindow
    from gacha_exporter.storage import (
        char_shields,
        history,
        pool_assignments,
        pool_patches,
        profiles,
        settings,
    )

    data = tmp_path / "data"
    users = data / "users"
    users.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(profiles, "get_data_dir", lambda: str(data))
    monkeypatch.setattr(settings, "get_data_dir", lambda: str(data))
    monkeypatch.setattr(char_shields, "get_data_dir", lambda: str(data))
    monkeypatch.setattr(pool_assignments, "get_data_dir", lambda: str(data))
    monkeypatch.setattr(pool_patches, "get_data_dir", lambda: str(data))
    monkeypatch.setattr(history, "get_users_dir", lambda: str(users))

    pm = profiles.ProfileManager()
    return MainWindow(characters={}, pools={}, pity_groups={}, profiles=pm)


class _RunningWorker:
    """替身 worker：只回答 isRunning（不启动任何真实抓包）。"""

    def isRunning(self) -> bool:
        return True


def test_d10_menu_items_wired(_main_window, monkeypatch):
    """两项菜单文案逐字（§8.2）+ **动作确实连到槽**（触发即进入槽函数）。

    用替身把「会阻塞的两个出口」短路：导出对话框替身直接 reject，文件选择器返回
    「取消」—— 这样 trigger() 不会弹窗、可断言槽真的被调到。
    """
    from gacha_exporter.gui import main_window as mw_mod

    calls = {}

    class _FakeExportDialog:
        def __init__(self, *args, **kwargs):
            calls["export_ctor"] = True
            # 钉**接线**（不是实现）：`(accounts, active_pid, stamp)` 三参 + `parent=`
            # —— 默认文件名由对话框按范围现算，主窗口只传时间戳（09-21 修）。
            assert len(args) == 3, args
            assert isinstance(args[0], list) and args[0], args[0]
            assert isinstance(args[1], str)
            assert re.fullmatch(r"\d{8}_\d{4}", args[2]), args[2]
            assert "parent" in kwargs

        def exec(self):
            return 0  # QDialog.DialogCode.Rejected

    class _FakeFileDialog:
        @staticmethod
        def getOpenFileName(*args, **kwargs):
            return "", ""   # 用户取消

        @staticmethod
        def getSaveFileName(*args, **kwargs):
            return "", ""

    monkeypatch.setattr(mw_mod, "ExportSyncDialog", _FakeExportDialog)
    monkeypatch.setattr(mw_mod, "QFileDialog", _FakeFileDialog)

    mw = _main_window
    assert mw._act_sync_export.text() == "导出记录文件…"
    assert mw._act_sync_import.text() == "导入记录文件…"
    assert mw._act_sync_export.isEnabled()
    assert mw._act_sync_import.isEnabled()

    mw._act_sync_export.trigger()
    assert calls.get("export_ctor") is True      # 动作 → 导出槽 → 对话框
    mw._act_sync_import.trigger()                # 动作 → 导入槽 → 取消后返回


def test_d11_sync_actions_disabled_while_capturing(_main_window):
    """§6.3/§9：抓包中整个菜单项置灰。"""
    mw = _main_window
    mw.worker = None
    mw._refresh_sync_actions()
    assert mw._act_sync_export.isEnabled() and mw._act_sync_import.isEnabled()

    mw.worker = _RunningWorker()
    mw._refresh_sync_actions()
    assert not mw._act_sync_export.isEnabled()
    assert not mw._act_sync_import.isEnabled()

    mw.worker = None
    mw._refresh_sync_actions()
    assert mw._act_sync_export.isEnabled() and mw._act_sync_import.isEnabled()


# ===========================================================================
# D12~D14 导出文件名随「范围」联动（09-21 修 `_refresh` 空壳）
# ===========================================================================

def test_d12_export_filename_follows_scope():
    """全部账号 ⇒ `jinlin-records-all-<戳>.json`；切回当前账号 ⇒ 回到账号名。"""
    accounts = [_device("a", "甲", [_rec(1000)]), _device("b", "乙", [_rec(2000)])]
    dlg = sd.ExportSyncDialog(accounts, "b", "20260921_1130")
    assert dlg.path() == "jinlin-records-乙-20260921_1130.json"

    dlg.radio_all.setChecked(True)
    assert dlg.path() == "jinlin-records-all-20260921_1130.json"

    dlg.radio_current.setChecked(True)
    assert dlg.path() == "jinlin-records-乙-20260921_1130.json"


def test_d13_export_all_name_has_no_account_name():
    """「全部账号」是**独立分支**：与单账号名无关，恒为 `-all-`。"""
    accounts = [_device("a", "甲", [_rec(1000)])]
    dlg = sd.ExportSyncDialog(accounts, "a", "20260921_1130")
    dlg.radio_all.setChecked(True)
    assert dlg.path() == "jinlin-records-all-20260921_1130.json"
    assert "甲" not in dlg.path()


def test_d14_user_edited_path_is_not_clobbered():
    """用户手改过文件名 ⇒ 切换范围**不覆盖**；仍是自动名 ⇒ 只换文件名、保留目录。"""
    accounts = [_device("a", "甲", [_rec(1000)])]
    dlg = sd.ExportSyncDialog(accounts, "a", "20260921_1130")

    # ① 手改名（非自动名）⇒ 切换范围不动它
    dlg.edit_path.setText("mine.json")
    dlg.radio_all.setChecked(True)
    assert dlg.path() == "mine.json"
    dlg.radio_current.setChecked(True)
    assert dlg.path() == "mine.json"

    # ② 仍是自动名、只是带了目录 ⇒ 只换文件名部分
    dlg2 = sd.ExportSyncDialog(accounts, "a", "20260921_1130")
    dlg2.edit_path.setText(os.path.join("sub", "jinlin-records-甲-20260921_1130.json"))
    dlg2.radio_all.setChecked(True)
    assert dlg2.path() == os.path.join("sub", "jinlin-records-all-20260921_1130.json")


# ===========================================================================
# D15~D18 「不导入」第三态（契约 MODE_SKIP，2026-09-22）
# ===========================================================================

def test_d15_skip_path_hides_detail_rows():
    """SKIP：与「新建」同款地收掉合并相关行，且**连对照表一起收**，只留一行说明。"""
    file_records = [_rec(1000), _rec(2000)]
    parsed = _parsed([_file_account("甲", file_records)])
    device = [_device("a", "甲", [_rec(9000)])]
    choices = sync_file.default_choices(parsed, device)
    assert choices[0]["mode"] == sync_file.MODE_MERGE    # 同名 ⇒ 默认合并

    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")
    dlg.radio_skip.setChecked(True)
    assert not _visible(dlg.target_row, dlg)
    assert not _visible(dlg.choose_row, dlg)
    assert not _visible(dlg.lbl_conflict, dlg)
    assert not _visible(dlg.lbl_discard, dlg)
    assert not _visible(dlg.cmp_box, dlg)                # 对照表整块收掉
    assert _visible(dlg.lbl_new_note, dlg)
    note = dlg.lbl_new_note.text()
    assert "不导入" in note and "甲" in note and "2 条" in note


def test_d16_skip_keeps_target_and_chosen():
    """§6.2-c：选 SKIP **不清空** `target` / `chosen` —— 改回「合并」时原目标必须还在。"""
    parsed = _parsed([_file_account("甲", [_rec(1000)])])
    device = [_device("a", "甲", [_rec(1000)]), _device("b", "乙", [_rec(2000)])]
    choices = sync_file.default_choices(parsed, device)
    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")

    dlg.radio_device.setChecked(True)          # 先选「用应用里的」（留下 chosen=DEVICE）
    target_before = dlg.combo_target.currentData()
    dlg.radio_skip.setChecked(True)            # 再选「不导入」
    dlg.radio_merge.setChecked(True)           # 再改回「合并」
    assert dlg.radio_device.isChecked()        # chosen 仍在
    assert dlg.combo_target.currentData() == target_before    # target 仍在
    out = dlg.choices()[0]
    assert out["mode"] == sync_file.MODE_MERGE
    assert out["chosen"] == sync_file.CHOSEN_DEVICE


def test_d17_skip_survives_account_switch():
    """SKIP 后切到别的账号再切回：`target` / `chosen` 都不丢（走 `_store_current` / `_load_index`）。"""
    parsed = _parsed([_file_account("甲", [_rec(1000)]), _file_account("乙", [_rec(2000)])])
    device = [_device("a", "甲", [_rec(1000)]), _device("b", "乙", [_rec(2000)])]
    choices = sync_file.default_choices(parsed, device)
    dlg = sd.ImportSyncDialog(parsed, device, choices, "f.json")

    dlg.radio_device.setChecked(True)
    target_before = dlg.combo_target.currentData()
    dlg.radio_skip.setChecked(True)
    dlg._step(1)                               # 切到账号 2（账号 1 的选择在此存回 choices）
    assert not dlg.radio_skip.isChecked()      # 账号 2 仍是默认的「合并」
    dlg._step(-1)                              # 切回账号 1
    assert dlg.radio_skip.isChecked()
    assert dlg.combo_target.currentData() == target_before
    assert dlg.radio_device.isChecked()

    out = dlg.choices()
    assert out[0]["mode"] == sync_file.MODE_SKIP
    assert out[0]["target"] == target_before
    assert out[0]["chosen"] == sync_file.CHOSEN_DEVICE


def test_d18_all_skip_yields_empty_plan():
    """全 SKIP ⇒ 契约层**不报错**、计划为空；UI 侧据此**不调事务壳**（守卫留给调用方 bug）。"""
    parsed = _parsed([_file_account("甲", [_rec(1000)]), _file_account("乙", [_rec(2000)])])
    choices = sync_file.default_choices(parsed, [])
    dlg = sd.ImportSyncDialog(parsed, [], choices, "f.json")

    dlg.radio_skip.setChecked(True)            # 账号 1
    dlg._step(1)                               # 存账号 1 → 载入账号 2
    dlg.radio_skip.setChecked(True)            # 账号 2
    out = dlg.choices()                        # `choices()` 会把当前账号也存下来
    assert [c["mode"] for c in out] == [sync_file.MODE_SKIP, sync_file.MODE_SKIP]

    plan = dlg.plan()
    assert plan["ok"] is True
    assert plan["accounts"] == []
