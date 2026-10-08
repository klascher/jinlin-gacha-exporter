"""抽卡服务器地址动态发现 - 全量 TCP 抓包 + 协议特征识别。

发现流程：sniff 所有 TCP 流量（排除局域网 src），对每条流重组切帧，用抽卡协议特征
（4 字节长度头 + Protobuf 0x1a/0x08/0x10/0x18，可解出 pool_id/item_id/timestamp）
判定命中，命中流的 (src_ip, sport) 即抽卡服务器地址。由 GUI 的「开始捕获/完成」
按钮控制起止，仅分析这段时间窗口内的流量。

不依赖固定端口，地址变更后用户自助翻页即可重新发现。
"""
import socket
import threading
from collections import defaultdict
from typing import Callable, List, Optional, Tuple

from ..analysis.parser import looks_like_gacha
from ..capture.engine import extract_frames
from ..models import CaptureConfig
from ..utils.helpers import check_npcap_installed
from ..utils.logger import get_logger

logger = get_logger(__name__)

# BPF 过滤：排除局域网 src，抽卡服务器为公网，减小抓包量
_BPF_FILTER = (
    "tcp and not (src net 192.168.0.0/16 or 10.0.0.0/8 "
    "or 172.16.0.0/12 or 127.0.0.0/8)"
)

# sniff 单次阻塞窗口。scapy sniff(timeout=X) 到 X 秒即返回（无论有无流量），据此在
# 每次返回后检查停止事件，实现无流量也能及时打断；避免 stop_filter 无流量永不触发。
# 取 0.3s：停止响应 ~0.3s 内，且窗口够长不频繁起停 Npcap socket（复用阻塞内核读）。
_SNIFF_TIMEOUT = 0.3


class AddressDiscovery:
    """地址发现器 - 抓取时间窗口内的全量 TCP，用协议特征识别抽卡流。

    复用 capture.engine.extract_frames 切帧 + analysis.parser.looks_like_gacha
    特征判定，与持续抓包的 CaptureEngine 解耦。
    """

    def __init__(self, config: Optional[CaptureConfig] = None):
        self.config = config or CaptureConfig()
        self._running = False
        self._stop_event: threading.Event = threading.Event()
        self._streams: dict = defaultdict(bytes)
        self._found: set = set()  # 已命中的 (src_ip, sport)
        self._on_hit: Optional[Callable[[Tuple[str, int]], None]] = None
        self.used_iface: Optional[str] = None  # 本次实际抓包用的网卡（成功时供 P2 学习）

    def discover(
        self,
        on_hit: Optional[Callable[[Tuple[str, int]], None]] = None,
        stop_event: Optional[threading.Event] = None,
        iface: str = "",
    ) -> List[Tuple[str, int]]:
        """运行一次发现扫描，返回去重的 (ip, port) 列表。

        阻塞直到 stop_event 被 set（GUI「完成」按钮触发）。应在 QThread 中调用。

        Args:
            on_hit: 每次发现新地址时的实时回调（刷新 UI 计数）
            stop_event: 外部停止信号
            iface: 要嗅探的网卡（NPF 名）；空 = 由 scapy 决定（旧行为）

        Returns:
            去重的 (ip, port) 列表
        """
        installed, working = check_npcap_installed()
        if not installed:
            raise RuntimeError("未检测到 Npcap，请先安装 Npcap (WinPcap API-compatible Mode)")
        if not working:
            raise RuntimeError("Npcap 已安装但初始化失败")

        self._on_hit = on_hit
        self._stop_event = stop_event or threading.Event()
        self._running = True
        self._streams.clear()
        self._found.clear()
        self.used_iface = iface or None

        logger.info(f"开始地址发现：全量抓包 + 协议特征识别（iface={self.used_iface or '自动'}）")
        try:
            from scapy.all import sniff

            sniff_kwargs = {
                "filter": _BPF_FILTER,
                "prn": self._process_packet,
                "store": False,
            }
            if iface:
                sniff_kwargs["iface"] = iface
            # 不用 stop_filter：无流量时它只在收到下一包才触发，未命中地址（或本就没
            # 流量）时 sniff 永不返回、线程残留，导致「完成/取消」后 GUI 阻塞等线程
            # 退出而无响应。改为超时轮询——每个 timeout 块返回一次并检查停止事件，
            # 停止响应约 0.3s 内，彻底消除「线程永不退出 + GUI 长等待」。
            while not self._stop_event.is_set():
                sniff(timeout=_SNIFF_TIMEOUT, **sniff_kwargs)
        except socket.error as e:
            logger.error(f"地址发现 socket 错误: {e}")
        finally:
            self._running = False
            logger.info(f"地址发现结束，命中 {len(self._found)} 个地址: {sorted(self._found)}")

        return sorted(self._found)

    def _process_packet(self, pkt) -> None:
        """处理单个包：重组流 + 切帧 + 抽卡特征试探，命中记录 (src_ip, sport)。"""
        from scapy.all import IP, TCP, Raw

        if not (IP in pkt and TCP in pkt and Raw in pkt):
            return

        key = (pkt[IP].src, pkt[TCP].sport, pkt[IP].dst, pkt[TCP].dport)
        payload = bytes(pkt[Raw].load)
        self._streams[key] += payload

        frames, remaining = extract_frames(
            self._streams[key], self.config.min_frame_len, self.config.max_frame_len
        )
        self._streams[key] = remaining

        for frame in frames:
            if looks_like_gacha(frame):
                addr = (pkt[IP].src, pkt[TCP].sport)
                if addr not in self._found:
                    self._found.add(addr)
                    logger.info(f"发现抽卡服务器地址: {addr[0]}:{addr[1]}")
                    if self._on_hit:
                        self._on_hit(addr)

    def stop(self) -> None:
        """请求停止发现（GUI「完成」按钮调用）。"""
        self._stop_event.set()

    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._running

    def discover_addresses(self) -> List[Tuple[str, int]]:
        """兼容旧接口的别名。"""
        return self.discover()
