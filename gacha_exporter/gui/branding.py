"""品牌/文案常量 - 公告弹窗与主窗口共用，改这里即全局生效。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

# 赞助信息（公告弹窗底部 + 主窗口右下角，同一来源）
SPONSOR_TEXT = "本工具由金麟街云老板独家赞助"

# 官网常见问题页（主窗口状态栏右下角常驻入口，与官网 /docs/常见问题.html 一致）
FAQ_URL = "https://github.com/klascher/jinlin-gacha-exporter/issues"


def sponsor_label(centered: bool = False) -> QLabel:
    """构造赞助信息 QLabel（公告弹窗居中、主窗口右对齐，样式统一弱化）。"""
    label = QLabel(SPONSOR_TEXT)
    label.setAlignment(Qt.AlignHCenter if centered else (Qt.AlignRight | Qt.AlignVCenter))
    label.setStyleSheet("color: #999; padding: 2px 8px;")
    return label


def faq_label() -> QLabel:
    """构造主窗口状态栏右下角「常见问题」常驻可点链接（样式同赞助，弱化灰）。

    点击调起系统浏览器打开官网 FAQ；与下发给玩家的 使用说明 指向同一页面。
    """
    label = QLabel(
        f'<a style="text-decoration:none; color:#999;" href="{FAQ_URL}">❓ 常见问题</a>'
    )
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    label.setStyleSheet("padding: 2px 8px;")
    label.setOpenExternalLinks(True)
    return label
