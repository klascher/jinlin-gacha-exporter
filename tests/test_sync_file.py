# -*- coding: utf-8 -*-
"""跨端互通纯函数层测试（S1~S8）。

被测：`gacha_exporter/storage/sync_file.py`
契约：`U1互通层-契约冻结与实现定案-2026-09-21.md` §2~§4
格式：`数据互通-设计稿-2026-09-20.md`（v3.6）§3

分组：
    S1 解析成功路径        S2 四类错误码      S3 边界与空值
    S4 比对三集合          S5 默认选择        S6 落库计划
    S7 导出侧（事件/自检/信封）
    S8 黄金样本自检（两端共用同一份 fixture ⇒ 交叉验证的地基）
    S9 「不导入」第三态（契约 `MODE_SKIP`）

⚠️ 本测试只跑**纯函数**，不 patch 任何路径、不碰真实 data 目录。
"""
import json
from pathlib import Path

import pytest

from gacha_exporter.storage.sync_file import (
    CHOSEN_DEVICE,
    CHOSEN_FILE,
    ERR_BAD_FORMAT,
    ERR_CORRUPT,
    ERR_NO_ACCOUNTS,
    ERR_VERSION_TOO_HIGH,
    FALLBACK_NAME,
    FORMAT_NAME,
    FORMAT_VERSION,
    MODE_MERGE,
    MODE_NEW,
    MODE_SKIP,
    NEWER_DEVICE,
    NEWER_FILE,
    NEWER_TIE,
    analyze_events,
    build_envelope,
    build_export_account,
    compare,
    compute_integrity,
    default_choices,
    dumps,
    plan_import,
    parse,
)

_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden-jinlin-records-v1.json"
_GOLDEN_ANDROID = (_ROOT / "mobile" / "android" / "app" / "src" / "test" / "resources"
                   / "golden-jinlin-records-v1.json")


# ---------- 工具 ----------

def _rec(ts, pos, pool="20001", item="90000019"):
    return {"pool_id": pool, "item_id": item, "timestamp": ts,
            "batch_seq": 0, "position_in_batch": pos}


def _account(name, records, total=None, view_totals=None, account_id=None):
    return {"name": name, "account_id": account_id, "records": records,
            "total": total, "view_totals": view_totals or {}}


def _env(accounts, version=FORMAT_VERSION, fmt=FORMAT_NAME, **extra):
    obj = {"format": fmt, "version": version, "exported_at": "2026-09-21T10:00:00",
           "source": {"platform": "desktop", "app": "jinlin-gacha-exporter", "app_version": "0.1.0"},
           "accounts": accounts}
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False)


def _device(account_id, name, records, total=None, view_totals=None):
    return {"id": account_id, "name": name, "records": records,
            "total": total, "view_totals": view_totals or {}}


# ===========================================================================
# S1 解析成功路径
# ===========================================================================

def test_s1_parse_minimal_ok():
    result = parse(_env([_account("甲", [_rec(1000, 0)])]))
    assert result["ok"] is True
    assert result["error"] is None
    assert len(result["accounts"]) == 1
    account = result["accounts"][0]
    assert account["name"] == "甲"
    assert account["records"] == [_rec(1000, 0)]


def test_s1_parse_accepts_bytes():
    assert parse(_env([_account("甲", [])]).encode("utf-8"))["ok"] is True


def test_s1_parse_preserves_source_and_exported_at():
    result = parse(_env([_account("甲", [])]))
    assert result["source"]["platform"] == "desktop"
    assert result["exported_at"] == "2026-09-21T10:00:00"


def test_s1_parse_record_keys_are_exactly_five():
    result = parse(_env([_account("甲", [_rec(1000, 3)])]))
    assert set(result["accounts"][0]["records"][0]) == {
        "pool_id", "item_id", "timestamp", "batch_seq", "position_in_batch"}


# ===========================================================================
# S2 四类错误码
# ===========================================================================

