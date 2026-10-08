# -*- coding: utf-8 -*-
"""跨端抽卡记录文件互通 —— **纯函数层**（解析 / 比对 / 计划 / 封装）。

设计依据
--------
- 格式与语义：`数据互通-设计稿-2026-09-20.md`（**v3.6**）§3 / §4
- 函数契约与错误码：`U1互通层-契约冻结与实现定案-2026-09-21.md` §2~§4
- 上游背景：`U1互通层-开工前设计完备性核查-2026-09-21.md`

本模块**不做任何 IO**
--------------------
不读文件、不写文件、不取当前时间、不 import Qt、不接触任何路径。调用方负责：

1. 读出文件文本 → `parse(text)`
2. 从 `HistoryStore` 取出设备侧同名账号现值 → 组成 `device_accounts`
3. UI 收集用户选择 → `choices`（初始值可用 `default_choices()`）
4. `plan_import(parsed, device_accounts, choices)` → `SyncPlan`
5. 用户确认后按计划**一次提交**（整组原子回滚，见定案稿 §6）

这样切分的理由：本层能在 `C:/Python/python.exe`（3.11，带 pytest）下跑**真单测**；
Android 侧同名同义的 `SyncFile.kt` 是它的逐行镜像（Kotlin 侧同样不碰 IO）。

与本地账号文件的关系
--------------------
互通文件是**独立信封**（`jinlin-records` v1），**不是** `users/<id>.json` 的兼容格式。
两者的 `records[]` 逐字同构（5 键），但顶层键与语义不同 ⇒ 两份账号文件**不可直接互换**。

内部结构的键名一律 **snake_case**（与信封 JSON 同构，两端零映射层）。
"""
import json
from collections import Counter
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 常量（错误码两端**同名同值**；不设 OK 常量，用 ok/error 表达）
# ---------------------------------------------------------------------------

FORMAT_NAME = "jinlin-records"
FORMAT_VERSION = 1

ERR_BAD_FORMAT = "BAD_FORMAT"
ERR_VERSION_TOO_HIGH = "VERSION_TOO_HIGH"
ERR_NO_ACCOUNTS = "NO_ACCOUNTS"
ERR_CORRUPT = "CORRUPT"

# 记录**身份键**：缺任一 ⇒ 算不出身份 ⇒ 整份文件 CORRUPT（定案稿 §4）
IDENTITY_KEYS = ("pool_id", "item_id", "timestamp")
# 坐标键：缺 ⇒ 按 0 补，**不报错**（坐标只在事件内部比较，补 0 不影响完整性判定）
COORD_KEYS = ("batch_seq", "position_in_batch")

# `ImportChoice` 的字段名（两端同名）
INDEX_KEY = "index"
MODE_KEY = "mode"
TARGET_KEY = "target"
CHOSEN_KEY = "chosen"

MODE_NEW = "NEW"
MODE_MERGE = "MERGE"
# 「不导入这个用户」第三态：该账号整份不进计划（手机侧也不动）。
# ⚠️ 刻意**不叫** `DISCARD` —— `PlannedAccount.discard` 是「合并时败方独有被丢弃的条数」，
# 前提是**该账号要导入**；两者同词不同义，读代码会错。
MODE_SKIP = "SKIP"

CHOSEN_FILE = "FILE"
CHOSEN_DEVICE = "DEVICE"

NEWER_FILE = "FILE"
NEWER_DEVICE = "DEVICE"
NEWER_TIE = "TIE"

# 合法事件规模（与 `analysis/pos_align.py` 的 `VALID_EVENT_SIZES` 同源语义）
VALID_EVENT_SIZES = (1, 10)

# 文件账号缺 `name` 时的降级名（归并键缺失不丢记录，仅展示层兜底）
FALLBACK_NAME = "未命名"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _err(code: str, detail: Optional[str] = None) -> Dict[str, Any]:
    """构造失败结果（`accounts` 恒为空数组，**不返回半份数据**）。"""
    return {
        "ok": False,
        "error": {"code": code, "detail": detail},
        "source": None,
        "exported_at": None,
        "accounts": [],
    }


def identity(record: Dict[str, Any]) -> tuple:
    """记录身份键 `(pool_id, item_id, timestamp)`。

    ⚠️ 十连里同名角色会让身份键**重复**（实测数据常见）⇒ 统计三集合时必须按
    **多重集**计数（见 `compare`），不能退化成 set。
    """
    return (record.get("pool_id"), record.get("item_id"), record.get("timestamp"))


