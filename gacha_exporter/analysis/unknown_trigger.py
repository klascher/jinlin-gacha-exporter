"""未知 id 识别触发与页内配对（卡池识别 P3）。

两条纯逻辑链路，均无 Qt / OCR 依赖，便于单测：

1. `UnknownRecognitionTrigger`：从「全部卡池」页响应中挑出未知 item_id /
   pool_id，按 1.5 秒防抖合并成页级任务；同一页只截一次，失败不无限重试。
2. `validate_page`：把抓包响应与契约记录页 OCR 结果对齐（防错配第三道闸）。
   页号/总页数/行数硬校验；行序默认正向（已 40/40 实测），再用已知角色名做
   模糊校验，正向/反向都不达标则拒绝自动绑定，交人工。

设计依据：visual_aid/卡池识别/设计稿.md §4、§5、§7。
底线：宁可拒识，不可错绑——屏幕与抓包对不上时一律交人工。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..models import Character, GachaRecord
from ..utils.logger import get_logger
from .gacha_history_ocr import HistoryPage, HistoryRow
from .pool_view import PAGE_SIZE, is_all_pools

logger = get_logger(__name__)

# 防抖窗口：只用于合并同一页的重复/重发响应（通常几十毫秒内到达）。
# 2026-10-21 实机两次踩坑：
#   ① 1.5s 时用户约 1 秒/页翻页，必然在翻走后才截屏（截图已是下一页）；
#   ② 用户抓包时工具在后台，中途提示根本看不到——必须让触发尽量不失败。
# 故压到 0.15s（配合 50ms 轮询，响应后约 0.15~0.2s 截屏），并额外截一张
# 兜底图防止游戏 UI 渲染慢；页号闸仍保留，失败只在停止汇总里提示。
DEFAULT_DEBOUNCE_SECONDS = 0.15
# 已知角色名与 OCR 名字的最低相似度（归一化后 SequenceMatcher）。
NAME_MATCH_THRESHOLD = 0.6
# 已知行匹配比例低于该值即认为行序不可信。
ROW_ORDER_MIN_RATIO = 0.8
# 会话内排队页数与单页未知目上限：防御异常流量，正常十连/翻页远达不到。
MAX_QUEUED_PAGES = 37
MAX_UNKNOWN_PER_PAGE = 13


@dataclass(frozen=True)
class RecognitionTask:
    """一页待识别的任务：一个响应页 + 该页所有记录（保持响应顺序）。"""

    view: str
    offset: int
    total: int
    records: Tuple[GachaRecord, ...]
    unknown_item_ids: Tuple[str, ...]
    unknown_pool_ids: Tuple[str, ...]
    created_at: float

    @property
    def page(self) -> int:
        """抓包页号（1 = 最新页）。"""
        return self.offset // PAGE_SIZE + 1

    @property
    def total_page(self) -> int:
        """服务器权威总页数。"""
        return max(1, math.ceil(self.total / PAGE_SIZE))

    @property
    def is_last_page(self) -> bool:
        """是否最后一页（末页允许不足 5 行）。"""
        return self.page >= self.total_page

    @property
    def key(self) -> Tuple[str, int, int]:
        """页级去重键。"""
        return (self.view, self.offset, self.total)


@dataclass(frozen=True)
class RowBinding:
    """截图行 ↔ 响应记录（均 0-based，行 0 = 屏幕第一行）。"""

    row_index: int
    record_index: int
    record: GachaRecord
    row: HistoryRow
    item_unknown: bool
    pool_unknown: bool


@dataclass
class PageValidation:
    """页级校验结果。ok=False 时 reason 面向用户展示。"""

    ok: bool
    reason: str = ""
    order: str = "none"  # forward / reverse / none
    ratio: float = 0.0
    bindings: List[RowBinding] = field(default_factory=list)
    # 未知池 id -> 该页看到的池名（去重、保序）；多对多时交人工
    unknown_pool_names: Dict[str, List[str]] = field(default_factory=dict)
    page: Optional[HistoryPage] = None
    task: Optional[RecognitionTask] = None

    @property
    def unknown_item_bindings(self) -> List[RowBinding]:
        """只含未知角色的绑定行（生成建议用）。"""
        return [b for b in self.bindings if b.item_unknown]


class UnknownRecognitionTrigger:
    """未知 id 触发器的页级防抖队列。

    调用方在每帧响应后 `feed(...)`，用 QTimer 周期性 `poll()`；到点返回
    最新一页的任务。窗口内更早的页视为「已被用户翻走」，但**不再**静默记
    attempted（2026-10-22 优先级铁律：未截过图的页只能列入未完成、允许翻回
    重触发，见 `take_abandoned()`）。
    """

    def __init__(self, debounce: float = DEFAULT_DEBOUNCE_SECONDS) -> None:
        self._debounce = max(0.0, debounce)
        self._groups: Dict[Tuple[str, int, int], RecognitionTask] = {}
        self._last_feed = 0.0
        self._attempted_items: set = set()
        self._attempted_pools: set = set()
        self._done_items: set = set()
        self._done_pools: set = set()
        # 防抖窗口内被翻走、从未交给截屏的页（2026-10-22 优先级铁律）：不能像
        # 以前那样标 attempted——标了用户翻回去也不会再触发，等于静默丢页。
        self._abandoned: List[RecognitionTask] = []

    # ---- 喂入与产出 ----

    def feed(
        self,
        records: Sequence[GachaRecord],
        view: Optional[str],
        offset: Optional[int],
        total: Optional[int],
        known_item_ids: Iterable[str],
        known_pool_ids: Iterable[str],
        now: Optional[float] = None,
    ) -> bool:
        """登记一页响应中的未知 id。

        Args:
            records: 本页解析出的记录（响应顺序，0 = 最新一条）
            view: `PoolViewTracker` 视图标识；非「全部卡池」直接忽略
            offset/total: 响应回显的页位置与权威总数；缺失则不自动识别
            known_item_ids: 当前生效角色 id（含 role + overlay）
            known_pool_ids: 当前生效卡池 id（含 role + overlay）
            now: 测试注入时钟

        Returns:
            True 表示该页存在可触发的新未知 id，已进入防抖队列。
        """
        if view is None or not is_all_pools(view):
            return False
        if offset is None or total is None or total <= 0 or offset < 0:
            return False
        if not records:
            return False

        known_items = set(known_item_ids)
        known_pools = set(known_pool_ids)
        unknown_items: List[str] = []
        unknown_pools: List[str] = []
        for rec in records:
            if (
                rec.item_id
                and rec.item_id not in known_items
                and rec.item_id not in self._attempted_items
                and rec.item_id not in self._done_items
                and rec.item_id not in unknown_items
            ):
                unknown_items.append(rec.item_id)
            if (
                rec.pool_id
                and rec.pool_id not in known_pools
                and rec.pool_id not in self._attempted_pools
                and rec.pool_id not in self._done_pools
                and rec.pool_id not in unknown_pools
            ):
                unknown_pools.append(rec.pool_id)
        if not unknown_items and not unknown_pools:
            return False
        if len(unknown_items) + len(unknown_pools) > MAX_UNKNOWN_PER_PAGE:
            # 单页未知目过多更像流量/解析异常，宁可不截屏也不乱绑
            logger.warning(
                f"单页未知目 {len(unknown_items) + len(unknown_pools)} 条，"
                f"超出锚点容量 {MAX_UNKNOWN_PER_PAGE}，本页转人工配对"
            )
            return False

        ts = time.monotonic() if now is None else now
        key = (view, int(offset), int(total))
        if key not in self._groups and len(self._groups) >= MAX_QUEUED_PAGES:
            logger.warning(f"待识别页已达 {MAX_QUEUED_PAGES} 页上限，忽略后续页 {key}")
            return False
        task = RecognitionTask(
            view=view,
            offset=int(offset),
            total=int(total),
            records=tuple(records),
            unknown_item_ids=tuple(unknown_items),
            unknown_pool_ids=tuple(unknown_pools),
            created_at=ts,
        )
        # 同页重复到达（翻页重抓）：直接覆盖为最新快照；否则新增一组
        self._groups[key] = task
        self._last_feed = ts
        return True

    def poll(self, now: Optional[float] = None) -> Optional[RecognitionTask]:
        """防抖窗口结束后返回最新一页的任务；未到点/无任务返回 None。

        更早的页会被判为「已翻走」：但**不**记 attempted（2026-10-22 优先级
        铁律——它们从未截过图，标 attempted 会让用户翻回去也不再触发，等于
        静默丢页）。它们经 `take_abandoned()` 交给调用方列入「未完成」，翻回
        该页收到新响应时会自然重新触发。
        """
        if not self._groups:
            return None
        ts = time.monotonic() if now is None else now
        if ts - self._last_feed < self._debounce:
            return None
        latest = max(self._groups.values(), key=lambda t: t.created_at)
        for key, task in list(self._groups.items()):
            if key == latest.key:
                continue
            self._abandoned.append(task)
        self._groups.clear()
        self._attempted_items.update(latest.unknown_item_ids)
        self._attempted_pools.update(latest.unknown_pool_ids)
        return latest

    def take_abandoned(self) -> List[RecognitionTask]:
        """取走「防抖窗口内被翻走、未截屏」的页（取走即清空）。

        调用方应把它们列入停止汇总的「未完成」；其未知 id 未标 attempted，
        用户翻回该页时 `feed()` 会重新触发。
        """
        dropped = self._abandoned
        self._abandoned = []
        return dropped

    def finish(self, task: RecognitionTask, status: str = "recognized") -> None:
        """标记任务结束。recognized/skipped 的 id 本会话不再触发。"""
        if status in ("recognized", "skipped"):
            self._done_items.update(task.unknown_item_ids)
            self._done_pools.update(task.unknown_pool_ids)

    def retry(self, task: RecognitionTask) -> None:
        """把一次失败任务的 id 重新放行（用户翻回该页即可再次触发）。

        失败任务在 poll 时已记入 attempted；若不放行，状态栏提示「请回到
        第 N 页」后用户真的翻回去也不会触发（2026-10-21 实机踩到）。
        """
        self.allow_retry(task.unknown_item_ids, task.unknown_pool_ids)

    def latest_pending(self) -> Optional[RecognitionTask]:
        """防抖队列里最新的一页（供前台提示「哪一页有未知 id」用）。"""
        if not self._groups:
            return None
        return max(self._groups.values(), key=lambda t: t.created_at)

    def allow_retry(self, item_ids: Iterable[str] = (), pool_ids: Iterable[str] = ()) -> None:
        """手动「重新识别」：清除失败/已处理标记。"""
        for i in item_ids:
            self._attempted_items.discard(i)
            self._done_items.discard(i)
        for p in pool_ids:
            self._attempted_pools.discard(p)
            self._done_pools.discard(p)

    def cancel_pending(self) -> List[RecognitionTask]:
        """停止抓包：清空未到点的任务并返回（停止汇总「未完成」）。"""
        pending = list(self._groups.values())
        for task in pending:
            self._attempted_items.update(task.unknown_item_ids)
            self._attempted_pools.update(task.unknown_pool_ids)
        self._groups.clear()
        return pending

    def reset_session(self) -> None:
        """新一次抓包：清空全部状态（已写入 overlay 的 id 会从 known 里消失）。"""
        self._groups.clear()
        self._abandoned.clear()
        self._last_feed = 0.0
        self._attempted_items.clear()
        self._attempted_pools.clear()
        self._done_items.clear()
        self._done_pools.clear()

    @property
    def pending_count(self) -> int:
        """防抖队列中的页数。"""
        return len(self._groups)


# ---- 页内配对 ----


def normalize_name(text: str) -> str:
    """归一化角色名：只留中英文数字，消除间隔号/空格/OCR 标点差异。"""
    return "".join(ch for ch in (text or "") if ch.isalnum())


def name_similarity(ocr_name: str, real_name: str) -> float:
    """OCR 名字与真实名字的相似度（0~1）。"""
    a, b = normalize_name(ocr_name), normalize_name(real_name)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _order_ratio(
    records: Sequence[GachaRecord],
    rows: Sequence[HistoryRow],
    characters: Mapping[str, Character],
    reverse: bool,
) -> Tuple[float, int]:
    """按给定行序，统计已知角色行的名字匹配比例。

    Returns:
        (匹配比例, 已知行数)；无已知行时比例为 0。
    """
    known = 0
    matched = 0
    n = min(len(records), len(rows))
    for i in range(n):
        rec = records[i]
        char = characters.get(rec.item_id)
        if char is None or not char.name or char.name.startswith("未知"):
            continue
        row = rows[n - 1 - i] if reverse else rows[i]
        known += 1
        if name_similarity(row.name, char.name) >= NAME_MATCH_THRESHOLD:
            matched += 1
    return (matched / known if known else 0.0), known


def validate_page(
    task: RecognitionTask,
    page: HistoryPage,
    characters: Mapping[str, Character],
    known_pool_ids: Iterable[str] = (),
) -> PageValidation:
    """把抓包任务与契约记录页 OCR 结果对齐（页号 → 行数 → 行序三道闸）。

    Args:
        task: 触发时保存的响应页快照
        page: `gacha_history_ocr` 的识别结果
        characters: 当前生效角色映射（用于已知行模糊校验）
        known_pool_ids: 当前生效卡池 id（只用于区分未知池名）

    Returns:
        PageValidation；任何不确定都 ok=False，且不生成自动绑定。
    """
    result = PageValidation(ok=False, page=page, task=task)
    if not page.ok:
        result.reason = page.error or "页面识别失败"
        return result
    if page.page != task.page or page.total_page != task.total_page:
        result.reason = (
            f"请回到第 {task.page} 页（当前第 {page.page} 页，总页数"
            f" {page.total_page}/{task.total_page}）"
        )
        return result
    if len(page.rows) != len(task.records):
        result.reason = f"行数不一致：截图 {len(page.rows)} 行，抓包 {len(task.records)} 条"
        return result
    if not task.is_last_page and not page.complete:
        result.reason = "页面未加载完整（不足 5 行），请等待加载后重试"
        return result
    if not task.records:
        result.reason = "抓包记录为空"
        return result

    fwd_ratio, known_rows = _order_ratio(task.records, page.rows, characters, reverse=False)
    if known_rows == 0:
        result.reason = "本页没有可用于校验的已知角色行，需人工配对"
        return result
    if fwd_ratio >= ROW_ORDER_MIN_RATIO:
        order, ratio = "forward", fwd_ratio
    else:
        rev_ratio, _ = _order_ratio(task.records, page.rows, characters, reverse=True)
        if rev_ratio >= ROW_ORDER_MIN_RATIO:
            order, ratio = "reverse", rev_ratio
        else:
            result.reason = "已知角色行与截图名字匹配不足，需人工配对"
            return result

    known_pools = set(known_pool_ids)
    n = len(task.records)
    unknown_pool_names: Dict[str, List[str]] = {}
    for i, rec in enumerate(task.records):
        row = page.rows[n - 1 - i] if order == "reverse" else page.rows[i]
        item_unknown = rec.item_id not in characters
        pool_unknown = bool(rec.pool_id) and rec.pool_id not in known_pools
        result.bindings.append(
            RowBinding(
                row_index=n - 1 - i if order == "reverse" else i,
                record_index=i,
                record=rec,
                row=row,
                item_unknown=item_unknown,
                pool_unknown=pool_unknown,
            )
        )
        if pool_unknown and row.pool_name:
            names = unknown_pool_names.setdefault(rec.pool_id, [])
            if row.pool_name not in names:
                names.append(row.pool_name)

    # bindings 按行序返回，便于确认窗左侧 id、右侧行一一展示
    result.bindings.sort(key=lambda b: b.row_index)
    result.ok = True
    result.order = order
    result.ratio = ratio
    result.unknown_pool_names = unknown_pool_names
    return result