@pytest.mark.parametrize("text", [
    "",                                    # 空
    "not json at all",                     # 非 JSON
    "[1, 2, 3]",                           # 根不是对象
    '{"format": "other-tool"}',            # format 不符
    '{"version": 1}',                      # 缺 format
])
def test_s2_bad_format(text):
    result = parse(text)
    assert result["ok"] is False
    assert result["error"]["code"] == ERR_BAD_FORMAT
    assert result["accounts"] == []


def test_s2_bad_format_not_utf8():
    assert parse(b"\xff\xfe\x00bad")["error"]["code"] == ERR_BAD_FORMAT


def test_s2_version_too_high():
    result = parse(_env([_account("甲", [])], version=FORMAT_VERSION + 1))
    assert result["ok"] is False
    assert result["error"]["code"] == ERR_VERSION_TOO_HIGH


@pytest.mark.parametrize("text", [
    '{"format": "jinlin-records"}',
    '{"format": "jinlin-records", "accounts": []}',
    '{"format": "jinlin-records", "accounts": null}',
])
def test_s2_no_accounts(text):
    assert parse(text)["error"]["code"] == ERR_NO_ACCOUNTS


@pytest.mark.parametrize("account", [
    {"name": "甲"},                                                   # 缺 records
    {"name": "甲", "records": None},                                  # records 非数组
    {"name": "甲", "records": [{"pool_id": "1", "item_id": "2"}]},     # 缺 timestamp
    {"name": "甲", "records": [{"item_id": "2", "timestamp": 1}]},     # 缺 pool_id
    {"name": "甲", "records": ["not-a-dict"]},                        # 记录非对象
])
def test_s2_corrupt(account):
    assert parse(_env([account]))["error"]["code"] == ERR_CORRUPT


def test_s2_corrupt_reports_account_index():
    result = parse(_env([_account("甲", []), {"name": "乙", "records": [{"pool_id": "1"}]}]))
    assert result["error"]["code"] == ERR_CORRUPT
    assert "1" in result["error"]["detail"]


def test_s2_failure_never_returns_partial_accounts():
    result = parse(_env([_account("甲", [_rec(1, 0)]), {"name": "乙"}]))
    assert result["ok"] is False
    assert result["accounts"] == []


# ===========================================================================
# S3 边界与空值
# ===========================================================================

def test_s3_version_missing_is_lenient():
    text = '{"format": "jinlin-records", "accounts": [{"name": "甲", "records": []}]}'
    assert parse(text)["ok"] is True


def test_s3_version_as_string_is_accepted():
    assert parse(_env([_account("甲", [])], version="1"))["ok"] is True


def test_s3_empty_records_is_legal():
    result = parse(_env([_account("甲", [])]))
    assert result["ok"] is True
    assert result["accounts"][0]["records"] == []


def test_s3_total_null_is_legal():
    result = parse(_env([_account("甲", [], total=None)]))
    assert result["ok"] is True
    assert result["accounts"][0]["total"] is None


def test_s3_total_bad_value_degrades_to_none():
    text = '{"format":"jinlin-records","accounts":[{"name":"甲","records":[],"total":"abc"}]}'
    assert parse(text)["accounts"][0]["total"] is None


def test_s3_view_totals_missing_is_empty_map():
    assert parse(_env([_account("甲", [])]))["accounts"][0]["view_totals"] == {}


def test_s3_view_totals_bad_values_are_skipped():
    text = ('{"format":"jinlin-records","accounts":[{"name":"甲","records":[],'
            '"view_totals":{"0@0":793,"bad":"x"}}]}')
    assert parse(text)["accounts"][0]["view_totals"] == {"0@0": 793}


def test_s3_missing_coord_keys_filled_with_zero():
    text = ('{"format":"jinlin-records","accounts":[{"name":"甲","records":'
            '[{"pool_id":"1","item_id":"2","timestamp":3}]}]}')
    account = parse(text)["accounts"][0]
    assert account["records"][0]["batch_seq"] == 0
    assert account["records"][0]["position_in_batch"] == 0


