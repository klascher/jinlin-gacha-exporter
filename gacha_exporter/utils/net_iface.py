"""抓包网卡自动选择 — 不依赖 scapy 默认接口实用。

背景（docs/抓包网卡错选问题分析与方案.md）：windows 路由表存在多条默认路由时，
scapy 按 metric 选最小时会把默认接口解析到断链的虚拟网卡（如 TAP，169.254 APIPA、
零流量），导致正式抓包与地址发现同时 0 条。本模块按「目标 IP 路由决策 + APIPA
护栏」自动选卡：

- 遍历 NPF 接口，读各自 IPv4；
- 剔除 loopback / 0.0.0.0 / 169.254（APIPA = 断链/虚拟残留）；
- 对有目标 IP 时查 OS 路由取出口接口并健康校验；无目标（地址发现前）用哨兵公网 IP。
- resolve_capture_iface() 组装三层优先树：P1 显式 → P2 学习(复查) → P3 实时。

**懒加载约束**：scapy 相关 import 一律在函数内，禁止模块顶层 —— `test_startup_imports.py`
强制启动路径不拉重依赖（沿 engine._open_pcap / helpers 先例）。
"""
import ctypes
import ipaddress
from typing import Dict, List, Optional, Tuple

from .logger import get_logger

logger = get_logger(__name__)

# 哨兵公网 IP：地址发现阶段（尚无目标服务器 IP）用它查"上公网走哪块卡"出口。
# 抽卡服务器为公网、与哨兵同出口，够用。策略路由/VPN 分隧道时为⚠️级，交给下拉兜底。
_SENTINEL_PUBLIC_IP = "1.1.1.1"

IFACE_AUTO = ""  # 空串 = 智能（自动），落到 P2/P3

# ---- 链路状态判据（Windows）----
# 背景：仅靠 IP 类别（APIPA 护栏）挡不住"虚拟网卡拿到正常私有地址"的情况——
# VPN / 加速器 / TAP-Wintun 类虚拟网卡即使处于断开状态，也常残留 metric 更小的
# 默认网关；一旦被短暂激活就会拿到正常私有地址，顺利通过健康校验并抢走默认路由，
# scapy 由此把抓包出口解析到这条零流量的隧道，正式抓包与地址发现同时 0 条。
# 故补一条与机器无关的通用判据：链路未 Up 的适配器一律不作抓包出口。
_IF_OPER_STATUS_UP = 1
_GAA_FLAG_INCLUDE_ALL_INTERFACES = 0x0100
_ADAPTERS_BUFFER_SIZE = 16384
_ERROR_BUFFER_OVERFLOW = 111


class _IPAdapterAddresses(ctypes.Structure):
    """IP_ADAPTER_ADDRESSES 前缀结构（只用到 Next / AdapterName / OperStatus）。"""