def _as_int(value: Any) -> Optional[int]:
    """宽容取整：`None` 原样返回；转不动也返回 `None`（不报错）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _view_totals(raw: Any) -> Dict[str, int]:
    """`view_totals` 规范化：键转 str，值转 int；坏值**跳过**该键（不报错）。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        iv = _as_int(v)
        if iv is not None:
            out[str(k)] = iv
    return out


# ---------------------------------------------------------------------------
# ① parse —— 文件文本 → ParsedFile
# ---------------------------------------------------------------------------

def parse(text: Any) -> Dict[str, Any]:
    """解析互通文件文本。

    Args:
        text: JSON 文本（`str` / `bytes`）。**不要传已解析的 dict** —— 本函数是
            两端逐字对齐的入口，Android 侧同样只吃字符串。

    Returns:
        `ParsedFile`：成功 `{"ok": True, "error": None, "source", "exported_at", "accounts"}`；
        失败 `{"ok": False, "error": {"code","detail"}, "source": None, "exported_at": None,
        "accounts": []}`。错误码见模块常量。

    边界（定案稿 §4 逐条落实）:
        - 空字节 / 非 JSON / 根不是对象 / `format` 不符        ⇒ `BAD_FORMAT`
        - `version` 缺失 ⇒ 视为 1（宽容）；`version > 1`       ⇒ `VERSION_TOO_HIGH`
        - `accounts` 缺失或 `[]`                               ⇒ `NO_ACCOUNTS`
        - 某账号缺 `records`／某条记录缺身份键                 ⇒ `CORRUPT`
        - 记录缺坐标键 ⇒ **按 0 补**；`name` 缺失 ⇒ 降级名；`total` 坏值 ⇒ `None`
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            return _err(ERR_BAD_FORMAT, "not-utf8")
    if not isinstance(text, str):
        return _err(ERR_BAD_FORMAT, "not-text")

    try:
        obj = json.loads(text)
    except Exception as exc:  # noqa: BLE001 —— 任何解析异常都归为格式错
        return _err(ERR_BAD_FORMAT, "json: %s" % exc)

    if not isinstance(obj, dict):
        return _err(ERR_BAD_FORMAT, "root-not-object")

    if obj.get("format") != FORMAT_NAME:
        return _err(ERR_BAD_FORMAT, "format=%r" % (obj.get("format"),))

    version = _as_int(obj.get("version", FORMAT_VERSION))
    if version is None:
        return _err(ERR_BAD_FORMAT, "version=%r" % (obj.get("version"),))
    if version > FORMAT_VERSION:
        return _err(ERR_VERSION_TOO_HIGH, "version=%d" % version)

    raw_accounts = obj.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        return _err(ERR_NO_ACCOUNTS, None)

    accounts: List[Dict[str, Any]] = []
    for ai, raw_account in enumerate(raw_accounts):
        if not isinstance(raw_account, dict):
            return _err(ERR_CORRUPT, "account#%d: not-object" % ai)

        raw_records = raw_account.get("records")
        if not isinstance(raw_records, list):
            return _err(ERR_CORRUPT, "account#%d: no-records" % ai)

        records: List[Dict[str, Any]] = []
        for ri, raw_record in enumerate(raw_records):
            if not isinstance(raw_record, dict):
                return _err(ERR_CORRUPT, "account#%d record#%d: not-object" % (ai, ri))
            if any(k not in raw_record for k in IDENTITY_KEYS):
                return _err(ERR_CORRUPT, "account#%d record#%d: no-identity" % (ai, ri))
            # 5 键逐字同构：身份键原样，坐标键缺则补 0
            record = {k: raw_record[k] for k in IDENTITY_KEYS}
            for k in COORD_KEYS:
                record[k] = raw_record.get(k, 0)
            records.append(record)

        name = raw_account.get("name")
        if not isinstance(name, str) or not name:
            name = FALLBACK_NAME

        total = _as_int(raw_account.get("total"))

        account: Dict[str, Any] = {
            "name": name,
            "account_id": raw_account.get("account_id"),
            "records": records,
            "total": total,
            "view_totals": _view_totals(raw_account.get("view_totals")),
            "events": raw_account.get("events") if isinstance(raw_account.get("events"), list) else None,
            "integrity": raw_account.get("integrity") if isinstance(raw_account.get("integrity"), dict) else None,
        }
        accounts.append(account)

    return {
        "ok": True,
        "error": None,
        "source": obj.get("source") if isinstance(obj.get("source"), dict) else None,
        "exported_at": obj.get("exported_at") if isinstance(obj.get("exported_at"), str) else None,
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# ② compare —— 设备侧 × 文件侧 → 三集合预览
# ---------------------------------------------------------------------------

def compare(device_records: List[Dict[str, Any]],
            file_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """比对两侧记录，产出 Step2 预览所需的全部数字。

    Args:
        device_records: 设备侧**那个同名账号**的记录（与 UI、与文件整体无关）。
        file_records: 文件侧同账号的记录。

    Returns:
        `CompareResult`：
        `both` / `device_only` / `file_only`（**多重集**计数，见下）、
        `device_count` / `file_count`（原始条数）、
        `device_max_ts` / `file_max_ts`（`None` = 该侧无记录）、
        `newer`（`FILE` | `DEVICE` | `TIE`，用于 Step2 默认选中项）。

    ⚠️ **三集合用多重集而非集合**：十连里同名角色（同 pool 同 item 同 ts）合法存在，
        退化成 set 会把 `both` 少算、破坏 `both + device_only == device_count` 的守恒。
    """
    device_counter = Counter(identity(r) for r in device_records)
    file_counter = Counter(identity(r) for r in file_records)

    both = sum(min(device_counter[k], file_counter[k])
               for k in set(device_counter) | set(file_counter))
    device_count = sum(device_counter.values())
    file_count = sum(file_counter.values())

    device_max_ts = max((r.get("timestamp") for r in device_records), default=None)
    file_max_ts = max((r.get("timestamp") for r in file_records), default=None)

    if device_max_ts is None and file_max_ts is None:
        newer = NEWER_TIE
    elif file_max_ts is None:
        newer = NEWER_DEVICE
    elif device_max_ts is None:
        newer = NEWER_FILE
    elif file_max_ts > device_max_ts:
        newer = NEWER_FILE
    elif device_max_ts > file_max_ts:
        newer = NEWER_DEVICE
    else:
        newer = NEWER_TIE

    return {
        "both": both,
        "device_only": device_count - both,
        "file_only": file_count - both,
        "device_count": device_count,
        "file_count": file_count,
        "device_max_ts": device_max_ts,
        "file_max_ts": file_max_ts,
        "newer": newer,
    }


def find_device_account(device_accounts: List[Dict[str, Any]], name: str) -> int:
    """按 `name` 找设备侧同名账号，返回下标；找不到返回 -1。

    归并键是 `name`（**不是** `account_id` —— 两端各自随机，必然不同）。
    文件内多个账号同名时，`parse` 的下标已能区分；此处只做「文件账号 → 设备侧同名」。
    """
    for i, account in enumerate(device_accounts):
        if account.get("name") == name:
            return i
    return -1


# ---------------------------------------------------------------------------
# ③ default_choices —— UI Step1 的初始选择
# ---------------------------------------------------------------------------

def default_choices(parsed: Dict[str, Any],
                    device_accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为文件里每个账号给出**初始选择**（用户可在 UI 改）。

    - 设备侧**无**同名账号 ⇒ `mode=NEW`（`target=None`，`chosen` 恒 `FILE`）
    - 设备侧**有**同名账号 ⇒ `mode=MERGE`，`chosen` 取 `compare().newer`
      （`FILE` → 用文件、`DEVICE` → 用手机、`TIE` → 默认**用文件**：用户的动作是拿来导入）

    Args:
        parsed: `parse()` 的成功结果。
        device_accounts: 设备侧全部账号现值（每项至少含 `id` / `name` / `records`）。
    """
    choices: List[Dict[str, Any]] = []
    for index, account in enumerate(parsed.get("accounts") or []):
        di = find_device_account(device_accounts, account["name"])
        if di < 0:
            choices.append({INDEX_KEY: index, MODE_KEY: MODE_NEW, TARGET_KEY: None, CHOSEN_KEY: CHOSEN_FILE})
            continue
        device = device_accounts[di]
        result = compare(device.get("records") or [], account["records"])
        chosen = CHOSEN_DEVICE if result["newer"] == NEWER_DEVICE else CHOSEN_FILE
        choices.append({
            INDEX_KEY: index,
            MODE_KEY: MODE_MERGE,
            TARGET_KEY: device.get("id"),
            CHOSEN_KEY: chosen,
        })
    return choices


