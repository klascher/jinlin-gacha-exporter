"""抓包网卡自动选择测试 - 路由决策 + APIPA 护栏 + 优先树 + 铁护栏。

通过 monkeypatch 注入接口列表与路由决策，不碰真实 Npcap/scapy。
"""
import json

import pytest

from gacha_exporter.storage import settings
from gacha_exporter.utils import net_iface
from gacha_exporter.utils.net_iface import (
    IFACE_AUTO,
    _is_healthy,
    list_capture_ifaces,
    pick_active_iface,
    resolve_capture_iface,
)


# ---- address_discovery 停止轮询：无流量也能及时退出（防 GUI 卡死回归） ----

def test_discover_exits_when_stopped_no_traffic(monkeypatch):
    """stop_event 设置后，即使 sniff 无流量也应很快退出（回归：曾永不返回卡死 GUI）。

    mock snack: scapy 的 sniff 调用到 stop_event 已在轮询间隙被设置后，核查循环
    条件并在下一个 timeout 周期退出 discover——验证「超时轮询 break stop_event」
    而非旧 stop_filter 依赖下一次收包。
    """
    import socket as _socket

    from gacha_exporter.loaders import address_discovery as ad

    calls = {"n": 0}

    def fake_check_npcap():
        return True, True

    def fake_sniff(timeout=None, **kwargs):
        calls["n"] += 1
        # 模拟无流量：sniff 不产生任何包，直接返回（等价于一个 timeout 周期结束）
        return

    monkeypatch.setattr(ad, "check_npcap_installed", fake_check_npcap)
    monkeypatch.setattr("scapy.all.sniff", fake_sniff)

    d = ad.AddressDiscovery()
    stop = __import__("threading").Event()
    stop.set()  # 尚未开始就请求停止
    result = d.discover(stop_event=stop)
    assert result == []
    # 轮询循环不应空转：stop_event 置位后至多再嗅探一次即返回
    assert calls["n"] <= 2


# ---- settings 槽隔离 ----

