# -*- coding: utf-8 -*-
"""跨端记录互通 —— **IO 事务壳**（薄层：整组原子落库）。

分层位置
--------
- `storage/sync_file.py`：**纯函数层**（解析 / 比对 / 计划 / 封装），零 IO、零时钟、零 Qt。
- 本模块：**唯一碰磁盘的一半** —— 读设备侧快照（导入比对、导出取材）、把 `SyncPlan`
  一次性落盘。

为什么要单独一层
----------------
两端既有的落库 API 都是**单账号**的（PC `HistoryService` / Android `HistoryStore`）。
互通要支持「一份文件含多个账号」，且用户 2026-09-21 裁定 **整组原子**：任一账号写失败
⇒ 本批**全部回滚**（`U1互通层-契约冻结与实现定案-2026-09-21.md` §6.2-b）。故在 IO 层包
一层事务壳；纯函数层与既有落库算法**一行不动**。

整组原子怎么落（定案稿 §6.2 a/b/e + §6.3）
------------------------------------------
1. **先解算、后落盘**：为每个账号算出目标 payload（含跨端键透传），此刻**一个字节都还没写**；
2. 逐个：备份现有文件 → `tmp + os.replace` 原子写；
3. 任一步抛错 ⇒ **逆序回滚**：把每个已入账账号还原为**写前原文**（写前不存在则删掉该文件），
   再撤掉本批**新建**的账号。

⚠️ 回滚依据是**内存里留存的写前原文**，不是磁盘上的备份文件。磁盘备份只是按既有命名规约
（`<pid>_<YYYYMMDD_HHMMSS>[_N].json`）留给用户「还原抽卡数据」列表选的，**不承担事务语义** ——
这样即使某账号历史为空（按既有规约不产生备份文件），回滚依然精确。

⚠️ 边界
------
- 本模块**不新增任何删除 API**；删文件的唯一入口仍是既有单点三闸（Android
  `StorageCleaner.delete()`）。PC 侧回滚只删**本批自己刚写的文件**与**本批自己刚生成的备份**，
  绝不触碰任何既有数据。
- 跨端键透传**复用** `HistoryService._carry_foreign_keys`（单源），不另写一份 —— 否则
  「手机 → PC → 手机」会在 PC 这一步被抹掉 `scan_gaps` / `source_packages` 等键。
"""
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..utils.logger import get_logger
from .history import HistoryService
from .sync_file import MODE_NEW

logger = get_logger(__name__)

# 原子写的临时文件后缀（与被写文件同目录，保证 `os.replace` 是同卷重命名）
_TMP_SUFFIX = ".sync-tmp"


@dataclass
class ApplyResult:
    """整组落库的结果。

    字段:
        ok: 是否成功。失败时 `written` 恒为空（整组回滚后不留半份结果）。
        error: 失败原因（人话，可直接进 `QMessageBox`）。
        written: 成功时每个账号的落库摘要
            `{"pid", "name", "mode", "records", "backup", "changed"}`；
            `records` 为落库后的条数，`backup` 为本次生成的备份路径（无则 None），
            `changed` 为 False 表示该账号与磁盘内容**逐字相同**、按幂等闸跳过了写盘。
        created: 本批**新建**的账号 id（成功时用于刷新账号列表）。
    """

    ok: bool
    error: Optional[str] = None
    written: List[Dict[str, Any]] = field(default_factory=list)
    created: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 磁盘小工具
# ---------------------------------------------------------------------------

def _users_dir(profiles: Any) -> str:
    """账号历史目录：优先用 `ProfileManager.users_dir`（单源），否则回退路径工具。"""
    d = getattr(profiles, "users_dir", None)
    if not d:
        from ..utils.paths import get_users_dir

        d = get_users_dir()
    return d


def _account_file(users: str, pid: str) -> str:
    return os.path.join(users, f"{pid}.json")


