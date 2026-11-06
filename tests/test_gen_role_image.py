# -*- coding: utf-8 -*-
"""tools/gen_role_image.py 单测 - 表解析校验规则与真实全表护栏（2026-10-13）。"""
import importlib.util
import os
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gen_role_image",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "gen_role_image.py"),
)
gen_role_image = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen_role_image)


def _write_table(tmp_path, rows):
    p = tmp_path / "词条库全表.md"
    lines = ["# 表", "", "| 角色 | 库序（1→12） |", "|---|---|"]
    lines += rows
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _row(cid="90000001", entries="1决胜S 2孤注一掷A 3攻其不备A 4暴伤A 5攻击B 6暴击B 7普攻B 8生命B 9攻击C 10生命C 11防御C 12增伤C"):
    return f"| {cid} | {entries} |"


def test_parse_valid(tmp_path):
    libs = gen_role_image.parse_table(_write_table(tmp_path, [_row()]))
    assert libs["90000001"][0] == {"seq": 1, "name": "决胜", "grade": "S"}
    assert len(libs["90000001"]) == 12
    assert [it["seq"] for it in libs["90000001"]] == list(range(1, 13))


def test_S_absent_A_leads(tmp_path):
    """S 档缺席角色 A 档顶到 1 号位（如 90000001），单调校验须放行。"""
    row = _row(entries="1留一地死灰A 2乘胜追击A 3攻其不备A 4暴击A 5攻击B 6暴击B 7普攻B 8生命B 9攻击C 10生命C 11防御C 12增伤C")
    libs = gen_role_image.parse_table(_write_table(tmp_path, [row]))
    assert libs["90000001"][0] == {"seq": 1, "name": "留一地死灰", "grade": "A"}


def test_entry_count_error(tmp_path):
    with pytest.raises(ValueError, match="11 条"):
        gen_role_image.parse_table(_write_table(tmp_path, [_row(entries="1决胜S 2孤注一掷A 3攻其不备A 4暴伤A 5攻击B 6暴击B 7普攻B 8生命B 9攻击C 10生命C 11防御C")]))


def test_seq_not_full_range(tmp_path):
    with pytest.raises(ValueError, match="1..12"):
        gen_role_image.parse_table(_write_table(tmp_path, [_row(entries="1决胜S 2孤注一掷A 3攻其不备A 4暴伤A 5攻击B 6暴击B 7普攻B 8生命B 9攻击C 10生命C 11防御C 11增伤C")]))


def test_grade_not_monotonic(tmp_path):
    with pytest.raises(ValueError, match="非单调"):
        gen_role_image.parse_table(_write_table(tmp_path, [_row(entries="1决胜S 2攻击B 3孤注一掷A 4暴伤A 5攻击B 6暴击B 7普攻B 8生命B 9攻击C 10生命C 11防御C 12增伤C")]))


def test_duplicate_cid(tmp_path):
    with pytest.raises(ValueError, match="重复"):
        gen_role_image.parse_table(_write_table(tmp_path, [_row(), _row()]))


@pytest.mark.skip(reason="词条库全表.md 已迁 example-key-dump-research 仓 文档/（2026-10-18 文件迁移），"
                         "练度功能亦已下线（2026-10-19）；全表护栏在研究仓维护")
def test_real_table_guard():
    """真实全表护栏：当前仓库内的 词条库全表.md 必须可解析且无硬错误。"""
    libs = gen_role_image.parse_table()
    assert len(libs) >= 48
    assert all(len(v) == 12 for v in libs.values())
