"""统计引擎 - 去重、映射、稀有度/卡池分布、共享保底追踪。

保底按「分组」聚合：同组卡池（如所有 UP 池、所有遴选池）共享保底计数与小/大保底
状态，跨池按时间戳旧->新合并计数；独立池（新手/常驻）各自一套、不跑状态机。
分组配置来自 role.json 的 pity_groups 段（pool.pity_group 字段指向组 key）。
"""
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Dict, List, Optional, Set, Tuple

from ..loaders.pool_groups import resolve_groups
from ..models import Character, GachaRecord, PityGroup, Pool
from ..storage.pool_patches import MANUAL_FILL_ID
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 合法事件大小：1 = 单抽、10 = 十连（规则 A1；与 analysis.pos_align 保持一致）
VALID_EVENT_SIZES = (1, 10)


def _is_manual_fill(rec: GachaRecord) -> bool:
    """是否为手动补录的垫抽填充记录（非真实已知抽取，不污染分布展示）。

    仅影响分布类展示方法（rarity/pool/unknown），不参与保底计算语义。
    """
    return rec.item_id == MANUAL_FILL_ID


def _group_by_ts(records) -> Dict[int, List[GachaRecord]]:
    """按 timestamp 分组（一个 ts = 一次抽卡动作 = 一个事件）。"""
    groups: Dict[int, List[GachaRecord]] = defaultdict(list)
    for rec in records:
        groups[rec.timestamp].append(rec)
    return dict(groups)


def _event_size_anomalies(records) -> List[Tuple[int, str, int]]:
    """事件大小 ∉ {1,10} 的事件 `[(ts, pool_id, size), ...]`（按 ts 升序）。"""
    out: List[Tuple[int, str, int]] = []
    groups = _group_by_ts(records)
    for ts in sorted(groups):
        group = groups[ts]
        if len(group) not in VALID_EVENT_SIZES:
            out.append((ts, group[0].pool_id, len(group)))
    return out


def _position_anomalies(records) -> List[Tuple[int, str, int]]:
    """**位置不连续**（缺格 / 坐标系统不一致）的事件 `[(ts, pool_id, size), ...]`。

    位置连续才说明事件「已定序」；保底消费序按位置排列，位置有洞或断裂时
    6★ 在十连内的落点会错。比 `_event_size_anomalies` 更严：能抓出「长度恰好
    10 但中间缺一格、另有一格重复」这类大小合法却未定序的事件
    （2026-09-11/12 实测三个老十连即「缺中格」形态）。
    """
    out: List[Tuple[int, str, int]] = []
    groups = _group_by_ts(records)
    for ts in sorted(groups):
        group = groups[ts]
        if len(group) <= 1:
            continue
        positions = sorted(r.position_in_batch for r in group)
        if positions != list(range(positions[0], positions[0] + len(positions))):
            out.append((ts, group[0].pool_id, len(group)))
    return out


