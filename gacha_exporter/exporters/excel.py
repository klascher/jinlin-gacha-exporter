"""Excel导出 — 记录明细表 + 统计表。

函数式导出，不抽基类。将来加 CSV/JSON 发现共性再抽。
依赖：openpyxl、models、analysis.stats.StatsEngine、utils.helpers
"""
from __future__ import annotations  # 注解不求值，openpyxl 懒加载（启动慢，仅在导出时导入）

from datetime import datetime
from typing import Dict, List, Optional

from ..analysis.stats import StatsEngine
from ..models import Character, GachaRecord, Pool
from ..utils.helpers import format_timestamp


def export_excel(
    records: List[GachaRecord],
    characters: Dict[str, Character],
    pools: Dict[str, Pool],
    filepath: str,
    stats: Optional[StatsEngine] = None,
    manual_bound_by_group: Optional[Dict[str, int]] = None,
) -> str:
    """导出 Excel 文件。

    Args:
        records: 抽卡记录
        characters: 角色映射
        pools: 卡池映射
        filepath: 输出文件路径
        stats: 统计引擎，提供则生成统计表
        manual_bound_by_group: {group_id: 组最早真实 ts}，命中 ts<该值追加「(补)」

    Returns:
        filepath
    """
    import openpyxl

    wb = openpyxl.Workbook()
    _write_records_sheet(wb, records, characters, pools)
    if stats:
        _write_stats_sheet(wb, stats, manual_bound_by_group)
    wb.save(filepath)
    return filepath


def _write_records_sheet(
    wb: openpyxl.Workbook,
    records: List[GachaRecord],
    characters: Dict[str, Character],
    pools: Dict[str, Pool],
) -> None:
    """写记录明细表。

    表头：序号/时间/卡池/角色/稀有度（加粗白字蓝底）
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    ws = wb.active
    ws.title = "抽卡记录"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    headers = ["序号", "时间", "卡池", "角色", "稀有度"]
    for col, title in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for i, rec in enumerate(records, 1):
        char = characters.get(rec.item_id, Character.default(rec.item_id))
        pool = pools.get(rec.pool_id, Pool.default(rec.pool_id))
        ws.cell(row=i + 1, column=1, value=i)
        ws.cell(row=i + 1, column=2, value=format_timestamp(rec.timestamp))
        ws.cell(row=i + 1, column=3, value=pool.name)
        ws.cell(row=i + 1, column=4, value=char.name)
        ws.cell(row=i + 1, column=5, value=f"{char.rarity}星" if char.rarity else "?")

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 26
    ws.column_dimensions["E"].width = 8


def _write_stats_sheet(wb: openpyxl.Workbook, stats: StatsEngine,
                        manual_bound_by_group: Optional[Dict[str, int]] = None) -> None:
    """写统计表。

    含：总记录数、稀有度分布、卡池分布、保底追踪。
    manual_bound_by_group 非空时，命中 ts<该组边界（手动补齐）追加「(补)」标记。
    """
    from openpyxl.styles import Font

    ws = wb.create_sheet("统计")
    ws.cell(row=1, column=1, value=f"抽卡统计报告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"总记录数: {stats.total}")

    row = 4
    ws.cell(row, column=1, value="稀有度分布").font = Font(bold=True)
    row += 1
    rarity_dist, _ = stats.rarity_distribution()
    for rarity in [6, 5, 4]:
        cnt = rarity_dist.get(rarity, 0)
        ws.cell(row, column=1, value=f"{rarity}星")
        ws.cell(row, column=2, value=cnt)
        ws.cell(row, column=3, value=f"{cnt / max(stats.total, 1) * 100:.1f}%")
        row += 1

    row += 1
    ws.cell(row, column=1, value="卡池分布").font = Font(bold=True)
    row += 1
    for name, cnt in stats.pool_distribution().items():
        ws.cell(row, column=1, value=name)
        ws.cell(row, column=2, value=cnt)
        row += 1

    row += 1
    ws.cell(row, column=1, value="保底追踪").font = Font(bold=True)
    row += 1
    for gkey, info in stats.shared_pity_tracking().items():
        state_str = f" | {info['state']}" if info["state"] else ""
        ws.cell(row, column=1, value=f"[{info['label']}]")
        ws.cell(
            row,
            column=2,
            value=(
                f"{info['current_pity']}/{info['hard_pity']}（距保底{info['distance_to_pity']}抽）"
                f" | 组内{info['total']}抽 | 6星x{info['six_count']}{state_str}"
            ),
        )
        row += 1
        for hit in info["hits"]:
            # UP/遴选池（featured）才有「歪」的概念；新手/常驻池的 6 星只是
            # 普通出货，直接写角色名，与 GUI 面板一致（此前曾一律写「歪了」）
            pool = stats.pools.get(hit["pool_id"])
            if pool is not None and pool.is_featured:
                mark = "命中UP" if hit["is_up"] else f"歪了 -> {hit['name']}"
            else:
                mark = hit["name"]
            gbound = manual_bound_by_group.get(gkey) if manual_bound_by_group else None
            if gbound is not None and hit.get("ts", 0) < gbound:
                mark += "（补）"
            ws.cell(row, column=3, value=f"第{hit['pity']}抽({hit['pool_name']}) -> {mark}")
            row += 1
        row += 1

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 35
