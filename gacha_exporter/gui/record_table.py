"""记录明细表格组件 — PySide6 QTableWidget。"""
from typing import Dict, List

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from ..models import Character, GachaRecord, Pool
from ..utils.helpers import format_timestamp


class RecordTable(QTableWidget):
    """抽卡记录表格。"""

    def __init__(self, parent=None):
        """初始化。"""
        super().__init__(parent)
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(["序号", "时间", "卡池", "角色", "稀有度"])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)

    def update_records(
        self,
        records: List[GachaRecord],
        characters: Dict[str, Character],
        pools: Dict[str, Pool],
    ) -> None:
        """刷新表格内容。

        Args:
            records: 抽卡记录
            characters: 角色映射
            pools: 卡池映射
        """
        self.setRowCount(len(records))
        for i, rec in enumerate(records):
            char = characters.get(rec.item_id, Character.default(rec.item_id))
            pool = pools.get(rec.pool_id, Pool.default(rec.pool_id))
            self.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.setItem(i, 1, QTableWidgetItem(format_timestamp(rec.timestamp)))
            self.setItem(i, 2, QTableWidgetItem(pool.name))
            self.setItem(i, 3, QTableWidgetItem(char.name))
            self.setItem(i, 4, QTableWidgetItem(f"{char.rarity}星" if char.rarity else "?"))
