"""通用工具函数 — 不归属特定模块的小工具。"""
import os
import re
from datetime import datetime


def safe_filename(name: str) -> str:
    """将文件名非法字符替换为下划线。

    Args:
        name: 原始字符串（如卡池名）

    Returns:
        安全的文件名字符串
    """
    return re.sub(r'[\\/:*?"<>|\[\]\s]+', "_", name).strip("_")


def format_timestamp(ts: int) -> str:
    """毫秒时间戳转可读字符串。

    Args:
        ts: 毫秒时间戳

    Returns:
        "%Y-%m-%d %H:%M:%S" 格式字符串
    """
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")


def check_npcap_installed() -> tuple:
    """检测 Npcap 是否安装并可用。

    Returns:
        (installed, working)
        - (False, False): 未安装
        - (True, True): 已安装且可用
        - (True, False): 已安装但初始化失败
    """
    install_dirs = [
        r"C:\Windows\System32\Npcap",
        r"C:\Windows\SysWOW64\Npcap",
    ]
    installed = any(os.path.isdir(p) for p in install_dirs)

    if installed:
        try:
            from scapy.all import get_windows_if_list

            ifaces = get_windows_if_list()
            if ifaces:
                return True, True
        except Exception:
            pass

        dll_paths = [
            r"C:\Windows\System32\Npcap\wpcap.dll",
            r"C:\Windows\System32\wpcap.dll",
        ]
        if any(os.path.exists(d) for d in dll_paths):
            return True, True
        return True, False

    return False, False
