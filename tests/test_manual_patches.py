"""手动补齐缺失历史测试 - 可编辑槽推导 + 合成记录注入 + 影子引擎。

覆盖：compute_editable 前缀/锚点判定、synthesize_records 时间序与填充-六星
推进、build_patched_engine 不动真实 records、填充不污染分布、补丁损坏回退。
冻结算法 shared_pity_tracking 仅作为消费方被断言，不改动。
"""
import json

import pytest

from gacha_exporter.analysis import manual_patches as mp
from gacha_exporter.analysis.manual_patches import (
    build_patched_engine,
    compute_editable,
    synthesize_records,
)
from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool
from gacha_exporter.storage import pool_patches as pp


# ---- 夹具 ----
def _record(pool_id: str, item_id: str, ts: int, pos: int) -> GachaRecord:
    return GachaRecord(pool_id=pool_id, item_id=item_id, timestamp=ts,
                       batch_seq=0, position_in_batch=pos)


def _pools():
    """30001/30002/30003 同属 UP 共享组，order 各不同（越旧 order 越小）。"""
    return {
        "30001": Pool(id="30001", name="UP1", type="UP", order=1, up_character="角色壹",
                      pity_group="shared:UP"),
        "30002": Pool(id="30002", name="遴选1", type="遴选", order=2, up_character="角色贰",
                      pity_group="shared:UP"),
        "30003": Pool(id="30003", name="UP2", type="UP", order=3, up_character="角色叁",
                      pity_group="shared:UP"),
        "31001": Pool(id="31001", name="新手", type="新手", order=1,
                      pity_group="solo:新手"),
    }


def _groups():
    return {
        "shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True),
        "solo:新手": PityGroup(id="solo:新手", label="新手池", hard_pity=10, has_state=False),
    }


def _chars():
    return {
        "0001": Character(id="0001", name="角色壹", rarity=6, limited=True),
        "0002": Character(id="0002", name="角色贰", rarity=6, limited=True),
        "0003": Character(id="0003", name="角色叁", rarity=6, limited=True),
        "4001": Character(id="4001", name="四星路人", rarity=4),
    }


def _real_records():
    """30003 有完整真实记录：25 垫 → R(6星) → 5 垫，ts 递增。"""
    recs = []
    t0 = 1_800_000_000_000
    for i, item in enumerate([*(["4001"] * 25), "0003", *(["4001"] * 5)]):
        recs.append(_record("30003", item, t0 + i * 1000, 10_000_000 - i))
    return recs, t0


# ---- compute_editable ----
def test_editable_anchor_and_prefix():
    pools, groups = _pools(), _groups()
    recs, _ = _real_records()
    ed = compute_editable(pools, groups, recs)
    up = ed["shared:UP"]
    # 30003 唯一有真实记录 -> 锚点；30001/30002 更旧无记录 -> 可补录
    assert up["has_record"] is True
    assert up["anchor"] == "30003"
    assert up["backfillable"] == ["30001", "30002"]
    assert up["backfill_names"]["30001"] == "UP1"
    assert up["backfill_names"]["30002"] == "遴选1"


def test_editable_middle_gap_not_backfillable():
    """中间断档（30002 无记录，但 30001/30003 有）不可补录 30002。"""
    pools, groups = _pools(), _groups()
    recs = [
        _record("30001", "0001", 1_700_000_000_000, 9_900_000),
        _record("30003", "0003", 1_800_000_000_000, 9_800_000),
    ]
    ed = compute_editable(pools, groups, recs)
    up = ed["shared:UP"]
    assert up["anchor"] == "30001"      # 有记录且最旧 = 锚点
    assert up["backfillable"] == []     # 30002 严格居中，非前缀，不可补