class StatsEngine:
    """抽卡统计引擎。"""

    def __init__(
        self,
        characters: Dict[str, Character],
        pools: Dict[str, Pool],
        pity_groups: Dict[str, PityGroup] = None,
        shielded: Set[str] = None,
    ):
        """初始化。

        Args:
            characters: 角色映射
            pools: 卡池映射（每个 Pool.pity_group 指向所属保底分组 key）
            pity_groups: 保底分组配置 {group_id: PityGroup}；缺省空 dict ->
                所有池兜底 solo（pool_groups.resolve_groups 处理）
            shielded: 用户屏蔽的陌生 item_id 集合；这些 ID 在保底计算中按非 6 星
                处理（陌生 ID 默认按 6 星，屏蔽则降为 4/5 星）。仅对未收录 ID 生效，
                元数据收录该 ID 后真实星级自动覆盖、屏蔽条目自动失效。
        """
        self.characters = characters
        self.pools = pools
        self.pity_groups: Dict[str, PityGroup] = pity_groups or {}
        self.shielded: Set[str] = shielded if shielded is not None else set()
        # 注意：用 is not None 而非 `or`，保证传入的 set 对象被原样持有，
        # 这样 GUI 侧 mutate 同一对象（add/discard）后 engine 立即可见。
        self.records: List[GachaRecord] = []
        self._seen_batches: Set[tuple] = set()  # 已见过的批次指纹（同会话整页重发）
        # **位置对齐去重**（见 docs/去重方案-位置对齐.md）：单次抓包**全收**，
        # 不做事件内去重（依据 B3 页互斥 + C5 页缓存），整页指纹仅作廉价兜底；
        # 跨会话的重复由 storage.history.merge 按 (timestamp, pos − Δ) 判定。
        # 这里维护**本次会话新增**的记录，供收尾时交给 history 做位置合并
        # （不能传 self.records：它含从磁盘加载的历史记录，位置本就在基准
        # 坐标系，会被 Δ 归一化误伤）。
        self._session_records: List[GachaRecord] = []
        # **实时位置判重**（2026-09-13）：会话 Δ 由 GUI 在首个响应后经
        # set_session_shift() 设置；设置后 add() 对每条记录按
        # `(ts, pos − Δ)` 查 `_known_positions`，命中当场不收——抓包过程中
        # 界面计数即为准（旧实现判重推迟到收尾，过程中虚高 33 条的教训）。
        # 未设置（首个响应未到 / 异常路径）→ 全收，收尾 merge 兜底。
        self._session_shift: int = 0
        self._shift_baseline_total: Optional[int] = None
        self._total_seen: Optional[int] = None
        self._known_positions: Set[Tuple[int, int]] = set()
        # **事件级判重索引**（旧格式历史兼容，见 docs/去重方案-旧历史兼容-事件级判重.md）：
        # 历史（会话起点快照）中 size ∈ {1,10} 的完整事件，重抓时按 ts 整体实时跳过
        # ——完整事件的跨会话判重只看 ts，历史 pos 是页内坐标还是绝对坐标都无关；
        # 部分事件（size ∉ {1,10}）的记录照常收下，由收尾 merge 做替换/合并。
        self._known_complete_ts: Set[int] = set()
        self._known_partial_ts: Set[int] = set()
        # 会话开始时已存在的事件时间戳集合：仅用于会话信号（见 _overlapped_history）。
        # 某记录的 ts 在此集合里 => 它是本次会话开始前就有的历史事件。会话内新建的
        # 事件不写入本集合，避免「同会话续页」被误判成旧记录。
        self._session_start_events: Set[int] = set()
        # 会话级信号（供 stop_capture 判断“是否已翻到历史边界”）：
        #   _session_new_count: 本次会话经去重后实际新增的条数（>0 表示抓到了新记录）
        #   _overlapped_history: 本次会话是否抓到过历史里已有事件的记录
        #       （True 表示已翻到与历史的重叠边界）
        self._session_new_count: int = 0
        self._overlapped_history: bool = False

    @property
    def total(self) -> int:
        """总记录数。"""
        return len(self.records)

    def add(self, records: List[GachaRecord], view: Optional[str] = None,
            direction: int = 0) -> int:
        """收下本批记录；**实时判重两层：事件级（ts）+ 位置级（pos − Δ）**，返回收下的条数。

        判定分层（位置对齐方案，见 `docs/去重方案-位置对齐.md`；
        事件级判重见 `docs/去重方案-旧历史兼容-事件级判重.md`）：

        - **事件级**：记录的 ts 命中历史**完整事件**（size ∈ {1,10}）→ 当场跳过。
          完整事件的跨会话判重只看 ts——历史 pos 是旧·页内坐标还是新·绝对坐标
          都无关，同一次抽卡不会因坐标系不同变成两次抽卡。这保证旧格式老用户
          重抓时**抓包过程中**计数就是准的。
        - **位置级**（`_session_shift` 已设置）：其余记录逐条算 `norm = pos − Δ`，
          `(ts, norm)` 命中历史位置 → 当场不收（2026-09-13 实测教训：判重若
          推迟到收尾，过程中总数虚高 = 与历史重叠的条数）。
        - **部分历史事件**（size ∉ {1,10}）：记录**照常收下**（不做位置判重），
          送到收尾 merge 做整体替换/合并——不收下就到不了收尾，替换无从谈起。
        - **收尾层**（`storage.history.merge`）：幂等复核 + 替换/落库。两层判据
          一致，实时跳过的收尾必然也跳过。

        会话内**不做事件内容去重**：十连内同名角色（A4 允许）位置不同，
        不会被误当成复抓吞掉。整页指纹只拦「网络重传 / 同帧重复解析」。

        Args:
            records: 新解析出的记录
            view: 本批所属视图身份。**不再参与判定**，保留形参以兼容调用点
                与既有测试（B8 已保证只有「全部卡池」入账）。
            direction: 翻页方向。同上，保留形参但不再参与判定。

        Returns:
            本次收下的记录数
        """
        if not records:
            return 0

        # 批次级去重：整批记录的有序序列指纹（只拦完全相同的重传）
        fingerprint = GachaRecord.batch_fingerprint(records)
        if fingerprint in self._seen_batches:
            logger.info(f"批次重复，跳过 {len(records)} 条 (累计 {self.total})")
            return 0
        self._seen_batches.add(fingerprint)

        accepted, skipped_rt, skipped_ev = [], 0, 0
        for rec in records:
            if (rec.timestamp in self._known_complete_ts
                    and self._shift_baseline_total is not None):
                # 事件级判重：历史完整事件 + Δ 已知 → ts 即身份，整条跳过。
                # 无论归一化位置是否命中（命中=位置判重也会跳；不命中=坐标系
                # 不可比，同一次抽卡不会因坐标系不同变成两次），都跳过。
                # Δ 未知时不跳（宁重复勿漏，S9 跨会话碎片场景依赖全收）。
                skipped_ev += 1
                continue
            if rec.timestamp in self._known_partial_ts:
                # 部分历史事件：不做位置判重，全收（收尾 merge 替换/合并）。
                # Δ 已知时同样归一化，保证落库坐标与其余新记录一致。
                self._overlapped_history = True
                if self._shift_baseline_total is not None:
                    accepted.append(
                        replace(rec, position_in_batch=rec.position_in_batch - self._session_shift)
                    )
                else:
                    accepted.append(rec)
                continue
            if self._shift_baseline_total is None:
                # Δ 未知（首个响应未到 / 异常路径）→ 无法归一化，保守全收
                accepted.append(rec)
                continue
            norm = rec.position_in_batch - self._session_shift
            key = (rec.timestamp, norm)
            if key in self._known_positions:
                skipped_rt += 1
                continue
            self._known_positions.add(key)
            # **收下即归一化**（`GachaRecord` 是 frozen dataclass → replace）：
            # 落库数据恒在同一坐标系，保底消费序/位置连续性判定才成立；
            # 收尾 merge 因此无需再归一化（shift=0 幂等复核）
            accepted.append(
                replace(rec, position_in_batch=norm)
            )

        self.records.extend(accepted)
        self._session_records.extend(accepted)
        self._session_new_count += len(accepted)
        # 命中会话开始前就有的历史事件 / 实时命中历史位置 -> 已翻到历史边界
        if (skipped_rt or skipped_ev
                or any(r.timestamp in self._session_start_events for r in records)):
            self._overlapped_history = True
        if skipped_ev or skipped_rt:
            logger.info(
                f"本批 {len(records)} 条: 事件级跳过 {skipped_ev} 条, "
                f"位置判重跳过 {skipped_rt}，收下 {len(accepted)}, 累计 {self.total}"
            )
        else:
            logger.info(f"本批 {len(records)} 条: 全收, 累计 {self.total}")
        return len(accepted)

    def begin_session_shift(self, baseline_total: int) -> None:
        """会话开始：记录落库基准 total，Δ 从 0 起算，并重建历史位置索引。

        由 GUI 在会话内**首个响应**到达后调用（此时权威 total 已知）。

        Args:
            baseline_total: 上次落库时的权威 total（`history.last_total`）；
                历史为空/被清空时由调用方传本次 total（Δ 从 0 起算）
        """
        self._shift_baseline_total = baseline_total
        self._total_seen = baseline_total
        self._session_shift = 0
        self._known_positions = {
            (r.timestamp, r.position_in_batch) for r in self.records
        }
        self._rebuild_event_index()

    def notify_total(self, total: int) -> None:
        """每次响应后上报权威 total；**会话中途抽卡 → Δ 动态跟随**。

        Δ 的正确定义是「当前 total − 落库基准 total」——它随会话中途的抽卡
        **动态增长**。v1 实现把它锁死在会话开始时的值，用户中途抽 1 抽后所有
        旧记录位置 +1 而 Δ 仍为 0 → 旧记录全部对不上位置、被当成新记录重复
        计入（2026-09-13 13:17 实测多算 10 条）。本方法就是修这个：
        total 每涨 k，Δ 立即 +k，已收记录不受影响（它们收下时已按当时的 Δ
        归一化，而归一化坐标是恒定的基准坐标）。
        """
        if self._shift_baseline_total is None or self._total_seen is None:
            return
        if total > self._total_seen:
            delta = total - self._total_seen
            self._total_seen = total
            self._session_shift += delta
            logger.warning(
                f"会话内权威 total 增加 {delta}（疑似抓包中抽卡）→ Δ 动态调整为 "
                f"{self._session_shift}；已收记录不受影响（收下时已按当时 Δ 归一化）"
            )

    def _refresh_known_positions(self) -> None:
        """按当前 records 重建历史位置索引 `(ts, pos)`（pos 已是基准坐标系）。"""
        self._known_positions = {
            (r.timestamp, r.position_in_batch) for r in self.records
        }

    def _rebuild_event_index(self) -> None:
        """按当前 records 重建事件完整性索引（事件级判重用，见兼容设计稿 §3.2）。

        完整事件（size ∈ {1,10}）→ `_known_complete_ts`（实时整条跳过）；
        部分事件（size ∉ {1,10}）→ `_known_partial_ts`（实时照常收下，
        收尾 merge 做整体替换）。索引是**会话起点快照**：会话内新收的记录
        不更新本索引（同会话重复由批指纹 + 位置判重兜住）。
        """
        sizes: Dict[int, int] = {}
        for r in self.records:
            sizes[r.timestamp] = sizes.get(r.timestamp, 0) + 1
        self._known_complete_ts = {
            ts for ts, n in sizes.items() if n in VALID_EVENT_SIZES
        }
        self._known_partial_ts = set(sizes) - self._known_complete_ts

    @property
    def session_records(self) -> List[GachaRecord]:
        """本次会话新增的记录（收尾时交给 `history.merge()` 做位置合并）。

        **必须用它而不是 `records`**：`records` 含从磁盘加载的历史记录，其位置
        已在基准坐标系，若一并按 Δ 归一化会把它们整体挪错。
        """
        return list(self._session_records)

    def finalize(self) -> int:
        """收尾钩子——位置对齐方案下是**空操作**（保留以兼容调用链）。

        旧的「挂起观察 + 收尾联立求解」是为「内容无法区分同名」打的补丁；
        位置对齐后单次抓包全收、跨会话按位置判重，不再需要收尾裁决。
        保留本方法（调用点：4 条落库路径）以便回滚与减少接线改动。
        """
        return 0

    def suspicious_events(self) -> List[Tuple[int, str, int]]:
        """违反「事件大小 ∈ {1,10}」不变量的事件 `[(ts, pool_id, size), ...]`。

        在会话收尾/合并时读取：<10 表示漏抓（如十连只抓到 5 条）、非 {1,10} 的
        异形表示多算。**不要在每页 add 后立即调用**——十连首页天然只有 5 条，
        尚未收齐会误报。
        """
        return _event_size_anomalies(self.records)

    def unresolved_events(self) -> List[Tuple[int, str, int]]:
        """**未定序**事件 `[(ts, pool_id, size), ...]`：事件内位置不连续。

        位置连续 + 大小合法才叫「已定序」。本判据能抓出「长度恰好 10 但中间缺一格、
        另有一格重复」这类大小合法却未定序的事件（2026-09-11/12 实测三个老十连即
        「缺中格」形态）——保底消费序按位置排，拼反即保底整段错位。
        """
        return _position_anomalies(self.records)

    # ---- 旧账本「槽位内容冲突」诊断（2026-09-14 注释保留）----
    # 内容模式时代探「坐标系判错」用的槽位冲突检查。位置对齐方案下位置即身份，
    # 同一 (ts, pos) 只可能保留一条记录，冲突检查恒空、已被 `unresolved_events()`
    # （位置断裂）覆盖。产线已无任何消费者，注释保留以便回滚。
    # @property
    # def slot_conflicts(self) -> List[Tuple[int, Optional[str], int, str, str]]:
    #     """同位置内容冲突——位置对齐方案下**恒为空**（保留以兼容调用点）。
    #
    #     位置即身份：同一 `(ts, pos)` 只可能保留一条记录（跨会话重复在合并时按
    #     位置跳过），因此不再存在「同位置不同内容」的情形。旧的槽位冲突检查是
    #     内容模式时代的产物（用于发现坐标系判错），现由 `unresolved_events()`
    #     （位置断裂）覆盖。
    #     """
    #     return []

    def clear(self) -> None:
        """清空所有记录。"""
        self.records.clear()
        self._session_records.clear()
        self._seen_batches.clear()
        self._session_start_events.clear()
        self._session_new_count = 0
        self._overlapped_history = False
        # total 快照已随历史清空 → Δ 基准失效，一并清除（GUI 会在下个会话重设）
        self._session_shift = 0
        self._shift_baseline_total = None
        self._total_seen = None
        self._known_positions = set()
        self._known_complete_ts = set()
        self._known_partial_ts = set()

    def begin_new_session(self) -> None:
        """刷新会话基线，供「停止抓包」后开启下一次抓取时调用。

        清空批指纹与**本次会话新增**列表，并把会话起点快照更新为「当前已累计的
        全部记录」（等价于已持久化历史）。下一次收尾时 `history.merge()` 只会拿到
        新一轮会话新增的记录，按位置并入历史。

        会话 Δ 一并清除：新会话的 Δ 要等首个响应的权威 total 到达后由 GUI 重设
        （`set_session_shift`），在此之前 add 保守全收。

        与旧实现的区别：不再重建事件账本（位置对齐方案下账本已退役），
        算法逻辑（shared_pity_tracking）保持冻结，这里只重置输入状态。
        """
        self._seen_batches.clear()
        self._session_records.clear()
        self._session_start_events = {r.timestamp for r in self.records}
        self._session_new_count = 0
        self._overlapped_history = False
        # 会话 Δ 一并清除：新会话的 Δ 要等首个响应的权威 total 到达后由 GUI 重设
        # （begin_session_shift），在此之前 add 保守全收。
        self._session_shift = 0
        self._shift_baseline_total = None
        self._total_seen = None
        self._known_positions = set()
        self._rebuild_event_index()

    @property
    def session_new_count(self) -> int:
        """本次会话收下的条数（>0 表示抓到了新记录）。"""
        return self._session_new_count

    @property
    def overlapped_history(self) -> bool:
        """本次会话是否抓到过与历史重叠的旧记录（已翻到历史边界）。"""
        return self._overlapped_history

    def load_records(self, records: List[GachaRecord],
                     replace: bool = False) -> None:
        """加载记录作为会话起点（默认追加），并更新会话起点快照。

        Args:
            records: 待加载记录（通常是落库历史）
            replace: True 时**替换**全部记录（收尾合并后以落库数据为准重建——
                合并时新记录的 `pos` 被 Δ 归一化过，必须整体重载才能保证
                保底消费序与界面统计一致）
        """
        if replace:
            self.records.clear()
        self.records.extend(records)
        self._session_start_events = {r.timestamp for r in self.records}
        # records 变了 → 位置索引按当前 shift 重建（shift 为 None 时集合为空，
        # 等 GUI 设置 Δ 时再建）
        self._refresh_known_positions()
        self._rebuild_event_index()

    def rarity_distribution(self) -> Tuple[Dict[int, int], Set[str]]:
        """稀有度分布。

        Returns:
            (rarity_counter_dict, unmapped_item_ids)
        """
        counter: Counter = Counter()
        unmapped: Set[str] = set()
        for rec in self.records:
            if _is_manual_fill(rec):
                continue
            char = self.characters.get(rec.item_id)
            if char:
                counter[char.rarity] += 1
            else:
                unmapped.add(rec.item_id)
        return dict(sorted(counter.items(), reverse=True)), unmapped

    def pool_distribution(self) -> Dict[str, int]:
        """卡池分布（按数量降序）。"""
        counter: Counter = Counter()
        for rec in self.records:
            if _is_manual_fill(rec):
                continue
            pool = self.pools.get(rec.pool_id)
            name = pool.name if pool else f"未知({rec.pool_id})"
            counter[name] += 1
        return dict(counter.most_common())

    def unknown_id_stats(self) -> Dict[str, int]:
        """统计所有未在元数据中的 item_id 及其出现次数（用于面板提示/屏蔽管理）。

        注意：返回的是"是否在元数据里"的客观事实，与是否被屏蔽无关；屏蔽状态
        由 self.shielded 单独读取。
        """
        counter: Counter = Counter()
        for rec in self.records:
            if _is_manual_fill(rec):
                continue
            if rec.item_id not in self.characters:
                counter[rec.item_id] += 1
        return dict(counter)

    def _order_records_for_pity(self, records: List[GachaRecord]) -> List[GachaRecord]:
        """保底消费序：**旧 -> 新**。

        事件之间按 `timestamp` 升序；事件内部按 `position_in_batch` **降序**
        （`pos` 越小越新，A5）。位置对齐方案下这一点是可靠的：

        - B8 后只剩「全部卡池」单一视图 → 同一会话内 `pos` 唯一且可比（实测
          511 条 pos 0~520 无重复、事件内 100% 连续）；
        - 跨会话合并时新记录的 `pos` 已被 Δ 归一化到历史坐标系（见
          `storage/history.merge`），因此落库数据的 `pos` 始终在同一坐标系里。

        `batch_seq` 新数据恒为 0（`models.py`），仅作兼容性次级键。
        """
        by_ts: Dict[int, List[GachaRecord]] = defaultdict(list)
        for rec in records:
            by_ts[rec.timestamp].append(rec)
        ordered: List[GachaRecord] = []
        for ts in sorted(by_ts):
            ordered.extend(self._order_within_event(ts, by_ts[ts]))
        return ordered

    def _order_within_event(self, ts: int,
                            group: List[GachaRecord]) -> List[GachaRecord]:
        """同一事件（同一 ts）内的旧->新序：位置降序（位置即身份）。"""
        if len(group) == 1:
            return list(group)
        return sorted(group, key=lambda r: (-r.batch_seq, -r.position_in_batch))

    def shared_pity_tracking(self) -> Dict[str, dict]:
        """各保底分组的共享保底追踪（6 星）。

        同组所有池的记录按 timestamp 升序（旧->新）合并成一条序列扫描；
        pity 从最近一次 6 星后累计，命中 6 星归 0 并按 is_up 翻转小/大保底状态
        （UP 命中 -> 小保底；歪 -> 大保底）。独立池（has_state=False）不跑状态机。

        每池分解（per_pool.padded）= 该池在组内最近一次 6 星之后的抽数，
        组内各池 padded 加和 = 组 current_pity。

        Returns:
            {
                group_id: {
                    "label": str, "hard_pity": int, "has_state": bool,
                    "state": "小保底"|"大保底"|None,
                    "total": int,            # 组内总抽数
                    "current_pity": int,     # 自最近 6 星以来的抽数
                    "distance_to_pity": int, # hard_pity - current_pity（>=0）
                    "six_count": int,
                    "up_count": int,          # 命中 UP 的 6 星数（is_up=True 的条数）
                    "hits": [{"pity","ts","pool_id","pool_name","name","is_up"}, ...],
                    "per_pool": {pool_id: {"padded","total","six_count","up_count","hits"}, ...},
                    "pool_ids": [str, ...],   # 归属该组的全部 pool_id
                },
                ...
            }
        """
        pool_to_group, group_configs = resolve_groups(self.pools, self.pity_groups)

        # 按分组聚合记录
        group_records: Dict[str, List[GachaRecord]] = defaultdict(list)
        for rec in self.records:
            gkey = pool_to_group.get(rec.pool_id, "solo:" + rec.pool_id)
            group_records[gkey].append(rec)

        # 分组展示顺序：有状态机（共享组）在前，独立组在后；各自按 label 排
        def _sort_key(gkey: str):
            cfg = group_configs.get(gkey)
            return (0 if (cfg and cfg.has_state) else 1, (cfg.label if cfg else gkey), gkey)

        result: Dict[str, dict] = {}
        for gkey in sorted(group_records.keys(), key=_sort_key):
            cfg = group_configs.get(gkey)
            if cfg is None:
                cfg = PityGroup(id=gkey, label=gkey, hard_pity=70, has_state=False)

            # 旧->新：timestamp 主排序；同一十连内多条 timestamp 相同，游戏以
            # 「新->旧」下发（帧内最新条目在前），需 batch_seq / position_in_batch
            # 降序作 tiebreaker 才能得到正确的旧->新序，否则十连内最新一条会被
            # 排到最前、保底计数错位。tiebreaker 语义约定「越小越新」由解析层
            # 保证：新数据 position_in_batch = 视图内绝对序号（与翻页方向无关，
            # 2026-08-18 修复），旧数据为帧序（抓包时新->旧翻页才正确）。
            recs = self._order_records_for_pity(group_records[gkey])

            pity = 0
            state = "小保底" if cfg.has_state else None  # 初始态
            hits: List[dict] = []
            per_pool_padded: Dict[str, int] = defaultdict(int)
            per_pool_total: Dict[str, int] = defaultdict(int)
            per_pool_hits: Dict[str, List[dict]] = defaultdict(list)

            for rec in recs:
                pity += 1
                per_pool_padded[rec.pool_id] += 1
                per_pool_total[rec.pool_id] += 1
                char = self.characters.get(rec.item_id)
                if char:
                    is_six = char.rarity == 6
                else:
                    # 陌生 ID：默认按 6 星；被屏蔽则按 4/5 星（不触发归零）
                    is_six = rec.item_id not in self.shielded
                if is_six:
                    pool = self.pools.get(rec.pool_id, Pool.default(rec.pool_id))
                    name = char.name if char else f"未知({rec.item_id})"
                    if char:
                        is_up = pool.is_featured and pool.up_character == char.name
                    else:
                        # 陌生 6 星：常驻歪角都已在元数据里，陌生的必是新角色=UP命中 -> 小保底
                        is_up = pool.is_featured
                    hits.append({
                        "pity": pity,
                        "ts": rec.timestamp,
                        "pool_id": rec.pool_id,
                        "pool_name": pool.name,
                        "name": name,
                        "is_up": is_up,
                    })
                    per_pool_hits[rec.pool_id].append({
                        "pity": pity,
                        "ts": rec.timestamp,
                        "name": name,
                        "is_up": is_up,
                    })
                    # 命中 6 星：组保底归 0，各池 padded 同步归 0
                    pity = 0
                    per_pool_padded = defaultdict(int)
                    # 状态翻转（仅 has_state 组）
                    if cfg.has_state:
                        state = "小保底" if is_up else "大保底"

            per_pool: Dict[str, dict] = {}
            for pid in per_pool_total:
                per_pool[pid] = {
                    "padded": per_pool_padded.get(pid, 0),
                    "total": per_pool_total[pid],
                    "six_count": len(per_pool_hits[pid]),
                    "up_count": sum(1 for h in per_pool_hits[pid] if h["is_up"]),
                    "hits": per_pool_hits[pid],
                }

            result[gkey] = {
                "label": cfg.label,
                "hard_pity": cfg.hard_pity,
                "has_state": cfg.has_state,
                "state": state,
                "total": len(recs),
                "current_pity": pity,
                "distance_to_pity": max(0, cfg.hard_pity - pity),
                "six_count": len(hits),
                "up_count": sum(1 for h in hits if h["is_up"]),
                "hits": hits,
                "per_pool": per_pool,
                "pool_ids": sorted(pid for pid, g in pool_to_group.items() if g == gkey),
            }

        return result

    def non_warp_rates(self, tracking: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
        """按「UP 出现在哪个池就算哪个池」计算每池/每组不歪率。

        对每个 has_state 分组，按组内 hits 全局时间序（旧->新，与
        shared_pity_tracking 排序一致）重放小/大保底状态机，给每次 6 星命中
        标记当时的保底态：
          - 小保底命中 UP（50/50 胜）-> 计入 wins 与 all_up；
          - 大保底必出 UP          -> 只计入 all_up（保底补偿，不算运气）；
          - 歪（小保底非 UP）      -> 两者都不计。
        按 hits 的 pool_id 分桶，即「UP 出现在哪个池就算哪个池」；跨池继承的
        大保底必出计入承接池 all_up，不搬回产生它的池。组/合计 = sum(wins) /
        sum(all_up)。rate=None 表示 all_up==0（无 UP 数据，如整池全歪）。

        同时输出组级 "guaranteed"：大保底必出 UP 命中的键集合（元素 =
        (pool_id, ts, pity, name)），供展示层把必出命中标为 [UP·必出]——
        组级 hits 与 per_pool hits 的同键命中均能匹配（两列表的 pity/ts/name
        同源同值）。

        新方法不改动冻结的 shared_pity_tracking()；仅消费其输出为展示派生指标。

        Args:
            tracking: shared_pity_tracking() 的返回；缺省时内部调用一次。

        Returns:
            {group_id: {
                "wins": int, "all_up": int, "rate": float|None,
                "guaranteed": {(pool_id, ts, pity, name), ...},
                "per_pool": {pool_id: {"wins","all_up","rate"}},
            }}
        """
        if tracking is None:
            tracking = self.shared_pity_tracking()

        result: Dict[str, dict] = {}
        for gkey, info in tracking.items():
            if not info["has_state"]:
                continue
            wins = 0
            all_up = 0
            per_wins: Dict[str, int] = defaultdict(int)
            per_all_up: Dict[str, int] = defaultdict(int)
            guaranteed: set = set()
            state = "小保底"  # 初始态与 shared_pity_tracking 状态机一致
            for h in info["hits"]:
                is_up = h["is_up"]
                if state == "小保底" and is_up:
                    wins += 1
                    per_wins[h["pool_id"]] += 1
                if is_up:
                    all_up += 1
                    per_all_up[h["pool_id"]] += 1
                    if state == "大保底":
                        # 大保底必出命中（前置歪可能在同池或跨池）
                        guaranteed.add(
                            (h["pool_id"], h["ts"], h["pity"], h["name"])
                        )
                state = "小保底" if is_up else "大保底"

            per_pool: Dict[str, dict] = {}
            for pid in per_all_up:
                per_pool[pid] = {
                    "wins": per_wins[pid],
                    "all_up": per_all_up[pid],
                    "rate": (per_wins[pid] / per_all_up[pid]) if per_all_up[pid] else None,
                }
            result[gkey] = {
                "wins": wins,
                "all_up": all_up,
                "rate": (wins / all_up) if all_up else None,
                "guaranteed": guaranteed,
                "per_pool": per_pool,
            }
        return result
