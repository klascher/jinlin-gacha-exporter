"""pool_groups 分组解析测试 - 分类、兜底、按组 hard_pity。"""
from gacha_exporter.loaders.pool_groups import resolve_groups
from gacha_exporter.models import Pool, PityGroup


def _pools():
    return {
        "10001": Pool(id="10001", name="新手池", type="新手", pity_group="solo:10001"),
        "20001": Pool(id="20001", name="常驻池", type="常驻", pity_group="solo:20001"),
        "20002": Pool(id="20002", name="遴选1", type="遴选", pity_group="shared:遴选"),
        "30001": Pool(id="30001", name="UP1", type="UP", pity_group="shared:UP"),
        "99999": Pool(id="99999", name="未配池", type="未知"),  # 无 pity_group 字段
    }


def _groups():
    return {
        "shared:UP": PityGroup(id="shared:UP", label="UP池", hard_pity=70, has_state=True),
        "shared:遴选": PityGroup(id="shared:遴选", label="遴选池", hard_pity=70, has_state=True),
        "solo:10001": PityGroup(id="solo:10001", label="新手池", hard_pity=30, has_state=False),
        "solo:20001": PityGroup(id="solo:20001", label="常驻池", hard_pity=70, has_state=False),
    }


def test_classifies_shared_and_solo():
    p2g, _ = resolve_groups(_pools(), _groups())
    assert p2g["30001"] == "shared:UP"
    assert p2g["20002"] == "shared:遴选"
    assert p2g["10001"] == "solo:10001"
    assert p2g["20001"] == "solo:20001"


def test_unconfigured_pool_falls_back_to_solo():
    """无 pity_group 字段 -> 兜底 solo:pid，用 pool.name 作 label，默认 70/无状态。"""
    p2g, gcfg = resolve_groups(_pools(), _groups())
    assert p2g["99999"] == "solo:99999"
    solo = gcfg["solo:99999"]
    assert solo.hard_pity == 70
    assert solo.has_state is False
    assert solo.label == "未配池"


def test_undefined_group_reference_falls_back():
    """pool 引用了未定义的组 key -> 兜底 solo。"""
    pools = {"30001": Pool(id="30001", name="UP1", type="UP", pity_group="shared:不存在")}
    p2g, gcfg = resolve_groups(pools, _groups())
    assert p2g["30001"] == "solo:30001"
    assert gcfg["solo:30001"].hard_pity == 70


def test_hard_pity_from_config():
    _, gcfg = resolve_groups(_pools(), _groups())
    assert gcfg["solo:10001"].hard_pity == 30
    assert gcfg["shared:UP"].hard_pity == 70
    assert gcfg["shared:UP"].has_state is True


def test_empty_pity_groups_all_solo():
    """role.json 无 pity_groups 段 -> 所有池兜底 solo（即便 pool 写了 pity_group 字段）。"""
    p2g, gcfg = resolve_groups(_pools(), {})
    for pid in ["10001", "20001", "20002", "30001", "99999"]:
        assert p2g[pid].startswith("solo:")
    # 10001 写了 pity_group="solo:10001" 但无 groups 配置 -> 兜底默认 70（非 30）
    assert gcfg["solo:10001"].hard_pity == 70
