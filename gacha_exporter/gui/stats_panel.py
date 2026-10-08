"""统计面板组件 - 展示稀有度分布、卡池分布、保底追踪（总保底/每池垫两视图）。"""
from typing import Dict

from PySide6.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget

from ..analysis.stats import StatsEngine
from ..models import Character, Pool


def _pool_sort_key(pid: str):
    """pool_id 排序：数字按数值，否则按字符串。"""
    try:
        return (0, int(pid))
    except ValueError:
        return (1, pid)


class StatsPanel(QWidget):
    """统计面板。"""

    def __init__(self, parent=None):
        """初始化。"""
        super().__init__(parent)
        self.text = QTextEdit(self)
        self.text.setReadOnly(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("统计信息"))
        layout.addWidget(self.text)

    def update_stats(
        self,
        stats: StatsEngine,
        characters: Dict[str, Character],
        pools: Dict[str, Pool],
        pity_view: str = "shared",
    ) -> None:
        """刷新统计面板。

        Args:
            stats: 统计引擎
            characters: 角色映射（预留）
            pools: 卡池映射
            pity_view: 保底视图 - shared=总保底（组级），per_pool=每池垫（分解）
        """
        lines = [f"总记录数: {stats.total}", ""]

        lines.append("【稀有度分布】")
        rarity_dist, _ = stats.rarity_distribution()
        for rarity in [6, 5, 4]:
            cnt = rarity_dist.get(rarity, 0)
            pct = cnt / max(stats.total, 1) * 100
            lines.append(f"  {rarity}星: {cnt} ({pct:.1f}%)")
        unknowns = stats.unknown_id_stats()
        if unknowns:
            lines.append(f"  未映射ID({len(unknowns)}):保底默认按6星,点\"未映射ID\"按钮可屏蔽")
            for iid in sorted(unknowns, key=lambda x: (-unknowns[x], x))[:15]:
                tag = "已屏蔽(4/5星)" if iid in stats.shielded else "按6星"
                lines.append(f"    {iid}: {unknowns[iid]}次 [{tag}]")
        lines.append("")

        lines.append("【卡池分布】")
        for name, cnt in stats.pool_distribution().items():
            lines.append(f"  {name}: {cnt}")
        lines.append("")

        lines.append("【保底追踪】")
        if pity_view == "per_pool":
            lines.extend(self._render_per_pool(stats, pools))
        else:
            lines.extend(self._render_shared(stats, pools))
        lines.append("")

        self.text.setText("\n".join(lines))

    def _render_shared(self, stats: StatsEngine, pools: Dict[str, Pool]):
        """总保底视图：每组共享保底 + 组级命中历史。"""
        lines = []
        for gkey, info in stats.shared_pity_tracking().items():
            state_str = f" | {info['state']}" if info["state"] else ""
            lines.append(
                f"  [{info['label']}] {info['current_pity']}/{info['hard_pity']}"
                f"（距保底{info['distance_to_pity']}抽）{state_str}"
            )
            lines.append(f"    组内总抽数: {info['total']} | 6星x{info['six_count']}")
            for hit in info["hits"]:
                mark = f"命中up -> {hit['name']}" if hit["is_up"] else f"歪了 -> {hit['name']}"
                lines.append(f"    第{hit['pity']}抽({hit['pool_name']}) -> {mark}")
            lines.append("")
        return lines

    def _render_per_pool(self, stats: StatsEngine, pools: Dict[str, Pool]):
        """每池垫视图：每池自组内最近6星后的抽数 + 每池命中历史。

        padded>0 的池显示“垫X / 总Y”；padded=0（被后续6星消费、当前周期未垫）
        的池省略“垫X / ”前缀，仅显示“总Y”，避免“垫0”误导。
        """
        lines = []
        for gkey, info in stats.shared_pity_tracking().items():
            state_str = f" | {info['state']}" if info["state"] else ""
            lines.append(
                f"  [{info['label']}] 共享保底 {info['current_pity']}/{info['hard_pity']}{state_str}"
            )
            for pid in sorted(info["per_pool"].keys(), key=_pool_sort_key):
                pinfo = info["per_pool"][pid]
                pool = pools.get(pid)
                pname = pool.name if pool else pid
                six_str = f" | 6星x{pinfo['six_count']}" if pinfo["six_count"] else ""
                pad_part = f"垫{pinfo['padded']} / " if pinfo["padded"] else ""
                lines.append(f"    {pname}: {pad_part}总{pinfo['total']}{six_str}")
                for hit in pinfo["hits"]:
                    mark = f"命中up -> {hit['name']}" if hit["is_up"] else f"歪了 -> {hit['name']}"
                    lines.append(f"      第{hit['pity']}抽 -> {mark}")
            lines.append("")
        return lines