def test_editable_ignores_stale_order():
    """锚点/前缀按 pool_id 纯数字序，忽略已弃用的 order 脏值。

    复刻真实场景：30005 被误配 order=1（比 30003/30004 旧），但记录只存在于
    30003/30004/30005。纯 id 序下锚点应是 30003、可补前缀 30001+30002，
    而非被 30005 的脏 order 抢走锚点。
    """
    pools = {
        "30001": Pool(id="30001", name="UP1", type="UP", order=1, up_character="a",
                      pity_group="shared:UP"),
        "30002": Pool(id="30002", name="UP2", type="UP", order=2, up_character="b",
                      pity_group="shared:UP"),
        "30003": Pool(id="30003", name="UP3", type="UP", order=3, up_character="c",
                      pity_group="shared:UP"),
        "30004": Pool(id="30004", name="UP4", type="UP", order=4, up_character="d",
                      pity_group="shared:UP"),
        # 关键：30005 号最大（最新）却带脏 order=1
        "30005": Pool(id="30005", name="UP5", type="UP", order=1, up_character="e",
                      pity_group="shared:UP"),
    }
    groups = {"shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True)}
    # 模拟 sample30003：仅 30003/30004/30005 有真实记录
    recs = [
        _record("30003", "0001", 1_800_000_000_000, 9_900_000),
        _record("30004", "0002", 1_800_000_010_000, 9_800_000),
        _record("30005", "0003", 1_800_000_020_000, 9_700_000),
    ]
    ed = compute_editable(pools, groups, recs)
    up = ed["shared:UP"]
    assert up["anchor"] == "30003"                      # 纯 id 序 -> 30003 最旧有记录
    assert up["backfillable"] == ["30001", "30002"]     # 30001/30002 都比 30003 旧且无记录


def test_editable_group_without_records_closed():
    pools, groups = _pools(), _groups()
    ed = compute_editable(pools, groups, [])
    assert ed["shared:UP"]["has_record"] is False
    assert ed["shared:UP"]["anchor"] is None
    assert ed["shared:UP"]["backfillable"] == []


# ---- synthesize_records ----
def test_synthesize_order_and_bound(pools=_pools(), groups=_groups(),
                                    chars=_chars(), recs=None, patches=None):
    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, t0 = _real_records()
    patches = {
        # 补录池：各池内绝对抽数 -> 第 40 抽 c1、第 55 抽 c2
        "30001": {"items": [{"char": "0001", "draw": 40}]},
        "30002": {"items": [{"char": "0002", "draw": 55}]},
        # 锚点：30003 开头垫 10 抽
        "30003": {"leading": 10},
    }
    manual, aug, bound = synthesize_records(pools, chars, groups, patches, recs)
    # bound = 组最早真实 ts
    assert bound == {"shared:UP": t0}
    # 补录池 30001(更旧, 39+1) + 30002(54+1)，锚点 leading 10
    assert len(manual) == 39 + 1 + 54 + 1 + 10
    # 所有合成记录 ts 均严格落在最早真实 ts 之前
    assert all(r.timestamp < t0 for r in manual)
    # 时间序：按池新旧 -> 30001 全部 < 30002 全部 < 锚点 leading(仍 < t0)
    t30001 = [r.timestamp for r in manual if r.pool_id == "30001"]
    t30002 = [r.timestamp for r in manual if r.pool_id == "30002"]
    tlead = [r.timestamp for r in manual if r.pool_id == "30003"]
    assert max(t30001) < min(t30002) < max(t30002) < min(tlead) < t0
    # 单池内 ts 递增、position（越小越新）严格递减
    assert t30001 == sorted(t30001) and t30002 == sorted(t30002)
    # 六星命中条数 = items 数
    assert sum(1 for r in manual if r.item_id in ("0001", "0002")) == 2
    # aug_characters 扩增填充 sentinel（rarity=4 非六星）
    assert aug[pp.MANUAL_FILL_ID].rarity == 4


def test_synthesize_returns_real_untouched():
    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, _ = _real_records()
    real_copy = list(recs)
    _, aug, _ = synthesize_records(pools, chars, groups,
                                   {"30003": {"leading": 5}}, recs)
    assert recs == real_copy            # 入参未被就地修改


# ---- build_patched_engine + 统计 ----
def test_shadow_engine_does_not_mutate_real():
    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, t0 = _real_records()
    patches = {
        "30001": {"items": [{"char": "0001", "draw": 40}]},
        "30002": {"items": [{"char": "0002", "draw": 55}]},
        "30003": {"leading": 10},
    }
    engine = StatsEngine(chars, pools, groups)
    engine.load_records(recs)
    real_total = engine.total
    manual, aug, bound = synthesize_records(pools, chars, groups, patches, recs)
    shadow = build_patched_engine(engine, manual, aug)
    # 真实引擎记录数与历史 key 均不受影响
    assert engine.total == real_total
    assert shadow.total == real_total + len(manual)
    # 影子引擎记录 = 真实 + 手动，且顺序正确推进共享保底
    track = shadow.shared_pity_tracking()["shared:UP"]
    assert track["six_count"] == 3                       # c1@30001 + c2@30002 + 真实R
    assert track["current_pity"] == 5                    # R 归零后 5 垫
    # 「手」命中 = ts<bound：补录 c1/c2 是；真实 R 不是
    manual_hits = [h for h in track["hits"] if h["ts"] < bound["shared:UP"]]
    assert sorted(h["name"] for h in manual_hits) == ["角色壹", "角色贰"]
    assert any(h["name"] == "角色叁" and h["ts"] >= bound["shared:UP"]
               for h in track["hits"])


def test_fillers_excluded_from_distributions():
    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, _ = _real_records()
    patches = {
        "30001": {"items": [{"char": "0001", "draw": 40}]},
        "30003": {"leading": 10},
    }
    engine = StatsEngine(chars, pools, groups)
    engine.load_records(recs)
    manual, aug, bound = synthesize_records(pools, chars, groups, patches, recs)
    shadow = build_patched_engine(engine, manual, aug)
    rarity, unmapped = shadow.rarity_distribution()
    # 6 星 = c1 + 真实 R；填充(4001_手动)不算 6 星，映射为 4 但被过滤
    rd = dict(rarity)
    # 4 星：真实 r4 共 30 条（anchor 填充被过滤）
    dist = shadow.pool_distribution()
    assert rd.get(6) == 2
    assert rd.get(4) == 30
    assert dist["UP1"] == 1            # 仅 c1
    assert dist["UP2"] == 31           # 真实 25+R+5，leading 填充被过滤
    assert mp.MANUAL_FILL_ID not in unmapped


def test_default_pity_groups_ok():
    """pity_groups 缺省为 None 时也能合成（resolve_groups 兜底 solo）。"""
    pools = _pools()
    chars = _chars()
    recs, _ = _real_records()
    manual, aug, bound = synthesize_records(pools, chars, None,
                                            {"30003": {"leading": 3}}, recs)
    assert len(manual) == 3
    # 无分组配置 -> 每池兜底 solo；bound 落在该池的 solo 组
    assert "solo:30003" in bound


# ---- 存储 ----
def _patch_path(tmp_path, monkeypatch):
    p = str(tmp_path / "pool_patches.json")
    monkeypatch.setattr(pp, "_patches_path", lambda: p)
    return p


def test_storage_missing_empty(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    assert pp.load_pool_patches("ACC") == {}


def test_storage_roundtrip_and_isolation(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    pp.save_pool_patches("A", {"30003": {"leading": 10}})
    pp.save_pool_patches("B", {"30001": {"items": [{"char": "0001", "draw": 40}]}})
    assert pp.load_pool_patches("A") == {"30003": {"leading": 10, "items": []}}
    assert pp.load_pool_patches("B") == {
        "30001": {"leading": 0, "items": [{"char": "0001", "draw": 40}]}}
    # 账号隔离：B 看不到 A 的补丁
    assert "30003" not in pp.load_pool_patches("B")


def test_storage_rejects_invalid(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(ValueError):
        pp.save_pool_patches("A", {"30003": {"leading": -1}})
    with pytest.raises(ValueError):
        pp.save_pool_patches("A", {
            "30001": {"items": [{"char": "0001", "draw": 40},
                                {"char": "0002", "draw": 40}]}})


def test_storage_corrupt_falls_back(tmp_path, monkeypatch):
    _patch_path(tmp_path, monkeypatch)
    open(_patch_path(tmp_path, monkeypatch), "w", encoding="utf-8").write("not json{")
    assert pp.load_pool_patches("A") == {}


def test_storage_drops_invalid_pool_keeps_rest(tmp_path, monkeypatch):
    p = _patch_path(tmp_path, monkeypatch)
    pp.save_pool_patches("A", {"30003": {"leading": 5}})
    data = json.loads(open(p, encoding="utf-8").read())
    data["pool_patches"]["A"]["30001"] = {"leading": -9}   # 非法
    open(p, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    loaded = pp.load_pool_patches("A")
    assert "30001" not in loaded
    assert loaded["30003"]["leading"] == 5


# ---- MaintenanceDialog「还原此池」回归（offscreen） ----
def test_dialog_reset_pool_clears_editor_and_patch():
    """还原此池必须真正把补丁删干净。回归「还原后 _refresh 又把编辑器残留内容写回、
    补丁原样回来」的 bug（见 待办清单 §0.5 高项）。

    根因：_reset_pool 只删 _patches[pid]，随后 _refresh -> _gather_editor_into_patches
    会把仍旧显示的 spin/表格行重新写回补丁。修复=还原前先清空编辑器控件（blockSignals
    防回环）再删补丁。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from gacha_exporter.gui.maintenance_dialog import MaintenanceDialog

    QApplication.instance() or QApplication([])

    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, _ = _real_records()
    ed = compute_editable(pools, groups, recs)
    assert ed["shared:UP"]["anchor"] == "30003"
    assert ed["shared:UP"]["backfillable"] == ["30001", "30002"]

    # 给补录池 30001 一个既有补丁（引用其 UP 角色=合法候选；30001 UP=角色壹=0001）
    existing = {"30001": {"leading": 0, "items": [{"char": "0001", "draw": 3}]}}
    dlg = MaintenanceDialog(None, pools, chars, groups, recs,
                            editable=ed, existing=existing)
    # 已有补丁默认视为已保存
    assert dlg._pool_status("30001") == "saved"

    # 切到 30001 补录槽（构建表格编辑器载入既有项）
    slot = dlg._slot_map["backfill:30001"]
    dlg._select_slot(slot)
    assert dlg._backfill_table is not None
    assert dlg._backfill_table.rowCount() == 1
    # 还原前补丁在
    assert dlg._patches.get("30001")

    dlg._reset_pool("30001")

    # 补丁删除干净、编辑器清空、状态回到未填写
    assert not dlg._patches.get("30001") or not (
        dlg._patches.get("30001", {}).get("items") or
        dlg._patches.get("30001", {}).get("leading"))
    assert dlg._backfill_table.rowCount() == 0
    assert dlg._pool_status("30001") == "empty"

    # 锚点槽同理：还原后 leading 归 0、状态 empty
    a_slot = dlg._slot_map["anchor:30003"]
    dlg._select_slot(a_slot)
    assert dlg._leading_spin is not None
    dlg._reset_pool("30003")
    assert not (dlg._patches.get("30003", {}).get("leading"))
    assert dlg._pool_status("30003") == "empty"


def test_dialog_illegal_draw_recorded_not_silently_dropped():
    """【中】非法「第几抽」不能再静默丢行：有角色但 draw 非法的行，gather 时记入
    _draw_errors（供底部保存拦截），且不入 items。/ _validate_pool 只看到 items，
    靠 gather 记录才能拦住保存。见 待办清单 §0.5 中项。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from gacha_exporter.gui.maintenance_dialog import MaintenanceDialog

    QApplication.instance() or QApplication([])

    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, _ = _real_records()
    ed = compute_editable(pools, groups, recs)
    dlg = MaintenanceDialog(None, pools, chars, groups, recs, editable=ed, existing={})
    dlg._select_slot(dlg._slot_map["backfill:30001"])

    # 一行：有角色（合法候选=30001 的 UP 角色壹）但「第几抽」非法文本
    dlg._add_backfill_row("角色壹", 1)
    dlg._backfill_table.item(0, 1).setText("abc")

    dlg._gather_editor_into_patches()

    # 非法行不进 items（不污染算法），但被记录 -> 保存会拦截而非静默丢行
    assert dlg._patches["30001"]["items"] == []
    assert dlg._draw_errors.get("30001"), "非法行应记入 _draw_errors 供保存拦截"

    # 改成合法值 -> 记录清空、items 进入
    dlg._backfill_table.item(0, 1).setText("5")
    dlg._gather_editor_into_patches()
    assert not dlg._draw_errors.get("30001")
    assert [it["draw"] for it in dlg._patches["30001"]["items"]] == [5]


def test_dialog_illegal_draw_not_mislabeled_as_duplicate():
    """【低】重复判定误标：两行同类非法「第几抽」（解析失败）应都标「须为正整数」，
    不能把后者误标成「与另一条重复」。判重只对合法 draw。见 待办清单 §0.5 低项。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from gacha_exporter.gui.maintenance_dialog import MaintenanceDialog

    QApplication.instance() or QApplication([])

    pools, groups = _pools(), _groups()
    chars = _chars()
    recs, _ = _real_records()
    ed = compute_editable(pools, groups, recs)
    dlg = MaintenanceDialog(None, pools, chars, groups, recs, editable=ed, existing={})
    dlg._select_slot(dlg._slot_map["backfill:30001"])

    # 两行都输非法 draw 文本，角色都是合法候选（角色壹）
    dlg._add_backfill_row("角色壹", 1)
    dlg._backfill_table.item(0, 1).setText("abc")
    dlg._add_backfill_row("角色壹", 2)
    dlg._backfill_table.item(1, 1).setText("xyz")

    errs = dlg._backfill_row_errors()
    assert errs[0] == "第几抽须为正整数"
    assert errs[1] == "第几抽须为正整数"
    assert "与另一条重复" not in errs[0]
    assert "与另一条重复" not in errs[1]

    # 合法但重复的 draw 仍应判重
    dlg._backfill_table.item(0, 1).setText("5")
    dlg._backfill_table.item(1, 1).setText("5")
    errs = dlg._backfill_row_errors()
    assert "第几抽 5 与另一条重复" in errs[1]