def test_s3_missing_name_degrades_not_raises():
    text = '{"format":"jinlin-records","accounts":[{"records":[]}]}'
    result = parse(text)
    assert result["ok"] is True
    assert result["accounts"][0]["name"] == FALLBACK_NAME


def test_s3_duplicate_account_names_are_legal():
    result = parse(_env([_account("同名", []), _account("同名", [])]))
    assert result["ok"] is True
    assert len(result["accounts"]) == 2


def test_s3_events_and_integrity_are_optional():
    result = parse(_env([_account("甲", [])]))
    assert result["accounts"][0]["events"] is None
    assert result["accounts"][0]["integrity"] is None


def test_s3_events_and_integrity_passed_through_when_present():
    text = ('{"format":"jinlin-records","accounts":[{"name":"甲","records":[],'
            '"events":[{"ts":1,"count":1,"ids":[0],"coord":"absolute","complete":true}],'
            '"integrity":{"record_count":0}}]}')
    account = parse(text)["accounts"][0]
    assert account["events"][0]["coord"] == "absolute"
    assert account["integrity"]["record_count"] == 0


# ===========================================================================
# S4 比对三集合
# ===========================================================================

def test_s4_identical_sides():
    recs = [_rec(1000, 0), _rec(1000, 1)]
    result = compare(recs, list(recs))
    assert result == {"both": 2, "device_only": 0, "file_only": 0,
                      "device_count": 2, "file_count": 2,
                      "device_max_ts": 1000, "file_max_ts": 1000, "newer": NEWER_TIE}


def test_s4_file_only_records():
    result = compare([_rec(1000, 0)], [_rec(1000, 0), _rec(900, 0)])
    assert result["both"] == 1 and result["file_only"] == 1 and result["device_only"] == 0


def test_s4_device_only_records():
    result = compare([_rec(1000, 0), _rec(900, 0)], [_rec(1000, 0)])
    assert result["device_only"] == 1 and result["file_only"] == 0


def test_s4_multiset_counts_ten_pull_duplicates():
    """十连同名（同 pool 同 item 同 ts）在两侧都存在时按**多重集**计，不得退化成 set。"""
    dup = [_rec(1000, i) for i in range(3)]          # 同身份键 ×3
    result = compare(dup, dup[:2])
    assert result["both"] == 2
    assert result["device_only"] == 1
    assert result["device_count"] == 3


def test_s4_conservation_always_holds():
    device = [_rec(1000, 0), _rec(900, 0), _rec(800, 0)]
    file = [_rec(900, 0), _rec(700, 0)]
    result = compare(device, file)
    assert result["both"] + result["device_only"] == result["device_count"]
    assert result["both"] + result["file_only"] == result["file_count"]


@pytest.mark.parametrize("device_ts,file_ts,expected", [
    (1000, 2000, NEWER_FILE),
    (2000, 1000, NEWER_DEVICE),
    (1000, 1000, NEWER_TIE),
])
def test_s4_newer_decision(device_ts, file_ts, expected):
    assert compare([_rec(device_ts, 0)], [_rec(file_ts, 0)])["newer"] == expected


def test_s4_empty_both_sides_is_tie():
    result = compare([], [])
    assert result["newer"] == NEWER_TIE
    assert result["device_max_ts"] is None and result["file_max_ts"] is None


def test_s4_one_side_empty_picks_the_other():
    assert compare([], [_rec(1, 0)])["newer"] == NEWER_FILE
    assert compare([_rec(1, 0)], [])["newer"] == NEWER_DEVICE


# ===========================================================================
# S5 默认选择
# ===========================================================================

def test_s5_no_same_name_means_new():
    parsed = parse(_env([_account("新号", [_rec(1000, 0)])]))
    choices = default_choices(parsed, [_device("d1", "旧号", [_rec(1, 0)])])
    assert choices == [{"index": 0, "mode": MODE_NEW, "target": None, "chosen": CHOSEN_FILE}]