def read_text_file(path: str) -> Optional[str]:
    """读文本文件；不存在或读不出返回 `None`（不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"读取文件失败 {path}: {e}")
        return None


def write_export_file(path: str, text: str) -> None:
    """写导出的互通文件。

    ⚠️ **不做平台换行翻译**（`newline=""`）：`sync_file.dumps()` 产出什么字节就写什么字节
    ⇒ 同一份内容在任何平台**逐字节相同**，两端样本可直接对哈希。账号文件那条路
    （`_atomic_write_text`）刻意保留 Windows 翻译，以与既有 `HistoryService._write_payload`
    的落盘风格一致。
    """
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read_json(path: str) -> Any:
    """读 JSON；不存在 / 解析失败返回 `None`（不抛）。"""
    text = read_text_file(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _snapshot(path: str) -> Tuple[Optional[str], bool]:
    """记录写前状态：返回 `(原文, 是否原本就存在)`。

    原文读不出时返回 `(None, True)` —— 回滚阶段会保留现状并告警，**绝不当成「原本不存在」
    去删文件**（那会把用户一份虽然坏、但可能可修的记录直接抹掉）。
    """
    if not os.path.exists(path):
        return None, False
    return read_text_file(path), True


def _atomic_write_text(path: str, text: str, translate_newlines: bool = True) -> None:
    """`tmp + os.replace` 原子写。

    Args:
        translate_newlines: True 时走文本默认换行翻译（与 `HistoryService._write_payload`
            的 `json.dump` 落盘风格一致，Windows 上是 CRLF）；回滚阶段写**写前原文**必须
            传 False，否则原文里的 `\\r\\n` 会被二次翻译成 `\\r\\r\\n`。
    """
    tmp = path + _TMP_SUFFIX
    try:
        with open(tmp, "w", encoding="utf-8",
                  newline=None if translate_newlines else "") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 失败必须把临时文件清掉：否则会在 users/ 里留下 `.sync-tmp` 垃圾
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _backup_copy(path: str, pid: str, users: str) -> Optional[str]:
    """按既有备份命名规约复制一份（`<pid>_<时间戳>[_N].json`）。

    命名与 `HistoryService._backup()` **完全一致** ⇒ 生成的备份用户能直接在
    「还原抽卡数据」列表里看到并选中。同秒多次备份用 `_N` 顺延（沿用既有规约）。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(users, f"{pid}_{ts}.json")
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(users, f"{pid}_{ts}_{n}.json")
        n += 1
    try:
        shutil.copyfile(path, dst)
    except OSError as e:
        # 备份失败 ⇒ 事务无法保证可回退，交由调用方中止（不静默降级）
        raise RuntimeError(f"备份 {pid} 失败：{e}") from e
    return dst


# ---------------------------------------------------------------------------
# ① 设备侧快照（只读）
# ---------------------------------------------------------------------------

def load_device_accounts(profiles: Any) -> List[Dict[str, Any]]:
    """读出设备侧**全部账号现值**（只读快照）。

    用于「导入前比对」与「导出取材」两处。**直接读盘**而不是问 `HistoryService`：
    - 落库动作一律先收尾再合并（`_finalize_capture_session`），故非抓包态下磁盘即真相；
    - 不必给 `HistoryService` 加任何新访问器（它是承重模块，能不动就不动）。

    Returns:
        `[{"id", "name", "records", "total", "view_totals"}]`；文件缺失 / 损坏时该账号按
        **空记录**返回（不抛、不跳过 —— 账号列表必须与 `ProfileManager` 一一对应）。
    """
    users = _users_dir(profiles)
    out: List[Dict[str, Any]] = []
    for prof in profiles.list_profiles():
        payload = _read_json(_account_file(users, prof.id))
        records: List[Dict[str, Any]] = []
        total = None
        view_totals: Dict[str, Any] = {}
        if isinstance(payload, dict):
            raw = payload.get("records")
            if isinstance(raw, list):
                records = [dict(r) for r in raw if isinstance(r, dict)]
            total = payload.get("total")
            vt = payload.get("view_totals")
            if isinstance(vt, dict):
                view_totals = dict(vt)
        out.append({
            "id": prof.id,
            "name": prof.name,
            "records": records,
            "total": total,
            "view_totals": view_totals,
        })
    return out


