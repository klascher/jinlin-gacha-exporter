"""HistoryService 测试 - Set-based 去重与增量合并（多账号 profile-aware）。"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from gacha_exporter.models import GachaRecord, SessionContext
from gacha_exporter.storage.history import HistoryService

_PID = "default"  # 测试用固定账号 id


def _record(pool_id: str, item_id: str, timestamp: int,
            pos: int = 0) -> GachaRecord:
    """构造一条记录。

    `pos` = `position_in_batch`（位置即身份，见位置对齐方案）：**同一事件内的
    多条记录必须给不同 pos**，否则会被判成同一个位置（真实数据里同一事件的
    pos 是连续且唯一的）。
    """
    return GachaRecord(
        pool_id=pool_id,
        item_id=item_id,
        timestamp=timestamp,
        batch_seq=0,
        position_in_batch=pos,
    )


def _backups(tmp):
    """返回该账号在 users/ 下的备份文件列表（不含数据文件 default.json）。"""
    return list(Path(tmp, "users").glob(f"{_PID}_*.json"))


def _write_payload(path, records):
    """把一份 `{"records": [...]}` 写盘（records 为 raw dict 列表）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, ensure_ascii=False)


def _record_dict(pool_id="20001", item_id="101", timestamp=1):
    return {
        "pool_id": pool_id,
        "item_id": item_id,
        "timestamp": timestamp,
        "batch_seq": 0,
        "position_in_batch": 0,
    }


def test_load_empty_history():
    """空历史目录应返回空记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            assert svc.records == []
            assert svc.is_empty()


def test_merge_adds_new_records():
    """merge 应追加历史中没有的记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            added = svc.merge([_record("20001", "101", 1), _record("20001", "102", 2)])
            assert added == 2
            assert len(svc.records) == 2


def test_merge_skips_duplicate_keys():
    """相同关键字的记录不应重复追加。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            svc.merge([_record("20001", "101", 1)])
            added = svc.merge([_record("20001", "101", 1), _record("20001", "102", 2)])
            assert added == 1
            assert len(svc.records) == 2


def test_merge_incremental_export():
    """增量导出：旧记录跳过，新记录追加。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            # 第一次完整导出
            svc.merge([
                _record("20001", "101", 1),
                _record("20001", "102", 2),
                _record("20001", "103", 3),
            ])
            # 第二次只抓到新记录 + 部分旧记录
            added = svc.merge([
                _record("20001", "104", 4),  # 新
                _record("20001", "103", 3),  # 旧，应跳过
                _record("20001", "102", 2),  # 旧，应跳过
            ])
            assert added == 1
            assert len(svc.records) == 4


def test_has_overlap():
    """has_overlap 应能检测给定记录是否与历史存在关键字重叠。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            svc.merge([_record("20001", "101", 1)])
            assert svc.has_overlap([_record("20001", "101", 1), _record("20001", "999", 9)])
            assert not svc.has_overlap([_record("20001", "999", 9)])


def test_persistence():
    """保存后重新加载应保留记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc1 = HistoryService(_PID)
            svc1.merge([_record("20001", "101", 1)])

            svc2 = HistoryService(_PID)
            assert len(svc2.records) == 1
            added = svc2.merge([_record("20001", "101", 1), _record("20001", "102", 2)])
            assert added == 1


def test_clear_wipes_memory_and_disk():
    """clear 应先备份再清空：磁盘写入空历史，备份保留旧记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            svc.merge([_record("20001", "101", 1), _record("20001", "102", 2)])
            assert len(svc.records) == 2

            backup = svc.clear()
            assert svc.is_empty()
            assert svc.records == []

            # 磁盘历史已清空（payload 另含 total 快照字段，见位置对齐方案）
            with open(svc.history_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            assert payload["records"] == []
            assert payload["total"] is None and payload["view_totals"] == {}

            # 返回了备份路径，备份存在且保留旧记录
            assert backup is not None
            backups = _backups(tmp)
            assert backups == [Path(backup)]
            with open(backup, "r", encoding="utf-8") as f:
                payload = json.load(f)
            assert len(payload["records"]) == 2


def test_clear_empty_history_makes_no_backup():
    """无历史记录时 clear 不应生成备份。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            backup = svc.clear()
            assert backup is None
            assert _backups(tmp) == []