def test_s5_same_name_merges_and_prefers_newer_file():
    parsed = parse(_env([_account("甲", [_rec(2000, 0)])]))
    choices = default_choices(parsed, [_device("d1", "甲", [_rec(1000, 0)])])
    assert choices[0]["mode"] == MODE_MERGE
    assert choices[0]["target"] == "d1"
    assert choices[0]["chosen"] == CHOSEN_FILE


def test_s5_same_name_prefers_device_when_device_is_newer():
    parsed = parse(_env([_account("甲", [_rec(1000, 0)])]))
    choices = default_choices(parsed, [_device("d1", "甲", [_rec(2000, 0)])])
    assert choices[0]["chosen"] == CHOSEN_DEVICE


def test_s5_one_choice_per_file_account():
    parsed = parse(_env([_account("甲", []), _account("乙", []), _account("丙", [])]))
    assert len(default_choices(parsed, [])) == 3


# ===========================================================================
# S6 落库计划
# ===========================================================================

def _merge_setup():
    """设备侧「甲」有 3 条，文件侧「甲」有 2 条（1 条重合）。"""
    device = [_device("d1", "甲", [_rec(1000, 0), _rec(900, 0), _rec(800, 0)], total=3)]
    file_records = [_rec(1000, 0), _rec(700, 0)]
    parsed = parse(_env([_account("甲", file_records, total=2)]))
    return device, parsed, file_records


def test_s6_new_writes_file_side_and_discards_nothing():
    parsed = parse(_env([_account("甲", [_rec(1000, 0)], total=1)]))
    plan = plan_import(parsed, [], [{"index": 0, "mode": MODE_NEW, "target": None,
                                    "chosen": CHOSEN_FILE}])
    account = plan["accounts"][0]
    assert account["mode"] == MODE_NEW
    assert account["target"] is None
    assert account["discard"] == 0
    assert account["write"]["records"] == [_rec(1000, 0)]
    assert account["write"]["total"] == 1


def test_s6_merge_choosing_file_discards_device_only():
    device, parsed, file_records = _merge_setup()
    plan = plan_import(parsed, device, [{"index": 0, "mode": MODE_MERGE, "target": "d1",
                                         "chosen": CHOSEN_FILE}])
    account = plan["accounts"][0]
    assert account["write"]["records"] == file_records
    assert account["discard"] == 2      # 设备独有 900/800 两条


def test_s6_merge_choosing_device_discards_file_only():
    device, parsed, _ = _merge_setup()
    plan = plan_import(parsed, device, [{"index": 0, "mode": MODE_MERGE, "target": "d1",
                                         "chosen": CHOSEN_DEVICE}])
    account = plan["accounts"][0]
    assert account["write"]["records"] == device[0]["records"]
    assert account["write"]["total"] == 3
    assert account["discard"] == 1      # 文件独有 700 一条


def test_s6_device_choice_still_appears_in_plan():
    """chosen=DEVICE 的账号也必须在计划里（整组回滚要求集合完整）。"""
    device, parsed, _ = _merge_setup()
    plan = plan_import(parsed, device, [{"index": 0, "mode": MODE_MERGE, "target": "d1",
                                         "chosen": CHOSEN_DEVICE}])
    assert len(plan["accounts"]) == 1
    assert plan["accounts"][0]["chosen"] == CHOSEN_DEVICE


def test_s6_plan_covers_every_file_account():
    parsed = parse(_env([_account("甲", []), _account("乙", []), _account("丙", [])]))
    choices = default_choices(parsed, [])
    assert len(plan_import(parsed, [], choices)["accounts"]) == 3


def test_s6_write_bundle_is_a_copy_not_shared_reference():
    device, parsed, _ = _merge_setup()
    plan = plan_import(parsed, device, [{"index": 0, "mode": MODE_NEW, "target": None,
                                         "chosen": CHOSEN_FILE}])
    plan["accounts"][0]["write"]["records"].append(_rec(1, 1))
    assert len(parsed["accounts"][0]["records"]) == 2