# ---------------------------------------------------------------------------
# ④ plan_import —— UI 选择 → 落库执行单（整份文件全部账号）
# ---------------------------------------------------------------------------

def plan_import(parsed: Dict[str, Any],
                device_accounts: List[Dict[str, Any]],
                choices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把「文件 + 设备现值 + 用户选择」解算成**落库执行单**。

    Args:
        parsed: `parse()` 的成功结果。
        device_accounts: 设备侧全部账号现值，每项形如
            `{"id": str, "name": str, "records": [...], "total": int|None,
              "view_totals": {...}}`。
        choices: `ImportChoice` 列表，与 `parsed["accounts"]` **一一对应**（同一顺序、同长度）。
            `{"index": int, "mode": "NEW"|"MERGE"|"SKIP", "target": str|None, "chosen": "FILE"|"DEVICE"}`

    Returns:
        `SyncPlan`：`{"ok": True, "error": None, "accounts": [...]}`。
        每个账号项含 `index` / `name` / `mode` / `target` / `chosen` /
        **`write`**（最终落盘三件套 `records` / `total` / `view_totals`）/ **`discard`**（将丢弃条数）。

    落库取值矩阵（定案稿 §2.6）:
        - `mode=NEW`                    ⇒ `write` 全部来自**文件**
        - `mode=MERGE` & `chosen=FILE`  ⇒ `write` 全部来自**文件**
        - `mode=MERGE` & `chosen=DEVICE`⇒ `write` 全部来自**设备现值**（实为 no-op，但**必须在计划里**，
          否则整组回滚时集合不完整）
        - `mode=SKIP`                   ⇒ **该账号不进计划**：文件里的不写进应用，应用里原有的也不动；
          `target` / `chosen` **一律忽略且不校验**（但 UI 不清空它们，见定案稿 §6.2-c）

    ⚠️ `SKIP` 是唯一**不产出账号项**的 mode ⇒ `len(plan["accounts"])` 不再等于文件账号数。
        `PlannedAccount.index` 保留**原文件下标**（文件 3 个账号跳过第 2 个 ⇒ 计划里是 `0` 与 `2`）。

    Raises:
        ValueError: `choices` 与文件账号数量/顺序不符、缺字段、`MERGE` 无合法 `target`
            —— 属**调用方 bug**，不是文件错误，故不套用文件错误码（两端同名同义：Kotlin 抛
            `IllegalArgumentException`）。校验放在本层，保证「计划一旦产出即可无条件执行」。
    """
    file_accounts = parsed.get("accounts") or []
    if len(choices) != len(file_accounts):
        raise ValueError("choices 数量 %d 与文件账号数 %d 不符" % (len(choices), len(file_accounts)))

    device_by_id = {a.get("id"): a for a in device_accounts}

    planned: List[Dict[str, Any]] = []
    for file_index, account in enumerate(file_accounts):
        choice = choices[file_index]
        if choice.get(INDEX_KEY) not in (None, file_index):
            raise ValueError("choices[%d].index=%r 与位置不符" % (file_index, choice.get(INDEX_KEY)))

        mode = choice.get(MODE_KEY)
        chosen = choice.get(CHOSEN_KEY, CHOSEN_FILE)
        target = choice.get(TARGET_KEY)

        if mode not in (MODE_NEW, MODE_MERGE, MODE_SKIP):
            raise ValueError("choices[%d].mode=%r 非法" % (file_index, mode))

        # SKIP ⇒ 该账号整份不导入：不进计划。
        # ⚠️ `continue` 必须在上面 mode 白名单校验**之后** —— 放前面会把「非法 mode」
        # 静默吞掉，正好违反「校验放在纯函数层，保证计划一旦产出即可无条件执行」的初衷。
        if mode == MODE_SKIP:
            continue

        file_bundle = {
            "records": [dict(r) for r in account["records"]],
            "total": account.get("total"),
            "view_totals": dict(account.get("view_totals") or {}),
        }

        if mode == MODE_NEW:
            write = file_bundle
            discard = 0
            target = None
        else:
            if chosen not in (CHOSEN_FILE, CHOSEN_DEVICE):
                raise ValueError("choices[%d].chosen=%r 非法" % (file_index, chosen))
            device = device_by_id.get(target)
            if device is None:
                raise ValueError("choices[%d].target=%r 不在设备账号里" % (file_index, target))
            device_bundle = {
                "records": [dict(r) for r in (device.get("records") or [])],
                "total": device.get("total"),
                "view_totals": dict(device.get("view_totals") or {}),
            }
            result = compare(device_bundle["records"], file_bundle["records"])
            if chosen == CHOSEN_FILE:
                write = file_bundle
                discard = result["device_only"]   # 丢弃手机侧独有
            else:
                write = device_bundle
                discard = result["file_only"]     # 丢弃文件侧独有

        planned.append({
            "index": file_index,
            "name": account["name"],
            "mode": mode,
            "target": target,
            "chosen": chosen,
            "write": write,
            "discard": discard,
        })

    return {"ok": True, "error": None, "accounts": planned}


# ---------------------------------------------------------------------------
# ⑤ 导出侧：事件分析 / 自检 / 信封封装
# ---------------------------------------------------------------------------

def analyze_events(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 `timestamp` 分组，产出 `events[]`（定案稿 §3.2 规则，两端逐字一致）。

    ```
    按 ts 给 records 分组（按 records 的**原始顺序**首次出现为准），对每一组：
      coord    = 组内 position_in_batch 互不相同 ? "absolute" : "page"
      complete = (组内条数 == 1 或 10)
    ```

    `ids` 指向 `records[]` 的下标（从 0 开始）。
    ⚠️ 本函数**不写进本端账号文件** —— `events[]` 只是给导入端读的元数据，
    导入完成后本端按自己的方式重建（PC `HistoryStore._analyze_events()` / Android `PositionAlign`）。
    """
    order: List[int] = []
    groups: Dict[int, List[int]] = {}
    for i, record in enumerate(records):
        ts = record.get("timestamp")
        if ts not in groups:
            groups[ts] = []
            order.append(ts)
        groups[ts].append(i)

    events: List[Dict[str, Any]] = []
    for ts in order:
        ids = groups[ts]
        positions = [records[i].get("position_in_batch") for i in ids]
        count = len(ids)
        events.append({
            "ts": ts,
            "count": count,
            "ids": list(ids),
            "coord": "absolute" if len(set(positions)) == count else "page",
            "complete": count in VALID_EVENT_SIZES,
        })
    return events


def compute_integrity(records: List[Dict[str, Any]],
                      events: List[Dict[str, Any]],
                      total: Optional[int]) -> Dict[str, Any]:
    """导出方自检结论（定案稿 §3.3）。**只能由导出方生成** —— 导入方无法事后反推。"""
    incomplete = [e for e in events if not e.get("complete")]
    return {
        "record_count": len(records),
        "authoritative_total": total,
        "count_matches": total is not None and total == len(records),
        "event_count": len(events),
        "incomplete_events": len(incomplete),
        "partial_records": sum(int(e.get("count") or 0) for e in incomplete),
        "has_anchor": total is not None,
    }


def build_export_account(account_id: str,
                         name: str,
                         records: List[Dict[str, Any]],
                         total: Optional[int],
                         view_totals: Optional[Dict[str, Any]] = None,
                         source_total: Optional[int] = None) -> Dict[str, Any]:
    """封装一个导出账号（`records[]` 逐字搬运，`events`/`integrity` 现算）。

    Args:
        records: 该账号要导出的记录（**原样**，含坐标的「脏」也如实保留）。
        total: 跟随导出的权威总抽数（丢了它导入端就失去 Δ 基准）。
        view_totals: 视图总抽数；`None` 视为 `{}`。
        source_total: 计算 `integrity.has_anchor` 用；缺省同 `total`。
    """
    recs = [dict(r) for r in records]
    events = analyze_events(recs)
    anchor: Optional[int] = total if source_total is None else source_total
    return {
        "account_id": account_id,
        "name": name,
        "total": total,
        "view_totals": dict(view_totals or {}),
        "records": recs,
        "events": events,
        "integrity": compute_integrity(recs, events, anchor),
    }


def build_envelope(accounts: List[Dict[str, Any]],
                   exported_at: str,
                   source: Dict[str, Any]) -> Dict[str, Any]:
    """封装整份互通信封（顶层 4 键 + `accounts`）。**不取时钟** —— `exported_at` 由调用方注入。"""
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": exported_at,
        "source": dict(source),
        "accounts": accounts,
    }


def dumps(envelope: Dict[str, Any]) -> str:
    """序列化为文件文本（`indent=2` + 末尾换行，与 PC 既有 `json.dump` 风格一致）。

    ⚠️ 两端必须用**同一套序列化参数**，否则「两端样本逐字节相同」不成立。
    Android 侧 `MiniJson` 只负责**读**，写由 Kotlin 侧自建（缩进风格需与本文一致）。
    """
    return json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
