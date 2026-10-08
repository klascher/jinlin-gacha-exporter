"""旧版 history.json 兼容（事件级判重）测试。

设计稿：docs/去重方案-旧历史兼容-事件级判重.md §5（T1~T6/T8/T9）。

旧格式特征（2026-09-13 实测 sample.json 解码）：total/view_totals 快照缺失；
batch_seq=页取序号（每页 5 条一个 batch）、position_in_batch=页内序 0~4；
十连跨两页（5+5）→ 同一 ts 的两半 pos 值重复。
新格式：batch_seq=0、position_in_batch=视图内绝对序号（0=最新）。

先红后绿：T1/T2/T3/T4/T6/T8 在旧代码上跑红（复现 2026-09-13 13:47 sample 多算），
T5/T9 为既有行为回归保护（应常绿）。
"""
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from gacha_exporter.analysis.pos_align import merge_by_position
from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.models import GachaRecord
from gacha_exporter.storage.history import HistoryService

PID = "default"

# ---------- 构造器 ----------

def old_ten_draw(ts, items, bs_a, bs_b, pool_id="30001"):
    """旧格式十连：10 条拆两页（每页 5 条、页内序 0~4）；items 按从新到旧。

    只传 5 个 item 时即「部分事件」（旧抓包只拿到一页）。
    """
    recs = [GachaRecord(pool_id, item, ts, batch_seq=bs_a, position_in_batch=i)
            for i, item in enumerate(items[:5])]
    recs += [GachaRecord(pool_id, item, ts, batch_seq=bs_b, position_in_batch=i)
             for i, item in enumerate(items[5:])]
    return recs


def new_ten_draw(ts, items, base, pool_id="30001"):
    """新格式十连：绝对坐标 base..base+9（items[0]=该事件最新一条 → pos=base）。"""
    return [GachaRecord(pool_id, item, ts, batch_seq=0, position_in_batch=base + i)
            for i, item in enumerate(items)]


def old_single(ts, item, bs, pool_id="30001"):
    return [GachaRecord(pool_id, item, ts, batch_seq=bs, position_in_batch=0)]


def new_single(ts, item, pos, pool_id="30001"):
    return [GachaRecord(pool_id, item, ts, batch_seq=0, position_in_batch=pos)]


ITEMS = [f"1{i:02d}" for i in range(10)]  # 十个不同 item，排除同名干扰（A4 不在本处）

TS_A = 1787000000000   # 旧十连（较新）
TS_B = 1786500000000   # 旧单抽
TS_N1 = 1789300000000  # 新抽事件 1
TS_N2 = 1789300600000  # 新抽事件 2


def legacy_history_records():
    """模拟 sample 形态的旧格式历史：2 个完整事件（1 十连 + 1 单抽），无快照。"""
    return old_ten_draw(TS_A, ITEMS, 3, 4) + old_single(TS_B, "199", 5)


def make_engine():
    """事件级判重不消费角色/卡池元数据，空配置即可。"""
    return StatsEngine({}, {}, {})


