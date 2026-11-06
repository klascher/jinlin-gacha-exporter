"""未知 id 触发 + 页内配对测试（纯逻辑，无 OCR/Qt）。"""
import pytest

from gacha_exporter.analysis.gacha_history_ocr import HistoryPage, HistoryRow
from gacha_exporter.analysis.unknown_trigger import (
    UnknownRecognitionTrigger,
    name_similarity,
    normalize_name,
    validate_page,
)
from gacha_exporter.models import Character, GachaRecord

ALL = "0"


def rec(item_id, pool_id="20009", ts=1_760_000_000_000):
    return GachaRecord(pool_id=pool_id, item_id=item_id, timestamp=ts)


def rows(names, pools=None, star=4):
    pools = pools or ["示例卡池001"] * len(names)
    return [
        HistoryRow(row=i + 1, ocr_name=n, name=n, rarity=star, pool_name=p)
        for i, (n, p) in enumerate(zip(names, pools))
    ]


def page_of(row_list, page=1, total_page=178, headers=True, error=""):
    return HistoryPage(
        width=2400,
        height=1080,
        page=page,
        total_page=total_page,
        rows=row_list,
        headers_ok=headers,
        title_found=headers,
        error=error,
    )


CHARS = {
    "1001": Character(id="1001", name="示例角色028", rarity=6),
    "1002": Character(id="1002", name="示例角色018", rarity=6),
    "1003": Character(id="1003", name="示例角色047", rarity=5),
    "1004": Character(id="1004", name="示例角色035", rarity=5),
    "1005": Character(id="1005", name="示例角色050", rarity=5),
}
KNOWN_IDS = set(CHARS)
KNOWN_POOLS = {"20009"}


def test_feed_ignores_non_all_pools_and_missing_page():
    t = UnknownRecognitionTrigger(debounce=1.0)
    assert not t.feed([rec("9999")], "20009", 0, 100, KNOWN_IDS, KNOWN_POOLS, now=0)
    assert not t.feed([rec("9999")], ALL, None, 100, KNOWN_IDS, KNOWN_POOLS, now=0)
    assert not t.feed([rec("1001")], ALL, 0, 100, KNOWN_IDS, KNOWN_POOLS, now=0)
    assert t.pending_count == 0


def test_debounce_returns_task_and_page_numbers():
    t = UnknownRecognitionTrigger(debounce=1.5)
    assert t.feed([rec("1001"), rec("9999")], ALL, 5, 887, KNOWN_IDS, KNOWN_POOLS, now=10)
    assert t.poll(now=11.0) is None
    task = t.poll(now=11.5)
    assert task is not None
    assert task.page == 2 and task.total_page == 178
    assert task.unknown_item_ids == ("9999",)
    assert [r.item_id for r in task.records] == ["1001", "9999"]
    assert t.pending_count == 0


def test_newer_page_supersedes_older():
    t = UnknownRecognitionTrigger(debounce=1.0)
    t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=10)
    t.feed([rec("8888")], ALL, 5, 887, KNOWN_IDS, KNOWN_POOLS, now=10.4)
    task = t.poll(now=11.5)
    assert task is not None and task.page == 2
    assert task.unknown_item_ids == ("8888",)
    # 2026-10-22 优先级铁律：未截屏的第 1 页不再标 attempted（旧行为是静默
    # 丢弃，实机导致用户被迫返回手动补截），改为交给调用方列入「未完成」，
    # 用户翻回去时必须能重新触发。
    dropped = t.take_abandoned()
    assert [d.page for d in dropped] == [1]
    assert t.take_abandoned() == []
    assert t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=12)


def test_abandoned_cleared_on_reset():
    t = UnknownRecognitionTrigger(debounce=1.0)
    t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=10)
    t.feed([rec("8888")], ALL, 5, 887, KNOWN_IDS, KNOWN_POOLS, now=10.4)
    t.poll(now=11.5)
    t.reset_session()
    assert t.take_abandoned() == []


def test_attempted_blocks_refeed_until_retry():
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    assert t.poll(now=0) is not None
    assert not t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=1)
    t.allow_retry(["9999"])
    assert t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=2)


