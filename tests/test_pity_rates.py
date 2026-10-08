"""non_warp_rates 不歪率(出货率)计算测试。

口径:不歪率 = 小保底 50/50 命中 UP 数(wins) / 全部 UP 出货数(all_up, 含大保底必出)。
「UP 出现在哪个池就算哪个池」——跨池继承的大保底必出计入承接池 all_up,不搬回
产生它的池。组/合计 = sum(wins)/sum(all_up)。rate=None 表示 all_up==0(无 UP 数据)。
"""
from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool


def make_engine():
    """30001/30002 同属 shared:UP 组(共享保底+状态);30003 常驻兜底 solo(无状态)。

    101 诺雅=6星(两 UP 池的 up)，104 歪角=6星(非 UP)。
    """
    characters = {
        "101": Character(id="101", name="诺雅", rarity=6, limited=True),
        "102": Character(id="102", name="里奥", rarity=5, limited=False),
        "103": Character(id="103", name="艾米", rarity=4, limited=False),
        "104": Character(id="104", name="歪角", rarity=6, limited=False),
    }
    pools = {
        "30001": Pool(id="30001", name="示例卡池002", type="UP", order=1, up_character="诺雅", pity_group="shared:UP"),
        "30002": Pool(id="30002", name="第二UP池", type="UP", order=2, up_character="诺雅", pity_group="shared:UP"),
        "30003": Pool(id="30003", name="常驻池", type="常驻", order=0),
    }
    pity_groups = {
        "shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True),
    }
    return StatsEngine(characters, pools, pity_groups)


def _rates(stats):
    return stats.non_warp_rates()


def test_cross_pool_guaranteed_attributed_to_appearing_pool():
    """用户案例:池1 歪-up-歪-up-歪-up-up-歪 | 池2 up-up-up-up(共享组跨池)。

    池1 尾歪产生的大保底被池2 首UP 继承 -> 计入池2 all_up 且不算 wins;
    池1 自身三次必出UP(第2/4/6出货)计入池1 all_up 也不算 wins。
    期望:池1=1/4=25%,池2=3/4=75%,组合计=4/8=50%。
    """
    stats = make_engine()
    seq = [
        # 池1(30001): 歪-up-歪-up-歪-up-up-歪
        ("30001", "104"), ("30001", "101"), ("30001", "104"), ("30001", "101"),
        ("30001", "104"), ("30001", "101"), ("30001", "101"), ("30001", "104"),
        # 池2(30002): up-up-up-up
        ("30002", "101"), ("30002", "101"), ("30002", "101"), ("30002", "101"),
    ]
    stats.load_records([
        GachaRecord(pid, iid, 1000 + 100 * i) for i, (pid, iid) in enumerate(seq)
    ])

    up = _rates(stats)["shared:UP"]
    assert up["per_pool"]["30001"] == {"wins": 1, "all_up": 4, "rate": 0.25}
    assert up["per_pool"]["30002"] == {"wins": 3, "all_up": 4, "rate": 0.75}
    assert up["wins"] == 4 and up["all_up"] == 8 and up["rate"] == 0.5


def test_basic_win_guarantee_win_win():
    """单池:歪(小保底)->必出UP(继承)->50/50胜->50/50胜。

    期望:wins=2, all_up=3, rate=2/3≈66.7%。
    """
    stats = make_engine()
    stats.load_records([
        GachaRecord("30001", "104", 1000),  # 歪 -> 大保底
        GachaRecord("30001", "101", 2000),  # 必出UP(不算 wins)
        GachaRecord("30001", "101", 3000),  # 50/50 胜
        GachaRecord("30001", "101", 4000),  # 50/50 胜
    ])
    up = _rates(stats)["shared:UP"]
    assert up["per_pool"]["30001"] == {"wins": 2, "all_up": 3, "rate": 2 / 3}
    assert up["wins"] == 2 and up["all_up"] == 3 and up["rate"] == 2 / 3


