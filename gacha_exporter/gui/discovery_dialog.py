"""地址发现对话框 - 引导用户翻页，时间窗口内全量抓包识别抽卡服务器地址。

用户点「开始捕获」后切换到游戏抽卡界面翻几页，再点「完成」，工具对这段时间
内的流量做协议特征识别，命中流的 (ip, port) 即抽卡服务器地址。识别成功后由
主窗口替换默认端口并持久化。
"""
import threading
from typing import List, Tuple

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..loaders.address_discovery import AddressDiscovery
from ..storage.settings import (
    load_capture_iface,
    save_capture_iface_learned,
)
from ..utils.logger import get_logger
from ..utils.net_iface import IFACE_AUTO, list_capture_ifaces, resolve_capture_iface

logger = get_logger(__name__)

_GUIDE = (
    "1. 点击下方「开始捕获」，工具开始监听网络流量\n"
    "2. 切换到游戏，打开「抽卡记录」页面，在单独卡池页\n"
    "   （如 UP 池、常驻池）翻阅几页记录，或切换几个卡池\n"
    "   ★ 请勿切到「全部卡池」页面：翻过的页会被游戏缓存，\n"
    "     之后正式抓包时这些页将抓不到，需重新登录游戏才能恢复\n"
    "3. 回到工具，点击「完成」\n\n"
    "工具会自动识别这段时间内与抽卡服务器的通信地址，\n"
    "识别成功后将自动保存为当前账号的抓包地址。"
)


class DiscoveryWorker(QThread):
    """后台运行 AddressDiscovery.discover 的工作线程。

    on_hit 回调在 scapy 抓包线程触发，通过信号投递到主线程刷新 UI；
    finished_discovery 在 discover 返回后发出最终结果列表。
    """

    hit_found = Signal(str, int)  # (ip, port)
    finished_discovery = Signal(list)  # List[Tuple[str, int]]
    error_occurred = Signal(str)

    def __init__(self, iface: str = "", parent=None):
        super().__init__(parent)
        self._discovery = AddressDiscovery()
        self._stop_event = threading.Event()
        self._iface = iface

    def run(self) -> None:
        try:
            result = self._discovery.discover(
                on_hit=self._on_hit,
                stop_event=self._stop_event,
                iface=self._iface,
            )
            self.finished_discovery.emit(list(result))
        except Exception as e:
            logger.exception(f"地址发现失败: {e}")
            self.error_occurred.emit(str(e))

    def used_iface(self) -> str:
        """返回本次实际抓包用的网卡（Discover 结束后有效，供 P2 学习）。"""
        return self._discovery.used_iface or ""

    def _on_hit(self, addr) -> None:
        """scapy 线程回调：转发为新地址信号。"""
        self.hit_found.emit(addr[0], addr[1])

    def stop_discovery(self) -> None:
        """请求停止发现（GUI「完成」按钮调用）。"""
        self._stop_event.set()


