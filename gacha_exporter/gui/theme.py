"""主题 - 固定浅色外观，不跟随系统深色模式。

用户反馈（问题反馈 5）：部分用户系统/应用为深色模式时，Qt 组件跟随系统
主题导致页面显示与本机测试不一致。此处统一固定 Fusion 样式 + 显式浅色
调色板，并在 Qt >= 6.8 上强制 ColorScheme.Light，阻断系统主题变化的影响。
"""
from PySide6.QtGui import QColor, QPalette


def _light_palette() -> QPalette:
    """构造显式浅色调色板（Windows 浅色模式标准色值）。"""
    p = QPalette()
    window = QColor(0xF0, 0xF0, 0xF0)
    text = QColor(0x00, 0x00, 0x00)
    disabled_text = QColor(0xA0, 0xA0, 0xA0)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        p.setColor(group, QPalette.ColorRole.Window, window)
        p.setColor(group, QPalette.ColorRole.WindowText, text)
        p.setColor(group, QPalette.ColorRole.Base, QColor(0xFF, 0xFF, 0xFF))
        p.setColor(group, QPalette.ColorRole.AlternateBase, QColor(0xF0, 0xF0, 0xF0))
        p.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(0xFF, 0xFF, 0xDC))
        p.setColor(group, QPalette.ColorRole.ToolTipText, text)
        p.setColor(group, QPalette.ColorRole.Text, text)
        p.setColor(group, QPalette.ColorRole.Button, window)
        p.setColor(group, QPalette.ColorRole.ButtonText, text)
        p.setColor(group, QPalette.ColorRole.BrightText, QColor(0xFF, 0x00, 0x00))
        p.setColor(group, QPalette.ColorRole.Highlight, QColor(0x00, 0x78, 0xD7))
        p.setColor(group, QPalette.ColorRole.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
        p.setColor(group, QPalette.ColorRole.Link, QColor(0x00, 0x00, 0xFF))
        p.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(0x80, 0x80, 0x80))
    dg = QPalette.ColorGroup.Disabled
    p.setColor(dg, QPalette.ColorRole.Window, window)
    p.setColor(dg, QPalette.ColorRole.WindowText, disabled_text)
    p.setColor(dg, QPalette.ColorRole.Base, QColor(0xFF, 0xFF, 0xFF))
    p.setColor(dg, QPalette.ColorRole.AlternateBase, QColor(0xF0, 0xF0, 0xF0))
    p.setColor(dg, QPalette.ColorRole.Text, disabled_text)
    p.setColor(dg, QPalette.ColorRole.Button, window)
    p.setColor(dg, QPalette.ColorRole.ButtonText, disabled_text)
    p.setColor(dg, QPalette.ColorRole.Highlight, QColor(0xA0, 0xA0, 0xA0))
    p.setColor(dg, QPalette.ColorRole.HighlightedText, disabled_text)
    return p


def apply_light_theme(app) -> None:
    """给 QApplication 固定浅色主题（Fusion 样式 + 显式浅色调色板）。

    Fusion 是跨平台样式，完全遵循 setPalette，不随系统深色模式切换；
    原生 windowsvista 样式在系统深色模式下会自动换用深色调色板，导致
    界面因用户系统设置而异。Qt >= 6.8 再叠加 setColorScheme(Light)，
    阻断运行中系统主题切换的影响。
    """
    app.setStyle("Fusion")
    app.setPalette(_light_palette())
    try:
        from PySide6.QtCore import Qt
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except (AttributeError, RuntimeError):
        pass  # Qt < 6.8 无此 API：显式 setPalette 已足够
