"""抓包工作线程 — QThread 包装 CaptureEngine，避免阻塞 GUI。"""
from PySide6.QtCore import QThread, Signal

from ..capture.engine import CaptureEngine
from ..models import CaptureConfig


class CaptureWorker(QThread):
    """后台抓包工作线程。"""

    packet_received = Signal(bytes)
    request_received = Signal(bytes)
    error_occurred = Signal(str)

    def __init__(self, config: CaptureConfig, parent=None):
        """初始化。

        Args:
            config: 抓包配置
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.engine = CaptureEngine(config)
        self.engine.set_packet_callback(self._on_packet)
        self.engine.set_request_callback(self._on_request)
        self._config = config

    def _on_packet(self, frame: bytes) -> None:
        """收到服务器->客户端完整帧时发射信号。"""
        self.packet_received.emit(frame)

    def _on_request(self, frame: bytes) -> None:
        """收到客户端->服务器完整帧（切池/翻页请求）时发射信号。"""
        self.request_received.emit(frame)

    def run(self) -> None:
        """线程入口 — 调用引擎阻塞抓包。"""
        try:
            self.engine.start()
        except Exception as e:
            self.error_occurred.emit(str(e))

    def stop(self) -> None:
        """请求停止抓包。"""
        self.engine.stop()