# ---------------------------------------------------------------------------
# ② payload 解算（纯函数：write 三件套 + 磁盘既有对象 → 落盘 payload）
# ---------------------------------------------------------------------------

def build_payload(write: Dict[str, Any], existing: Any) -> Dict[str, Any]:
    """把计划里的 `write` 三件套解算成真正要落盘的 payload。

    - 三件套 = `records` / `view_totals` / `total`（与 `HistoryService._payload()` 同形，
      这样既有的 merge / 还原 / 幂等比较都能原样工作）；
    - 再把磁盘上「本模块不拥有」的顶层键并回来（跨端键透传）。

    ⚠️ 透传直接调 `HistoryService._carry_foreign_keys`：**单源**。若这里另写一份，
    将来 `_CLEARED_FOREIGN_KEYS`（`last_capture_detail` 置 `null`）的语义一变就会漂移。
    """
    payload: Dict[str, Any] = {
        "records": [dict(r) for r in (write.get("records") or [])],
        "view_totals": dict(write.get("view_totals") or {}),
        "total": write.get("total"),
    }
    if isinstance(existing, dict):
        HistoryService._carry_foreign_keys(payload, existing)
    return payload


# ---------------------------------------------------------------------------
# ③ 整组原子落库
# ---------------------------------------------------------------------------

def _undo_created(profiles: Any, created: List[str]) -> None:
    """撤掉本批新建的账号。

    调用时机固定在**文件回滚之后** —— 那时这些账号的 `users/<id>.json` 已被删掉，
    `ProfileManager.delete()` 找不到文件就不会再产生一份归档副本（不留垃圾）。
    """
    for pid in reversed(created):
        try:
            profiles.delete(pid)
        except Exception as e:  # noqa: BLE001 —— 回滚尽力而为，失败只告警
            logger.warning(f"回滚新建账号失败 {pid}: {e}")


def _discard_backup(path: Optional[str]) -> None:
    """删掉**本批自己刚生成**的备份文件。

    回滚成功后再留着它已无意义，且会污染用户的「还原抽卡数据」列表（内容与当前文件
    逐字节相同，选中还原是空操作）。⚠️ **只在原文件已成功还原之后调用** —— 还原失败时
    这份备份是唯一的兜底副本。
    """
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"回滚时清理备份失败 {path}: {e}")


def _rollback(journal: List[Dict[str, Any]]) -> None:
    """逆序把每个已入账账号还原为写前状态（并清掉本批自己造的备份）。"""
    for item in reversed(journal):
        path = item["path"]
        restored = True
        try:
            if not item["existed"]:
                if os.path.exists(path):
                    os.remove(path)
            elif item["prev_text"] is None:
                # 写前就读不出（文件损坏 / 被占用）：任何"还原"都可能更糟，保留现状
                restored = False
                logger.warning(f"回滚：{path} 写前原文不可读，保留现状不回写")
            else:
                _atomic_write_text(path, item["prev_text"], translate_newlines=False)
        except Exception as e:  # noqa: BLE001
            # 回滚必须尽力而为：任一账号还原失败只能告警，绝不能让异常再往上冒
            # （否则调用方拿到的是回滚过程的异常，而非真正的失败原因）。
            restored = False
            logger.warning(f"回滚文件失败 {path}: {e}")
        if restored:
            _discard_backup(item.get("backup"))