def _write_users_file(tmp, records, total=None, view_totals=None):
    path = Path(tmp, "users", f"{PID}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"records": records}
    if total is not None:
        payload["total"] = total
    if view_totals is not None:
        payload["view_totals"] = view_totals
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _hist_service(tmp):
    return patch("gacha_exporter.storage.history.get_users_dir",
                 return_value=Path(tmp, "users"))


# ---------- 收尾合并（pos_align.merge_by_position） ----------

def test_merge_legacy_complete_event_skipped():
    """T1: 旧格式完整事件重抓（新坐标）→ 整事件跳过，0 新增、无 fallback。

    旧代码表现（红）：pos 坐标系不同全部判新（+11），且十连事件位置重复
    触发 fallback 整段追加——正是 2026-09-13 13:47 sample +25 的机制。
    """
    hist = legacy_history_records()
    new = new_ten_draw(TS_A, ITEMS, 500) + new_single(TS_B, "199", 510)
    report = merge_by_position(hist, new, shift=0)
    assert report.total_added == 0
    assert report.fallback_events == []
    assert report.event_skipped == 2


def test_draw_two_then_recapture():
    """T3: 抽 2 抽后重抓 —— 只新增 2 个新事件，旧事件全部 ts 命中跳过。"""
    hist = legacy_history_records()
    new = (new_ten_draw(TS_A, ITEMS, 500) + new_single(TS_B, "199", 510)
           + new_single(TS_N1, "198", 0) + new_single(TS_N2, "197", 1))
    report = merge_by_position(hist, new, shift=0)
    assert report.total_added == 2
    assert report.event_skipped == 2
    assert {r.timestamp for r in report.added} == {TS_N1, TS_N2}


def test_partial_history_replaced_by_complete_capture():
    """T4: 部分事件 + 完整重抓 → 新完整事件整体替换，事件大小恢复 ∈{1,10}。

    旧代码表现（红）：位置坐标不重合 → 10 条全追加 → 事件变 15 条（大小异常）。
    """
    hist = old_ten_draw(TS_A, ITEMS[:5], 30, 31)  # 只抓到前半页（页内序 0~4）
    assert len(hist) == 5
    new = new_ten_draw(TS_A, ITEMS, 500)
    report = merge_by_position(hist, new, shift=0)
    assert report.replaced_events == [TS_A]
    assert report.total_added == 10


def test_partial_plus_partial_keeps_position_merge():
    """T5: 双方都是部分事件 → 保持既有「位置可比 → 逐槽判定」行为（回归保护）。"""
    hist = [
        GachaRecord("30001", "101", TS_A, 0, 100),
        GachaRecord("30001", "103", TS_A, 0, 105),
    ]
    new = [
        GachaRecord("30001", "103", TS_A, 0, 105),
        GachaRecord("30001", "104", TS_A, 0, 106),
        GachaRecord("30001", "105", TS_A, 0, 107),
    ]
    report = merge_by_position(hist, new, shift=0)
    assert report.replaced_events == []
    assert report.total_added == 2


def test_merge_does_not_touch_history_records():
    """T9: 合并绝不修改历史记录本身（旧数据一个字节不改的契约）。"""
    hist = legacy_history_records()
    snapshot = [asdict(r) for r in hist]
    new = new_ten_draw(TS_A, ITEMS, 500) + new_single(TS_B, "199", 510)
    merge_by_position(hist, new, shift=0)
    assert [asdict(r) for r in hist] == snapshot


# ---------- 实时判重（stats.add） ----------

def test_realtime_skip_legacy_complete_event():
    """T2: 旧格式历史重抓，实时层即跳过 —— 抓包过程中计数不虚高。"""
    engine = make_engine()
    engine.load_records(legacy_history_records())
    engine.begin_session_shift(baseline_total=11)
    engine.notify_total(11)
    new = new_ten_draw(TS_A, ITEMS, 500) + new_single(TS_B, "199", 510)
    assert engine.add(new) == 0
    assert engine.total == 11


def test_realtime_accepts_partial_event_records():
    """部分历史事件的记录实时照常收下（要送到收尾 merge 做替换/合并），常绿。"""
    engine = make_engine()
    engine.load_records(old_ten_draw(TS_A, ITEMS[:5], 30, 31))
    engine.begin_session_shift(baseline_total=5)
    assert engine.add(new_ten_draw(TS_A, ITEMS, 500)) == 10


# ---------- HistoryService：total 快照条件回填 / 替换落库 ----------

def test_total_backfill_when_all_events_complete():
    """T6a: 旧格式且全部事件完整 → total 回填为记录条数。"""
    records = [asdict(r) for r in legacy_history_records()]
    with tempfile.TemporaryDirectory() as tmp:
        with _hist_service(tmp):
            _write_users_file(tmp, records)
            svc = HistoryService(PID)
            assert svc.last_total == 11
            assert svc._view_totals == {"0@0": 11}


def test_no_backfill_when_partial_event_present():
    """T6b: 存在部分事件 → 无法确知真实总数，不回填（维持 None 回退）。"""
    records = [asdict(r) for r in old_ten_draw(TS_A, ITEMS[:5], 30, 31)]
    with tempfile.TemporaryDirectory() as tmp:
        with _hist_service(tmp):
            _write_users_file(tmp, records)
            svc = HistoryService(PID)
            assert svc.last_total is None


def test_backfill_not_applied_to_new_format():
    """新格式文件已有 total → 原样保留，不被覆盖。"""
    records = [asdict(r) for r in legacy_history_records()]
    with tempfile.TemporaryDirectory() as tmp:
        with _hist_service(tmp):
            _write_users_file(tmp, records, total=99, view_totals={"0@0": 99})
            svc = HistoryService(PID)
            assert svc.last_total == 99


def test_history_service_replaces_partial_event():
    """T4 端到端：历史里的部分事件被新完整事件替换落库。"""
    with tempfile.TemporaryDirectory() as tmp:
        with _hist_service(tmp):
            svc = HistoryService(PID)
            svc.merge(old_ten_draw(TS_A, ITEMS[:5], 30, 31))
            assert len(svc.records) == 5
            added = svc.merge(new_ten_draw(TS_A, ITEMS, 500))
            assert added == 10
            ev = [r for r in svc.records if r.timestamp == TS_A]
            assert len(ev) == 10
            assert {r.position_in_batch for r in ev} == set(range(500, 510))


# ---------- sample 真实数据回归（脱敏 fixture） ----------

FIXTURE = Path(__file__).parent / "fixtures" / "sample_legacy.json"


def _simulated_full_recapture(hist):
    """模拟新格式全量重抓：事件按 ts 从新到旧、事件内按 (bs,pos) 排，赋绝对坐标。"""
    groups = {}
    for r in hist:
        groups.setdefault(r.timestamp, []).append(r)
    base = 0
    new = []
    for ts in sorted(groups, reverse=True):
        ev = sorted(groups[ts], key=lambda r: (r.batch_seq, r.position_in_batch))
        for i, r in enumerate(ev):
            new.append(GachaRecord(r.pool_id, r.item_id, ts, 0, base + i))
        base += len(ev)
    return new


def test_sample_real_data_full_recapture_zero_added():
    """T8: sample 真实 521 条（旧格式）+ 模拟全量重抓 → 0 新增。

    注意断言的是**结果**而非机制：模拟重抓的绝对坐标与旧·页内序号在最新几个
    事件上数值巧合重合（都落在 0~34），这几个事件走位置判重（同样跳过）、
    其余走事件级跳过——两条路都 0 新增，故不断言 event_skipped 的具体个数。
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    hist = [GachaRecord(**r) for r in payload["records"]]
    assert len(hist) == 521
    report = merge_by_position(hist, _simulated_full_recapture(hist), shift=0)
    assert report.total_added == 0
    assert report.fallback_events == []
    assert report.suspicious == []
    assert report.replaced_events == []


def test_sample_real_data_total_backfill():
    """T8: sample 真实数据加载 → 全部事件完整 → total 回填 521。"""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        with _hist_service(tmp):
            _write_users_file(tmp, payload["records"])
            svc = HistoryService(PID)
            assert svc.last_total == 521
            assert len(svc.complete_event_ts) == 71
            assert svc.partial_event_ts == set()
