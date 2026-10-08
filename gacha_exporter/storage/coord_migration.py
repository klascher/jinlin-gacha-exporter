"""启动迁移：把 `users/<id>.json` 的历史坐标系归一（幂等、可回滚）。

**只覆盖 `payload["records"]`**，其余顶层键（`session` / Android 写过来的
`scan_gaps` / `source_packages` / `last_capture_detail` / 未来未知键）**原样写回**
—— 迁移只做它该做的那一件事，不做任何「顺手清理」。

设计要点（详见 `PC端统一坐标迁移-落码方案-2026-09-20.md`）：

- **幂等判据** = 文件里还存在 `batch_seq != 0` 的记录（`coord_unify.needs_unify`）；
- **双份备份**：① 既有规约快照 `<id>_<ts>.json`（GUI「还原备份」能直接选中）
  ② `<id>.json.pre_unify`（防①被 `MAX_BACKUPS=30` + `BUDGET_BYTES=20MB` 裁剪挤掉）；
- **原子写盘**：tmp + `os.replace`。`HistoryService._load()` 遇到坏 JSON 是
  `except` → `records = []`（**静默变空**，比报错危险得多），所以必须原子；
- **绝不阻断启动**：单账号任何异常都吞掉并记日志，其余账号继续。
"""
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import List, Optional

from ..analysis.coord_unify import apply_unify, plan_unify
from ..models import GachaRecord
from ..utils.logger import get_logger
from ..utils.paths import get_users_dir
from .history import backup_account_history

logger = get_logger(__name__)

# 备份文件规约 `<id>_<YYYYMMDD_HHMMSS>.json`（可带 `_N` 后缀）。
# 正式账号 id 是 uuid hex，**不含下划线**，因此这个正则能可靠区分备份快照。
_BACKUP_RE = re.compile(r"_\d{8}_\d{6}(_\d+)?\.json$")


def is_official_file(name: str) -> bool:
    """`<id>.json` 是正式数据文件；带时间戳的是备份快照，必须排除。

    `.pre_unify` 快照不以 `.json` 结尾，天然被排除。

    Args:
        name: `users/` 下的文件名（不含目录）。

    Returns:
        True 表示这是应参与迁移的正式账号文件。
    """
    return name.endswith(".json") and not _BACKUP_RE.search(name)


@dataclass
class AccountResult:
    """单账号迁移结果（供汇总日志与单测断言）。"""

    pid: str
    status: str  # migrated / skipped / invalid / failed
    count: int = 0
    changed: int = 0
    snapshot: Optional[str] = None
    legacy_snapshot: Optional[str] = None
    detail: str = ""


def _snapshot(path: str) -> str:
    """留一份 `<id>.json.pre_unify`（已存在则加时间戳，不覆盖早期原件）。

    与既有规约快照分开放：这份不受 `HistoryService` 的裁剪策略约束，
    是「回滚路径」的最后一道保险。
    """
    snap = path + ".pre_unify"
    if os.path.exists(snap):
        snap = f"{path}.pre_unify_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copyfile(path, snap)
    return snap


def _gap_report(records: List[GachaRecord]) -> str:
    """把旧编号的「洞」记成一行文本。

    这是**迁移前最后一次**能读到旧编号的机会：`batch_seq` 被置 0 之后，
    「当时缺了哪些页」就再也推不出来了。所以逐账号写进 `app.log` 留档。
    """
    legacy = [r for r in records if r.batch_seq != 0]
    if not legacy:
        return "无旧格式记录"
    pages = sorted({r.batch_seq for r in legacy})
    missing = [p for p in range(pages[0], pages[-1] + 1) if p not in set(pages)]
    # PAGE_SIZE=5 是经验规则（非协议字段），此处仅作粗略记录
    slots = len(pages) * 5
    holes = max(0, slots - len(legacy))
    return (
        f"旧格式 {len(legacy)} 条 / 新格式 {len(records) - len(legacy)} 条；"
        f"页号 {pages[0]}..{pages[-1]}（缺 {missing or '无'}）；"
        f"按每页 5 格铺满的空缺 ≈ {holes} 格"
    )