class DiscoveryDialog(QDialog):
    """地址发现对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自助获取抽卡地址")
        self.resize(480, 460)
        self._worker: DiscoveryWorker | None = None
        self._found: List[Tuple[str, int]] = []
        self._closed = False  # dialog 已关闭（取消/关窗），忽略迟到信号
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        guide = QLabel(_GUIDE)
        guide.setWordWrap(True)
        layout.addWidget(guide)

        # 网卡选择行：默认「智能（自动）」，用户可手动指定本次发现的抓包网卡。
        iface_row = QHBoxLayout()
        iface_row.addWidget(QLabel("抓包网卡:"))
        self.combo_iface = QComboBox()
        self.combo_iface.addItem("智能（自动）", IFACE_AUTO)
        # 告示当前显式选择（P1）；未设则为智能
        current = load_capture_iface()
        for name, ip in list_capture_ifaces():
            label = f"{name}（{ip}）" if ip else name
            self.combo_iface.addItem(label, name)
            if name == current:
                self.combo_iface.setCurrentIndex(self.combo_iface.count() - 1)
        if not current:
            self.combo_iface.setCurrentIndex(0)
        iface_row.addWidget(self.combo_iface, 1)
        layout.addLayout(iface_row)

        self.count_label = QLabel("已识别地址数：0")
        self.count_label.setStyleSheet("color: #666; padding: 6px 0;")
        layout.addWidget(self.count_label)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("开始捕获")
        self.btn_done = QPushButton("完成")
        self.btn_done.setEnabled(False)
        self.btn_cancel = QPushButton("取消")
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_done)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_done.clicked.connect(self._on_done)
        self.btn_cancel.clicked.connect(self.reject)

    def _on_start(self) -> None:
        """启动后台发现线程（传入本次选择的网卡；空=智能，由 resolve 兜底 P2/P3）。

        worker 不设 parent，并在 finished 后 deleteLater 自清理（detach 模式）——
        这样即使关闭对话框时线程仍在收尾（真实抓包量大时 sniff 轮询返回偏慢），
        主线程也无须 wait()，对话框可立即关闭，不影响后续创建新 worker。
        """
        chosen = self.combo_iface.currentData() or ""
        self._worker = DiscoveryWorker(iface=chosen or resolve_capture_iface())
        self._worker.hit_found.connect(self._on_hit)
        self._worker.finished_discovery.connect(self._on_finished)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()
        self.btn_start.setEnabled(False)
        self.btn_done.setEnabled(True)
        self.count_label.setText("捕获中…请在单独卡池页翻页（勿切「全部卡池」）")
        self.count_label.setStyleSheet("color: #c0392b; padding: 6px 0;")

    def _on_hit(self, ip: str, port: int) -> None:
        """实时刷新已识别地址计数。"""
        self._found.append((ip, port))
        self.count_label.setText(
            f"已识别地址数：{len(self._found)}（最新 {ip}:{port}）"
        )

    def _on_done(self) -> None:
        """用户点击完成：停止抓包，等待 discover 返回最终结果。"""
        self.btn_done.setEnabled(False)
        if self._worker:
            self._worker.stop_discovery()

    def _on_finished(self, result: list) -> None:
        """发现结束：更新结果并关闭对话框（带回结果）。

        取消路径下对话框已关闭、线程改为后台收尾，其 finished_discovery 是
        迟到信号——此时 dialog 已 reject（exec 已返回），直接忽略，避免对已
        关闭对话框再调 accept() / 误写 P2 学习。
        """
        if self._closed:
            logger.debug("对话框已关闭，忽略迟到的发现结束信号")
            return
        self._found = list(result)
        if self._found:
            self.count_label.setText(f"识别完成，共 {len(self._found)} 个地址")
            self.count_label.setStyleSheet("color: #27ae60; padding: 6px 0;")
            # P2 学习：仅在 P1 显式为空时，记录本次成功命中的网卡供下次复用。
            # 铁护栏：不覆盖用户显式选择（save_capture_iface_learned 只在 P1 空时调用）。
            if not load_capture_iface() and self._worker and self._worker.used_iface():
                save_capture_iface_learned(self._worker.used_iface())
                logger.info(f"已学习抓包网卡: {self._worker.used_iface()}")
        else:
            self.count_label.setText("未识别到抽卡地址，请确认已在单独卡池页翻页（勿切「全部卡池」）")
            self.count_label.setStyleSheet("color: #c0392b; padding: 6px 0;")
        self.accept()

    def _on_error(self, msg: str) -> None:
        QMessageBox.critical(self, "发现失败", msg)
        self.btn_start.setEnabled(True)
        self.btn_done.setEnabled(False)

    def get_addresses(self) -> List[Tuple[str, int]]:
        """返回最终识别到的地址列表（关闭后由主窗口读取）。"""
        return list(self._found)

    def _request_stop(self) -> None:
        """统一收尾：请求后台线程停止 + 标记已关闭（忽略迟到信号）。

        discover 已改用超时轮询（每 0.3s 检查一次 stop_event），取消路径只需
        set stop 即可让线程自行收尾（≤0.3s + 1 次 sniff 内退出）。worker 采用
        QThread 标准 detach 模式（不设 parent + finished→deleteLater），主线程
        不 wait，消除旧版 wait(1000) 在抓包量大、sniff(timeout) 返回偏慢时对
        GUI 的长阻塞。
        """
        if self._worker and self._worker.isRunning():
            self._worker.stop_discovery()
            logger.info("已请求停止地址发现线程（后台自行收尾，不阻塞 GUI）")
        self._closed = True

    def reject(self) -> None:
        """取消：先收尾再关闭。

        Qt 的 accept()/reject() 走 done()→hide()，**不触发 closeEvent**，故收尾
        逻辑不能只写在 closeEvent 里——否则点「取消」/直接关窗时线程不会停止，
        finished_discovery 永不到达，本次识别结果（含 P2 学习网卡）全部丢失，
        后台线程也会残留。实测日志「开始地址发现」17 次而「已请求停止线程」
        0 次，即由此而来。
        """
        self._request_stop()
        super().reject()

    def accept(self) -> None:
        """完成：标记已关闭，避免迟到的发现结束信号再操作本对话框。"""
        self._closed = True
        super().accept()

    def closeEvent(self, event) -> None:
        """窗口关闭事件：走同一套收尾逻辑（不阻塞主线程）。"""
        self._request_stop()
        super().closeEvent(event)