def test_s6_default_choices_then_plan_roundtrip():
    device, parsed, _ = _merge_setup()
    plan = plan_import(parsed, device, default_choices(parsed, device))
    assert plan["ok"] is True
    assert plan["accounts"][0]["mode"] == MODE_MERGE


@pytest.mark.parametrize("choices", [
    [],                                                                    # 数量不符
    [{"index": 0, "mode": MODE_NEW, "target": None, "chosen": CHOSEN_FILE},
     {"index": 1, "mode": MODE_NEW, "target": None, "chosen": CHOSEN_FILE}],  # 多一个
])
def test_s6_choice_count_mismatch_raises(choices):
    parsed = parse(_env([_account("甲", [])]))
    with pytest.raises(ValueError):
        plan_import(parsed, [], choices)


def test_s6_merge_without_valid_target_raises():
    parsed = parse(_env([_account("甲", [])]))
    with pytest.raises(ValueError):
        plan_import(parsed, [], [{"index": 0, "mode": MODE_MERGE, "target": "nope",
                                  "chosen": CHOSEN_FILE}])


def test_s6_invalid_mode_raises():
    parsed = parse(_env([_account("甲", [])]))
    with pytest.raises(ValueError):
        plan_import(parsed, [], [{"index": 0, "mode": "COPY", "target": None,
                                  "chosen": CHOSEN_FILE}])


def test_s6_invalid_chosen_raises():
    device = [_device("d1", "甲", [])]
    parsed = parse(_env([_account("甲", [])]))
    with pytest.raises(ValueError):
        plan_import(parsed, device, [{"index": 0, "mode": MODE_MERGE, "target": "d1",
                                      "chosen": "BOTH"}])


# ===========================================================================
# S7 导出侧
# ===========================================================================

def test_s7_analyze_events_groups_by_timestamp():
    events = analyze_events([_rec(1000, 0), _rec(1000, 1), _rec(900, 0)])
    assert [e["count"] for e in events] == [2, 1]
    assert events[0]["ids"] == [0, 1] and events[1]["ids"] == [2]


def test_s7_analyze_events_coord_absolute_when_positions_distinct():
    events = analyze_events([_rec(1000, i) for i in range(10)])
    assert events[0]["coord"] == "absolute"
    assert events[0]["complete"] is True


def test_s7_analyze_events_coord_page_when_positions_repeat():
    """旧格式（跨帧）组内位置重复 ⇒ 必须判为 page。"""
    records = []
    for batch in (0, 1):
        for pos in range(5):
            record = _rec(1000, pos)
            record["batch_seq"] = batch
            records.append(record)
    events = analyze_events(records)
    assert events[0]["coord"] == "page"
    assert events[0]["complete"] is True


def test_s7_analyze_events_marks_fragment_incomplete():
    events = analyze_events([_rec(1000, i) for i in range(5)])
    assert events[0]["complete"] is False


def test_s7_integrity_flags():
    records = [_rec(1000, i) for i in range(10)] + [_rec(900, i) for i in range(5)]
    events = analyze_events(records)
    integrity = compute_integrity(records, events, total=15)
    assert integrity == {"record_count": 15, "authoritative_total": 15,
                         "count_matches": True, "event_count": 2,
                         "incomplete_events": 1, "partial_records": 5,
                         "has_anchor": True}


def test_s7_integrity_without_anchor():
    records = [_rec(1000, 0)]
    integrity = compute_integrity(records, analyze_events(records), total=None)
    assert integrity["has_anchor"] is False
    assert integrity["count_matches"] is False


def test_s7_build_export_account_fills_events_and_integrity():
    account = build_export_account("acc1", "甲", [_rec(1000, 0)], total=1,
                                   view_totals={"0@0": 1})
    assert account["events"][0]["count"] == 1
    assert account["integrity"]["count_matches"] is True
    assert account["view_totals"] == {"0@0": 1}