def apply_plan(profiles: Any, plan: Dict[str, Any],
               users_dir: Optional[str] = None) -> ApplyResult:
    """把 `SyncPlan` **整组原子**落库。

    Args:
        profiles: `ProfileManager`（新建账号、校验目标账号都经它；它是账号注册表的单源）。
        plan: `sync_file.plan_import()` 的成功结果（`{"ok": True, "accounts": [...]}`）。
        users_dir: 覆盖账号历史目录（测试用）；缺省取 `profiles.users_dir`。

    Returns:
        `ApplyResult`。失败时 `written` 恒为空 —— 整组回滚后不留半份结果。

    ⚠️ 调用前置：**抓包中不可调用**（调用方判 `running`，两端同约定）。本模块不认识
    抓包状态，无法自查。
    """
    if not isinstance(plan, dict) or not plan.get("ok"):
        return ApplyResult(ok=False, error="计划无效（plan_import 未成功产出）")

    entries = plan.get("accounts") or []
    if not entries:
        return ApplyResult(ok=False, error="计划为空：没有要落库的账号")

    users = users_dir or _users_dir(profiles)
    os.makedirs(users, exist_ok=True)

    # ---- 阶段 1：建账号 + 解算 payload（此刻磁盘一个字节都还没动） ----
    prepared: List[Dict[str, Any]] = []
    created: List[str] = []
    try:
        for entry in entries:
            mode = entry.get("mode")
            if mode == MODE_NEW:
                prof = profiles.create(entry.get("name") or "")
                created.append(prof.id)
                pid, name = prof.id, prof.name
            else:
                pid = entry.get("target")
                if not pid:
                    raise ValueError("合并项缺少目标账号 id")
                prof = profiles.get(pid)
                if prof is None:
                    raise ValueError(f"目标账号不存在: {pid}")
                name = prof.name

            path = _account_file(users, pid)
            prev_text, existed = _snapshot(path)
            existing = None
            if prev_text is not None:
                try:
                    parsed = json.loads(prev_text)
                    existing = parsed if isinstance(parsed, dict) else None
                except Exception:
                    existing = None
            prepared.append({
                "pid": pid,
                "name": name,
                "mode": mode,
                "path": path,
                "prev_text": prev_text,
                "existed": existed,
                "existing": existing,
                "payload": build_payload(entry.get("write") or {}, existing),
            })
    except Exception as e:  # noqa: BLE001 —— 准备阶段失败：撤掉已建账号即可，磁盘未动
        logger.warning(f"互通导入：准备阶段失败，撤销本批新建账号: {e}")
        _undo_created(profiles, created)
        return ApplyResult(ok=False, error=f"准备阶段失败：{e}")

    # ---- 阶段 2：逐个 备份 → 原子写（journal 记写前状态，供逆序回滚） ----
    journal: List[Dict[str, Any]] = []
    written: List[Dict[str, Any]] = []
    try:
        for item in prepared:
            unchanged = item["existed"] and item["existing"] == item["payload"]
            if unchanged:
                # 幂等闸：与磁盘逐字相同 → 不写盘、不备份。否则「选中应用里的那一份」
                # 每次都会白造一个备份（既有 `_write_payload` 同款守卫）。
                written.append({
                    "pid": item["pid"], "name": item["name"], "mode": item["mode"],
                    "records": len(item["payload"]["records"]),
                    "backup": None, "changed": False,
                })
                continue

            backup = None
            if item["existed"] and (item["existing"] or {}).get("records"):
                # 有非空历史才留用户可见的备份（沿用既有规约，避免备份泛滥）
                backup = _backup_copy(item["path"], item["pid"], users)
            item["backup"] = backup  # 回滚成功时要把本批自己造的备份一并清掉

            journal.append(item)  # 先入账再写：写一半失败也能被回滚覆盖
            text = json.dumps(item["payload"], ensure_ascii=False, indent=2)
            _atomic_write_text(item["path"], text)

            written.append({
                "pid": item["pid"], "name": item["name"], "mode": item["mode"],
                "records": len(item["payload"]["records"]),
                "backup": backup, "changed": True,
            })
    except Exception as e:  # noqa: BLE001 —— 任一账号失败 ⇒ 整组回滚
        logger.warning(f"互通导入：写入失败，开始整组回滚: {e}")
        _rollback(journal)
        _undo_created(profiles, created)
        return ApplyResult(ok=False, error=f"写入失败，已全部回滚：{e}")

    logger.info(
        "互通导入完成：%d 个账号（新建 %d），实际写盘 %d 个"
        % (len(written), len(created), sum(1 for w in written if w["changed"]))
    )
    return ApplyResult(ok=True, written=written, created=created)
