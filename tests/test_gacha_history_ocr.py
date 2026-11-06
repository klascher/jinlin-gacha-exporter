"""契约记录页 OCR 解析测试。

纯逻辑用合成 OCR 框（不依赖模型）；真实 8 页样本测试在缺图/缺 rapidocr
时自动跳过（CI 与仅开发环境无 OCR 依赖都能跑）。
"""
import glob
from pathlib import Path

import pytest

from gacha_exporter.analysis.gacha_history_ocr import (
    OCRBox,
    normalize_separators,
    normalize_time,
    parse_boxes,
    parse_name_and_rarity,
    strip_ui_noise,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = sorted(glob.glob(str(ROOT / "dump" / "素材" / "抽卡页面-[0-9]*.jpg")))


def box(text, x1, y1, x2, y2, score=0.99, v4=""):
    return OCRBox(text=text, score=score, x1=x1, y1=y1, x2=x2, y2=y2, v4_text=v4)


def _layout_boxes():
    """2400×1080 表头 + 两行数据 + 页码 + 面板外悬浮窗文本。"""
    boxes = [
        box("契约记录", 548, 206, 685, 242),
        box("风格名称", 943, 308, 1071, 342),
        box("类型", 1303, 307, 1372, 344),
        box("时间", 1628, 307, 1698, 344),
        box("7 / 178", 1281, 833, 1396, 860),
        # 面板外悬浮窗（左上角）：不得进入任何列
        box("抓包", 120, 60, 180, 95),
        box("录制中", 130, 400, 200, 430),
    ]
    rows = [
        (400, "示例角色018（6）", "示例卡池001", "2026-09-29 10:23:36"),
        (492, "示例角色043", "示例卡池001", "2026-09-29 10:23:21"),
    ]
    for cy, name, pool, ts in rows:
        boxes.append(box(name, 888, cy - 18, 1150, cy + 18))
        boxes.append(box(pool, 1276, cy - 16, 1430, cy + 16))
        boxes.append(box(ts, 1548, cy - 17, 1756, cy + 17))
    boxes.append(box("新", 1111, 372, 1155, 398))  # 第 1 行右上角独立新标
    return boxes


def test_parse_layout_rows_and_ignore_floating_window():
    page = parse_boxes(_layout_boxes(), 2400, 1080)
    assert page.ok and page.page == 7 and page.total_page == 178
    assert [r.name for r in page.rows] == ["示例角色018", "示例角色043"]
    assert page.rows[0].rarity == 6 and page.rows[0].new_tag is True
    assert page.rows[1].rarity == 4 and page.rows[1].new_tag is False
    assert all(r.pool_name == "示例卡池001" for r in page.rows)
    assert page.rows[0].time_norm == "2026-09-29 10:23:36"
    assert page.complete is False


def test_name_containing_xin_is_not_new_tag():
    # 「财富新贵」含「新」但不是新标；只有独立框/星级后紧跟的「新」才算
    assert parse_name_and_rarity("示例角色043") == ("示例角色043", 4, False)
    boxes = _layout_boxes()
    page = parse_boxes(boxes, 2400, 1080)
    assert page.rows[1].new_tag is False


def test_star_parsing_drops_glyph():
    assert parse_name_and_rarity("示例角色050（5）") == ("示例角色050", 5, False)
    assert parse_name_and_rarity("示例角色018（6♛）") == ("示例角色018", 6, False)
    assert parse_name_and_rarity("示例角色040") == ("示例角色040", 4, False)


def test_star_glyph_read_as_digit_not_on_name():
    """2026-10-23 实机：v5 把 ♛ 读成 4 → ``（64）``。

    旧正则把名字留成「示例角色039（64）」、星级跌回 4★，UP 下拉与
    UP 推断都认不出人；现在首数字后允许短尾，角标不粘名字、星级仍正确。
    """
    assert parse_name_and_rarity("示例角色039（64）") == (
        "示例角色039", 6, False
    )
    assert parse_name_and_rarity("示例角色018（6♛4）") == (
        "示例角色018", 6, False
    )


def test_toast_and_square_separator_are_cleaned():
    """2026-10-23 全量评测：游戏「操作太频繁」浮层与 ▪ 分隔符。

    v4/v5 都被浮层污染（11 行）、v5 另把中点读成 ▪（5 行）；两者都不是列
    内容，必须在进入行解析前清掉。
    """
    assert normalize_separators("三流侦探▪嘀嗒") == "示例角色001"
    assert strip_ui_noise("示例角色009操作太频繁，请稍后再试") == "示例角色009"
    assert strip_ui_noise("操作太频繁，请稍后再试") == ""
    assert strip_ui_noise("稍后再试") == ""
    assert strip_ui_noise("正常名字") == "正常名字"

    boxes = _layout_boxes()
    # 浮层提示作为独立框压在名字列同一行：不得拼进名字
    boxes.append(box("操作太频繁，请稍后再试", 888, 472, 1300, 512))
    page = parse_boxes(boxes, 2400, 1080)
    assert [r.name for r in page.rows] == ["示例角色018", "示例角色043"]


def test_star_then_new_in_same_box():
    boxes = _layout_boxes()
    boxes = [
        b for b in boxes
        if not (b.text == "新")
    ]
    for b in boxes:
        if b.text == "示例角色018（6）":
            b2 = box("示例角色018（6）新", b.x1, b.y1, b.x2 + 40, b.y2)
            boxes[boxes.index(b)] = b2
    page = parse_boxes(boxes, 2400, 1080)
    assert page.rows[0].name == "示例角色018"
    assert page.rows[0].rarity == 6 and page.rows[0].new_tag is True


def test_page_arrow_prefix():
    boxes = _layout_boxes()
    for i, b in enumerate(boxes):
        if b.text == "7 / 178":
            boxes[i] = box("<7/178", b.x1, b.y1, b.x2, b.y2)
    page = parse_boxes(boxes, 2400, 1080)
    assert page.page == 7 and page.total_page == 178


def test_time_space_swallowed():
    assert normalize_time("2026-10-0614:07:16") == "2026-10-06 14:07:16"
    assert normalize_time("2026-10-06 14:07:16") == "2026-10-06 14:07:16"
    assert normalize_time("乱码") == ""


def test_missing_headers_rejected():
    page = parse_boxes([box("随便", 100, 100, 200, 130)], 2400, 1080)
    assert not page.ok and "表头" in page.error


def test_missing_title_rejected():
    boxes = [b for b in _layout_boxes() if b.text != "契约记录"]
    page = parse_boxes(boxes, 2400, 1080)
    assert not page.ok and "契约记录" in page.error


@pytest.mark.skipif(not SAMPLES, reason="缺少 dump/素材 抽卡页面样本")
def test_real_samples_8_pages_40_rows():
    pytest.importorskip("rapidocr_onnxruntime")
    from gacha_exporter.analysis.gacha_history_ocr import recognize_file

    pages = [recognize_file(p) for p in SAMPLES]
    assert len(pages) == 8
    assert [p.page for p in pages] == list(range(1, 9))
    assert all(p.total_page == 178 for p in pages)
    assert all(p.ok for p in pages), [p.error for p in pages]
    rows = [r for p in pages for r in p.rows]
    assert len(rows) == 40
    # 已知样本的关键字段（OCR 漏字由调用方模糊匹配，这里钉住漏字现状）
    assert rows[0].name == "示例角色028"
    assert rows[0].time_norm == "2026-10-06 14:07:16"
    knight = next(r for r in rows if r.name == "示例角色018")
    assert knight.rarity == 6 and knight.new_tag is True
    assert knight.pool_name == "示例卡池001"
    gifts = next(r for r in rows if r.name == "示例角色016")
    assert gifts.rarity == 5 and gifts.new_tag is False
    assert all(r.rarity in (4, 5, 6) for r in rows)
    # UID 绝不进入结果
    assert all("UID" not in r.ocr_name and "102917442" not in r.ocr_name for r in rows)


# ---- C6 列定向重识别（2026-10-22）：归一化与回退路径 ----

def test_separator_variants_normalized():
    from gacha_exporter.analysis.gacha_history_ocr import normalize_separators

    assert normalize_separators("铁桶怪客•园丁") == "示例角色048"
    assert normalize_separators("香料商人・叻沙") == "示例角色050"
    assert normalize_separators("温情杀手．主厨") == "示例角色028"
    # 正常「·」与其他字符不受影响
    assert normalize_separators("示例角色028") == "示例角色028"
    assert normalize_separators("") == ""


def test_parse_boxes_normalizes_separator_in_name_and_pool():
    boxes = [
        b if b.text not in ("示例角色018（6）", "示例卡池001")
        else box(b.text.replace("·", "•"), b.x1, b.y1, b.x2, b.y2)
        for b in _layout_boxes()
    ]
    page = parse_boxes(boxes, 2400, 1080)
    assert page.ok
    row = page.rows[0]
    assert row.name == "示例角色018"
    assert row.pool_name == "示例卡池001"


def test_refine_name_boxes_without_image_returns_unchanged():
    import gacha_exporter.analysis.gacha_history_ocr as gh

    boxes = _layout_boxes()
    out = gh.refine_name_boxes(None, boxes, 2400, 1080)
    assert out == list(boxes)


def test_refine_name_boxes_falls_back_when_model_missing(monkeypatch):
    import gacha_exporter.analysis.gacha_history_ocr as gh

    monkeypatch.setattr(gh, "get_name_ocr", lambda: None)
    boxes = _layout_boxes()
    # image 只被 _rec_crop 使用；模型不可用时应完全不触碰图像
    out = gh.refine_name_boxes(object(), boxes, 2400, 1080)
    assert out == list(boxes)


# ---- C6 分歧核对（2026-10-22）：v4 原文保留、行标记与本机日志 ----

def test_refine_name_boxes_keeps_v4_text(monkeypatch):
    import gacha_exporter.analysis.gacha_history_ocr as gh

    monkeypatch.setattr(gh, "get_name_ocr", lambda: object())
    monkeypatch.setattr(
        gh, "_rec_crop", lambda ocr, image, b: ("示例角色040", 0.97)
    )
    boxes = _layout_boxes()
    out = gh.refine_name_boxes(object(), boxes, 2400, 1080)
    refined = [b for b in out if b.v4_text]
    # 两行 ×（名字列 + 池名列）= 4 个框被重识别
    assert len(refined) == 4
    for b in refined:
        assert b.text == "示例角色040" and b.v4_text
    # 表头、「新」标、页码框（横向落在名字列内）不参与重识别
    assert not any(
        b.v4_text for b in out if b.text in ("风格名称", "契约记录", "新", "7 / 178")
    )


def test_diverged_name_and_pool_flagged_on_row():
    boxes = _layout_boxes()
    for i, b in enumerate(boxes):
        if b.text == "示例角色018（6）":
            boxes[i] = box(b.text, b.x1, b.y1, b.x2, b.y2, v4="默之人（6）")
        elif b.text == "示例卡池001" and b.y1 < 450:
            boxes[i] = box(b.text, b.x1, b.y1, b.x2, b.y2, v4="不曾摧折之")
    page = parse_boxes(boxes, 2400, 1080)
    row = page.rows[0]
    assert row.name == "示例角色018" and row.name_v4 == "默之人"
    assert row.name_diverged is True
    assert row.pool_name == "示例卡池001" and row.pool_v4 == "不曾摧折之"
    assert row.pool_diverged is True
    # 未重识别的行不产生分歧
    assert page.rows[1].name_v4 == "" and page.rows[1].name_diverged is False


def test_diverged_rarity_flagged_on_row():
    """v5 只改写了星级角标、名字本体相同：名字不算分歧，星级要点出来。"""
    boxes = _layout_boxes()
    for i, b in enumerate(boxes):
        if b.text == "示例角色018（6）":
            boxes[i] = box(b.text, b.x1, b.y1, b.x2, b.y2, v4="示例角色018（5）")
    row = parse_boxes(boxes, 2400, 1080).rows[0]
    assert row.rarity == 6 and row.rarity_v4 == 5
    assert row.rarity_diverged is True
    assert row.name_diverged is False
    assert parse_boxes(_layout_boxes(), 2400, 1080).rows[0].rarity_diverged is False


def test_separator_only_difference_is_not_divergence():
    """v4 用异体分隔符、v5 归一后只差中点：不算分歧（名字本体相同）。"""
    boxes = _layout_boxes()
    for i, b in enumerate(boxes):
        if b.text == "示例角色018（6）":
            boxes[i] = box(b.text, b.x1, b.y1, b.x2, b.y2, v4="执剑人•骑士（6）")
    row = parse_boxes(boxes, 2400, 1080).rows[0]
    assert row.name_v4 == "示例角色018"
    assert row.name_diverged is False


def test_divergence_is_logged(caplog):
    import gacha_exporter.analysis.gacha_history_ocr as gh

    boxes = _layout_boxes()
    for i, b in enumerate(boxes):
        if b.text == "示例角色018（6）":
            boxes[i] = box(b.text, b.x1, b.y1, b.x2, b.y2, v4="默之人（6）")
    page = parse_boxes(boxes, 2400, 1080)
    with caplog.at_level("INFO", logger=gh.__name__):
        gh._log_divergences(page)
    text = caplog.text
    assert "C6 分歧" in text
    assert "默之人" in text and "示例角色018" in text


# ---- 2026-10-24 C6 裁剪非对称与页码不变量 ----


def test_rec_crop_uses_wider_horizontal_pad(monkeypatch):
    """左右 20px 负责找回首字，上下 10px 只留字高、少带背景噪声。"""
    import sys
    import types

    import gacha_exporter.analysis.gacha_history_ocr as gh

    regions = []
    resized = []

    class Crop:
        size = 1

    class Image:
        shape = (200, 200, 3)

        def __getitem__(self, key):
            regions.append(key)
            return Crop()

    fake_cv2 = types.SimpleNamespace(
        INTER_CUBIC=0,
        resize=lambda crop, size, fx, fy, interpolation: resized.append((fx, fy)) or crop,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    def fake_ocr(crop, use_det, use_cls, use_rec):
        return [("示例角色018", 0.97)], None

    text, score = gh._rec_crop(fake_ocr, Image(), box("x", 100, 100, 140, 130))
    assert text == "示例角色018" and score == 0.97
    # (y1-10, y2+10) × (x1-20, x2+20)
    assert regions == [(slice(90, 140), slice(80, 160))]
    assert resized == [(3, 3)]


def test_refined_box_never_becomes_page_number():
    """v5 重识别框不能当页码：全屏截图底部杂框可能被读成 ``1700007/0052``。

    真页码 ``7 / 178`` 在 y≈846，杂框在 y≈1050 更靠下；旧逻辑取最靠下的
    page-like 框，会把页号解析成 1700007/52 并导致页号校验失败。
    """
    boxes = _layout_boxes()
    boxes.append(box("1700007/0052", 903, 1040, 999, 1060, v4="IUU/LUUUUL"))
    page = parse_boxes(boxes, 2400, 1080)
    assert page.ok and page.page == 7 and page.total_page == 178


def test_refined_page_box_is_fallback_when_v4_has_none():
    """v4 没读出页码时，v5 重读结果仍可兜底。"""
    boxes = [b for b in _layout_boxes() if b.text != "7 / 178"]
    boxes.append(box("7 / 178", 1281, 833, 1396, 860, v4="7|178"))
    page = parse_boxes(boxes, 2400, 1080)
    assert page.ok and page.page == 7 and page.total_page == 178