def test_s7_envelope_roundtrip_is_lossless():
    account = build_export_account("acc1", "甲", [_rec(1000, 0), _rec(1000, 1)], total=2)
    envelope = build_envelope([account], "2026-09-21T10:00:00",
                              {"platform": "desktop", "app": "jinlin-gacha-exporter"})
    parsed = parse(dumps(envelope))
    assert parsed["ok"] is True
    assert parsed["accounts"][0]["records"] == account["records"]
    assert parsed["accounts"][0]["total"] == 2
    assert parsed["accounts"][0]["events"] == account["events"]


def test_s7_dumps_header_keys():
    obj = json.loads(dumps(build_envelope([], "2026-09-21T10:00:00", {"platform": "desktop"})))
    assert list(obj) == ["format", "version", "exported_at", "source", "accounts"]


# ===========================================================================
# S8 黄金样本自检（两端共用同一份 ⇒ 交叉验证的地基）
# ===========================================================================

@pytest.fixture(scope="module")
def golden():
    assert _GOLDEN.exists(), "黄金样本缺失：%s" % _GOLDEN
    with open(_GOLDEN, "r", encoding="utf-8") as f:
        return parse(f.read())


def test_s8_golden_parses_with_three_accounts(golden):
    assert golden["ok"] is True
    assert [a["name"] for a in golden["accounts"]] == ["全新账号", "混合账号", "残片无基准"]


def test_s8_golden_account1_is_ideal_path(golden):
    account = golden["accounts"][0]
    assert account["integrity"]["count_matches"] is True
    assert account["integrity"]["has_anchor"] is True
    assert account["integrity"]["incomplete_events"] == 0
    assert {e["coord"] for e in account["events"]} == {"absolute"}
    assert account["total"] == len(account["records"])


def test_s8_golden_account2_has_page_segment_and_real_gap(golden):
    account = golden["accounts"][1]
    assert "page" in {e["coord"] for e in account["events"]}
    assert account["integrity"]["count_matches"] is False     # 真实缺口
    assert account["integrity"]["has_anchor"] is True
    assert any(r["batch_seq"] != 0 for r in account["records"])


def test_s8_golden_account3_has_fragment_without_anchor(golden):
    account = golden["accounts"][2]
    assert account["total"] is None
    assert account["integrity"]["has_anchor"] is False
    assert account["integrity"]["incomplete_events"] > 0
    assert account["integrity"]["partial_records"] > 0


def test_s8_golden_events_ids_cover_all_records(golden):
    for account in golden["accounts"]:
        seen = sorted(i for e in account["events"] for i in e["ids"])
        assert seen == list(range(len(account["records"])))


def test_s8_golden_coord_rule_matches_analyze_events(golden):
    """样本里的 `events[]` 必须与本模块的规则**重算结果一致**（否则样本本身不可信）。"""
    for account in golden["accounts"]:
        assert account["events"] == analyze_events(account["records"])


def test_s8_golden_integrity_matches_recompute(golden):
    for account in golden["accounts"]:
        assert account["integrity"] == compute_integrity(
            account["records"], account["events"], account["integrity"]["authoritative_total"])


def test_s8_golden_export_is_lossless(golden):
    """把样本重新封装导出再解析 ⇒ 记录与三件套一字不变（导出侧与解析侧自洽）。"""
    rebuilt = []
    for account in golden["accounts"]:
        rebuilt.append(build_export_account(
            account["account_id"] or "x", account["name"], account["records"],
            account["total"], account["view_totals"]))
    envelope = build_envelope(rebuilt, "2026-09-21T10:00:00", {"platform": "desktop"})
    again = parse(dumps(envelope))
    assert [a["records"] for a in again["accounts"]] == [a["records"] for a in golden["accounts"]]
    assert [a["total"] for a in again["accounts"]] == [a["total"] for a in golden["accounts"]]


