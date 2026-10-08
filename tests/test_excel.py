"""Excel 导出测试：保底追踪 6 星命中行的标记。

新手/常驻池（非 featured）没有「歪」的概念，6 星出货直接写角色名；
UP/遴选池才写「命中UP」/「歪了 -> 名」（与 GUI 面板语义一致）。
"""

import tempfile
import os
from pathlib import Path

import openpyxl
import pytest

from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.exporters.excel import export_excel
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool


def make_engine():
    characters = {
        # UP 池 6 星角色 + 常驻歪角 + 新手池出货
        "601": Character("601", "UP角色·甲", 6, True),
        "602": Character("602", "常驻角色·乙", 6, False),
    }
    pools = {
        "30001": Pool("30001", "UP池", "UP", 1, 6, "UP角色·甲", "shared:UP"),
        "10001": Pool("10001", "新手池", "新手", pity_group="solo:10001"),
    }
    groups = {"shared:UP": PityGroup("shared:UP", "UP池", 70, True),
              "solo:10001": PityGroup("solo:10001", "新手池", 30, False)}
    return StatsEngine(characters, pools, groups, set()), characters, pools


def export_and_read_hits(records):
    stats, characters, pools = make_engine()
    stats.add(records)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.xlsx")
        export_excel(stats.records, characters, pools, path, stats=stats)
        wb = openpyxl.load_workbook(path)
    # 收集统计表 C 列的 6 星命中行
    lines = [str(c.value) for row in wb["统计"].iter_rows() for c in row
             if c.value and str(c.value).startswith("第")]
    return lines


def test_solo_pool_six_star_no_gone_wrong_mark():
    """新手/常驻池 6 星不写「歪了」，直接写角色名。"""
    lines = export_and_read_hits([
        GachaRecord("10001", "602", 1000),  # 新手池出常驻 6 星
    ])
    assert lines == ["第1抽(新手池) -> 常驻角色·乙"]


def test_featured_pool_keeps_up_and_wrong_marks():
    """UP 池保留 命中UP / 歪了 语义。"""
    lines = export_and_read_hits([
        GachaRecord("30001", "602", 1000),  # 歪
        GachaRecord("30001", "601", 2000),  # 命中 UP
    ])
    # 连续两个 6 星，各自命中后保底归零，pity 计数均为 1
    assert "第1抽(UP池) -> 歪了 -> 常驻角色·乙" in lines
    assert "第1抽(UP池) -> 命中UP" in lines
