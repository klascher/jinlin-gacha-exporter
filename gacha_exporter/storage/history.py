"""历史记录持久化与合并 — 保存到用户数据目录 history.json。

**去重方案：位置对齐**（见 `docs/去重方案-位置对齐.md`）。B8（仅采纳「全部卡池」）
之后只剩单一视图，`position_in_batch` 成为唯一且可比的记录身份，于是：

- 单次抓包**全收**（不做事件内去重；依据 B3 页互斥 + C5 页缓存）；
- 跨会话合并按 **`(timestamp, pos − Δ)`** 判重 → 同名记录 `pos` 不同，天然不会被吞；
- **Δ = 权威 total 增量**（`infer_shift_from_total`）：抽卡让列表变长、历史记录位置整体
  后移，后移量恰好等于新增抽卡数。**不用内容比对推 Δ**（十连同名使内容比对必然误判，
  见设计稿 §4.3）。

合并时会把新记录的 `pos` **就地减去 Δ**，使落库数据始终处于同一坐标系——这样
保底消费序（按 pos）、事件内位置连续性判定都成立，且下次合并的基准自然延续。

依赖：models、analysis.pos_align、utils.paths、json
"""
import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Dict, List, Optional

from ..analysis.pos_align import VALID_EVENT_SIZES, MergeReport, merge_by_position
from ..models import GachaRecord, SessionContext
from ..utils.logger import get_logger
from ..utils.paths import get_users_dir

logger = get_logger(__name__)

# 历史文件里记录权威 total 时使用的视图键（B8 后用 "0@0"；老文件可能是裸 "0"）
_ALL_POOLS_VIEW_KEYS = ("0@0", "0")

# 跨端继承时**不照搬旧值、显式置 None** 的顶层键。`last_capture_detail` 的语义是
# 「上一场抓包」：记录被本次 PC 抓包接续（或清空）之后，旧明细就失效了 —— 原样留着
# 会让手机记录页显示「上次 N 条」而列表却对不上（安卓 clear() 也是同样处置）。
_CLEARED_FOREIGN_KEYS = ("last_capture_detail",)


@dataclass(frozen=True)
class BackupInfo:
    """一个可还原的备份点（`<pid>_<时间戳>.json`）。

    字段:
        pid: 所属账号 id
        path: 备份文件绝对路径
        when: 解析出的备份时刻（未带 _N 后缀的那部分时间戳）
        records: 该备份内含记录条数（读文件计数）
        label: 人类可读展示串，如 "2026-08-17 21:52:43（5 条）"
        filename: 原文件名（含 _N 后缀，供同名同秒唯一区分）
    """

    pid: str
    path: str
    when: datetime
    records: int
    label: str
    filename: str


def _parse_backup_ts(filename: str, pid: str) -> Optional[datetime]:
    """从备份文件名解析出备份时刻；不合规返回 None（不列入，防误选）。

    规约：`<pid>_<YYYYMMDD_HHMMSS>.json`，可带 `_N` 重复后缀
    （`_backup`/`_unique_archive` 同秒多次备份生成），此处剥离 `_N` 后解析。
    """
    prefix = f"{pid}_"
    if not filename.startswith(prefix) or not filename.endswith(".json"):
        return None
    ts_part = filename[len(prefix):-len(".json")]
    parts = ts_part.split("_")
    if len(parts) == 3 and parts[2].isdigit():
        ts_part = "_".join(parts[:2])
    try:
        return datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


