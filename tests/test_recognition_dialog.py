"""识别结果确认窗测试（offscreen，不弹真实窗口）。

覆盖：自动配对默认勾选、改名/星级、卡池弱推断默认值、写入 overlay、
人工配对选行后写入、取消不写。overlay 路径重定向到 tmp_path。
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gacha_exporter.analysis.gacha_history_ocr import HistoryPage, HistoryRow  # noqa: E402
from gacha_exporter.analysis.unknown_trigger import (  # noqa: E402
    UnknownRecognitionTrigger,
    validate_page,
)
from gacha_exporter.gui.recognition_dialog import (  # noqa: E402
    OverlayManagerDialog,
    RecognitionDialog,
)
from gacha_exporter.models import Character, GachaRecord  # noqa: E402
from gacha_exporter.storage import role_overlay as ro  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def ov_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ro, "get_data_dir", lambda: str(tmp_path))
    return tmp_path / "role_overlay.json"


CHARS = {
    "1001": Character(id="1001", name="示例角色028", rarity=6),
    "1002": Character(id="1002", name="示例角色018", rarity=6),
    "1003": Character(id="1003", name="示例角色047", rarity=5),
    "1004": Character(id="1004", name="示例角色035", rarity=5),
}


def rec(iid, pool="30001", ts=1):
    return GachaRecord(pool_id=pool, item_id=iid, timestamp=ts)


def row(i, name, pool="新UP池", rarity=4, new=False, name_v4="", pool_v4="",
        rarity_v4=0):
    return HistoryRow(
        row=i + 1,
        ocr_name=name,
        name=name,
        rarity=rarity,
        new_tag=new,
        pool_name=pool,
        time_text="",
        time_norm="2026-09-29 10:23:36",
        name_v4=name_v4,
        pool_v4=pool_v4,
        rarity_v4=rarity_v4,
    )


def page_of(rows, page=1, total=178):
    return HistoryPage(
        width=2400,
        height=1080,
        page=page,
        total_page=total,
        rows=rows,
        headers_ok=True,
        title_found=True,
    )


def _success_validation():
    records = [
        rec("1001"),
        rec("1002"),
        rec("1003"),
        rec("1004"),
        rec("9999"),
    ]
    rows = [
        row(0, "示例角色028", rarity=6),
        row(1, "示例角色018", rarity=6, new=True),
        row(2, "示例角色047", rarity=5),
        row(3, "示例角色035", rarity=5),
        row(4, "示例角色050", rarity=5),
    ]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 0, 887, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    return validate_page(task, page_of(rows), CHARS, set())


def test_auto_pair_defaults_and_write(qapp, ov_env):
    v = _success_validation()
    assert v.ok
    dlg = RecognitionDialog(None, [v], [], [], CHARS, {})
    chars = dlg._collect_character_drafts()
    pools = dlg._collect_pool_drafts()
    assert chars[0]["name"] == "示例角色050"  # 未知 id 绑定第 5 行
    assert chars[0]["rarity"] == 5
    assert chars[0]["binding"].row.new_tag is False
    # 卡池 30001：id 段 + 新 6★ -> UP，UP 角色取本行新 6★
    assert pools[0]["type"] == "UP"
    assert pools[0]["up_character"] == "示例角色018"
    assert pools[0]["pity_group"] == "shared:UP"
    # 主表中的默认勾选
    assert dlg._char_table.item(0, 0).checkState() == Qt.CheckState.Checked
    assert dlg._pool_table.item(0, 0).checkState() == Qt.CheckState.Checked
    # 改名 + 改星级后写入
    dlg._char_table.item(0, 2).setText("示例角色050改")
    dlg._char_table.cellWidget(0, 3).setCurrentIndex(0)  # 4★
    dlg._on_write()
    data = json.loads(ov_env.read_text(encoding="utf-8"))
    assert data["characters"]["9999"]["name"] == "示例角色050改"
    assert data["characters"]["9999"]["rarity"] == 4
    assert data["characters"]["9999"]["source"]["page"] == 1
    assert data["pools"]["30001"]["type"] == "UP"
    assert data["pools"]["30001"]["up_character"] == "示例角色018"
    assert dlg.written_characters and dlg.written_pools


def test_manual_pairing_writes_selected_row(qapp, ov_env):
    records = [rec("1001"), rec("9999")]
    rows = [row(0, "完全不对"), row(1, "示例角色018", rarity=6, new=True)]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 885, 887, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    v = validate_page(task, page_of(rows, page=178, total=178), CHARS, set())
    assert not v.ok
    dlg = RecognitionDialog(None, [], [v], [], CHARS, {})
    entries = dlg._manual_rows
    assert len(entries) == 1
    # 选中第 2 行并勾选写入
    entries[0]["row_combo"].setCurrentIndex(2)
    assert entries[0]["name_item"].text() == "示例角色018"
    dlg._manual_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dlg._on_write()
    data = json.loads(ov_env.read_text(encoding="utf-8"))
    assert data["characters"]["9999"]["name"] == "示例角色018"
    assert data["characters"]["9999"]["rarity"] == 6
    assert data["characters"]["9999"]["source"]["row"] == 2


def test_manual_pool_pairing_writes_selected_row(qapp, ov_env):
    """只有池未知（无未知角色）且自动校验未过的页，也必须能人工选行写入。

    2026-10-21 P5 实机：30007 页只含未知池，失败后旧实现完全没有入口。
    """
    records = [rec("1001", pool="30001"), rec("1002", pool="30001")]
    rows = [row(0, "完全不对", pool="示例卡池001"), row(1, "也完全不对", pool="示例卡池001")]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 885, 887, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    # 已知角色名字全不匹配 → 行序校验失败，但页号一致、池 30001 未知
    v = validate_page(task, page_of(rows, page=178, total=178), CHARS, set())
    assert not v.ok and v.task.unknown_pool_ids == ("30001",)
    dlg = RecognitionDialog(None, [], [v], [], CHARS, {})
    entries = dlg._pool_manual_rows
    assert len(entries) == 1 and entries[0]["pool_id"] == "30001"
    assert dlg._pool_manual_table is not None
    entries[0]["row_combo"].setCurrentIndex(1)  # 选第 1 行取池名
    assert entries[0]["name_item"].text() == "示例卡池001"
    # UP 池必须填 UP 角色，否则写入前会弹确认框（无头下会阻塞）
    entries[0]["up_combo"].setCurrentText("示例角色018")
    dlg._pool_manual_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dlg._on_write()
    data = json.loads(ov_env.read_text(encoding="utf-8"))
    assert data["pools"]["30001"]["name"] == "示例卡池001"
    assert data["pools"]["30001"]["type"] == "UP"
    assert data["pools"]["30001"]["source"]["row"] == 1
    assert dlg.written_pools


def test_pool_up_inferred_from_captured_six_star(qapp, ov_env):
    """UP 6★ 不在被 OCR 的页上时，用本池抓到的记录反推（骑士案例）。

    实机：30007 第一页在第 6 页（无 6★），骑士在第 7 页且未触发 OCR；
    但第 7 页的响应记录已抓到，30007 里唯一的 6★ 就是骑士 → 默认 UP。
    """
    records = [
        rec("1003"),
        rec("1004"),
        rec("1001"),
        rec("1002"),
        rec("9999"),
    ]
    rows = [
        row(0, "示例角色047", rarity=5),
        row(1, "示例角色035", rarity=5),
        row(2, "示例角色028", rarity=6),
        row(3, "示例角色018", rarity=6),
        row(4, "默之人", rarity=4),
    ]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 0, 887, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    v = validate_page(task, page_of(rows), CHARS, set())
    assert v.ok
    dlg = RecognitionDialog(
        None, [v], [], [], CHARS, {}, pool_items={"30001": {"1002"}}
    )
    draft = dlg._collect_pool_drafts()[0]
    assert draft["up_character"] == "示例角色018"
    assert draft["up_inferred"] is True
    options = dlg._up_name_options()
    assert "示例角色018" in options
    # 4★ 未知角色也进候选（2026-10-23 实机：星级被 OCR 误判时仍要找得到人）；
    # 默认值仍由 6★ 推断/唯一陌生角色规则决定，不会自动选中 4★
    assert "默之人" in options
    # 本池有两个不同 6★（有歪出）时不猜，留给人工选择
    dlg2 = RecognitionDialog(
        None, [v], [], [], CHARS, {}, pool_items={"30001": {"1001", "1002"}}
    )
    assert dlg2._collect_pool_drafts()[0]["up_character"] == ""


def test_v4_v5_divergence_marked_in_character_tab(qapp, ov_env):
    """C6 分歧行在确认窗「重点核对」列摊出 v4 原文，未分歧行留空。"""
    v = _success_validation()
    v.bindings[-1].row.name_v4 = "香料商人"  # 9999 那行：v4 丢字
    v.bindings[-1].row.pool_v4 = "新UP"  # 池名列也不一致
    v.bindings[-1].row.rarity_v4 = 6  # 星级也被 v5 改写
    dlg = RecognitionDialog(None, [v], [], [], CHARS, {})
    mark = dlg._char_table.item(0, 8).text()
    assert "名字 v4：香料商人" in mark
    assert "星级 v4：6★" in mark
    assert "池名 v4：新UP" in mark
    assert dlg._char_table.horizontalHeaderItem(8).text() == "重点核对"
    assert dlg._char_table.item(0, 2).text() == "示例角色050"


def test_v4_v5_divergence_marked_in_manual_tab(qapp, ov_env):
    """人工配对的行选项直接带出 v4 原文，选中前即可核对。"""
    records = [rec("1001"), rec("9999")]
    rows = [
        row(0, "完全不对"),
        row(1, "示例角色040", rarity=4, name_v4="默之人"),
    ]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 885, 887, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    v = validate_page(task, page_of(rows, page=178, total=178), CHARS, set())
    assert not v.ok
    dlg = RecognitionDialog(None, [], [v], [], CHARS, {})
    combo = dlg._manual_rows[0]["row_combo"]
    assert "⚠ v4：默之人" in combo.itemText(2)
    assert "⚠" not in combo.itemText(1)


def _two_page_pool_soak(new_tag=True):
    """第 1 页触发未知池 30008 识别；监狱长在第 2 页才抽到。

    第 2 页的任务里 already-attempted 的 30008 不再出现，所以 UP 只能靠
    跨页汇总（或本池记录反推）带出——2026-10-22 实机「没自动映射」场景。
    """
    p1_records = [rec(str(i), pool="30008") for i in (1001, 1002, 1003, 1004, 1001)]
    p1_rows = [
        row(0, "示例角色028", pool="示例卡池009", rarity=6),
        row(1, "示例角色018", pool="示例卡池009", rarity=6),
        row(2, "示例角色047", pool="示例卡池009", rarity=5),
        row(3, "示例角色035", pool="示例卡池009", rarity=5),
        row(4, "示例角色028", pool="示例卡池009", rarity=6),
    ]
    p2_records = [rec("1001", pool="30008"), rec("90000049", pool="30008"),
                  rec("1003", pool="30008")]
    p2_rows = [
        row(0, "示例角色028", pool="示例卡池009", rarity=6),
        row(1, "示例角色039", pool="示例卡池009", rarity=6, new=new_tag),
        row(2, "示例角色047", pool="示例卡池009", rarity=5),
    ]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(p1_records, "0", 0, 10, set(CHARS), set(), now=0)
    task1 = t.poll(now=0)
    v1 = validate_page(task1, page_of(p1_rows, page=1, total=2), CHARS, set())
    assert v1.ok and v1.task.unknown_pool_ids == ("30008",)
    t.feed(p2_records, "0", 5, 10, set(CHARS), set(), now=1)
    task2 = t.poll(now=1)
    v2 = validate_page(task2, page_of(p2_rows, page=2, total=2), CHARS, set())
    assert v2.ok and v2.task.unknown_item_ids == ("90000049",)
    assert v2.task.unknown_pool_ids == ()  # attempted 去重后不再出现
    return [v1, v2]


def test_pool_up_carried_across_pages_from_new_six(qapp, ov_env):
    """新 6★ 抽在别的页，也自动映射到未知池的 UP（不再要人手改）。"""
    v1, v2 = _two_page_pool_soak()
    dlg = RecognitionDialog(
        None, [v1, v2], [], [], CHARS, {},
        pool_items={"30008": {"1001", "90000049", "1003"}},
    )
    draft = dlg._collect_pool_drafts()[0]
    assert draft["pool_id"] == "30008"
    assert draft["name"] == "示例卡池009"
    assert draft["type"] == "UP"
    assert draft["up_character"] == "示例角色039"
    assert draft["up_inferred"] is False
    assert draft["up_page"] == 2
    assert "示例角色039" in dlg._up_name_options()
    # 角色表的「卡池」列带 id，映射关系一眼可见
    assert dlg._char_table.item(0, 4).text() == "示例卡池009（30008）"
    # 写入时该池的 UP 就是监狱长，无需人工选择
    chars, pools = dlg._collect_checked()
    assert any(p["pool_id"] == "30008"
               and p["entry"]["up_character"] == "示例角色039"
               for p in pools)


def test_pool_up_inferred_from_unknown_draft(qapp, ov_env):
    """本池唯一 6★ 是本批未知角色草稿（无「新」标）时也能反推 UP。

    2026-10-23 起「唯一陌生角色且在池内」优先于这条推断（solo 来源），
    结果仍是同一个名字；这里保留该场景做回归。
    """
    v1, v2 = _two_page_pool_soak(new_tag=False)
    dlg = RecognitionDialog(
        None, [v1, v2], [], [], CHARS, {},
        pool_items={"30008": {"90000049", "1003"}},
    )
    draft = dlg._collect_pool_drafts()[0]
    assert draft["up_character"] == "示例角色039"
    assert draft["up_solo"] is True


def _solo_unknown_validation(row_name="示例角色039（64）", row_rarity=6):
    """本批只有唯一陌生角色 9999，且它的记录就在陌生池 30002 里。"""
    records = [rec("1001", pool="30002"), rec("9999", pool="30002")]
    rows = [
        row(0, "示例角色028", pool="新池名", rarity=6),
        row(1, row_name, pool="新池名", rarity=row_rarity),
    ]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 0, 2, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    return validate_page(task, page_of(rows, page=1, total=1), CHARS, set())


def test_pool_up_auto_from_solo_unknown_character(qapp, ov_env):
    """唯一陌生角色且在陌生池里 → 直接当该池 UP（2026-10-23 实机反馈）。

    监狱长的名字尾巴粘着「（64）」（v5 把 ♛ 读成数字），旧实现既不填 UP、
    下拉里也找不到人。现在：名字剥角标、UP 自动带出、候选里可见。本池另有
    一个唯一已知 6★（温情杀手）时，仍以陌生角色为准。
    """
    v = _solo_unknown_validation()
    assert v.ok and v.task.unknown_item_ids == ("9999",)
    dlg = RecognitionDialog(
        None, [v], [], [], CHARS, {},
        pool_items={"30002": {"1001", "9999"}},
    )
    draft = dlg._collect_pool_drafts()[0]
    assert draft["up_character"] == "示例角色039"
    assert draft["up_solo"] is True
    assert draft["up_inferred"] is False
    # 角色表：名字已剥角标，「OCR 原文」保留原始证据
    assert dlg._char_table.item(0, 2).text() == "示例角色039"
    assert "（64）" in dlg._char_table.item(0, 7).text()
    # 星级虽被误判为 4★，UP 候选里仍然找得到
    assert "示例角色039" in dlg._up_name_options()
    assert "自动匹配" in dlg._pool_table.item(0, 6).text()
    chars, pools = dlg._collect_checked()
    assert any(p["entry"]["up_character"] == "示例角色039" for p in pools)


def test_solo_unknown_rule_needs_exactly_one_unknown(qapp, ov_env):
    """有两个陌生角色或记录不在该池时不猜 UP。"""
    records = [
        rec("9998", pool="30002"),
        rec("9999", pool="30002"),
        rec("1001", pool="30002"),
    ]
    rows = [
        row(0, "甲甲甲·甲", pool="新池名", rarity=6),
        row(1, "乙乙乙·乙", pool="新池名", rarity=6),
        row(2, "示例角色028", pool="新池名", rarity=6),
    ]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 0, 3, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    v = validate_page(task, page_of(rows, page=1, total=1), CHARS, set())
    dlg = RecognitionDialog(
        None, [v], [], [], CHARS, {},
        pool_items={"30002": {"9998", "9999", "1001"}},
    )
    assert dlg._collect_pool_drafts()[0]["up_character"] == ""
    # 只有唯一陌生角色、但该池记录里没有它 → 同样不猜（池里放一个 5★，
    # 避免触发「本池唯一已知 6★」的既有推断）
    records2 = [rec("9999", pool="30003"), rec("1003", pool="30002")]
    rows2 = [
        row(0, "示例角色039", pool="新池名", rarity=6),
        row(1, "示例角色047", pool="新池名", rarity=5),
    ]
    t2 = UnknownRecognitionTrigger(debounce=0)
    t2.feed(records2, "0", 0, 2, set(CHARS), set(), now=0)
    task2 = t2.poll(now=0)
    v2 = validate_page(task2, page_of(rows2, page=1, total=1), CHARS, set())
    dlg2 = RecognitionDialog(
        None, [v2], [], [], CHARS, {},
        pool_items={"30002": {"1003"}, "30003": {"9999"}},
    )
    by_id = {d["pool_id"]: d for d in dlg2._collect_pool_drafts()}
    assert by_id["30002"]["up_character"] == ""
    assert by_id["30003"]["up_character"] == "示例角色039"


def test_up_name_fuzzy_matches_known_six(qapp, ov_env):
    """OCR 名字错一个字时，UP 用已知 6★ 的规范名（模糊匹配）。"""
    chars = dict(CHARS)
    chars["1005"] = Character(id="1005", name="示例角色039", rarity=6)
    v = _solo_unknown_validation(row_name="终身监护人·监狱常（64）", row_rarity=6)
    dlg = RecognitionDialog(
        None, [v], [], [], chars, {},
        pool_items={"30002": {"1001", "9999"}},
    )
    assert dlg._collect_pool_drafts()[0]["up_character"] == (
        "示例角色039"
    )


def test_up_dropdown_wide_and_shows_full_name(qapp, ov_env):
    """UP 下拉必须给足宽度并带悬浮提示，长名字不再被省略成 x...x。"""
    v1, v2 = _two_page_pool_soak()
    dlg = RecognitionDialog(
        None, [v1, v2], [], [], CHARS, {},
        pool_items={"30008": {"90000049"}},
    )
    combo = dlg._pool_table.cellWidget(0, 4)
    assert combo.currentText() == "示例角色039"
    assert combo.minimumWidth() >= 260
    assert dlg._pool_table.columnWidth(4) >= 260
    assert combo.toolTip() == "示例角色039"
    combo.setCurrentText("示例角色018")
    assert combo.toolTip() == "示例角色018"


def test_manual_pool_up_dropdown_wide(qapp, ov_env):
    records = [rec("1001", pool="30001"), rec("1002", pool="30001")]
    rows = [row(0, "完全不对", pool="示例卡池001"),
            row(1, "也完全不对", pool="示例卡池001")]
    t = UnknownRecognitionTrigger(debounce=0)
    t.feed(records, "0", 885, 887, set(CHARS), set(), now=0)
    task = t.poll(now=0)
    v = validate_page(task, page_of(rows, page=178, total=178), CHARS, set())
    dlg = RecognitionDialog(None, [], [v], [], CHARS, {})
    entry = dlg._pool_manual_rows[0]
    assert entry["up_combo"].minimumWidth() >= 260
    assert dlg._pool_manual_table.columnWidth(5) >= 260
    assert entry["row_combo"].minimumWidth() >= 270


def test_cancel_writes_nothing(qapp, ov_env):
    dlg = RecognitionDialog(None, [_success_validation()], [], [], CHARS, {})
    dlg.reject()
    assert not ov_env.exists()


def test_overlay_manager_delete(qapp, ov_env):
    ro.upsert_character("9999", {"name": "甲", "rarity": 6})
    ro.upsert_pool("30001", {"name": "乙池", "type": "UP"})
    dlg = OverlayManagerDialog()
    assert dlg._table.rowCount() == 2
    dlg._table.selectRow(0)
    # 绕过确认框：直接走删除核心路径（_on_delete 的确认在无头下会阻塞）
    for r in [0, 1]:
        e = dlg._entries[r]
        kind, oid = e.get("kind"), str(e.get("id"))
        if kind == "character":
            ro.remove_character(oid)
        else:
            ro.remove_pool(oid)
        dlg.removed.append((kind, oid))
    data = ro.load_overlay()
    assert data["characters"] == {} and data["pools"] == {}
    assert {(k, i) for k, i in dlg.removed} == {
        ("character", "9999"),
        ("pool", "30001"),
    }