def migrate_one(path: str) -> AccountResult:
    """迁移单个账号文件；任何异常都不得抛出（迁移失败不能阻断启动）。

    Args:
        path: `users/<id>.json` 绝对路径。

    Returns:
        `AccountResult`（status ∈ migrated / skipped / invalid / failed）。
    """
    pid = os.path.basename(path)[: -len(".json")]
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.warning(f"[坐标迁移] {pid}: 读取/解析失败，跳过: {e}")
        return AccountResult(pid, "invalid", detail=f"读取失败: {e}")

    if not isinstance(payload, dict):
        logger.warning(f"[坐标迁移] {pid}: 顶层结构不是对象，跳过")
        return AccountResult(pid, "invalid", detail="顶层不是 JSON 对象")

    raw = payload.get("records") or []
    try:
        records = [GachaRecord(**r) for r in raw]
    except TypeError as e:
        # 字段与 GachaRecord 不符时**不猜**：猜错正是会导致历史静默清空的那类风险
        logger.warning(f"[坐标迁移] {pid}: records 字段与 GachaRecord 不符，跳过: {e}")
        return AccountResult(pid, "invalid", detail=f"字段不符: {e}")

    plan = plan_unify(records)
    if not plan.needed:
        logger.info(f"[坐标迁移] {pid}: 已统一（{len(records)} 条），未写盘未备份")
        return AccountResult(pid, "skipped", count=len(records), detail="已统一")

    for msg in plan.warnings:
        logger.warning(f"[坐标迁移] {pid}: {msg}")
    logger.info(f"[坐标迁移] {pid}: 迁移前编号全貌 —— {_gap_report(records)}")

    snapshot = None
    legacy_snapshot = None
    try:
        # ① 既有规约快照：与 GUI「还原备份」列表同规约，用户可直接在界面上选中
        snapshot = backup_account_history(pid)
        # ② 迁移专属快照：防①被 MAX_BACKUPS / BUDGET_BYTES 裁剪挤掉
        legacy_snapshot = _snapshot(path)

        new_payload = dict(payload)  # 保留全部其它顶层键（含未知键、原键序）
        new_payload["records"] = [asdict(r) for r in apply_unify(records, plan)]

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.exception(f"[坐标迁移] {pid}: 失败，尝试回滚")
        if legacy_snapshot and os.path.exists(legacy_snapshot):
            try:
                shutil.copyfile(legacy_snapshot, path)
                logger.warning(f"[坐标迁移] {pid}: 已从 {legacy_snapshot} 回滚")
            except Exception:
                logger.exception(f"[坐标迁移] {pid}: 回滚也失败，请手工从备份恢复")
        return AccountResult(pid, "failed", count=len(records), detail=str(e))

    logger.info(
        f"[坐标迁移] {pid}: 已统一 {len(records)} 条"
        f"（实际改动 {plan.changed} 条），快照 "
        f"{os.path.basename(snapshot) if snapshot else '-'} / "
        f"{os.path.basename(legacy_snapshot) if legacy_snapshot else '-'}"
    )
    return AccountResult(
        pid,
        "migrated",
        count=len(records),
        changed=plan.changed,
        snapshot=snapshot,
        legacy_snapshot=legacy_snapshot,
    )


def migrate_coord_frame(users_dir: Optional[str] = None) -> List[AccountResult]:
    """扫描 `users/` 下全部正式文件并逐个迁移；返回逐账号结果。

    Args:
        users_dir: 账号历史目录；缺省取 `utils.paths.get_users_dir()`。

    Returns:
        逐账号 `AccountResult` 列表（备份快照已被排除）。
    """
    users_dir = users_dir or get_users_dir()
    try:
        names = sorted(os.listdir(users_dir))
    except OSError as e:
        logger.warning(f"[坐标迁移] 扫描目录失败: {e}")
        return []

    results: List[AccountResult] = []
    for name in names:
        if not is_official_file(name):
            continue
        try:
            results.append(migrate_one(os.path.join(users_dir, name)))
        except Exception:  # 兜底：单账号异常不许影响其余账号与启动
            logger.exception(f"[坐标迁移] {name}: 未捕获异常，跳过")

    if results:
        migrated = [r for r in results if r.status == "migrated"]
        logger.info(
            f"[坐标迁移] 完成：检查 {len(results)} 份，迁移 {len(migrated)} 份，"
            f"跳过 {len(results) - len(migrated)} 份"
        )
    return results
