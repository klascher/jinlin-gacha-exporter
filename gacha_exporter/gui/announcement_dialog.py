"""公告弹窗 - 启动时展示所有未读公告。

置顶公告（pinned）：强制「我已了解并同意」确认（免责声明用），全部确认前
底部按钮禁用、红字提示。更新公告（update）：「不再提示」复选框，可勾选标记
已读。content 支持 Markdown 渲染（QTextBrowser.setMarkdown）。
"""
from datetime import datetime, timezone
from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..models import Announcement
from ..storage.announcements import mark_read
from .branding import sponsor_label


def _ts_to_date(ts: int) -> str:
    """Unix 秒 -> yyyy-mm-dd（UTC，仅展示用）。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        return ""


class AnnouncementDialog(QDialog):
    """公告展示对话框。接收未读公告列表，合并一个窗口展示。"""

    def __init__(self, announcements: List[Announcement], parent=None):
        super().__init__(parent)
        self.setWindowTitle("公告")
        self.resize(560, 600)
        self._checkboxes: Dict[str, QCheckBox] = {}
        self._pinned_btns: Dict[str, QPushButton] = {}
        self._pinned_confirmed: Dict[str, bool] = {}

        pinned = sorted(
            [a for a in announcements if a.type == "pinned"],
            key=lambda a: a.ts,
            reverse=True,
        )
        updates = sorted(
            [a for a in announcements if a.type != "pinned"],
            key=lambda a: a.ts,
            reverse=True,
        )

        root = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)

        if pinned:
            lbl = QLabel("📌 置顶公告（须确认后可关闭）")
            lbl.setStyleSheet("font-weight: bold; color: #c0392b;")
            content_layout.addWidget(lbl)
            for a in pinned:
                content_layout.addWidget(self._build_pinned_card(a))
        if updates:
            lbl = QLabel("📋 更新公告")
            lbl.setStyleSheet("font-weight: bold;")
            content_layout.addWidget(lbl)
            for a in updates:
                content_layout.addWidget(self._build_update_card(a))

        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self._hint = QLabel("⚠ 请先阅读并确认所有置顶公告")
        self._hint.setStyleSheet("color: #c0392b;")
        root.addWidget(self._hint)

        buttons = QDialogButtonBox()
        self._btn_all = buttons.addButton(
            "全部不再提示", QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_close = buttons.addButton(
            "关闭", QDialogButtonBox.ButtonRole.RejectRole)
        self._btn_all.clicked.connect(self._mark_all_read)
        self._btn_close.clicked.connect(self._close)
        root.addWidget(buttons)

        # 固定联系信息（窗口最底，不随公告滚动，邮箱可选可复制）
        contact = QLabel("如有问题请前往 GitHub Issues 反馈")
        contact.setTextInteractionFlags(Qt.TextSelectableByMouse)
        contact.setAlignment(Qt.AlignCenter)
        root.addWidget(contact)

        # 赞助信息（邮箱下方；文案统一放 branding.py，与主窗口右下角同步）
        root.addWidget(sponsor_label(centered=True))

        self._update_button_state()

    def _build_pinned_card(self, ann: Announcement) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Box)
        l = QVBoxLayout(card)
        l.addWidget(self._header(ann, bold=True))
        body = QTextBrowser()
        body.setMarkdown(ann.content)
        body.setOpenExternalLinks(True)
        body.setFixedHeight(200)
        l.addWidget(body)
        btn = QPushButton("我已了解并同意")
        btn.clicked.connect(lambda _, a=ann, b=btn: self._confirm_pinned(a, b))
        l.addWidget(btn)
        self._pinned_btns[ann.id] = btn
        self._pinned_confirmed[ann.id] = False
        return card

    def _build_update_card(self, ann: Announcement) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Box)
        l = QVBoxLayout(card)
        l.addWidget(self._header(ann, bold=False))
        body = QTextBrowser()
        body.setMarkdown(ann.content)
        body.setOpenExternalLinks(True)
        body.setFixedHeight(200)
        l.addWidget(body)
        cb = QCheckBox("不再提示")
        l.addWidget(cb)
        self._checkboxes[ann.id] = cb
        return card

    def _header(self, ann: Announcement, bold: bool) -> QLabel:
        date = _ts_to_date(ann.ts)
        text = f"{ann.title}    {date}".rstrip()
        lbl = QLabel(text)
        if bold:
            lbl.setStyleSheet("font-weight: bold;")
        return lbl

    def _confirm_pinned(self, ann: Announcement, btn: QPushButton) -> None:
        mark_read(ann.id)
        self._pinned_confirmed[ann.id] = True
        btn.setText("✓ 已确认")
        btn.setEnabled(False)
        self._update_button_state()

    def _all_pinned_confirmed(self) -> bool:
        return all(self._pinned_confirmed.values()) if self._pinned_confirmed else True

    def _update_button_state(self) -> None:
        ok = self._all_pinned_confirmed()
        self._btn_close.setEnabled(ok)
        self._btn_all.setEnabled(ok)
        self._hint.setVisible(not ok)

    def _mark_all_read(self) -> None:
        """标记所有未读已读（置顶已确认 + 更新全部）后关闭。"""
        for aid in self._checkboxes:
            mark_read(aid)
        self.accept()

    def _close(self) -> None:
        """关闭：标记勾选的更新已读，未勾的下次还弹。"""
        for aid, cb in self._checkboxes.items():
            if cb.isChecked():
                mark_read(aid)
        self.reject()

    def _warn_pinned_pending(self) -> None:
        """置顶未确认时弹提示，避免用户以为 X 失灵是 bug。"""
        QMessageBox.warning(
            self, "请确认置顶公告",
            "请先阅读并点击所有置顶公告的「我已了解并同意」后再关闭。")

    def reject(self) -> None:
        """置顶未全部确认前禁止关闭（Esc / 关闭按钮）：弹提示后拦截。"""
        if not self._all_pinned_confirmed():
            self._warn_pinned_pending()
            return
        super().reject()

    def closeEvent(self, event):
        """标题栏 X：置顶未确认时弹提示并忽略关闭。"""
        if not self._all_pinned_confirmed():
            self._warn_pinned_pending()
            event.ignore()
            return
        super().closeEvent(event)