def test_retry_task_allows_same_page_after_failure():
    """失败后翻回同页必须能再次触发（2026-10-21 实机：提示让回第 10 页，但
    该池已在 attempted，翻回去也不会触发）。"""
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed([rec("1001", pool_id="30007")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    t.finish(task, "failed")
    assert not t.feed(
        [rec("1001", pool_id="30007")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=1
    )
    t.retry(task)
    assert t.feed(
        [rec("1001", pool_id="30007")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=2
    )


def test_latest_pending_reports_newest_page():
    t = UnknownRecognitionTrigger(debounce=10)
    t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=10)
    t.feed([rec("8888")], ALL, 5, 887, KNOWN_IDS, KNOWN_POOLS, now=11)
    latest = t.latest_pending()
    assert latest is not None and latest.page == 2
    t.cancel_pending()
    assert t.latest_pending() is None


def test_finish_recognized_blocks_until_retry():
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    t.finish(task, "recognized")
    assert not t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=1)
    t.allow_retry(["9999"])
    assert t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=2)
    t.reset_session()
    assert t.pending_count == 0


def test_cancel_pending_returns_tasks():
    t = UnknownRecognitionTrigger(debounce=10)
    t.feed([rec("9999")], ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    pending = t.cancel_pending()
    assert len(pending) == 1 and pending[0].page == 1
    assert t.poll(now=100) is None


def test_name_similarity_normalizes_ocr_noise():
    assert normalize_name("示例角色018") == "执剑人骑士"
    assert name_similarity("职业因徒·主厨", "示例角色028") < 0.6
    assert name_similarity("示例角色018", "示例角色018") == 1.0


def _five_records():
    return [
        rec("1001", ts=100),
        rec("1002", ts=99),
        rec("1003", ts=98),
        rec("9999", ts=97),
        rec("1005", ts=96),
    ]


def test_validate_forward_binds_unknown_row():
    recs = _five_records()
    page = page_of(rows(["示例角色028", "示例角色018", "示例角色047", "默之人", "示例角色050"]))
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert v.ok and v.order == "forward", v.reason
    unknown = v.unknown_item_bindings
    assert len(unknown) == 1
    b = unknown[0]
    assert b.record.item_id == "9999"
    assert b.row_index == 3 and b.row.name == "默之人"
    assert b.item_unknown and not b.pool_unknown


def test_validate_reverse_order_detected():
    recs = _five_records()
    page = page_of(rows(["示例角色050", "默之人", "示例角色047", "示例角色018", "示例角色028"]))
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert v.ok and v.order == "reverse"
    assert v.unknown_item_bindings[0].row_index == 1


def test_validate_rejects_wrong_page():
    recs = _five_records()
    page = page_of(rows(["示例角色028"] * 5), page=9)
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert not v.ok and "第 1 页" in v.reason and "第 9 页" in v.reason


def test_validate_rejects_row_count_mismatch():
    recs = _five_records()
    page = page_of(rows(["示例角色028"] * 4))
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert not v.ok and "行数不一致" in v.reason


def test_validate_rejects_unmatched_names():
    recs = _five_records()
    page = page_of(rows(["甲", "乙", "丙", "丁", "戊"]))
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 0, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert not v.ok and "人工配对" in v.reason


def test_validate_rejects_no_known_rows():
    recs = [rec("9999")]
    page = page_of(rows(["未知名"]), page=178, total_page=178)
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 885, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert not v.ok and "没有可用于校验" in v.reason


def test_validate_last_page_allows_partial_rows():
    recs = [rec("1001", ts=2), rec("9999", ts=1)]
    page = page_of(rows(["示例角色028", "默之人"]), page=178, total_page=178)
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 885, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert v.ok, v.reason
    assert v.unknown_item_bindings[0].row_index == 1


def test_validate_collects_unknown_pool_names():
    recs = [rec("1001", pool_id="30001"), rec("1002", pool_id="30001")]
    page = page_of(
        rows(["示例角色028", "示例角色018"], pools=["新UP池", "新UP池"]),
        page=178,
        total_page=178,
    )
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(recs, ALL, 885, 887, KNOWN_IDS, KNOWN_POOLS, now=0)
    task = t.poll(now=0)
    v = validate_page(task, page, CHARS, KNOWN_POOLS)
    assert v.ok
    assert v.unknown_pool_names == {"30001": ["新UP池"]}
    assert all(b.pool_unknown for b in v.bindings)


def test_feed_rejects_oversized_unknown_page():
    """单页未知目超过防御上限：不排队，交人工。"""
    t = UnknownRecognitionTrigger(debounce=0)
    many = [rec(f"999{i}", pool_id=f"3000{i}") for i in range(14)]
    assert not t.feed(many, ALL, 0, 100, KNOWN_IDS, set(), now=0)
    assert t.pending_count == 0


def test_feed_caps_queued_pages():
    """会话内排队页数达到上限后不再接收新页；同页覆盖不受影响。"""
    t = UnknownRecognitionTrigger(debounce=0)
    for i in range(37):
        assert t.feed([rec("9999")], ALL, i * 5, 500, KNOWN_IDS, KNOWN_POOLS, now=0)
    assert t.feed([rec("9999")], ALL, 37 * 5, 500, KNOWN_IDS, KNOWN_POOLS, now=0) is False
    # 已在队列中的页仍可被最新快照覆盖
    assert t.feed([rec("9999")], ALL, 0, 500, KNOWN_IDS, KNOWN_POOLS, now=0)
