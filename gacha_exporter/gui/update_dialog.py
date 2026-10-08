"""更新提示对话框 - 支持提示不强制 / 强制两种模式。

changelog 说明：version.json 里用 `\n` 写换行，`json.load` 后会变成真实换行；
但 `QTextBrowser.setMarkdown` 会把**单个裸换行折叠成空格**，导致 `\n` 看着不换行。
渲染前把所有「单个换行」转成 Markdown 硬换行（行尾补两空格），空行/列表等其余
Markdown 语法不受影响。（2026-09-14）

「立即更新」：full_exe 走浏览器下载新 exe；否则后台下载 app.zip.pending
（校验 sha256），完成后提示重启生效。
「稍后/暂不更新」：prompt 记 acknowledged（同版本不再弹）；force 记
rejected_forced（下次启动同步闸门直接禁用抓包）。
"""
import re
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..loaders.remote_data import resolve_appzip_url
from ..models import VersionInfo
from ..storage.settings import save_acknowledged_version, save_rejected_forced_version
from ..utils.logger import get_logger
from .update_downloader import UpdateDownloader

logger = get_logger(__name__)

_MODE_PROMPT = "prompt"
_MODE_FORCE = "force"


class UpdateDialog(QDialog):
    """更新提示对话框。

    Args:
        info: 服务端版本信息（VersionInfo）
        mode: "prompt"（提示不强制）或 "force"（强制）
    """

    def __init__(self, info: VersionInfo, mode: str = _MODE_PROMPT, parent=None):
        super().__init__(parent)
        self.info = info
        self.mode = mode if mode in (_MODE_PROMPT, _MODE_FORCE) else _MODE_PROMPT
        self._downloading = False
        self._worker: UpdateDownloader | None = None
        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(480)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QLabel(f"发现新版本：{self.info.latest_version}")
        header.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(header)

        if self.mode == _MODE_FORCE:
            hint = QLabel("本次为强制更新，未更新前「开始抓包」将被禁用（仍可查看历史记录）。")
            hint.setStyleSheet("color: #c0392b;")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        browser = QTextBrowser()
        changelog = self.info.changelog or "（无更新说明）"
        # Markdown 会折叠单个裸换行 → 把「前后都不是换行」的单个 \n 转成硬换行
        # （行尾补两空格）。空行（\n\n）和列表等其余语法原样保留。
        changelog = re.sub(r"(?<!\n)\n(?!\n)", "  \n", changelog)
        browser.setMarkdown(changelog)
        browser.setMinimumHeight(220)
        layout.addWidget(browser)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        btn_row = QHBoxLayout()
        self.btn_update = QPushButton("立即更新")
        later_text = "暂不更新" if self.mode == _MODE_FORCE else "稍后"
        self.btn_later = QPushButton(later_text)
        btn_row.addWidget(self.btn_update)
        btn_row.addWidget(self.btn_later)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.btn_update.clicked.connect(self._on_update)
        self.btn_later.clicked.connect(self._on_later)

    # ---- 立即更新 ----
    def _on_update(self) -> None:
        """「立即更新」：full_exe 浏览器下载；否则后台下载补丁。"""
        if self.info.full_exe:
            # 需换 exe（依赖变更）：浏览器打开下载地址
            if self.info.update_url:
                webbrowser.open(resolve_appzip_url(self.info.update_url))
            save_acknowledged_version(self.info.latest_version)
            logger.info(f"打开新版本下载地址: {self.info.update_url}")
            self.accept()
            return
        if not self.info.update_url:
            QMessageBox.warning(self, "更新失败", "服务器未提供更新地址，请稍后再试。")
            return
        self._start_download()

    def _start_download(self) -> None:
        """启动补丁下载线程。"""
        self._downloading = True
        self.btn_update.setEnabled(False)
        self.btn_later.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._worker = UpdateDownloader(
            resolve_appzip_url(self.info.update_url),
            self.info.update_sha256,
            self.info.update_signature,
            parent=self,
        )
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_download_finished)
        self._worker.start()

    def _on_download_finished(self, ok: bool, msg: str) -> None:
        """下载结束：成功即时退出让用户重新打开（重开时 bootstrap 自动应用），失败恢复重试。"""
        self._downloading = False
        if ok:
            QMessageBox.information(
                self,
                "更新已就绪",
                msg + "\n\n程序将自动退出，重新打开本工具即自动应用更新（无需再做任何操作）。",
            )
            self.accept()
            self._request_app_close()
        else:
            self.progress.setVisible(False)
            self.btn_update.setEnabled(True)
            self.btn_later.setEnabled(True)
            QMessageBox.warning(self, "下载失败", msg)

    def _request_app_close(self) -> None:
        """确认后立即退出：复用主窗口 closeEvent 的安全收尾（停抓包线程 + 合并历史）。

        更新补丁（app.zip.pending + sidecar）已就绪，重启（本 dialog 的父窗口通常就是
        主窗口）会让引导期 swap_pending 消费它并生效。找到带 pity_panel 的主顶层窗口
        close()（走其 closeEvent 合并历史）；找不到则直接 quit。关闭最后一个顶层窗口后
        Qt quitOnLastWindowClosed 会自然退出进程。
        """
        app = QApplication.instance()
        for tl in app.topLevelWidgets():
            if getattr(tl, "pity_panel", None) is not None:  # 主窗口特征（避免 import 主窗口）
                tl.close()
                return
        app.quit()

    # ---- 稍后 / 暂不更新 ----
    def _on_later(self) -> None:
        """「稍后/暂不」：按模式持久化确认状态后关闭。"""
        if self.mode == _MODE_FORCE:
            save_rejected_forced_version(self.info.latest_version)
            logger.info(f"用户暂不强制更新，已记 rejected_forced={self.info.latest_version}")
        else:
            save_acknowledged_version(self.info.latest_version)
            logger.info(f"用户稍后更新，已记 acknowledged={self.info.latest_version}")
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        """下载进行中禁止关闭（避免残留半包）。"""
        if self._downloading:
            event.ignore()
            return
        super().closeEvent(event)