def test_merge_backs_up_prior_state():
    """merge 写盘前应先备份上一份有效历史，备份保留旧记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            svc.merge([_record("20001", "101", 1)])
            # 首次写入无前序文件，不应产生备份
            assert _backups(tmp) == []

            svc.merge([_record("20001", "102", 2)])
            # 第二次写入前备份了上一份（仅 101）
            backups = _backups(tmp)
            assert len(backups) == 1
            with open(backups[0], "r", encoding="utf-8") as f:
                old = json.load(f)
            assert len(old["records"]) == 1
            assert old["records"][0]["item_id"] == "101"
            # 当前历史已合并为两条
            assert len(svc.records) == 2


def test_save_backs_up_prior_state():
    """save 覆盖写盘前同样应先备份。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            svc.merge([_record("20001", "101", 1)])
            svc.save(
                [_record("20001", "101", 1), _record("20001", "102", 2)],
                SessionContext(current_pool_id="all"),
            )
            backups = _backups(tmp)
            assert len(backups) == 1
            with open(backups[0], "r", encoding="utf-8") as f:
                old = json.load(f)
            assert len(old["records"]) == 1
            assert old["records"][0]["item_id"] == "101"


def test_backups_listing():
    """backups() 只列合规备份、按时间倒序、label 人类可读、records 计数正确。

    含一个坏名（时间戳不合法）和一个其它前缀文件，均不应列入。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td, "users")
        base.mkdir(parents=True, exist_ok=True)
        # 数据文件（不算备份）
        _write_payload(base / f"{_PID}.json", [_record_dict(item_id="999", timestamp=0)])
        # 三个合规备份（乱序写盘，期望按时间倒序返回）；其中两份同秒（_1/_2 后缀），
        # label 必须能区分，否则选择框永远选不中第二份
        _write_payload(base / f"{_PID}_20260815_120000.json", [_record_dict(item_id="101", timestamp=1)])
        _write_payload(base / f"{_PID}_20260817_215243_1.json",
                       [_record_dict(item_id="102", timestamp=2), _record_dict(item_id="103", timestamp=3)])
        _write_payload(base / f"{_PID}_20260817_215243_2.json", [_record_dict(item_id="104", timestamp=4)])
        _write_payload(base / f"{_PID}_20260816_080000.json", [_record_dict(item_id="105", timestamp=5)])
        # 坏名：时间戳第二位非数字 -> 应被跳过
        _write_payload(base / f"{_PID}_2026ab17_120000.json", [_record_dict(item_id="999")])
        # 其它账号前缀 -> 应被跳过
        _write_payload(base / "otheracct_20260815_120000.json", [_record_dict(item_id="888")])

        with patch("gacha_exporter.storage.history.get_users_dir", return_value=base):
            svc = HistoryService(_PID)
            infos = svc.backups()
        assert [b.filename for b in infos] == [
            f"{_PID}_20260817_215243_2.json",  # 同秒 _2 创建更晚，排在 _1 前
            f"{_PID}_20260817_215243_1.json",
            f"{_PID}_20260816_080000.json",
            f"{_PID}_20260815_120000.json",
        ]
        assert infos[0].records == 1
        assert infos[0].label == "2026-08-17 21:52:43（1 条） 同秒_2"
        assert infos[1].records == 2
        assert infos[1].label == "2026-08-17 21:52:43（2 条） 同秒_1"
        assert infos[2].records == 1
        assert infos[3].records == 1
        # 全部 label 互不相同（选择框可唯一区分）
        labels = [b.label for b in infos]
        assert len(set(labels)) == len(labels)


def test_restore_keeps_backup_and_swaps():
    """restore(B)：当前文件被备份为当天时间戳、内容被 B 覆盖、B 原件仍在。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td, "users")
        base.mkdir(parents=True, exist_ok=True)
        # 当前污染文件 A
        _write_payload(base / f"{_PID}.json", [_record_dict(item_id="101", timestamp=1)])
        # 干净旧备份 B
        backup_b = base / f"{_PID}_20260810_100000.json"
        _write_payload(backup_b, [_record_dict(item_id="202", timestamp=2)])

        with patch("gacha_exporter.storage.history.get_users_dir", return_value=base):
            svc = HistoryService(_PID)
            backup_cur = svc.restore(str(backup_b))

        # 还原前当前状态被备份为当天时间戳新文件，内容 = 原 A（101）
        today_prefix = f"{_PID}_{datetime.now():%Y%m%d}_"
        today_backups = [p for p in base.glob(f"{_PID}_*.json")
                         if p.name.startswith(today_prefix)]
        assert len(today_backups) == 1
        assert backup_cur == str(today_backups[0])
        with open(today_backups[0], "r", encoding="utf-8") as f:
            assert json.load(f)["records"][0]["item_id"] == "101"

        # 当前文件已覆盖为 B（202）
        with open(base / f"{_PID}.json", "r", encoding="utf-8") as f:
            assert json.load(f)["records"][0]["item_id"] == "202"

        # B 原件仍在
        assert backup_b.exists()