_IPAdapterAddresses._fields_ = [
    ("_align", ctypes.c_ulonglong),                    # Length(4) + IfIndex(4)
    ("Next", ctypes.POINTER(_IPAdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("_skip_anycast", ctypes.c_void_p),
    ("_skip_unicast", ctypes.c_void_p),
    ("_skip_multicast", ctypes.c_void_p),
    ("_skip_dns", ctypes.c_void_p),
    ("_dns_suffix", ctypes.c_wchar_p),
    ("_description", ctypes.c_wchar_p),
    ("_friendly_name", ctypes.c_wchar_p),
    ("_physical", ctypes.c_ubyte * 8),
    ("_physical_len", ctypes.c_ulong),
    ("_flags", ctypes.c_ulong),
    ("_mtu", ctypes.c_ulong),
    ("_if_type", ctypes.c_ulong),
    ("OperStatus", ctypes.c_ulong),
]


def _iface_guid(iface: str) -> str:
    """从 NPF 名（\\Device\\NPF_{GUID}）里取 GUID，取不到返回 ""。"""
    if not iface:
        return ""
    tail = iface.rsplit("{", 1)[-1]
    if "}" not in tail:
        return ""
    return tail.split("}", 1)[0].strip().upper()


def _link_status_map() -> Optional[Dict[str, bool]]:
    """本机网卡的链路状态表 {GUID: 是否 Up}（GUID 大写、不含花括号）。

    Returns:
        状态表；非 Windows / 查询失败返回 None —— 调用方按「不限制」处理。
        只踢"明确 Down"的卡：查不到的接口一律放行，判据只做减法。
    """
    if not hasattr(ctypes, "windll"):
        return None
    try:
        get_adapters = ctypes.windll.iphlpapi.GetAdaptersAddresses
        size = ctypes.c_ulong(_ADAPTERS_BUFFER_SIZE)
        buf = ctypes.create_string_buffer(size.value)

        def _query() -> int:
            return get_adapters(
                2,  # AF_INET
                _GAA_FLAG_INCLUDE_ALL_INTERFACES,
                None,
                buf,
                ctypes.byref(size),
            )

        ret = _query()
        if ret == _ERROR_BUFFER_OVERFLOW and size.value > _ADAPTERS_BUFFER_SIZE:
            buf = ctypes.create_string_buffer(size.value)
            ret = _query()
        if ret != 0:
            logger.debug(f"查询网卡链路状态失败（code={ret}），降级：不做链路限制")
            return None

        status: Dict[str, bool] = {}
        node = ctypes.cast(buf, ctypes.POINTER(_IPAdapterAddresses))
        while node:
            adapter = node.contents
            guid = _iface_guid(
                (adapter.AdapterName or b"").decode("ascii", "ignore")
            )
            if guid:
                status[guid] = adapter.OperStatus == _IF_OPER_STATUS_UP
            node = adapter.Next
        if not status:
            # 正常机器至少能枚举到一块网卡；空表说明结构体布局与当前系统不符，
            # 此时按"取不到状态"降级，避免判据在个别机器上误杀全部网卡。
            logger.debug("网卡链路状态表为空，降级：不做链路限制")
            return None
        return status
    except Exception as e:  # 结构/系统差异一律降级，不阻塞选卡
        logger.debug(f"查询网卡链路状态异常，降级：不做链路限制（{e}）")
        return None


def is_iface_link_up(iface: str) -> bool:
    """接口链路是否 Up（Windows 依据 OperStatus）。

    取不到状态（非 Windows / 查询失败）、接口名不含 GUID（如 NPF_Loopback）、
    或该接口不在系统适配器表中时，一律返回 True —— 降级放行，不改变既有行为。
    """
    status = _link_status_map()
    if status is None:
        return True
    guid = _iface_guid(iface)
    if not guid or guid not in status:
        return True
    return status[guid]


def _is_healthy(ip: str) -> bool:
    """是否健康外联网卡（剔除 loopback / 0.0.0.0 / 169.254 APIPA）。"""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local:  # 127.0.0.0/8、169.254/16(APIPA)
        return False
    if addr.is_unspecified:  # 0.0.0.0
        return False
    return True


def list_capture_ifaces() -> List[Tuple[str, str]]:
    """列出本机全部可嗅探接口（NPF 名，IPv4）。

    Returns:
        [(iface_name, ip_or_empty), ...]，顺序即 Npcap 返回顺序（含断链/虚拟卡，
        是否健康由调用方用 _is_healthy 判断，或直接交给 pick/resolve 过滤）。
    """
    from scapy.all import get_if_addr, get_if_list  # 懒加载

    out: List[Tuple[str, str]] = []
    try:
        for name in get_if_list():
            try:
                ip = get_if_addr(name) or ""
            except Exception:
                ip = ""
            out.append((name, ip))
    except Exception:
        return out
    return out


def _healthy_ifaces() -> List[Tuple[str, str]]:
    """过滤出健康外联网卡（剔掉 loopback/0.0.0.0/169.254）。"""
    return [(n, ip) for n, ip in list_capture_ifaces() if _is_healthy(ip)]


def _usable_ifaces() -> List[Tuple[str, str]]:
    """健康 + 链路 Up 的网卡（在 _healthy_ifaces 之上叠加链路判据）。

    链路判据只做减法：若它把全部健康卡都剔掉了（异常环境），回退到仅按地址
    健康筛选并告警，避免出现「一块卡都选不出来」。
    """
    healthy = _healthy_ifaces()
    usable = [(n, ip) for n, ip in healthy if is_iface_link_up(n)]
    if usable or not healthy:
        return usable
    logger.warning("链路状态判据剔除了全部健康网卡，回退为仅按地址健康筛选")
    return healthy


def pick_active_iface(target: Optional[str] = None) -> Optional[str]:
    """按路由决策选出当前应抓包的健康网卡。

    Args:
        target: 目标服务器 IP。有则精确查其出口；None（地址发现前）用哨兵公网 IP。

    Returns:
        选中的 NPF 接口名，无可选健康卡时返回 None（调用方回退旧行为/下拉兜底）。

    不依赖 scapy 默认 `conf.iface`（那是 bug 源），只借用 `conf.route.route()` 拿
    出口接口，再做健康校验覆写：即使路由被 metric 小的持久路由 hijack 到 TAP，
    目标 TAP 的接口因 IP 不健康也会被剔除，回退到健康卡。
    """
    healthy = _usable_ifaces()
    if not healthy:
        return None

    from scapy.all import conf  # 懒加载

    dst = target or _SENTINEL_PUBLIC_IP
    try:
        routed_iface, _src, _gw = conf.route.route(dst)
        # 路由出口若本身健康，直接用（WLAN 正常路径）。
        for name, ip in healthy:
            if name == routed_iface:
                return name
        # 路由出口被断链卡（TAP）占用 —— 回退健康卡第一个有真实外联地址者。
        return healthy[0][0]
    except Exception:
        return healthy[0][0]


def resolve_capture_iface(target: Optional[str] = None) -> str:
    """组装网卡来源优先树，返回本次应用的接口名（"" = 自动/无可选）。

    优先级：P1 显式（用户下拉）→ P2 学习（上次成功，复查存在+健康）→ P3 实时。
    铁护栏：本函数只读各槽，永不写 P1；P2/P3 由独立写入方（保存/学习函数）负责。

    Args:
        target: 目标服务器 IP（地址发现成功后），供 P3 精确路由；None 用哨兵。

    Returns:
        NPF 接口名，或无合适接口时 ""（调用方回退旧行为：不传 iface）。
    """
    from ..storage.settings import load_capture_iface, load_capture_iface_learned

    # P1 显式（用户手动下拉）—— 直接信，不校验。
    explicit = load_capture_iface()
    if explicit:
        return explicit

    # P2 学习（上次成功那块）—— 复查"现在仍存在 + 地址类健康"才用。
    learned = load_capture_iface_learned()
    if learned:
        for name, _ip in list_capture_ifaces():
            # 复查"仍存在 + 地址健康 + 链路 Up"：卡被禁用/断开则降级 P3。
            if name == learned and _is_healthy(_ip) and is_iface_link_up(name):
                return learned

    # P3 实时（智能默认/哨兵）—— 每次现算，永远追随当下拓扑。
    picked = pick_active_iface(target)
    return picked or ""