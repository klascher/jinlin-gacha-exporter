"""地址发现对话框：关闭收尾测试。

回归核心：Qt 的 accept()/reject() 走 done()→hide()，**不触发 closeEvent**。
收尾（停后台线程 + 标记已关闭）必须覆盖 reject/accept 两条路径，否则点
「取消」/关窗时线程不会停止，finished_discovery 永不到达，本次识别结果与
P2 学习网卡全部丢失，后台线程也会残留。
"""
from PySide6.QtWidgets import QApplication

from gacha_exporter.gui import discovery_dialog as dd

app = QApplication.instance() or QApplication([])


class _FakeWorker:
    """替身 worker：只关心 isRunning / stop_discovery 的调用情况。"""

    def __init__(self, running: bool = True):
        self._running = running
        self.stopped = False

    def isRunning(self) -> bool:
        return self._running

    def stop_discovery(self) -> None:
        self.stopped = True
        self._running = False


def _make_dialog(monkeypatch, worker):
    """构造对话框（隔离 scapy 与真实 settings），并注入替身 worker。"""
    monkeypatch.setattr(
        dd, "list_capture_ifaces",
        lambda: [(r"\\Device\\NPF_{WLAN}", "192.168.0.105")],
    )
    monkeypatch.setattr(dd, "load_capture_iface", lambda: "")
    dlg = dd.DiscoveryDialog()
    dlg._worker = worker
    return dlg


def test_reject_stops_worker_and_marks_closed(monkeypatch):
    """点「取消」：必须停线程 + 标记已关闭（回归：收尾曾只写在 closeEvent 里）。"""
    worker = _FakeWorker(running=True)
    dlg = _make_dialog(monkeypatch, worker)
    dlg.reject()
    assert worker.stopped is True
    assert dlg._closed is True


def test_accept_marks_closed(monkeypatch):
    """点「完成」：标记已关闭；线程已自然结束时不再重复 stop。"""
    worker = _FakeWorker(running=False)
    dlg = _make_dialog(monkeypatch, worker)
    dlg.accept()
    assert dlg._closed is True
    assert worker.stopped is False


def test_close_without_worker_is_safe(monkeypatch):
    """未启动抓包就取消（worker 为 None）：不抛异常，仍标记已关闭。"""
    dlg = _make_dialog(monkeypatch, worker=None)
    dlg.reject()
    assert dlg._closed is True
