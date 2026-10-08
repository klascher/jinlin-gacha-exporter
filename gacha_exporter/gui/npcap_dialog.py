"""Npcap 依赖说明对话框 - 启动时检测，未安装则引导安装或退出。

本程序依赖开源组件 Npcap（https://npcap.com，BSD 许可）抓取本地数据包。未安装/
不可用时弹此对话框：随包安装包存在则可直接运行安装；否则引导官网下载；不接受则退出。

随包安装包探测：在 exe/项目根目录、redist/（开发环境）、辅助工具/（分发目录
固定结构）查找 npcap*.exe，任一命中即可被自动识别，供用户一键安装。
"""
import os
from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..utils.paths import get_app_dir

NPCAP_DOWNLOAD_URL = "https://npcap.com/#download"


def find_bundled_npcap_installer() -> Optional[str]:
    """在 exe/项目根目录及固定子目录查找随包 Npcap 安装包（npcap*.exe）。

    候选目录：根目录、redist/（开发环境）、辅助工具/（分发目录固定结构，
    与 使用说明.txt / 官网页面的「辅助工具 -> npcap」一致）。

    Returns:
        找到的安装包绝对路径，未找到返回 None。
    """
    base = get_app_dir()
    for d in (base, os.path.join(base, "redist"), os.path.join(base, "辅助工具")):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            low = name.lower()
            if low.endswith(".exe") and "npcap" in low:
                return os.path.join(d, name)
    return None


class NpcapDependencyDialog(QDialog):
    """Npcap 依赖说明对话框。

    chosen_action: "install"（运行随包安装包）/ "download"（打开官网）/
    "view"（仅查看已有数据，进入只读模式）/ "exit"（不同意退出）。
    """

    def __init__(self, bundled_path: Optional[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Npcap 抓包组件依赖说明")
        self.resize(580, 480)
        self.chosen_action = "exit"

        layout = QVBoxLayout(self)

        intro = QLabel(
            "本程序通过抓取本地网络数据包来记录抽卡信息，依赖开源组件 Npcap "
            "（https://npcap.com，BSD 许可，Wireshark 等工具亦使用）实现抓包。\n\n"
            "检测到 Npcap 尚未安装或不可用，需先安装才能使用抓包功能。\n"
            "若暂不安装，也可选择「仅查看已有数据」：可以查看/导出已保存的\n"
            "抽卡记录，但不能开始新的抓取。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if bundled_path:
            bundled_lbl = QLabel(
                f"检测到随包提供的 Npcap 安装包：\n  {bundled_path}\n"
                f"可直接点击下方「安装随包 Npcap」运行安装向导。"
            )
        else:
            bundled_lbl = QLabel(
                "未找到随包安装包，请点击下方「前往官网下载」获取最新版安装包。"
            )
        bundled_lbl.setWordWrap(True)
        layout.addWidget(bundled_lbl)

        steps = QTextBrowser()
        steps.setOpenExternalLinks(True)
        steps.setHtml(
            "<b>安装步骤：</b><br>"
            "1. 获取安装包：使用随包提供的，或前往 "
            "<a href=\"https://npcap.com/#download\">https://npcap.com/#download</a> "
            "下载最新版（选 Npcap 1.x 安装包）。<br>"
            "2. 运行安装包，按向导完成安装；建议保持默认选项，"
            "可勾选「Install Npcap in WinPcap API-compatible Mode」（兼容旧组件）。<br>"
            "3. 安装完成后，<b>重新启动本程序</b>即可正常抓包。"
        )
        steps.setMaximumHeight(130)
        layout.addWidget(steps)

        warn = QLabel(
            "说明：本程序依赖 Npcap 这一开源组件。不安装则无法抓取新记录，"
            "可选择「仅查看已有数据」或「不同意，退出」。"
        )
        warn.setStyleSheet("color: #c0392b;")
        warn.setWordWrap(True)
        layout.addWidget(warn)

        btns = QHBoxLayout()
        self.btn_install = QPushButton("安装随包 Npcap")
        self.btn_install.setEnabled(bool(bundled_path))
        self.btn_download = QPushButton("前往官网下载")
        self.btn_view = QPushButton("仅查看已有数据")
        self.btn_exit = QPushButton("不同意，退出")
        btns.addWidget(self.btn_install)
        btns.addWidget(self.btn_download)
        btns.addStretch()
        btns.addWidget(self.btn_view)
        btns.addWidget(self.btn_exit)
        layout.addLayout(btns)

        self.btn_install.clicked.connect(lambda: self._choose("install"))
        self.btn_download.clicked.connect(lambda: self._choose("download"))
        self.btn_view.clicked.connect(lambda: self._choose("view"))
        self.btn_exit.clicked.connect(lambda: self._choose("exit"))

    def _choose(self, action: str) -> None:
        self.chosen_action = action
        self.accept()