def test_s8_golden_identical_on_both_sides():
    """PC 与 Android 两份副本必须**逐字节相同**（Android 侧存在时才校验）。"""
    if not _GOLDEN_ANDROID.exists():
        pytest.skip("Android 侧样本不在本机（公开仓导出时可能剔除 mobile/）")
    assert _GOLDEN.read_bytes() == _GOLDEN_ANDROID.read_bytes()


# ===========================================================================
# S9 「不导入」第三态（契约 MODE_SKIP）
# ===========================================================================

def test_s9_skip_account_leaves_plan():
    """单项 SKIP ⇒ 该账号**不进计划**（事务壳零改动的前提）。"""
    parsed = parse(_env([_account("甲", [_rec(1000, 0)])]))
    plan = plan_import(parsed, [], [{"index": 0, "mode": MODE_SKIP, "target": None,
                                     "chosen": CHOSEN_FILE}])
    assert plan["ok"] is True
    assert plan["accounts"] == []


def test_s9_mixed_skips_keep_original_index():
    """NEW + SKIP + MERGE 混合 ⇒ 计划只剩两项，`index` 是**原文件下标** 0 与 2。"""
    device = [_device("d1", "丙", [_rec(1000, 0)])]
    parsed = parse(_env([
        _account("甲", [_rec(1000, 0)], total=1),
        _account("乙", [_rec(900, 0)], total=1),
        _account("丙", [_rec(800, 0)], total=1),
    ]))
    plan = plan_import(parsed, device, [
        {"index": 0, "mode": MODE_NEW, "target": None, "chosen": CHOSEN_FILE},
        {"index": 1, "mode": MODE_SKIP, "target": None, "chosen": CHOSEN_FILE},
        {"index": 2, "mode": MODE_MERGE, "target": "d1", "chosen": CHOSEN_FILE},
    ])
    assert [a["index"] for a in plan["accounts"]] == [0, 2]
    assert [a["name"] for a in plan["accounts"]] == ["甲", "丙"]


def test_s9_all_skipped_yields_empty_plan():
    """全 SKIP ⇒ 契约层**不报错**，只是计划为空（拦在 UI 层，事务壳的守卫不动）。"""
    parsed = parse(_env([_account("甲", []), _account("乙", [])]))
    choices = [{"index": i, "mode": MODE_SKIP, "target": None, "chosen": CHOSEN_FILE}
               for i in range(2)]
    plan = plan_import(parsed, [], choices)
    assert plan["ok"] is True
    assert plan["error"] is None
    assert plan["accounts"] == []


def test_s9_invalid_mode_still_raises():
    """钉住 SKIP 分支的**位置**：非法 mode 不能被它静默吞掉（校验必须仍在纯函数层）。"""
    parsed = parse(_env([_account("甲", [])]))
    with pytest.raises(ValueError):
        plan_import(parsed, [], [{"index": 0, "mode": "SKIPPED", "target": None,
                                  "chosen": CHOSEN_FILE}])


def test_s9_skip_does_not_validate_target():
    """SKIP 时 `target` / `chosen` 一律忽略且**不校验**（UI 层则照存不清空，定案稿 §6.2-c）。"""
    parsed = parse(_env([_account("甲", [])]))
    plan = plan_import(parsed, [], [{"index": 0, "mode": MODE_SKIP, "target": "不存在的账号",
                                     "chosen": "BOTH"}])
    assert plan["ok"] is True
    assert plan["accounts"] == []


def test_s9_default_choices_never_skip():
    """`default_choices` **不产出** SKIP ⇒ 默认行为与加第三态之前完全一致（回归）。"""
    device = [_device("d1", "甲", [_rec(1000, 0)])]
    parsed = parse(_env([_account("甲", [_rec(1000, 0)]), _account("乙", [])]))
    modes = {c["mode"] for c in default_choices(parsed, device)}
    assert modes == {MODE_MERGE, MODE_NEW}
