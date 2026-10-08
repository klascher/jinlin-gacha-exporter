"""抓包引擎 — 使用 scapy 监听指定端口，TCP 流重组，按 4 字节长度头切帧。

双方向处理：服务器->客户端（sport in ports / 来自 addresses）走 packet_callback
（抽卡记录响应）；客户端->服务器（dport in ports / 去往 addresses）走
request_callback（切池/翻页请求，供视图感知层配对当前卡池页面）。
"""
import os
import socket
import struct
import threading
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from ..models import CaptureConfig
from ..utils.helpers import check_npcap_installed
from ..utils.logger import get_logger
from .frame_schema import envelope_plausible

logger = get_logger(__name__)

# 原始抓包落盘仅 debug 模式（config.pcap_enabled 为 True 时）：把抽卡相关包
# 原样写入 data/logs/capture_<时间戳>.pcap，超过该上限停止记录。打包态恒
# False（见 debug_flags），生产不产生 pcap。
_PCAP_MAX_BYTES = 256 * 1024 * 1024

PacketCallback = Callable[[bytes], None]


def _flow_key(pkt) -> tuple:
    """生成 TCP 流四元组键（源地址、源端口、目的地址、目的端口）。"""
    from scapy.all import IP, TCP

    return (pkt[IP].src, pkt[TCP].sport, pkt[IP].dst, pkt[TCP].dport)


def _frame_start_plausible(
    data: bytes, pos: int, min_len: int, max_len: int, server_to_client: Optional[bool]
) -> bool:
    """data[pos:] 是否像一个合法帧的起点（长度合理 + 已知信封自洽）。

    失步重对齐评分用。信封校验按 frame_schema 白名单（抽卡/心跳两种已知）：
    未知消息类型的帧无法在此评分通过--但失步只会发生在流损坏之后，且我们
    只解析抽卡帧（信封已知），丢未知类型帧不影响业务；帧边界本身仍由长度头
    决定，未失步时未知类型照常切出。帧体尚不足 9B（信封读不全，半包）时按
    长度暂定可行，等更多字节再判。
    """
    if pos + 4 > len(data):
        return False
    length = struct.unpack(">I", data[pos:pos + 4])[0]
    if length < min_len or length > max_len:
        return False
    body = data[pos + 4:]
    if server_to_client is None:
        plausible = envelope_plausible(body, True) or envelope_plausible(body, False)
    else:
        plausible = envelope_plausible(body, server_to_client)
    if plausible:
        return True
    # 整帧已在缓冲但信封不符 -> 真不是帧起点；半包且信封字节未到齐 -> 暂定可行
    return 4 + length > len(data) and len(body) < 9


def extract_frames(
    data: bytes,
    min_len: int,
    max_len: int,
    server_to_client: Optional[bool] = None,
) -> Tuple[List[bytes], bytes]:
    """从缓冲区按大端 4 字节长度头切帧（模块级，供 CaptureEngine 与地址发现复用）。

    帧结构见 frame_schema（抓包样本逐字节核对）：长度头值 = 帧体字节数，双向一致；
    失步重对齐按「长度合理 + 已知信封」一次跳到下一个结构自洽位置（结构化，
    2026-08 重构；此前为逐字节盲试，任何短帧出现即连锁错位--曾致切池/翻页
    请求被吞、视图配对失效、跨卡池去重失效、同批记录重复计入）。

    Args:
        data: 当前流缓冲
        min_len / max_len: 长度头合法范围
        server_to_client: 流方向（True=S->C / False=C->S），信封校验按方向走；
            None 表示方向未知（地址发现），任一方向信封匹配即可

    Returns:
        (frames, remaining)
    """
    frames = []
    while len(data) >= 4:
        length = struct.unpack(">I", data[:4])[0]
        if length < min_len or length > max_len:
            # 越界：整帧在则整帧跳过（合法但超短/超长的帧，如 C->S 9B 心跳、
            # S->C 8B 被拒响应落在此分支时流仍对齐）；半包/真错位才重对齐
            if 4 + length <= len(data):
                data = data[4 + length:]
                continue
            # 结构化重对齐：跳到下一个「长度合理 + 信封自洽」的偏移；无候选
            # 则保留缓冲等更多字节（半包）
            resync_at = next(
                (
                    p
                    for p in range(1, len(data))
                    if _frame_start_plausible(data, p, min_len, max_len, server_to_client)
                ),
                None,
            )
            if resync_at is None:
                # 整段无结构自洽的候选 -> 视为垃圾丢弃，仅保留尾部 3 字节
                # （长度头可能跨 TCP 包分裂，为它保留重组成 4B 的机会）
                data = data[-3:]
                break
            data = data[resync_at:]
            continue
        if len(data) < 4 + length:
            break
        frames.append(data[4 : 4 + length])
        data = data[4 + length:]
    return frames, data


