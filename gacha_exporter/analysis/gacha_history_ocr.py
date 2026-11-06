"""契约记录页 OCR —— 纯解析，无 Qt 依赖，可离线批量跑。

输入一张游戏「契约记录」页截图，输出：页码/总页数、表头是否齐全、可见行
（名字、星级、「新」标、池名、时间）。识别只读取游戏面板内的文本：

- **UID 不读取、不输出**：只从表头列带与数据行取文本，左下角 UID 不在任何
  列带内，也不会进入返回值。
- **面板外干扰天然被排除**：列带由「风格名称 / 类型 / 时间」三个表头的实际
  OCR 位置建立，不写死屏幕坐标；左上角悬浮窗等面板外文本不落在列带内。
  若悬浮窗压进面板导致表头/行缺失，返回 ``ok=False``，由调用方提示重试，
  绝不静默错配。

实测要点（2026-10-21，2400×1080 8 页 40 行）：

- ``♛`` 花饰会被 OCR 丢掉（``（6♛）`` → ``（6）``），星级以括号内数字为准；
  颜色只作辅助（红=6 / 金=5），冲突时置 ``rarity_conflict``。
- 日期与时间之间的空格常被吞（``2026-10-0614:07:16``），按 14 位数字归一化。
- 页码左侧的翻页箭头可能并入文本框（``<2/178``），用 ``(\\d+)/(\\d+)`` 提取。
- 「新」标是名字右侧的**独立框**；绝不能用“名字包含『新』”判断
  （``示例角色043`` 是实测反例）。
- 行序：响应第 0 条 = 屏幕第一行，同秒重复行按响应原顺序展示（见设计稿
  §7.2/§13#3）。本模块只按屏幕 y 序返回行，配对由调用方完成。
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Optional, Sequence, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 表头/标题文本（OCR 可能漏字，用包含/模糊匹配）
_HEADER_STYLE = "风格名称"
_HEADER_TYPE = "类型"
_HEADER_TIME = "时间"
_TITLE = "契约记录"

_TIME_RE = re.compile(
    r"(\d{4})\D?(\d{2})\D?(\d{2})\D?(\d{2})\D?(\d{2})\D?(\d{2})"
)
_PAGE_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
# 星级角标：优先括号内数字；允许 ♛ 花饰被 OCR 丢掉/读成别的符号/右括号丢失。
# 2026-10-23 实机：v5 把 ♛ 读成数字（``（6♛）`` → ``（64）``），旧规则遇到
# 数字后还有数字就整段不匹配 → 名字尾巴留着「（64）」、星级跌回 4★，进而
# 让 UP 下拉/UP 推断全部认不出该角色。这里允许首数字后再跟至多 4 个非括号字符
# （含数字）；角色名本身从不含括号（见 server/data/role.json），不会误伤正名。
_STAR_RE = re.compile(r"[\s]*[（(]\s*([456])\s*[^)）]{0,4}[)）]?\s*$")

_OCR_LOCK = threading.Lock()
_OCR_SINGLETON = None

# 2026-10-22 优先级铁律（性能第一）：OCR 只许占 2 个 intra-op 核 + 1 个
# inter-op，把其余 CPU 留给截图/GUI/抓包主链路。不设时 onnxruntime 默认占满
# 全部物理核，实机出现过 OCR 期间定时器与截图被饿死、用户被迫翻回补截。
# 代价只有后台 OCR 略慢（约 2s→3-4s/页），结果只在停止时用。
OCR_INTRA_THREADS = 2
OCR_INTER_THREADS = 1

# 2026-10-22 C6 列定向混合（A 集 61/70 → 70/70，见 项目记录 §3.196）：
# 整页结构（表头/时间/页码）仍用 v4；名字列/池名列的框换 PP-OCRv5 mobile rec
# 单独重识别。v5 对紧框小字会丢中/尾字，必须扩边 + 放大后才稳定。
# 2026-10-24 44 失败页实测：左右 20 是「缄默之人/职业囚徒」首字找回的关键，
# 不能缩；上下从 20 缩到 10 可修掉白镰刀/红手套/伫立/盛装等 42 行误读且零回归。
NAME_REC_MODEL = "ch_PP-OCRv5_rec_mobile.onnx"
NAME_REC_PAD_X = 20
NAME_REC_PAD_Y = 10
NAME_REC_SCALE = 3
_NAME_OCR_LOCK = threading.Lock()
_NAME_OCR_SINGLETON = None
_NAME_OCR_UNAVAILABLE = False

# OCR 常把间隔号读成 • / ・ / ‧ 等异体；统一成名字里的「·」。
# 2026-10-23 全量评测：v5 另有 5 行把中点读成 ▪（U+25AA），一并对齐。
_SEP_TRANSLATION = str.maketrans(
    {
        "•": "·", "・": "·", "‧": "·", "∙": "·", "．": "·",
        "▪": "·", "▫": "·", "■": "·", "□": "·",
    }
)

# 游戏提示条「操作太频繁，请稍后再试」会浮在面板上，被 OCR 读进名字/池名列。
# 2026-10-23 全量评测：11 行名字被它污染（v4/v5 同时中招）。它在任何列都不是
# 有效内容，遇到就截断；OCR 偶有缺字，按「太频繁 / 稍后再试」两个锚点兜住。
_TOAST_RE = re.compile(r"(?:操作)?太频繁.*$|稍后再试.*$")


# ==================== 数据结构 ====================


@dataclass(frozen=True)
class OCRBox:
    """一个 OCR 文本框（原图像素坐标）。"""

    text: str
    score: float
    x1: int
    y1: int
    x2: int
    y2: int
    # C6 列定向重识别前的 v4 原文（只有被 v5 重识别过的框非空，用于 v4/v5
    # 分歧核对；不入 stats，只在确认窗「重点核对」与本机日志里出现）
    v4_text: str = ""

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0


@dataclass
class HistoryRow:
    """截图中的一行（字段为 OCR 原始值与解析值）。"""

    row: int
    ocr_name: str = ""
    name: str = ""
    rarity: int = 4
    rarity_conflict: bool = False
    new_tag: bool = False
    pool_name: str = ""
    time_text: str = ""
    time_norm: str = ""
    name_score: float = 0.0
    pool_score: float = 0.0
    time_score: float = 0.0
    # C6 的 v4 原文/星级（未重识别/未分歧时为空/0）；分歧行在确认窗标
    # 「重点核对」
    name_v4: str = ""
    pool_v4: str = ""
    rarity_v4: int = 0

    @property
    def name_diverged(self) -> bool:
        """v4 与 v5 对名字列给出的结果不同（仅比较角色名本身）。"""
        return bool(self.name_v4) and _name_norm(self.name_v4) != _name_norm(self.name)

    @property
    def pool_diverged(self) -> bool:
        """v4 与 v5 对池名列给出的结果不同。"""
        return bool(self.pool_v4) and _norm(self.pool_v4) != _norm(self.pool_name)

    @property
    def rarity_diverged(self) -> bool:
        """v4 与 v5 从名字框解析出的星级不同（角标可能被重识别改写）。"""
        return self.rarity_v4 > 0 and self.rarity_v4 != self.rarity

    def to_dict(self) -> dict:
        return {
            "row": self.row,
            "ocr_name": self.ocr_name,
            "name": self.name,
            "rarity": self.rarity,
            "rarity_conflict": self.rarity_conflict,
            "new_tag": self.new_tag,
            "pool_name": self.pool_name,
            "time_text": self.time_text,
            "time_norm": self.time_norm,
            "name_score": round(self.name_score, 3),
            "pool_score": round(self.pool_score, 3),
            "time_score": round(self.time_score, 3),
            "name_v4": self.name_v4,
            "pool_v4": self.pool_v4,
            "rarity_v4": self.rarity_v4,
        }


@dataclass
class HistoryPage:
    """一页契约记录的解析结果。"""

    width: int = 0
    height: int = 0
    page: int = 0
    total_page: int = 0
    rows: List[HistoryRow] = field(default_factory=list)
    headers_ok: bool = False
    title_found: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.headers_ok and bool(self.rows) and not self.error

    @property
    def complete(self) -> bool:
        """页面已加载满 5 行（末页可能不足，调用方结合 total 判断）。"""
        return self.ok and len(self.rows) == 5

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "complete": self.complete,
            "width": self.width,
            "height": self.height,
            "page": self.page,
            "total_page": self.total_page,
            "headers_ok": self.headers_ok,
            "title_found": self.title_found,
            "error": self.error,
            "rows": [r.to_dict() for r in self.rows],
        }


# ==================== 文本工具 ====================


def _norm(s: str) -> str:
    """去空白与不可见字符，用于表头/页码匹配。"""
    return re.sub(r"\s+", "", s or "")


def normalize_separators(text: str) -> str:
    """把 OCR 读出的中点异体（• ・ ‧ ∙ ．）统一为名字里的 ``·``。"""
    return (text or "").translate(_SEP_TRANSLATION)


def strip_ui_noise(text: str) -> str:
    """截掉浮层提示（「操作太频繁，请稍后再试」）等非列内容。"""
    return _TOAST_RE.sub("", str(text or "")).strip()


def _name_norm(s: str) -> str:
    """角色名归一化：只留中日文、字母、数字（保留 · 用于展示，匹配时去掉）。"""
    return re.sub(r"[^一-鿿A-Za-z0-9]", "", s or "")


def _text_like(text: str, target: str, threshold: float = 0.6) -> bool:
    """OCR 文本是否像目标文本（包含或模糊相似）。"""
    a, b = _norm(text), target
    if not a:
        return False
    if b in a or a in b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def normalize_time(text: str) -> str:
    """把时间 OCR 文本归一化为 ``YYYY-MM-DD HH:MM:SS``；无法解析返回空串。"""
    m = _TIME_RE.search(text or "")
    if not m:
        return ""
    try:
        dt = datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ==================== 版面锚定 ====================


@dataclass
class _Layout:
    width: int
    height: int
    name_band: Tuple[float, float]
    pool_band: Tuple[float, float]
    time_band: Tuple[float, float]
    header_y: float
    page_box: Optional[OCRBox]
    title_found: bool

    def in_name(self, b: OCRBox) -> bool:
        return self.name_band[0] <= b.cx <= self.name_band[1]

    def in_pool(self, b: OCRBox) -> bool:
        return self.pool_band[0] <= b.cx <= self.pool_band[1]

    def in_time(self, b: OCRBox) -> bool:
        return self.time_band[0] <= b.cx <= self.time_band[1]


def _find_header(boxes: Sequence[OCRBox], target: str) -> Optional[OCRBox]:
    """找最像目标表头的框：优先精确/包含，再按相似度与 y 取最上。"""
    best: Optional[Tuple[float, OCRBox]] = None
    for b in boxes:
        t = _norm(b.text)
        if not t:
            continue
        if t == target:
            rank = 2.0
        elif target in t or t in target:
            rank = 1.5
        else:
            ratio = SequenceMatcher(None, t, target).ratio()
            if ratio < 0.6:
                continue
            rank = ratio
        cand = (rank, b)
        # rank 高者优先；同分取 y 更小（表头在顶部）
        if best is None or cand[0] > best[0] + 1e-9 or (
            abs(cand[0] - best[0]) < 1e-9 and b.y1 < best[1].y1
        ):
            best = cand
    return best[1] if best else None


def locate_layout(boxes: Sequence[OCRBox], width: int, height: int) -> Optional[_Layout]:
    """由三列表头建立列带；找不到表头返回 None。

    列带以表头实际位置为锚（相对宽度），不写死 2400×1080 坐标。名字列右界
    取「类型」表头左侧，时间列以「时间」表头为中心；行文本再按 x 中心归带。
    """
    h_style = _find_header(boxes, _HEADER_STYLE)
    h_type = _find_header(boxes, _HEADER_TYPE)
    h_time = _find_header(boxes, _HEADER_TIME)
    if h_style is None or h_type is None or h_time is None:
        return None
    # 表头 y 应大致同行；差异过大说明匹配到了正文里的同名词，拒绝
    ys = [h_style.cy, h_type.cy, h_time.cy]
    if max(ys) - min(ys) > 0.04 * height:
        return None

    name_band = (h_style.x1 - 0.050 * width, h_type.x1 - 0.020 * width)
    pool_band = (h_type.x1 - 0.010 * width, h_type.x2 + 0.050 * width)
    time_band = (h_time.x1 - 0.060 * width, h_time.x2 + 0.060 * width)

    title_found = any(_text_like(b.text, _TITLE) for b in boxes)
    page_box = _find_page_box(boxes, width, height, h_time.cy)
    return _Layout(
        width=width,
        height=height,
        name_band=name_band,
        pool_band=pool_band,
        time_band=time_band,
        header_y=max(h_style.y2, h_type.y2, h_time.y2),
        page_box=page_box,
        title_found=title_found,
    )


def _find_page_box(
    boxes: Sequence[OCRBox], width: int, height: int, header_y: float
) -> Optional[OCRBox]:
    """找页码 ``x / y``：位于表头下方、页面底部中央区域，取最靠下的。"""
    candidates: List[OCRBox] = []
    for b in boxes:
        if b.y1 <= header_y:
            continue
        if not (0.30 * width <= b.cx <= 0.70 * width):
            continue
        if b.cy < 0.68 * height:
            continue
        if _PAGE_RE.search(_norm(b.text)):
            candidates.append(b)
    if not candidates:
        return None
    # C6 重识别只覆盖名字/池名列、且页码框被明确排除，所以 v4_text 非空
    # = v5 重读过的框。它们不得压过 v4 已识别到的页码：全屏截图底部杂框
    # （如 ``IUU/LUUUUL``）可能被 v5 读成 ``1700007/0052``，因更靠下而抢位。
    # 若 v4 没有任何页码候选，才允许用 v5 的结果兜底（例如 v4 把「40/194」
    # 读成没有斜杠的串、v5 重读后补回）。
    v4_candidates = [b for b in candidates if not b.v4_text]
    return max(v4_candidates or candidates, key=lambda b: (b.cy, b.score))


def extract_rows(
    boxes: Sequence[OCRBox], layout: _Layout, image=None
) -> List[HistoryRow]:
    """按时间列定位行，再取每行的名字/池名；不按时间重排。"""
    h, w = layout.height, layout.width
    # 时间框：表头下方、页面底部以上；一页最多 5 行
    times = [
        b for b in boxes
        if layout.in_time(b)
        and b.y1 > layout.header_y
        and b.cy < 0.95 * h
        and normalize_time(b.text)
    ]
    times.sort(key=lambda b: b.cy)
    if len(times) > 5:
        # 时间列出现多余文本（如悬浮窗/弹层）：无法确定哪 5 行，宁可失败
        logger.warning(f"契约记录时间列识别到 {len(times)} 个候选框，拒绝解析")
        return []

    rows: List[HistoryRow] = []
    row_tol = 0.032 * h
    for idx, tb in enumerate(times, start=1):
        row_boxes = [b for b in boxes if abs(b.cy - tb.cy) <= row_tol]
        name_boxes = sorted(
            [b for b in row_boxes if layout.in_name(b) and b is not tb],
            key=lambda b: b.x1,
        )
        pool_boxes = sorted(
            [b for b in row_boxes if layout.in_pool(b) and b is not tb],
            key=lambda b: b.x1,
        )
        # 「新」只认独立框；名字框里即便含「新」也不是新标
        new_tag = any(_norm(b.text) == "新" for b in row_boxes)
        name_boxes = [b for b in name_boxes if _norm(b.text) != "新"]
        pool_boxes = [b for b in pool_boxes if _norm(b.text) != "新"]

        ocr_name = normalize_separators(_norm("".join(b.text for b in name_boxes)))
        # 同框吞掉「新」标：仅当它紧跟星级右括号时才是新标（财富新贵不算）
        if re.search(r"[)）]\s*新$", ocr_name):
            ocr_name = re.sub(r"新$", "", ocr_name)
            new_tag = True
        name, rarity, conflict = parse_name_and_rarity(ocr_name)
        # C6 分歧核对：按同样规则还原 v4 文本；未被重识别（模型缺失/回退）
        # 时保持空串，不会产生假分歧。
        name_v4, rarity_v4 = "", 0
        if any(b.v4_text for b in name_boxes):
            raw_v4 = normalize_separators(
                _norm("".join(b.v4_text or b.text for b in name_boxes))
            )
            if re.search(r"[)）]\s*新$", raw_v4):
                raw_v4 = re.sub(r"新$", "", raw_v4)
            name_v4, rarity_v4, _ = parse_name_and_rarity(raw_v4)
        pool_v4 = ""
        if any(b.v4_text for b in pool_boxes):
            pool_v4 = normalize_separators(
                _norm("".join(b.v4_text or b.text for b in pool_boxes))
            )
        if image is not None and rarity in (5, 6) and name_boxes:
            nb = OCRBox(
                "", 0,
                min(b.x1 for b in name_boxes), min(b.y1 for b in name_boxes),
                max(b.x2 for b in name_boxes), max(b.y2 for b in name_boxes),
            )
            hint = _star_color_hint(image, nb)
            expected = 2 if rarity == 6 else 1
            if hint and hint != expected:
                conflict = True

        pool_name = normalize_separators(_norm("".join(b.text for b in pool_boxes)))
        rows.append(HistoryRow(
            row=idx,
            ocr_name=ocr_name,
            name=name,
            rarity=rarity,
            rarity_conflict=conflict,
            new_tag=new_tag,
            pool_name=pool_name,
            time_text=_norm(tb.text),
            time_norm=normalize_time(tb.text),
            name_score=_min_score(name_boxes),
            pool_score=_min_score(pool_boxes),
            time_score=tb.score,
            name_v4=name_v4,
            pool_v4=pool_v4,
            rarity_v4=rarity_v4,
        ))
    return rows


def _min_score(boxes: Sequence[OCRBox]) -> float:
    return min((b.score for b in boxes), default=0.0)


def parse_name_and_rarity(text: str) -> Tuple[str, int, bool]:
    """从名字文本解析 (干净名字, 星级, 是否有颜色/数字冲突)。

    规则（设计稿 §6.3）：尾部 ``（6）`` → 6★；``（5）`` → 5★；无角标 → 4★。
    OCR 丢 ``♛`` 是常态，数字是主证据。
    """
    raw = _norm(text)
    m = _STAR_RE.search(raw)
    if not m:
        return raw, 4, False
    rarity = int(m.group(1))
    name = raw[: m.start()].strip()
    return name, rarity, False


def _star_color_hint(image, box: OCRBox) -> int:
    """名字框尾部角标颜色提示：2=红(6★) / 1=金(5★) / 0=无法判断。

    只在高饱和高亮像素足够多时给结论；4★ 的深色正文不会命中（V 阈值）。
    """
    if image is None:
        return 0
    try:
        import cv2
    except Exception:  # noqa: BLE001 —— 颜色仅辅助，缺库不影响主流程
        return 0
    h, w = image.shape[:2]
    x1 = max(0, int(box.x1 + (box.x2 - box.x1) * 0.70))
    x2 = min(w, int(box.x2) + 4)
    y1, y2 = max(0, int(box.y1) - 2), min(h, int(box.y2) + 2)
    if x2 <= x1 or y2 <= y1:
        return 0
    hsv = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    hue = hsv[:, :, 0]
    bright = (sat > 90) & (val > 140)
    if not bright.any():
        return 0
    red = int((((hue < 10) | (hue > 170)) & bright).sum())
    gold = int((((hue >= 12) & (hue <= 40)) & bright).sum())
    if red < 8 and gold < 8:
        return 0
    return 2 if red >= gold else 1


# ==================== 主入口 ====================


def _make_boxes(res, scale: float) -> List[OCRBox]:
    """把 RapidOCR 原始输出转成原图坐标的 OCRBox。"""
    out: List[OCRBox] = []
    for item in res or []:
        try:
            box, text, score = item
        except (TypeError, ValueError):
            continue
        text = strip_ui_noise(str(text))
        if not text:
            continue
        xs = [float(p[0]) / scale for p in box]
        ys = [float(p[1]) / scale for p in box]
        out.append(OCRBox(
            text=text,
            score=float(score),
            x1=round(min(xs)), y1=round(min(ys)),
            x2=round(max(xs)), y2=round(max(ys)),
        ))
    return out


def get_ocr():
    """返回进程内共享的 RapidOCR 实例（首次调用才加载模型）。"""
    global _OCR_SINGLETON
    if _OCR_SINGLETON is not None:
        return _OCR_SINGLETON
    with _OCR_LOCK:
        if _OCR_SINGLETON is None:
            from rapidocr_onnxruntime import RapidOCR
            # Global 的线程数会被 UpdateParameters 同步到 Det/Cls/Rec 三段
            _OCR_SINGLETON = RapidOCR(
                intra_op_num_threads=OCR_INTRA_THREADS,
                inter_op_num_threads=OCR_INTER_THREADS,
            )
    return _OCR_SINGLETON


def ocr_boxes(image, ocr=None, upscale_small: bool = True) -> List[OCRBox]:
    """对整图 OCR，返回原图坐标文本框。小图自动放大以提升小字识别率。"""
    import cv2

    ocr = ocr or get_ocr()
    h, w = image.shape[:2]
    scale = 1.0
    if upscale_small and w < 1600:
        scale = 2.0
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    res, _ = ocr(image)
    return _make_boxes(res, scale)


def name_model_path() -> Optional[str]:
    """v5 名字识别模型路径（随 ``ocr`` runtime 下发到 rapidocr 包的 models/）。

    未下发时返回 None，识别自动沿用 v4，不报错、不阻断。
    """
    try:
        import rapidocr_onnxruntime
    except Exception:  # noqa: BLE001 —— 未装识别组件
        return None
    path = os.path.join(
        os.path.dirname(rapidocr_onnxruntime.__file__), "models", NAME_REC_MODEL
    )
    return path if os.path.isfile(path) else None


def get_name_ocr():
    """名字列专用 RapidOCR 实例（只替换 rec 模型）；不可用返回 None。"""
    global _NAME_OCR_SINGLETON, _NAME_OCR_UNAVAILABLE
    if _NAME_OCR_SINGLETON is not None or _NAME_OCR_UNAVAILABLE:
        return _NAME_OCR_SINGLETON
    with _NAME_OCR_LOCK:
        if _NAME_OCR_SINGLETON is None and not _NAME_OCR_UNAVAILABLE:
            path = name_model_path()
            if path is None:
                _NAME_OCR_UNAVAILABLE = True
                logger.info(f"未找到 {NAME_REC_MODEL}，名字列沿用 v4 识别")
                return None
            try:
                from rapidocr_onnxruntime import RapidOCR

                _NAME_OCR_SINGLETON = RapidOCR(
                    rec_model_path=path,
                    intra_op_num_threads=OCR_INTRA_THREADS,
                    inter_op_num_threads=OCR_INTER_THREADS,
                )
                _NAME_OCR_UNAVAILABLE = False
                logger.info(f"名字列识别模型已加载: {NAME_REC_MODEL}")
            except Exception as e:  # noqa: BLE001 —— 加载失败回退 v4
                _NAME_OCR_UNAVAILABLE = True
                logger.warning(f"v5 名字识别模型加载失败，回退 v4: {e}")
    return _NAME_OCR_SINGLETON


def _rec_crop(ocr, image, box: OCRBox) -> Tuple[str, float]:
    """裁剪框外扩 + 放大后只跑 rec；v5 对紧框小字会丢字，边距是关键。

    水平边距必须比垂直大：左右 20px 负责找回被 v4 检测框切掉的首字，
    上下 10px 足够容纳字高，多了反而把相邻 UI/背景读进名字里。
    """
    import cv2

    h, w = image.shape[:2]
    x1 = max(0, box.x1 - NAME_REC_PAD_X)
    y1 = max(0, box.y1 - NAME_REC_PAD_Y)
    x2 = min(w, box.x2 + NAME_REC_PAD_X)
    y2 = min(h, box.y2 + NAME_REC_PAD_Y)
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return box.text, box.score
    if NAME_REC_SCALE != 1:
        crop = cv2.resize(
            crop, None, fx=NAME_REC_SCALE, fy=NAME_REC_SCALE,
            interpolation=cv2.INTER_CUBIC,
        )
    res, _ = ocr(crop, use_det=False, use_cls=False, use_rec=True)
    if not res:
        return box.text, box.score
    return str(res[0][0]).strip(), float(res[0][1])


def refine_name_boxes(
    image, boxes: Sequence[OCRBox], width: int, height: int, ocr=None
) -> List[OCRBox]:
    """C6 列定向：名字列/池名列的框用 v5 rec 重识别，其余框原样保留。

    模型不可用、版面未锚定或单框重识别失败时，原样返回 v4 结果（绝不阻断）。
    """
    if image is None or not boxes:
        return list(boxes)
    try:
        layout = locate_layout(boxes, width, height)
    except Exception as e:  # noqa: BLE001 —— 重识别是增强项，失败不影响主流程
        logger.info(f"名字列重识别跳过（版面锚定失败）: {e}")
        return list(boxes)
    if layout is None:
        return list(boxes)
    ocr = ocr or get_name_ocr()
    if ocr is None:
        return list(boxes)
    out: List[OCRBox] = []
    for b in boxes:
        # 只重识别数据行里的名字/池名框：表头要留给 parse_boxes 重新锚定，
        # 「新」标是独立框、v5 扩边后容易被邻字带偏；页码框「7 / 178」也
        # 落在名字列的横向范围内，不能拿它当名字重识别。一律保留 v4 结果。
        if (
            not (layout.in_name(b) or layout.in_pool(b))
            or b.cy <= layout.header_y
            or b is layout.page_box
            or _norm(b.text) == "新"
            or not _norm(b.text)
        ):
            out.append(b)
            continue
        try:
            text, score = _rec_crop(ocr, image, b)
        except Exception as e:  # noqa: BLE001 —— 单框失败沿用 v4 文本
            logger.info(f"名字列单框重识别失败（沿用 v4）: {e}")
            out.append(b)
            continue
        text = normalize_separators(text) or b.text
        # v5 扩边后可能把浮层提示一起读进来；截掉后再决定是否回退 v4 原文
        text = strip_ui_noise(text) or b.text
        # v4 原文一并带上：解析成行后与 v5 比较，分歧行进日志并在确认窗标记
        out.append(replace(b, text=text, score=score, v4_text=b.text))
    return out


def _sanitize_box(b: OCRBox) -> OCRBox:
    """去掉框文本里的浮层提示；无变化时原样返回（避免无谓拷贝）。"""
    text = strip_ui_noise(b.text)
    v4 = strip_ui_noise(b.v4_text) if b.v4_text else ""
    if text == b.text and v4 == b.v4_text:
        return b
    return replace(b, text=text, v4_text=v4)


def parse_boxes(
    boxes: Sequence[OCRBox], width: int, height: int, image=None
) -> HistoryPage:
    """纯函数：由 OCR 框解析一页（便于单测，不需要模型）。"""
    page = HistoryPage(width=width, height=height)
    # 二道保险：调用方自建框（测试/旧路径）时也清掉浮层提示；`_make_boxes`
    # 已在生产链路清过一次。
    boxes = [b for b in (_sanitize_box(x) for x in boxes) if b.text]
    layout = locate_layout(boxes, width, height)
    if layout is None:
        page.error = "未找到「风格名称 / 类型 / 时间」表头，请确认已打开契约记录页"
        return page
    page.headers_ok = True
    page.title_found = layout.title_found
    if not layout.title_found:
        page.error = "未找到标题「契约记录」，请确认已打开契约记录页"
        return page
    if layout.page_box is not None:
        m = _PAGE_RE.search(_norm(layout.page_box.text))
        if m:
            page.page, page.total_page = int(m.group(1)), int(m.group(2))
    if not page.page or not page.total_page:
        page.error = "未识别到页码（x / y），无法与抓包页号校验"
        return page

    rows = extract_rows(boxes, layout, image=image)
    if not rows:
        page.error = "时间列未立行（契约记录页可能尚在翻页，暂不配对）"
        return page
    if len(rows) < 5:
        # 不补齐、不猜行：调用方据 complete=False 决定是否允许自动绑定
        logger.info(f"契约记录页仅识别到 {len(rows)} 行（末页或未加载完整）")
    page.rows = rows
    return page


def _log_divergences(page: HistoryPage) -> None:
    """把 v4/v5 名字列不一致的行写本机日志（零上行），供 soak 后重点核对。"""
    for r in page.rows:
        if r.name_diverged:
            logger.info(
                f"C6 分歧 第{page.page}页第{r.row}行 名字 "
                f"v4={r.name_v4} → v5={r.name}（score {r.name_score:.3f}）"
            )
        if r.pool_diverged:
            logger.info(
                f"C6 分歧 第{page.page}页第{r.row}行 池名 "
                f"v4={r.pool_v4} → v5={r.pool_name}（score {r.pool_score:.3f}）"
            )
        if r.rarity_diverged:
            logger.info(
                f"C6 分歧 第{page.page}页第{r.row}行 星级 "
                f"v4={r.rarity_v4}★ → v5={r.rarity}★"
            )


def recognize_image(image, ocr=None) -> HistoryPage:
    """识别一张 BGR 截图。任何 OCR 异常都转成 ``error``，不向上抛。"""
    try:
        h, w = image.shape[:2]
        boxes = ocr_boxes(image, ocr=ocr)
        # C6 列定向：名字/池名列换 v5 rec（未下发模型时原样返回 v4 结果）
        boxes = refine_name_boxes(image, boxes, w, h)
        page = parse_boxes(boxes, w, h, image=image)
        _log_divergences(page)
        return page
    except Exception as e:  # noqa: BLE001 —— OCR 失败由确认流程兜底
        logger.warning(f"契约记录页 OCR 失败: {e}")
        return HistoryPage(error=f"OCR 失败: {e}")


def read_image(path: str):
    """Windows 中文路径安全读图（cv2.imread 不支持非 ASCII 路径）。"""
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图片: {path}")
    return img


def decode_image(data: bytes):
    """由 PNG/JPG 字节解码为 BGR 图（截屏内存直达 OCR，不落临时文件）。"""
    import cv2
    import numpy as np

    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法解码截图字节")
    return img


def recognize_bytes(data: bytes, ocr=None) -> HistoryPage:
    """识别内存中的截图字节；解码失败同样转成 ``error``。"""
    try:
        return recognize_image(decode_image(data), ocr=ocr)
    except Exception as e:  # noqa: BLE001
        return HistoryPage(error=f"截图解码失败: {e}")


def recognize_file(path: str, ocr=None) -> HistoryPage:
    """离线识别一张截图文件（批量/测试入口）。"""
    return recognize_image(read_image(path), ocr=ocr)