class HistoryService:
    """历史记录服务（位置对齐去重，与统计层共用 `analysis/pos_align.py`）。

    **判重依据是位置，不是内容**：同一视图内 `position_in_batch` 是记录在列表里的
    绝对位置，十连同名两条的位置不同 → 位置即身份，既不会把同名第二份当重复吞掉，
    也不会把跨会话重抓的记录重复计入。内容只用于人眼核对，算法不消费。

    与统计层的分工：

    - `merge()` 接收**本次会话新增**的记录（`StatsEngine.session_records`），
      按位置并入落库记录；Δ（位置平移量）由权威 total 增量得出（`last_total` 快照
      随文件持久化），新记录的 `pos` 会就地减去 Δ 以统一坐标系。
    - 调用方在 merge 之后应让统计层以落库记录为准重建（保证消费序一致）。

    所有写盘操作（merge / save / clear）统一经 _write_payload：写盘前若磁盘
    上已有非空历史，先拷贝为 <profile_id>_<时间戳>.json 再写入，保证任何
    修改都不会无声覆盖掉上一份有效数据。
    """

    def __init__(self, profile_id: str):
        """初始化，按账号 id 定位历史文件并加载。

        Args:
            profile_id: 账号档案 id，历史存放在 data/users/<profile_id>.json
        """
        self.profile_id = profile_id
        self.users_dir = get_users_dir()
        os.makedirs(self.users_dir, exist_ok=True)
        self.history_file = os.path.join(self.users_dir, f"{profile_id}.json")
        self._records: List[GachaRecord] = []
        # 各视图的权威 total 快照（视图 -> 总抽数）与其落库时点，供下次合并推 Δ
        self._view_totals: Dict[str, int] = {}
        self._last_total: Optional[int] = None
        # 事件完整性索引（旧格式历史兼容，见 docs/去重方案-旧历史兼容-事件级判重.md）：
        # 完整事件（size ∈ {1,10}）跨会话判重只看 ts；部分事件走替换/兜底。
        self._complete_ts: set = set()
        self._partial_ts: set = set()
        # 最近一次 merge 的详细诊断（供 GUI 报警消费）
        self.last_report: Optional[MergeReport] = None
        self._load()

    def _analyze_events(self) -> None:
        """按当前记录重建事件完整性索引（完整 / 部分 ts 集合）。"""
        sizes: Dict[int, int] = {}
        for r in self._records:
            sizes[r.timestamp] = sizes.get(r.timestamp, 0) + 1
        self._complete_ts = {
            ts for ts, n in sizes.items() if n in VALID_EVENT_SIZES
        }
        self._partial_ts = set(sizes) - self._complete_ts

    @property
    def complete_event_ts(self) -> set:
        """历史中完整事件（size ∈ {1,10}）的 ts 集合（只读副本）。"""
        return set(self._complete_ts)

    @property
    def partial_event_ts(self) -> set:
        """历史中部分事件（size ∉ {1,10}）的 ts 集合（只读副本）。"""
        return set(self._partial_ts)

    def _load(self) -> None:
        """从文件加载历史记录与 total 快照。"""
        if not os.path.exists(self.history_file):
            self._records = []
            self._view_totals = {}
            self._last_total = None
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            records = payload.get("records", [])
            self._records = [GachaRecord(**r) for r in records]
            self._view_totals = {
                str(k): int(v) for k, v in (payload.get("view_totals") or {}).items()
            }
            raw_total = payload.get("total")
            self._last_total = int(raw_total) if raw_total is not None else None
            logger.info(f"已从历史记录加载 {len(self._records)} 条")
        except Exception as e:
            logger.warning(f"加载历史失败: {e}")
            self._records = []
            self._view_totals = {}
            self._last_total = None
        self._analyze_events()
        self._maybe_backfill_total()

    def _maybe_backfill_total(self) -> None:
        """旧格式历史无 total 快照时的**条件回填**（兼容设计稿 §3.2）。

        全部事件完整（size ∈ {1,10}）→ 历史条数即当时的权威总数，回填
        `total` 与 `view_totals["0@0"]`（仅内存，随下次落库持久化；幂等）。
        存在部分事件 → 无法确知真实总数（缺的记录数不可知），维持 None，
        由 GUI 的 Δ=0 回退路径兜底。
        """
        if self._last_total is not None or not self._records or self._partial_ts:
            return
        self._last_total = len(self._records)
        self._view_totals.setdefault("0@0", self._last_total)
        logger.info(
            f"旧格式历史无 total 快照且事件全部完整 → 回填 total={self._last_total}"
            f"（内存，随下次落库持久化）"
        )

    def _all_pools_total(self) -> Optional[int]:
        """当前已知的「全部卡池」权威 total（B8 后只有它入账）；未知返回 None。"""
        for key in _ALL_POOLS_VIEW_KEYS:
            if key in self._view_totals:
                return self._view_totals[key]
        return None

    @property
    def last_total(self) -> Optional[int]:
        """上次落库时的权威 total（Δ 基准）；无历史返回 None。"""
        return self._last_total

    def save(self, records: List[GachaRecord], session: SessionContext) -> None:
        """保存当前会话记录（覆盖式）。

        Args:
            records: 抽卡记录
            session: 会话上下文（含当前卡池、开始时间）
        """
        payload = {
            "session": {
                "current_pool_id": session.current_pool_id,
                "capture_start": session.capture_start.isoformat(),
            },
            "records": [asdict(r) for r in records],
            "view_totals": self._view_totals,
            "total": self._last_total,
        }
        self._write_payload(payload)
        self._records = list(records)
        self._analyze_events()
        logger.info(f"已保存 {len(records)} 条记录到 {self.history_file}")

    def merge(
        self, records: List[GachaRecord],
        view_totals: Optional[Dict[str, int]] = None,
        shift: int = 0,
    ) -> int:
        """把**本次会话新增**的记录按位置并入历史，返回新增条数（位置对齐去重）。

        判重是 `(timestamp, pos − shift)`：同名记录位置不同，不会被吞；跨会话
        重抓的同一批位置相同，会被跳过。

        **`records` 必须已处于基准坐标系**：生产路径下 `StatsEngine.add` 收下
        记录时已按当时的 Δ 归一化（Δ 随会话中途抽卡动态调整），故调用方恒传
        默认 `shift=0`（本方法只做幂等复核 + 追加，**不再自行推 Δ**——Δ 的
        推导在 stats 侧实时完成，见 `docs/去重方案-位置对齐.md` §4.1/§4.3）。

        Args:
            records: 本次会话新增的记录（**不含**已落库的历史记录）
            view_totals: 本次会话的 `{视图: 权威 total}` 快照；会并入历史快照，
                其中「全部卡池」的 total 作为下次合并的 Δ 基准
            shift: 位置归一化量；仅离线工具/测试直接喂未归一化记录时才显式传

        Returns:
            新增记录数（同时可通过 `self.last_report` 取详细诊断）
        """
        if view_totals:
            self._view_totals.update(
                {str(k): int(v) for k, v in view_totals.items()}
            )
        current_total = self._all_pools_total()
        if current_total is not None:
            self._last_total = current_total

        report = merge_by_position(self._records, records, shift=shift)
        self.last_report = report

        if shift:
            # 归一化写回：新记录的 pos 减去 shift 后与历史同坐标系。
            # 注意 `GachaRecord` 是 **frozen dataclass**，不能就地赋值，用 replace 生成。
            report.added = [
                replace(rec, position_in_batch=rec.position_in_batch - shift)
                for rec in report.added
            ]

        if report.fallback_events:
            logger.warning(
                f"历史合并：{len(report.fallback_events)} 个事件的历史位置断裂"
                f"（B8 前混视图遗留）→ 退化为整段追加，可能重复: "
                f"{report.fallback_events[:5]}"
            )
        if report.suspicious:
            logger.warning(
                f"历史合并：{len(report.suspicious)} 个事件大小异常 "
                f"（∉ {{1,10}}，建议重翻缺口页）: {report.suspicious[:5]}"
            )
        if report.replaced_events:
            # 部分事件被新完整事件整体替换：移除历史旧记录（绝不静默，写日志）。
            replaced = set(report.replaced_events)
            removed = sum(1 for r in self._records if r.timestamp in replaced)
            self._records = [
                r for r in self._records if r.timestamp not in replaced
            ]
            logger.warning(
                f"历史合并：{len(report.replaced_events)} 个部分事件被新完整"
                f"事件整体替换（移除旧 {removed} 条，新 {len(report.added)} 条）: "
                f"{report.replaced_events[:5]}"
            )

        if not report.added:
            # 无新增也要落 total 快照：否则下次仍按旧基准推 Δ，位置会整体错位
            self._write_payload(self._payload())
            self._analyze_events()
            logger.info(
                f"历史合并无新增（事件级跳过 {report.event_skipped} 个事件，"
                f"位置命中跳过 {report.skipped} 条，Δ={shift}）"
            )
            return 0

        self._records.extend(report.added)
        self._records.sort(key=lambda r: r.timestamp, reverse=True)
        self._analyze_events()
        self._write_payload(self._payload())
        logger.info(
            f"历史合并新增 {len(report.added)} 条（Δ={shift}，事件级跳过 "
            f"{report.event_skipped} 个事件，位置命中跳过 {report.skipped} 条），"
            f"当前共 {len(self._records)} 条"
        )
        return len(report.added)

    def _payload(self) -> dict:
        """构造当前历史的落库 payload（含 total 快照，供下次合并推 Δ）。"""
        return {
            "records": [asdict(r) for r in self._records],
            "view_totals": self._view_totals,
            "total": self._last_total,
        }

    @staticmethod
    def _carry_foreign_keys(payload: dict, existing: dict) -> dict:
        """把磁盘上**本模块不拥有**的顶层键并入待写 payload（跨端键透传）。

        PC 只认 `records` / `view_totals` / `total`；Android 还写 `scan_gaps` /
        `source_packages` / `last_capture_detail`。不继承的话，「手机 → PC → 手机」
        走一趟就会在 PC 落库这一步被抹掉 3 个键。

        两条规则：

        - **未知键一律原样继承**（含将来 Android 新增的键）——不解读、不校验，
          结构怎么变都照搬，PC 不必跟着回改；
        - `_CLEARED_FOREIGN_KEYS` 里的键**不照搬旧值**，改显式置 `None`（见常量
          注释）。若内存 payload 已自带该键（将来 PC 自己维护它），以内存为准。

        Args:
            payload: 内存构造的待写内容（**就地更新**后返回）。
            existing: 磁盘上现有的 JSON 对象。

        Returns:
            并入跨端键后的 payload。
        """
        for key, value in existing.items():
            if key in payload or key in _CLEARED_FOREIGN_KEYS:
                continue
            payload[key] = value
        for key in _CLEARED_FOREIGN_KEYS:
            if key in existing and key not in payload:
                payload[key] = None
        return payload

    def clear(self) -> Optional[str]:
        """清空全部历史记录（内存与磁盘），清空前先备份。

        用于“放弃旧数据、从当前抓取开始重新积累”的场景：清空后 history.json
        写为空，后续 merge 只会保留本次会话的新记录。为避免数据丢失，清空前
        会把现有 history.json 复制为 history_<清空时间戳>.json；仅在有历史
        记录时生成备份。

        Returns:
            备份文件绝对路径；无历史记录可备份时返回 None。
        """
        # 不透传跨端键：记录已被放弃，留着 `last_capture_detail` 等明细会自相矛盾
        backup = self._write_payload(
            {"records": [], "view_totals": {}, "total": None},
            preserve_unknown=False,
        )
        self._records = []
        # total 快照一并清空：清空后重新起算 Δ 基准（否则下次合并会按旧基准
        # 推出一个巨大的 Δ，把新记录的位置整体挪错）
        self._view_totals = {}
        self._last_total = None
        self._analyze_events()
        logger.info("已清空历史记录")
        return backup

    def _write_payload(
        self, payload: dict, preserve_unknown: bool = True
    ) -> Optional[str]:
        """写盘前先备份现有历史，然后写入新内容。

        所有修改 history.json 的操作（merge / save / clear）统一走这里，
        保证任何写操作都不会无声覆盖掉上一份有效数据：若磁盘上已存在非空
        历史，先把当前 history.json 拷贝为 history_<时间戳>.json 再写入；
        磁盘为空或不存在时跳过备份。现有文件损坏不可读时同样 salvage 一份，
        避免直接覆盖丢弃。

        **跨端键透传**（`preserve_unknown=True`，默认）：磁盘上本模块不拥有的
        顶层键（Android 的 `scan_gaps` / `source_packages` 等）并回待写内容，
        避免「手机 → PC → 手机」在 PC 落库这一步把键抹掉（见 `_carry_foreign_keys`）。
        `clear()` 传 False：记录被放弃，留着明细会对不上。

        **内容幂等**：payload 与磁盘现文件完全一致时不写盘、不备份——
        停止抓包/切账号/关窗的收尾 merge 即使什么都没抓到也会走到这里，
        不加这道闸会每次收尾都多一个备份文件（2026-09-13 实测 6 分钟 17 个）。
        ⚠️ 幂等比较必须用**并入跨端键之后**的 payload：拿朴素内存 payload 去比，
        磁盘有未知键时恒不等，这道闸会永久失效。

        Args:
            payload: 待写内容（`records` / `view_totals` / `total`）。
            preserve_unknown: 是否继承磁盘上的跨端未知键。

        Returns:
            本次生成的备份文件绝对路径；无需备份时（含幂等跳过）返回 None。
        """
        backup = None
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if preserve_unknown and isinstance(existing, dict):
                    payload = self._carry_foreign_keys(payload, existing)
                if existing == payload:
                    return None  # 内容零变化：不写盘、不备份
                if existing.get("records"):
                    backup = self._backup()
            except Exception as e:
                logger.warning(f"读取现有历史用于备份失败，原样备份: {e}")
                backup = self._backup()
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return backup

    @staticmethod
    def _count_records(path: str) -> int:
        """读一份 JSON 备份内的记录条数（读取失败按 0 处理）。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return len(json.load(f).get("records", []))
        except Exception as e:
            logger.warning(f"读取备份条数失败 {path}: {e}")
            return 0

    def backups(self) -> List[BackupInfo]:
        """扫描当前账号 users/ 下全部符合规约的备份，按时间倒序。

        规约：`<pid>_<YYYYMMDD_HHMMSS>.json`（可带 `_N` 后缀）。解析失败的
        不合规 `<pid>_*.json` 不列入（防误选）。无合规则返回 []。
        """
        infos: List[BackupInfo] = []
        try:
            names = os.listdir(self.users_dir)
        except OSError as e:
            logger.warning(f"扫描备份目录失败: {e}")
            return infos
        for name in names:
            when = _parse_backup_ts(name, self.profile_id)
            if when is None:
                continue
            path = os.path.join(self.users_dir, name)
            records = self._count_records(path)
            # 同秒多份备份（_N 后缀）必须在 label 里区分：否则两份 label 完全
            # 相同，选择框无法选中第二份。_N 越大创建越晚（_backup 同秒顺延）。
            dup_tag = ""
            tail = name[len(f"{self.profile_id}_"):-len(".json")].split("_")
            if len(tail) == 3 and tail[2].isdigit():
                dup_tag = f" 同秒_{tail[2]}"
            infos.append(
                BackupInfo(
                    pid=self.profile_id,
                    path=path,
                    when=when,
                    records=records,
                    label=f"{when:%Y-%m-%d %H:%M:%S}（{records} 条）{dup_tag}",
                    filename=name,
                )
            )
        # 倒序（新->旧）；同秒多份时 filename 大者（_N 大）在前，即更晚创建的在前
        infos.sort(key=lambda b: (b.when, b.filename), reverse=True)
        return infos

    def restore(self, backup_path: str) -> Optional[str]:
        """把当前 `users/<pid>.json` 还原为所选备份，并刷新本服务内存。

        先备份当前文件为当天时间戳新备份（保留「被污染/还原前」状态，可再回溯），
        再把所选备份覆盖为 `users/<pid>.json`（保留所选备份原件，可反复还原）。
        覆盖用 shutil.copyfile。还原后本服务从磁盘重新加载，records 即还原内容。

        Args:
            backup_path: 所选备份的绝对路径（通常来自 backups()）

        Returns:
            还原前为保当前状态而新建的备份绝对路径；当前文件不存在则只覆盖并
            返回 None。
        """
        backup_cur = None
        if os.path.exists(self.history_file):
            backup_cur = self._backup()
        try:
            shutil.copyfile(backup_path, self.history_file)
        except Exception as e:
            logger.exception(f"还原失败: {e}")
            raise
        self._load()
        logger.info(f"已还原历史到 {self.history_file}（源 {backup_path}，前状态备份 {backup_cur}）")
        return backup_cur

    def _backup(self) -> str:
        """把当前历史文件备份为带时间戳的副本（账号隔离）。

        备份名为 <profile_id>_<时间戳>.json，与账号数据文件 <profile_id>.json
        靠下划线区分。同秒内多次备份追加 _1、_2 后缀避免覆盖。
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(self.users_dir, f"{self.profile_id}_{ts}.json")
        n = 1
        while os.path.exists(backup):
            backup = os.path.join(self.users_dir, f"{self.profile_id}_{ts}_{n}.json")
            n += 1
        shutil.copyfile(self.history_file, backup)
        logger.info(f"历史已备份到 {backup}")
        return backup

    def has_overlap(self, records: List[GachaRecord]) -> bool:
        """检查给定记录是否与历史存在**事件级**重叠（同一 timestamp 的事件已存在）。

        只是「是否翻到了历史边界」的粗判（供 GUI 提示），不参与去重判定——
        判重由 `merge()` 按位置完成。
        """
        if not self._records or not records:
            return False
        existing = {r.timestamp for r in self._records}
        return not existing.isdisjoint({r.timestamp for r in records})

    def is_empty(self) -> bool:
        """历史记录是否为空。"""
        return not self._records

    @property
    def records(self) -> List[GachaRecord]:
        """当前历史记录（按时间倒序）。"""
        return list(self._records)


def backup_account_history(profile_id: str) -> str:
    """按既有备份规约对 `users/<pid>.json` 做一份快照。

    命名与 GUI「还原备份」列表（`HistoryService.backups()`）一致，因此启动迁移
    生成的快照**用户可直接在界面上选中还原**，无需手工改文件名。抽成模块级
    函数的目的：让备份命名规约**保持单源** —— 若出现第二份实现，将来改名必然
    漂移。

    Args:
        profile_id: 账号档案 id。

    Returns:
        备份文件绝对路径。
    """
    return HistoryService(profile_id)._backup()