class CaptureEngine:
    """回调式抓包引擎。"""

    def __init__(self, config: Optional[CaptureConfig] = None):
        """初始化。

        Args:
            config: 抓包配置，缺省使用 CaptureConfig()
        """
        self.config = config or CaptureConfig()
        self.packet_callback: Optional[PacketCallback] = None
        self.request_callback: Optional[PacketCallback] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._streams: Dict[tuple, bytes] = defaultdict(bytes)
        # 原始抓包落盘（pcap，仅 debug 模式）：把抽卡相关包（请求+响应、含方向
        # 判定后会被丢弃的）原样写盘，便于事后逐字节追踪源数据。打包态恒关闭，
        # 由 config.pcap_enabled 控制（见 debug_flags）。
        self._pcap_writer = None
        self._pcap_path: Optional[str] = None
        self._pcap_bytes: int = 0

    def set_packet_callback(self, callback: PacketCallback) -> None:
        """设置收到完整帧后的回调。"""
        self.packet_callback = callback

    def set_request_callback(self, callback: PacketCallback) -> None:
        """设置收到客户端->服务器方向完整帧的回调（切池/翻页请求）。"""
        self.request_callback = callback

    def _open_pcap(self) -> None:
        """开启一次抓包的原始包落盘（data/logs/capture_<时间戳>.pcap）。"""
        from scapy.utils import PcapWriter
        from scapy.all import DLT_EN10MB
        from ..utils.paths import get_logs_dir

        if self._pcap_writer is not None:
            self._close_pcap()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(get_logs_dir(), f"capture_{ts}.pcap")
        try:
            self._pcap_writer = PcapWriter(path, linktype=DLT_EN10MB, sync=True)
            self._pcap_path = path
            self._pcap_bytes = 0
            logger.info(f"原始抓包已开启: {path}")
        except Exception as e:
            logger.warning(f"开启原始抓包失败，跳过落盘: {e}")
            self._pcap_writer = None
            self._pcap_path = None

    def _close_pcap(self) -> None:
        """关闭并落盘当前原始抓包文件。"""
        if self._pcap_writer is not None:
            try:
                self._pcap_writer.close()
                logger.info(f"原始抓包已保存: {self._pcap_path}")
            except Exception as e:
                logger.warning(f"关闭原始抓包失败: {e}")
        self._pcap_writer = None
        self._pcap_path = None

    def _maybe_record_packet(self, pkt) -> None:
        """把命中抽卡范围（端口或已发现地址）的包原样写入 pcap。

        在方向判定之前调用：即使某个包随后因不匹配 addresses 被丢弃（比如请求
        发往了未发现的端点），也先落盘，保证事后能看清它的真实去向。
        """
        from scapy.all import IP, TCP

        if self._pcap_writer is None:
            return
        try:
            if IP not in pkt or TCP not in pkt:
                return
            if (pkt[TCP].sport in self.config.ports
                    or pkt[TCP].dport in self.config.ports
                    or (pkt[IP].src, pkt[TCP].sport) in self.config.addresses
                    or (pkt[IP].dst, pkt[TCP].dport) in self.config.addresses):
                self._pcap_writer.write(pkt)
                self._pcap_bytes += len(pkt)
                if self._pcap_bytes > _PCAP_MAX_BYTES:
                    logger.warning(f"原始抓包超过上限({_PCAP_MAX_BYTES}B)，停止记录")
                    self._close_pcap()
        except Exception as e:
            logger.warning(f"原始抓包写盘失败: {e}")

    def _process_packet(self, pkt) -> None:
        """处理单个 scapy 包。"""
        from scapy.all import IP, TCP, Raw

        # 原始抓包：先把命中抽卡范围的包写盘（含方向判定后会被丢弃的），再做业务
        # 处理——确保请求端点的真实去向不会被漏掉。
        self._maybe_record_packet(pkt)
        if not (IP in pkt and TCP in pkt and Raw in pkt):
            return
        # 双路过滤按方向匹配：
        # - S->C（服务器->客户端）：addresses 命中 (src, sport)，否则 sport in ports
        # - C->S（客户端->服务器）：addresses 命中 (dst, dport)，否则 dport in ports
        is_server_to_client: bool
        if self.config.addresses:
            if (pkt[IP].src, pkt[TCP].sport) in self.config.addresses:
                is_server_to_client = True
            elif (pkt[IP].dst, pkt[TCP].dport) in self.config.addresses:
                is_server_to_client = False
            else:
                # 诊断：端口在抽卡范围但方向不命中 addresses —— 请求可能去了
                # 未发现的端点（另一 IP 或另一端口），导致 C->S 方向整段丢失。
                if pkt[TCP].sport in self.config.ports or pkt[TCP].dport in self.config.ports:
                    logger.info(
                        f"方向不匹配丢弃 {pkt[IP].src}:{pkt[TCP].sport}->{pkt[IP].dst}:{pkt[TCP].dport} "
                        f"(端口在抽卡范围但不在 addresses={self.config.addresses})"
                    )
                return
        elif pkt[TCP].sport in self.config.ports:
            is_server_to_client = True
        elif pkt[TCP].dport in self.config.ports:
            is_server_to_client = False
        else:
            return

        key = _flow_key(pkt)
        payload = bytes(pkt[Raw].load)
        self._streams[key] += payload

        frames, remaining = extract_frames(
            self._streams[key],
            self.config.min_frame_len,
            self.config.max_frame_len,
            server_to_client=is_server_to_client,
        )
        self._streams[key] = remaining

        if not is_server_to_client:
            # 诊断：每个 C->S 包的重组/切帧情况。请求帧小（9~15B），若某段期间
            # 响应在、这里却没有 C->S 包日志 → 该方向包根本没进 scapy（接口/过滤/
            # 端点问题）；若有包但切帧=0 → 流重组失步，缓冲里是废字节。
            logger.info(
                f"C->S {pkt[IP].src}:{pkt[TCP].sport}->{pkt[IP].dst}:{pkt[TCP].dport} "
                f"payload={len(payload)}B 切帧={len(frames)} 余留={len(self._streams[key])}B"
            )
            if not frames and self._streams[key]:
                logger.info(f"  C->S 缓冲未成帧: {self._streams[key][:48].hex()}")

        callback = self.packet_callback if is_server_to_client else self.request_callback
        for frame in frames:
            if callback:
                try:
                    callback(frame)
                except Exception as e:
                    logger.exception(f"包回调异常: {e}")

    def _sniff_loop(self) -> None:
        """在独立线程中运行 scapy.sniff。"""
        from scapy.all import sniff

        logger.info(f"开始抓包，监听端口: {self.config.ports}")
        # 显式指定网卡时传入（避免 scapy 默认接口选错断链虚拟网卡）；空 = 交给
        # 调用方已在 CaptureConfig 里 resolve 过的 iface 或旧行为（不传由 scapy 决定）。
        sniff_kwargs = {
            "filter": "tcp",
            "prn": self._process_packet,
            "store": False,
            "stop_filter": lambda _: self._stop_event.is_set(),
        }
        if self.config.iface:
            sniff_kwargs["iface"] = self.config.iface
        try:
            sniff(**sniff_kwargs)
        except socket.error as e:
            logger.error(f"抓包 socket 错误: {e}")
        finally:
            self._running = False
            self._close_pcap()
            logger.info("抓包线程结束")

    def start(self) -> None:
        """启动抓包（阻塞直到 stop() 被调用）。

        注意：应在子线程中调用，避免阻塞 GUI 主线程。
        """
        installed, working = check_npcap_installed()
        if not installed:
            raise RuntimeError("未检测到 Npcap，请先安装 Npcap (WinPcap API-compatible Mode)")
        if not working:
            raise RuntimeError("Npcap 已安装但初始化失败，请检查 Npcap 服务/WinPcap API 模式")

        self._running = True
        self._stop_event.clear()
        self._streams.clear()
        # 原始抓包落盘仅 debug 模式（config.pcap_enabled，打包态恒 False）：
        # 生产不写 pcap，避免原始流量落盘（见 debug_flags）。
        if self.config.pcap_enabled:
            self._open_pcap()
        self._sniff_loop()

    def start_async(self) -> None:
        """在后台线程启动抓包。"""
        if self._running:
            return
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止抓包。"""
        self._stop_event.set()
        self._running = False
        logger.info("请求停止抓包")

    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._running