def test_restore_then_reload():
    """还原后以此账号新建 HistoryService 读到的是还原数据（心智模型）。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td, "users")
        base.mkdir(parents=True, exist_ok=True)
        _write_payload(base / f"{_PID}.json", [_record_dict(item_id="101", timestamp=1)])  # 污染 A
        backup_b = base / f"{_PID}_20260810_100000.json"
        _write_payload(backup_b, [_record_dict(item_id="202", timestamp=2)])  # 干净 B

        with patch("gacha_exporter.storage.history.get_users_dir", return_value=base):
            svc = HistoryService(_PID)
            svc.restore(str(backup_b))

            # 还原后本服务立刻可见还原内容
            assert [r.item_id for r in svc.records] == ["202"]
            # 新建服务（等价于重启后全新加载）读到同样还原内容
            svc2 = HistoryService(_PID)
            assert [r.item_id for r in svc2.records] == ["202"]


def test_profiles_are_isolated():
    """不同账号的历史相互隔离，互不串数据。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            a = HistoryService("acctA")
            b = HistoryService("acctB")
            a.merge([_record("20001", "101", 1)])
            b.merge([_record("20001", "202", 2)])
            assert len(a.records) == 1
            assert len(b.records) == 1
            assert a.records[0].item_id == "101"
            assert b.records[0].item_id == "202"
            # 重新加载互不干扰
            assert HistoryService("acctA").records[0].item_id == "101"
            assert HistoryService("acctB").records[0].item_id == "202"


def test_merge_keeps_ten_pull_same_name_duplicates():
    """十连同名重复（同 ts、不同位置）应全部保留，落盘不被合并成 1 条。

    位置对齐方案下同名不吞的依据正是**位置不同**（内容相同也一样能区分）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            # 一次十连抽到 101 两次 + 102 一次（同 timestamp，101 位置不同）
            added = svc.merge([
                _record("20001", "101", 5000, 100),
                _record("20001", "101", 5000, 105),
                _record("20001", "102", 5000, 106),
            ])
            assert added == 3
            assert len(svc.records) == 3  # 全部保留，未合并


def test_merge_recapture_dropped_but_ten_pull_dup_preserved():
    """重复抓的旧记录（位置命中历史）跳过；原有的十连同名重复保留。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            # 首次：十连抽到 101 两次（位置 100 / 105）
            svc.merge([
                _record("20001", "101", 5000, 100),
                _record("20001", "101", 5000, 105),
            ])
            assert len(svc.records) == 2

            # 重新加载（新会话，含 total 快照）
            svc2 = HistoryService(_PID)
            assert len(svc2.records) == 2
            # 再次抓到同一十连页(重复抓) + 一条新记录
            added = svc2.merge([
                _record("20001", "101", 5000, 100),  # 位置命中 -> 跳过
                _record("20001", "101", 5000, 105),  # 位置命中 -> 跳过
                _record("20001", "102", 6000, 100),  # 新事件
            ])
            assert added == 1
            assert len(svc2.records) == 3  # 原 2 条十连重复 + 1 新


def test_merge_no_change_skips_write_and_backup():
    """无新增的合并（内容零变化）→ 不写盘、不产生新备份（幂等）。

    背景：停止抓包/切账号/关窗都会走一次 merge，即使什么都没抓到（实时判重
    全部拦下）。旧实现的无新增路径仍 _write_payload → 对非空历史无条件先备份
    → 每次收尾都多一个备份文件（2026-09-13 实测 6 分钟 17 个）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with patch("gacha_exporter.storage.history.get_users_dir", return_value=Path(tmp, "users")):
            svc = HistoryService(_PID)
            # 第 1 次：空文件首写，无可备份 → 0 个备份
            svc.merge([_record("20001", "101", 1), _record("20001", "102", 2)])
            assert len(_backups(tmp)) == 0
            # 第 2 次：有新记录（真实收尾会带同样的 view_totals）→ 写盘 + 备份 1 份
            svc.merge([_record("20001", "103", 3)], {"0@0": 3})
            assert len(_backups(tmp)) == 1
            before = (Path(tmp, "users", f"{_PID}.json")).read_text(encoding="utf-8")

            # 第 3 次：什么都没抓到、total 快照也没变（如 stop 与 worker finished
            # 的第二次收尾）→ 内容零变化 → 不写盘、不备份
            added = svc.merge([], {"0@0": 3})
            assert added == 0
            assert len(_backups(tmp)) == 1, "无变化不应产生新备份"
            assert (Path(tmp, "users", f"{_PID}.json")).read_text(encoding="utf-8") == before
