"""调试抓包开关 —— 打包态恒关，仅开发环境可经环境变量开启。

pcap 落盘与逐帧取证日志（frame_debug/request_debug）都会把原始流量写盘，
消耗 I/O 且泄露抓包数据；生产（PyInstaller 打包）下必须全关。开发环境用
JINLIN_DEBUG=1 临时开启便于排障。

打包判定用 sys.frozen（bootstrap/onefile exe 恒为 True）：即使设置了
JINLIN_DEBUG 也无法在打包态开启，保证生产不产生取证类落盘。
"""
import os
import sys


def is_debug_capture_enabled() -> bool:
    """是否允许产生取证类落盘（pcap / 逐帧 hex 日志）。

    Returns:
        打包（sys.frozen）恒 False；开发环境 JINLIN_DEBUG=1 时为 True。
    """
    if getattr(sys, "frozen", False):
        return False
    return os.environ.get("JINLIN_DEBUG") == "1"
