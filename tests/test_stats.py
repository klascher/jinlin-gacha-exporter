"""统计引擎单元测试。"""
from gacha_exporter.analysis.stats import StatsEngine
from gacha_exporter.models import Character, GachaRecord, PityGroup, Pool


def make_engine(shielded=None):
    """构造测试用统计引擎。

    30001/30002 同属 shared:UP 组（共享保底+状态）；30003 未配 pity_group -> 兜底
    solo(70,无状态)；10001 配 solo:10001(30,无状态)。
    101 诺雅=6星(两 UP 池的 up)，104 歪角=6星(非 UP)，102 里奥=5星，103 艾米=4星。
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
        "10001": Pool(id="10001", name="新手池", type="新手", order=0, pity_group="solo:10001"),
    }
    pity_groups = {
        "shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True),
        "solo:10001": PityGroup(id="solo:10001", label="新手池", hard_pity=30, has_state=False),
    }
    return StatsEngine(characters, pools, pity_groups, shielded)


def test_add_and_dedup():
    """重复批次（翻页重发）应整批跳过。"""
    stats = make_engine()
    batch1 = [
        GachaRecord("30001", "101", 1000, batch_seq=0),
        GachaRecord("30001", "102", 1001, batch_seq=0),
    ]
    added1 = stats.add(batch1)
    assert added1 == 2
    assert stats.total == 2

    # 第二次提交相同批次（模拟重复翻页），应全部跳过
    batch2 = [
        GachaRecord("30001", "101", 1000, batch_seq=1),
        GachaRecord("30001", "102", 1001, batch_seq=1),
    ]
    added2 = stats.add(batch2)
    assert added2 == 0
    assert stats.total == 2


def test_pity_tracking():
    """6 星命中归零、记录命中历史与状态。"""
    stats = make_engine()
    recs = [
        GachaRecord("30001", "103", 100, batch_seq=0, position_in_batch=0),
        GachaRecord("30001", "103", 200, batch_seq=0, position_in_batch=1),
        GachaRecord("30001", "101", 300, batch_seq=0, position_in_batch=2),  # 第3抽命中 UP(诺雅)
    ]
    stats.add(recs)
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["current_pity"] == 0
    assert up["six_count"] == 1
    assert up["hits"][0]["pity"] == 3
    assert up["hits"][0]["is_up"] is True
    assert up["state"] == "小保底"  # 命中 UP -> 小保底


def test_five_star_not_pity_hit():
    """5 星不计入 6 星保底命中（里奥 rarity=5）。"""
    stats = make_engine()
    recs = [
        GachaRecord("30001", "103", 100, batch_seq=0, position_in_batch=0),  # 4 星
        GachaRecord("30001", "102", 200, batch_seq=0, position_in_batch=1),  # 5 星里奥
    ]
    stats.add(recs)
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["current_pity"] == 2
    assert up["six_count"] == 0
    assert up["hits"] == []
    assert up["state"] == "小保底"  # 初始态


def test_shared_pity_across_pools():
    """跨池共享保底：3001 中 UP 后又抽 10，切 3002 抽 20 -> 共享 30、小保底、距 40。"""
    stats = make_engine()
    recs = []
    for i in range(59):  # 3001 前 59 抽无 6 星
        recs.append(GachaRecord("30001", "103", 1000 + i, batch_seq=0, position_in_batch=i))
    recs.append(GachaRecord("30001", "101", 1059, batch_seq=0, position_in_batch=59))  # 第60抽 UP
    for i in range(10):  # 3001 再 10 抽无 6 星
        recs.append(GachaRecord("30001", "103", 2000 + i, batch_seq=1, position_in_batch=i))
    for i in range(20):  # 3002 抽 20 无 6 星
        recs.append(GachaRecord("30002", "103", 3000 + i, batch_seq=2, position_in_batch=i))
    stats.add(recs)
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["current_pity"] == 30          # 10 + 20
    assert up["distance_to_pity"] == 40       # 70 - 30
    assert up["state"] == "小保底"            # 上次命中 UP
    assert up["six_count"] == 1
    assert up["hits"][0]["pity"] == 60
    assert up["hits"][0]["is_up"] is True
    # 每池分解：3001 垫 10、3002 垫 20，加和 = 共享 30
    assert up["per_pool"]["30001"]["padded"] == 10
    assert up["per_pool"]["30002"]["padded"] == 20
    assert sum(p["padded"] for p in up["per_pool"].values()) == 30


def test_state_transition_up_and_miss():
    """UP 命中 -> 小保底；随后歪 -> 大保底。"""
    stats = make_engine()
    stats.add([
        GachaRecord("30001", "101", 100, batch_seq=0),  # UP 诺雅 -> 小保底
        GachaRecord("30001", "104", 200, batch_seq=0),  # 歪角(非UP 6星) -> 大保底
    ])
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["six_count"] == 2
    assert up["hits"][0]["is_up"] is True
    assert up["hits"][1]["is_up"] is False
    assert up["state"] == "大保底"  # 最后一次歪了
    assert up["current_pity"] == 0


def test_state_carries_across_pools():
    """3001 歪了(->大保底)，切到 3002 命中 UP：状态跨池带过去。"""
    stats = make_engine()
    stats.add([
        GachaRecord("30001", "104", 100, batch_seq=0),  # 3001 歪 -> 大保底
        GachaRecord("30002", "101", 200, batch_seq=1),  # 3002 命中 UP
    ])
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["six_count"] == 2
    assert up["hits"][0]["pool_id"] == "30001"
    assert up["hits"][0]["is_up"] is False
    assert up["hits"][1]["pool_id"] == "30002"
    assert up["hits"][1]["is_up"] is True
    assert up["state"] == "小保底"  # 最后命中 UP


def test_solo_pool_no_state():
    """独立池（常驻 30003 未配 pity_group）无状态机、自成一组、兜底 70。"""
    stats = make_engine()
    stats.add([
        GachaRecord("30003", "104", 100, batch_seq=0),  # 6 星
        GachaRecord("30003", "103", 200, batch_seq=0),
    ])
    solo = stats.shared_pity_tracking()["solo:30003"]
    assert solo["has_state"] is False
    assert solo["state"] is None
    assert solo["six_count"] == 1
    assert solo["current_pity"] == 1  # 6 星后 1 抽
    assert solo["hard_pity"] == 70   # 兜底默认


def test_hard_pity_per_group():
    """新手池 10001 硬保底 30（按组配置，非全局 70）。"""
    stats = make_engine()
    stats.add([GachaRecord("10001", "103", 100, batch_seq=0)])
    solo = stats.shared_pity_tracking()["solo:10001"]
    assert solo["hard_pity"] == 30
    assert solo["distance_to_pity"] == 29  # 30 - 1
    assert solo["has_state"] is False


def test_split_ten_pull_same_timestamp_pity_order():
    """同十连(同 timestamp)拆两帧时，保底计数须按真实抽序，最新一抽最后算。

    游戏以「新->旧」下发：最新帧(batch_seq 小)先到、帧内最新条目(pos 小)在前。
    故同 ts 十连内旧->新 = (batch_seq 降序, position 降序)。若只按 timestamp
    排，十连内退化为插入序(新->旧)，最新一抽(6星)会被排到最前、pity 错算为 1
    而非 10（复现用户反馈：新手池最后一抽被统计成第21抽）。
    """
    stats = make_engine()
    ts = 5000
    recs = []
    # 帧0（较新半）：pos0=6星101(最新一抽)，pos1-4=4星103
    recs.append(GachaRecord("30001", "101", ts, batch_seq=0, position_in_batch=0))
    for p in range(1, 5):
        recs.append(GachaRecord("30001", "103", ts, batch_seq=0, position_in_batch=p))
    # 帧1（较旧半）：pos0-4=4星103
    for p in range(0, 5):
        recs.append(GachaRecord("30001", "103", ts, batch_seq=1, position_in_batch=p))
    stats.add(recs)
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["six_count"] == 1
    assert up["hits"][0]["pity"] == 10  # 6星是第10抽(最新)，而非第1抽
    assert up["current_pity"] == 0


def test_same_char_multiple_in_batch():
    """同十连同名角色不应被误删。"""
    stats = make_engine()
    recs = [
        GachaRecord("30001", "101", 5000),
        GachaRecord("30001", "101", 5000),
        GachaRecord("30001", "101", 5000),
        GachaRecord("30001", "103", 5000),
    ]
    added = stats.add(recs)
    assert added == 4
    assert stats.total == 4


def test_split_ten_pull_across_pages():
    """十连被拆分到两页，跨页同名角色不应被误删。"""
    stats = make_engine()
    # 第一页：5 条，含 90000024
    page1 = [
        GachaRecord("20005", "90000020", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000019", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000041", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000024", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000019", 1784345661004, batch_seq=14),
    ]
    added1 = stats.add(page1)
    assert added1 == 5
    assert stats.total == 5

    # 第二页：5 条，也含 90000024（十连中同名角色，不同抽）
    page2 = [
        GachaRecord("20005", "90000028", 1784345661004, batch_seq=15),
        GachaRecord("20005", "90000013", 1784345661004, batch_seq=15),
        GachaRecord("20005", "90000013", 1784345661004, batch_seq=15),
        GachaRecord("20005", "90000040", 1784345661004, batch_seq=15),
        GachaRecord("20005", "90000024", 1784345661004, batch_seq=15),
    ]
    added2 = stats.add(page2)
    assert added2 == 5
    assert stats.total == 10


def test_resend_page_skipped():
    """重新翻页到已抓过的页面，应整批跳过。"""
    stats = make_engine()
    page1 = [
        GachaRecord("20005", "90000020", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000019", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000041", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000024", 1784345661004, batch_seq=14),
        GachaRecord("20005", "90000019", 1784345661004, batch_seq=14),
    ]
    stats.add(page1)
    assert stats.total == 5

    # 重发同一页（不同 batch_seq 但内容相同）
    page1_resend = [
        GachaRecord("20005", "90000020", 1784345661004, batch_seq=99),
        GachaRecord("20005", "90000019", 1784345661004, batch_seq=99),
        GachaRecord("20005", "90000041", 1784345661004, batch_seq=99),
        GachaRecord("20005", "90000024", 1784345661004, batch_seq=99),
        GachaRecord("20005", "90000019", 1784345661004, batch_seq=99),
    ]
    added = stats.add(page1_resend)
    assert added == 0
    assert stats.total == 5


def test_add_without_shift_collects_everything():
    """**未设置会话 Δ 时保持全收**（宁重复勿漏抓，收尾 merge 兜底）。

    shift 只在会话内第一次拿到权威 total 后由 GUI 设置；拿不到（异常路径）
    时无法归一化，保守全收。含十连同名的批次也原样保留（位置才是身份）。
    """
    stats = make_engine()
    # 会话开始时历史已有这两条
    stats.load_records([
        GachaRecord("30001", "101", 1000, batch_seq=0, position_in_batch=0),
        GachaRecord("30001", "102", 1001, batch_seq=0, position_in_batch=1),
    ])
    assert stats.total == 2

    # 新抓一批：1 新 + 2 旧(重复抓) + 十连同名(103 抽到 2 次)
    batch = [
        GachaRecord("30001", "103", 2000, batch_seq=1, position_in_batch=2),   # 新
        GachaRecord("30001", "101", 1000, batch_seq=1, position_in_batch=0),   # 旧，重复抓
        GachaRecord("30001", "102", 1001, batch_seq=1, position_in_batch=1),   # 旧，重复抓
        GachaRecord("30001", "103", 2000, batch_seq=1, position_in_batch=3),   # 十连同名
    ]
    added = stats.add(batch)
    assert added == 4                 # 无 Δ → 无法归一化 → 保守全收
    assert stats.total == 6
    # 十连同名两条都保留（位置才是身份，内容相同不影响）
    assert sum(1 for r in stats.records if r.item_id == "103") == 2
    # 本次会话新增只含本批收下的 4 条（收尾时交给 history 做位置合并）
    assert len(stats.session_records) == 4


def test_add_dedups_realtime_against_history_with_shift():
    """**实时位置判重**：设置会话 Δ 后，命中历史 `(ts, pos − Δ)` 的记录当场不收。

    用户场景（2026-09-13 12:51 实测）：抓全 521 → 停止 → 抽 2 抽 → 重抓——
    判重若只发生在收尾 merge，抓包过程中的界面总数会虚高（= 历史 + 本次全部收下）。
    实时判重让过程中数字就是准的。
    """
    stats = make_engine()
    stats.load_records([
        GachaRecord("30001", "101", 1000, batch_seq=0, position_in_batch=0),
        GachaRecord("30001", "102", 1001, batch_seq=0, position_in_batch=1),
    ])
    # 会话开始：落库基准 total=2；首个响应 total=4（期间抽了 2 抽）→ Δ=2
    stats.begin_session_shift(baseline_total=2)
    stats.notify_total(4)

    added = stats.add([
        GachaRecord("30001", "101", 1000, batch_seq=1, position_in_batch=2),  # norm=0 命中
        GachaRecord("30001", "102", 1001, batch_seq=1, position_in_batch=3),  # norm=1 命中
        GachaRecord("30001", "103", 2000, batch_seq=1, position_in_batch=0),  # 新事件
    ])
    assert added == 1, f"旧记录应被实时拦下，实收 {added}/3"
    assert stats.total == 3                       # 2(历史) + 1(新)
    assert len(stats.session_records) == 1        # 只含新事件
    # **收下即归一化**：pos 已折算回历史坐标系（0−2=−2，负数 = 比历史最新还新）
    assert stats.session_records[0].item_id == "103"
    assert stats.session_records[0].position_in_batch == -2
    # 新收下的记录也进位置索引：同会话内同位置再次到达同样拦下
    added2 = stats.add([GachaRecord("30001", "103", 2000, batch_seq=1, position_in_batch=0)])
    assert added2 == 0
    assert stats.overlapped_history is True       # 命中历史 = 已翻到边界


def test_add_realtime_dedup_survives_mid_session_draw():
    """**用户实测回归（2026-09-13 13:17 会话，多算 10 条）**：抓包中途抽卡。

    会话开始 total=10（Δ=0）→ 中途抽 1 抽（total=11，所有旧记录位置整体 +1）
    → **Δ 必须动态跟随 total（=11−基准10=1）**，否则旧记录全部对不上位置、
    被当成新记录重复计入（实测 9 个单抽事件各多 1 条）。同时新抽的记录
    归一化后落在负数区，靠 `(ts, pos)` 复合键与历史的负数位置区分（ts 不同）。
    """
    stats = make_engine()
    items = [f"X{i}" for i in range(1, 11)]       # X1 最新 … X10 最旧
    stats.load_records([GachaRecord("30001", it, 1000, 0, i) for i, it in enumerate(items)])
    stats.begin_session_shift(baseline_total=10)  # 落库时 total=10，会话开始 total=10 → Δ=0

    # 抽卡前先翻一页：全部命中
    assert stats.add([GachaRecord("30001", items[0], 1000, 1, 0)]) == 0

    # 中途抽 1 抽 → total 10→11
    stats.notify_total(11)
    # 抽卡后继续翻页：旧记录位置整体 +1；外加新抽的单抽 N（pos 0，全新 ts）
    added = stats.add([
        GachaRecord("30001", "N", 2000, 1, 0),                     # 新抽（norm=−1）
        GachaRecord("30001", items[0], 1000, 1, 1),                # norm=0 命中
        GachaRecord("30001", items[1], 1000, 1, 2),                # norm=1 命中
    ])
    assert added == 1, f"中途抽卡后旧记录必须仍被拦下，实收 {added}/3"
    # 新抽的 N 归一化 pos = 0 − 1 = −1；与历史的 −1（如有）靠 ts 区分，不冲突
    assert stats.session_records[-1].item_id == "N"
    assert stats.session_records[-1].position_in_batch == -1
    # 同一事件（T1）不得因重复投喂变多
    t1 = [r for r in stats.records if r.timestamp == 1000]
    assert len(t1) == 10 and len({r.position_in_batch for r in t1}) == 10


def test_add_realtime_dedup_respected_on_clear_and_reload():
    """clear / 重新加载历史后，位置索引必须同步，否则会把新抓当重复（或反之）。"""
    stats = make_engine()
    stats.load_records([GachaRecord("30001", "101", 1000, position_in_batch=5)])
    stats.begin_session_shift(baseline_total=5)    # total=5 → Δ=0
    assert stats.add([GachaRecord("30001", "101", 1000, position_in_batch=5)]) == 0

    stats.clear()
    assert stats.add([GachaRecord("30001", "101", 1000, position_in_batch=5)]) == 1

    # 重新加载历史（total 基准 9）→ 会话中 total 涨到 12 → Δ=3
    stats.load_records([GachaRecord("30001", "102", 2000, position_in_batch=9)])
    stats.begin_session_shift(baseline_total=9)
    stats.notify_total(12)
    assert stats.add([GachaRecord("30001", "102", 2000, position_in_batch=12)]) == 0  # 12−3=9 命中
    assert stats.add([GachaRecord("30001", "103", 3000, position_in_batch=13)]) == 1  # 新事件归一化 13−3=10


def test_clear_resets_history_snapshot():
    """清空后历史快照重置，后续抓取全保留。"""
    stats = make_engine()
    stats.load_records([GachaRecord("30001", "101", 1000, batch_seq=0)])
    assert stats.total == 1

    stats.clear()
    # 清空后重新抓同 key 记录，应作为新记录保留（快照已清空）
    added = stats.add([GachaRecord("30001", "101", 1000, batch_seq=1)])
    assert added == 1
    assert stats.total == 1


def test_session_signals_for_stop_capture():
    """stop_capture 判定信号：抓到新记录但未翻到历史边界时才应提示。

    - 仅抓新记录(未翻到历史): session_new_count>0 且 overlapped_history=False -> 提示
    - 抓到与历史重叠的旧记录: overlapped_history=True -> 不提示
    - 清空后信号重置
    """
    stats = make_engine()
    stats.load_records([
        GachaRecord("30001", "101", 1000, batch_seq=0),
        GachaRecord("30001", "102", 1001, batch_seq=0),
    ])
    # 场景A: 只抓新记录，未翻到历史 -> 应提示
    stats.add([GachaRecord("30001", "103", 2000, batch_seq=1)])
    assert stats.session_new_count == 1
    assert stats.overlapped_history is False

    # 场景B: 继续翻页，抓到与历史重叠的旧记录 -> 已到边界，不应提示
    stats.add([
        GachaRecord("30001", "104", 2001, batch_seq=2),  # 新
        GachaRecord("30001", "101", 1000, batch_seq=2),  # 旧(重叠)
    ])
    assert stats.overlapped_history is True
    assert stats.session_new_count == 3  # 本次会话累计收下 103 + 104 + 旧(101) 三条

    # 场景C: 清空后信号重置
    stats.clear()
    assert stats.session_new_count == 0
    assert stats.overlapped_history is False


def test_unknown_id_default_six_star_hit():
    """陌生ID默认按6星：featured池上视为命中UP -> 小保底，触发归零。"""
    stats = make_engine()
    stats.add([
        GachaRecord("30001", "103", 100),  # 4星 垫1
        GachaRecord("30001", "999", 200),  # 陌生ID -> 按6星命中 -> 小保底, 归零
        GachaRecord("30001", "103", 300),  # 垫1
    ])
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["six_count"] == 1
    assert up["hits"][0]["name"] == "未知(999)"
    assert up["hits"][0]["is_up"] is True   # 陌生6星在featured池=命中
    assert up["state"] == "小保底"
    assert up["current_pity"] == 1           # 6星后又垫1


def test_unknown_id_shielded_not_six():
    """屏蔽的陌生ID按4/5星：不触发归零，当普通抽计入。"""
    stats = make_engine()
    stats.shielded = {"999"}
    stats.add([
        GachaRecord("30001", "103", 100),
        GachaRecord("30001", "999", 200),  # 屏蔽 -> 非六星
        GachaRecord("30001", "103", 300),
    ])
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["six_count"] == 0
    assert up["hits"] == []
    assert up["current_pity"] == 3           # 全部计入
    assert up["state"] == "小保底"            # 初始态未翻转


def test_unknown_id_non_featured_resets_no_up():
    """陌生ID在非featured池(常驻)：按6星归零，但is_up=False，无状态机。"""
    stats = make_engine()
    stats.add([
        GachaRecord("30003", "103", 100),
        GachaRecord("30003", "999", 200),  # 陌生 -> 6星归零
        GachaRecord("30003", "103", 300),  # 垫1
    ])
    solo = stats.shared_pity_tracking()["solo:30003"]
    assert solo["six_count"] == 1
    assert solo["hits"][0]["is_up"] is False  # 常驻无UP概念
    assert solo["has_state"] is False
    assert solo["state"] is None
    assert solo["current_pity"] == 1


def test_unknown_id_stats_counts():
    """unknown_id_stats 返回未映射ID及出现次数（与屏蔽无关）。"""
    stats = make_engine()
    stats.add([
        GachaRecord("30001", "103", 100),
        GachaRecord("30001", "999", 200),
        GachaRecord("30001", "999", 300),
        GachaRecord("30001", "888", 400),
    ])
    assert stats.unknown_id_stats() == {"999": 2, "888": 1}


def test_shielded_set_shared_with_engine():
    """传入的shielded set被原样持有：外部mutate后engine立即按屏蔽处理。"""
    shields = set()
    stats = make_engine(shields)
    stats.add([GachaRecord("30001", "999", 100)])
    assert stats.shared_pity_tracking()["shared:UP"]["six_count"] == 1  # 默认按6星
    shields.add("999")  # 外部屏蔽同一set对象
    assert stats.shared_pity_tracking()["shared:UP"]["six_count"] == 0  # 现在按4/5星


def test_known_id_rarity_overrides_shield():
    """已映射ID真实星级覆盖屏蔽：元数据收录后屏蔽自动失效。"""
    stats = make_engine()
    stats.shielded = {"101"}  # 101 是已映射6星(诺雅)
    stats.add([GachaRecord("30001", "101", 100)])
    up = stats.shared_pity_tracking()["shared:UP"]
    assert up["six_count"] == 1  # 真实6星生效，屏蔽被忽略


def test_begin_new_session_resets_session_layer_state():
    """begin_new_session 重置会话级状态（新增列表 / 起点快照 / 计数）。

    **跨会话去重已不在 stats 层**：位置对齐方案下 stats 只做「单次抓包全收 +
    记录本次新增」，跨会话的重复由 `storage/history.merge()` 按
    `(ts, pos − Δ)` 判重（回归见 `tests/test_history.py` 与 `tests/test_pos_align.py`）。
    """
    stats = make_engine()
    # 会话A：空历史下首抓（含一十连同名对 103x2，位置不同 = 合法独立抽卡）
    session_a = [
        GachaRecord("30001", "101", 1000, batch_seq=0, position_in_batch=100),
        GachaRecord("30001", "103", 2000, batch_seq=0, position_in_batch=101),
        GachaRecord("30001", "103", 2000, batch_seq=0, position_in_batch=102),
    ]
    stats.add(session_a)
    assert stats.total == 3
    assert len(stats.session_records) == 3

    # 停止抓包时（历史合并之后）刷新会话级状态
    stats.begin_new_session()
    assert stats.session_new_count == 0
    assert stats.session_records == []      # 新增列表清空，下一轮重新累积
    assert stats.overlapped_history is False

    # 会话B：把会话A的整批又抓一遍 + 1 条真正的新记录
    # stats 层仍全收（4 条）；「不重复计入」由 history 的位置判重保证
    added = stats.add([
        GachaRecord("30001", "101", 1000, batch_seq=1, position_in_batch=100),
        GachaRecord("30001", "103", 2000, batch_seq=1, position_in_batch=101),
        GachaRecord("30001", "103", 2000, batch_seq=1, position_in_batch=102),
        GachaRecord("30001", "104", 3000, batch_seq=1, position_in_batch=99),
    ])
    assert added == 4
    assert stats.total == 7
    # 会话B 的新增列表 = 本批 4 条（含与历史重复的），交给 history 判重
    assert len(stats.session_records) == 4