def test_pool_with_only_warped_no_up_undefined():
    """池1 全歪(无 UP 出货)、池2 全为继承的必出 UP。

    池1 all_up=0 -> 不进入 per_pool(无 UP 数据,面板显示「-」);
    池2 的 UP 全是被迫必出 -> wins=0, rate=0.0%。
    """
    stats = make_engine()
    stats.load_records([
        GachaRecord("30001", "104", 1000),  # 歪 -> 大保底
        GachaRecord("30002", "101", 2000),  # 必出UP(继承)
        GachaRecord("30001", "104", 3000),  # 歪 -> 大保底
        GachaRecord("30002", "101", 4000),  # 必出UP(继承)
    ])
    up = _rates(stats)["shared:UP"]
    assert "30001" not in up["per_pool"]            # 无 UP 数据,不参与
    assert up["per_pool"]["30002"] == {"wins": 0, "all_up": 2, "rate": 0.0}
    assert up["wins"] == 0 and up["all_up"] == 2 and up["rate"] == 0.0


def test_non_state_groups_excluded():
    """非 has_state 组(常驻/新手)无 UP 概念,不出现在不歪率结果里。"""
    stats = make_engine()
    stats.load_records([
        GachaRecord("30001", "101", 1000),   # UP 命中
        GachaRecord("30003", "104", 2000),   # 常驻 6 星
    ])
    rates = _rates(stats)
    assert "shared:UP" in rates
    assert "solo:30003" not in rates


def test_empty_tracking_returns_empty():
    """无任何 6 星 -> 分组无 hits,rate 为 None(仍返回分组键)。"""
    stats = make_engine()
    stats.load_records([GachaRecord("30001", "103", 1000)])  # 4 星
    rates = _rates(stats)
    up = rates["shared:UP"]
    assert up == {
        "wins": 0, "all_up": 0, "rate": None,
        "guaranteed": set(), "per_pool": {},
    }


def test_guaranteed_keys_mark_guaranteed_up_hits():
    """大保底必出命中的键集合（[UP·必出] 展示标注的数据来源）。

    池1 歪-up-歪-up-歪-up-up-歪 | 池2 up-up-up-up：
    必出命中 = 池1 第2/4/6出货（下标 1/3/5）+ 池2 第1出货（下标 8，继承），
    共 4 条；其余 UP（下标 6/9/10/11）为 50/50 胜，不在集合内。
    键 = (pool_id, ts, pity, name)，与组级 hits 逐条对应。
    """
    stats = make_engine()
    seq = [
        ("30001", "104"), ("30001", "101"), ("30001", "104"), ("30001", "101"),
        ("30001", "104"), ("30001", "101"), ("30001", "101"), ("30001", "104"),
        ("30002", "101"), ("30002", "101"), ("30002", "101"), ("30002", "101"),
    ]
    stats.load_records([
        GachaRecord(pid, iid, 1000 + 100 * i) for i, (pid, iid) in enumerate(seq)
    ])
    hits = stats.shared_pity_tracking()["shared:UP"]["hits"]
    got = _rates(stats)["shared:UP"]["guaranteed"]
    assert len(got) == 4
    expected = {
        (h["pool_id"], h["ts"], h["pity"], h["name"])
        for i, h in enumerate(hits) if i in (1, 3, 5, 8)
    }
    assert got == expected


def test_guaranteed_keys_single_pool():
    """单池 歪->必出->胜->胜：仅第2出货（下标 1）是必出。"""
    stats = make_engine()
    stats.load_records([
        GachaRecord("30001", "104", 1000),  # 歪 -> 大保底
        GachaRecord("30001", "101", 2000),  # 必出UP
        GachaRecord("30001", "101", 3000),  # 50/50 胜
        GachaRecord("30001", "101", 4000),  # 50/50 胜
    ])
    hits = stats.shared_pity_tracking()["shared:UP"]["hits"]
    got = _rates(stats)["shared:UP"]["guaranteed"]
    assert got == {(hits[1]["pool_id"], hits[1]["ts"], hits[1]["pity"], hits[1]["name"])}