def _settings_path(tmp_path, monkeypatch):
    p = str(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_settings_path", lambda: p)
    return p


# ---- 健康判定 ----

def test_is_healthy_excludes_bad():
    assert _is_healthy("192.168.0.105") is True
    assert _is_healthy("10.0.0.5") is True
    assert _is_healthy("169.254.149.125") is False   # APIPA（断链虚拟卡）
    assert _is_healthy("127.0.0.1") is False          # loopback
    assert _is_healthy("0.0.0.0") is False            # unspecified
    assert _is_healthy("") is False
    assert _is_healthy("notanip") is False


# ---- pick_active_iface：路由决策 + APIPA 护栏 ----

def _fake_conf_route(monkeypatch, mapping):
    """把 scapy.conf.route.route 换成决定性 fake（按 dst 返回 iface）。"""
    import scapy.all as sp
    monkeypatch.setattr(
        sp.conf.route, "route",
        lambda dst: (mapping.get(dst, mapping.get("__default__", (None, None, None))))
    )


def _stub_ifaces(monkeypatch, ifaces):
    monkeypatch.setattr(net_iface, "list_capture_ifaces", lambda: ifaces)
    # 使 _healthy_ifaces 走真实 _is_healthy 过滤
    monkeypatch.setattr(net_iface, "_is_healthy", _is_healthy)


def test_pick_selects_healthy_when_route_hijacked(monkeypatch):
    """TAP（APIPA）+ WLAN 同存，即使路由指向 TAP，也应选健康 WLAN。"""
    ifaces = [
        (r"\\Device\\NPF_{TAP}", "169.254.149.125"),   # 断链虚拟卡
        (r"\\Device\\NPF_{WLAN}", "192.168.0.105"),
    ]
    _stub_ifaces(monkeypatch, ifaces)
    _fake_conf_route(monkeypatch, {"203.0.113.8": (r"\\Device\\NPF_{TAP}", "", "")})
    assert pick_active_iface("203.0.113.8") == r"\\Device\\NPF_{WLAN}"


def test_pick_uses_healthy_route_directly(monkeypatch):
    """路由本就指向健康 WLAN 时，直接用路由出口。"""
    ifaces = [(r"\\Device\\NPF_{WLAN}", "192.168.0.105")]
    _stub_ifaces(monkeypatch, ifaces)
    _fake_conf_route(monkeypatch, {"203.0.113.8": (r"\\Device\\NPF_{WLAN}", "", "")})
    assert pick_active_iface("203.0.113.8") == r"\\Device\\NPF_{WLAN}"


def test_pick_no_healthy_returns_none(monkeypatch):
    """全 APIPA / 空列表 → 无健康卡 → None（调用方回退旧行为）。"""
    _stub_ifaces(monkeypatch, [("TAP", "169.254.1.1")])
    assert pick_active_iface("1.2.3.4") is None
    _stub_ifaces(monkeypatch, [])
    assert pick_active_iface("1.2.3.4") is None


def test_pick_falls_back_to_first_healthy(monkeypatch):
    """路由异常/无目标时回退健康卡第一个有真实外联地址者。"""
    ifaces = [
        (r"\\Device\\NPF_{TAP}", "169.254.1.1"),
        (r"\\Device\\NPF_{WLAN}", "192.168.0.105"),
    ]
    _stub_ifaces(monkeypatch, ifaces)
    _fake_conf_route(monkeypatch, {})  # 无匹配 → fallback
    assert pick_active_iface("203.0.113.8") == r"\\Device\\NPF_{WLAN}"


def test_pick_uses_sentinel_for_address_discovery(monkeypatch):
    """无 target（地址发现前）用哨兵公网 IP 查出口。"""
    ifaces = [
        (r"\\Device\\NPF_{WLAN1}", "192.168.0.105"),
        (r"\\Device\\NPF_{WLAN2}", "192.168.1.5"),
    ]
    _stub_ifaces(monkeypatch, ifaces)
    _fake_conf_route(monkeypatch, {"1.1.1.1": (r"\\Device\\NPF_{WLAN2}", "", "")})
    assert pick_active_iface(None) == r"\\Device\\NPF_{WLAN2}"


# ---- list_capture_ifaces：顺序与 IP 兜底 ----

def test_list_capture_ifaces_wraps(get_if_list_mock):
    assert list_capture_ifaces() == [("A", "192.168.0.1"), ("B", "")]


@pytest.fixture
def get_if_list_mock(monkeypatch):
    import scapy.all as sp
    monkeypatch.setattr(sp, "get_if_list", lambda: ["A", "B"])
    monkeypatch.setattr(
        sp, "get_if_addr",
        lambda name: "192.168.0.1" if name == "A" else "",
    )
    return None


# ---- resolve_capture_iface：P1 > P2(复查) > P3 ----

def _settings_path2(tmp_path, monkeypatch):
    return _settings_path(tmp_path, monkeypatch)


def test_resolve_p1_explicit_wins(tmp_path, monkeypatch):
    """P1 显式非空 → 直接用，UTF 优先。"""
    _settings_path2(tmp_path, monkeypatch)
    settings.save_capture_iface(r"\\Device\\NPF_{EXPLICIT}")
    monkeypatch.setattr(net_iface, "pick_active_iface", lambda t=None: r"\\Device\\NPF_{P3}")
    assert resolve_capture_iface("203.0.113.8") == r"\\Device\\NPF_{EXPLICIT}"


def test_resolve_p2_learned_used_when_healthy(tmp_path, monkeypatch):
    """P1 空、P2 学习值仍存在且健康 → 用 P2（不会冒然用 P3）。"""
    _settings_path2(tmp_path, monkeypatch)
    settings.save_capture_iface_learned(r"\\Device\\NPF_{LEARNED}")
    monkeypatch.setattr(
        net_iface, "list_capture_ifaces",
        lambda: [(r"\\Device\\NPF_{LEARNED}", "10.0.0.5")],
    )
    monkeypatch.setattr(net_iface, "pick_active_iface", lambda t=None: r"\\Device\\NPF_{P3}")
    assert resolve_capture_iface(None) == r"\\Device\\NPF_{LEARNED}"


def test_resolve_p2_rejected_when_stale(tmp_path, monkeypatch):
    """P2 学习值已不存在/不健康（旧卡拔了/禁用）→ 落到 P3。"""
    _settings_path2(tmp_path, monkeypatch)
    settings.save_capture_iface_learned(r"\\Device\\NPF_{GONE}")
    monkeypatch.setattr(
        net_iface, "list_capture_ifaces",
        lambda: [(r"\\Device\\NPF_{CURRENT}", "192.168.0.105")],
    )
    monkeypatch.setattr(net_iface, "pick_active_iface", lambda t=None: r"\\Device\\NPF_{CURRENT}")
    assert resolve_capture_iface(None) == r"\\Device\\NPF_{CURRENT}"


def test_resolve_p3_when_nothing_learned(tmp_path, monkeypatch):
    """P1、P2 皆空 → P3。P3 无结果 → ""。"""
    _settings_path2(tmp_path, monkeypatch)
    monkeypatch.setattr(net_iface, "pick_active_iface", lambda t=None: r"\\Device\\NPF_{P3}")
    assert resolve_capture_iface(None) == r"\\Device\\NPF_{P3}"
    monkeypatch.setattr(net_iface, "pick_active_iface", lambda t=None: None)
    assert resolve_capture_iface(None) == ""


# ---- 铁护栏：自动解析/学习绝不写 P1 显式槽 ----

def test_automatic_never_writes_explicit_slot(tmp_path, monkeypatch):
    """resolve 只读各槽，绝不写 P1（即使 P1 原本为空也一样不写）。"""
    _settings_path2(tmp_path, monkeypatch)
    ifaces = [(r"\\Device\\NPF_{WLAN}", "192.168.0.105")]
    monkeypatch.setattr(net_iface, "list_capture_ifaces", lambda: ifaces)
    _fake_conf_route(monkeypatch, {"203.0.113.8": (r"\\Device\\NPF_{WLAN}", "", "")})
    resolve_capture_iface("203.0.113.8")
    # resolve 只读，绝不落盘 P1 显式槽（P3 是纯函数返回，settings.json 甚至不应生成）
    assert settings.load_capture_iface() == ""


# ---- settings 槽读写往返 ----

def test_capture_iface_slot_roundtrip(tmp_path, monkeypatch):
    _settings_path2(tmp_path, monkeypatch)
    assert settings.load_capture_iface() == ""
    settings.save_capture_iface(r"\\Device\\NPF_{X}")
    assert settings.load_capture_iface() == r"\\Device\\NPF_{X}"
    data = json.loads(open(_settings_path2(tmp_path, monkeypatch), encoding="utf-8").read())
    assert data["capture_iface"] == r"\\Device\\NPF_{X}"


def test_capture_iface_auto_is_empty(tmp_path, monkeypatch):
    _settings_path2(tmp_path, monkeypatch)
    settings.save_capture_iface(IFACE_AUTO)
    assert settings.load_capture_iface() == ""  # 智能(自动) 统一存 ""，落到 P2/P3


def test_capture_iface_learned_slot_roundtrip(tmp_path, monkeypatch):
    _settings_path2(tmp_path, monkeypatch)
    assert settings.load_capture_iface_learned() == ""
    settings.save_capture_iface_learned(r"\\Device\\NPF_{L}")
    assert settings.load_capture_iface_learned() == r"\\Device\\NPF_{L}"


# ---- 链路状态判据：断开/禁用的虚拟网卡不作抓包出口（2026-09-10） ----

def _stub_link_status(monkeypatch, up_names):
    """按接口名注入链路状态：在 up_names 中的为 Up，其余为 Down。"""
    monkeypatch.setattr(net_iface, "is_iface_link_up", lambda name: name in up_names)


def test_pick_skips_link_down_iface(monkeypatch):
    """回归：虚拟网卡拿到正常私有地址但链路断开 → 不得被选中。

    VPN/加速器类虚拟网卡断开时常残留 metric 更小的默认网关，被短暂激活后会拿到
    正常私有地址，通过 APIPA 护栏并抢走默认路由，导致抓包与地址发现同时 0 条。
    仅靠地址类别判据挡不住，须补与机器无关的链路状态判据。
    """
    ifaces = [
        (r"\\Device\\NPF_{ACCEL}", "10.0.9.9"),     # 私有地址，但链路 Down
        (r"\\Device\\NPF_{WLAN}", "192.168.0.105"),
    ]
    _stub_ifaces(monkeypatch, ifaces)
    _stub_link_status(monkeypatch, {r"\\Device\\NPF_{WLAN}"})
    _fake_conf_route(monkeypatch, {"203.0.113.8": (r"\\Device\\NPF_{ACCEL}", "", "")})
    assert pick_active_iface("203.0.113.8") == r"\\Device\\NPF_{WLAN}"


def test_pick_falls_back_when_all_link_down(monkeypatch):
    """链路判据只做减法：全部被判 Down 时回退为仅按地址健康筛选，不返回 None。"""
    ifaces = [
        (r"\\Device\\NPF_{A}", "192.168.0.105"),
        (r"\\Device\\NPF_{B}", "192.168.1.5"),
    ]
    _stub_ifaces(monkeypatch, ifaces)
    _stub_link_status(monkeypatch, set())
    _fake_conf_route(monkeypatch, {})
    assert pick_active_iface("203.0.113.8") == r"\\Device\\NPF_{A}"


def test_link_status_unavailable_degrades_to_healthy_only(monkeypatch):
    """取不到链路状态（非 Windows / 查询失败）→ 降级放行，行为与加判据前一致。"""
    ifaces = [
        (r"\\Device\\NPF_{TAP}", "169.254.1.1"),
        (r"\\Device\\NPF_{WLAN}", "192.168.0.105"),
    ]
    _stub_ifaces(monkeypatch, ifaces)
    monkeypatch.setattr(net_iface, "_link_status_map", lambda: None)
    assert net_iface.is_iface_link_up(r"\\Device\\NPF_{WLAN}") is True
    assert net_iface._usable_ifaces() == [(r"\\Device\\NPF_{WLAN}", "192.168.0.105")]


def test_link_status_empty_degrades_to_healthy_only(monkeypatch):
    """状态表为空（结构体布局与个别系统不符）→ 视为取不到状态，降级放行。"""
    ifaces = [(r"\\Device\\NPF_{WLAN}", "192.168.0.105")]
    _stub_ifaces(monkeypatch, ifaces)
    monkeypatch.setattr(net_iface, "_link_status_map", lambda: {})
    assert net_iface.is_iface_link_up(r"\\Device\\NPF_{WLAN}") is True


def test_resolve_p2_downgrade_when_link_down(tmp_path, monkeypatch):
    """P2 学习值仍存在但链路已断开 → 降级 P3（卡被禁用/拔掉同理）。"""
    _settings_path2(tmp_path, monkeypatch)
    settings.save_capture_iface_learned(r"\\Device\\NPF_{LEARNED}")
    monkeypatch.setattr(
        net_iface, "list_capture_ifaces",
        lambda: [(r"\\Device\\NPF_{LEARNED}", "192.168.0.105")],
    )
    _stub_link_status(monkeypatch, set())
    monkeypatch.setattr(net_iface, "pick_active_iface", lambda t=None: r"\\Device\\NPF_{P3}")
    assert resolve_capture_iface(None) == r"\\Device\\NPF_{P3}"