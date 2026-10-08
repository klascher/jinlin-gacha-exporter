"""版本检查对话框 - 显示工具版本 + 手动「检查更新」。

手动检查走后台线程 load_version_info()：无更新提示「已是最新版本」；
有新版本打开 UpdateDialog（静默类型也照常展示——用户主动检查就该看到结果）。
"""
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .. import __version__
from ..loaders.remote_data import load_version_info, set_remote_available
from ..models import VersionInfo
from ..utils.logger import get_logger
from ..utils.version import compare_versions
from .update_dialog import UpdateDialog

logger = get_logger(__name__)


class _CheckWorker(QThread):
    """后台执行版本检查，避免阻塞 UI。"""

    done = Signal(object)  # VersionInfo | None

    def run(self) -> None:
        # 手动检查重置健康缓存（不沿用启动时的结论），重新探测服务端可用性
        set_remote_available(None)
        self.done.emit(load_version_info())


class SettingsDialog(QDialog):
    """版本检查对话框（更多菜单「版本检查」入口）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("版本检查")
        self.resize(420, 180)
        self._worker: _CheckWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"工具版本：{__version__}"))

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #555;")
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.btn_check = QPushButton("检查更新")
        self.btn_close = QPushButton("关闭")
        btn_row.addWidget(self.btn_check)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

        self.btn_check.clicked.connect(self._on_check)
        self.btn_close.clicked.connect(self.accept)

    def _on_check(self) -> None:
        """手动检查更新：后台拉取版本信息，再按结果处理。"""
        self.btn_check.setEnabled(False)
        self.status_label.setText("正在检查更新…")
        self._worker = _CheckWorker(self)
        self._worker.done.connect(self._on_check_done)
        self._worker.start()

    def _on_check_done(self, info) -> None:
        """检查结果：None=失败；最新=已最新；有新版=开更新对话框。"""
        self.btn_check.setEnabled(True)
        if info is None:
            self.status_label.setText("无法获取更新信息，请检查网络后重试。")
            return
        if not isinstance(info, VersionInfo):
            self.status_label.setText("更新信息格式异常。")
            return
        cmp = compare_versions(info.latest_version, __version__)
        if cmp <= 0:
            self.status_label.setText(f"已是最新版本（{__version__}）。")
            return
        # 有新版本：弹更新对话框（silent 也展示——用户主动检查）
        dlg = UpdateDialog(info, mode="force" if info.update_type == "force" else "prompt", parent=self)
        dlg.exec()
        self.status_label.setText("")

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭前确保检查线程结束。"""
        if self._worker and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)
