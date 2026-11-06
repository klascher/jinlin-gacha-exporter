"""前台全屏截取（卡池识别 P3）。

只负责「拿到物理像素的屏幕图」，不落盘：返回 PNG 字节，调用方交给 OCR 后
即丢弃。Qt `QScreen.grabWindow(0)` 为主，`mss` 为多屏/DPI 异常时的回退。

隐私约束：不枚举文件名、不读游戏内存、不联网；图片只在本进程内存中存在。
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ScreenGrabError(RuntimeError):
    """截屏失败（无 Qt、黑色帧、权限等），消息面向用户。"""


@dataclass(frozen=True)
class Screenshot:
    """屏幕图：PNG 字节或 QImage + 实际像素尺寸 + 来源（qt/mss）。

    2026-10-22 优先级铁律（性能第一）：识别热路径改用 ``encode_png=False``，
    主线程只拿 QImage 不编码；PNG 编码/转 BGR 挪到 OCR 后台线程，避免主线程
    被图像编码拖慢截图链路。``data``（PNG）路径保留给离线工具与旧调用方。
    """

    data: bytes
    width: int
    height: int
    source: str
    image: Optional[object] = None  # QImage（encode_png=False 时填充）


def _foreground_rect() -> Optional[Tuple[int, int, int, int]]:
    """当前前台窗口矩形 (left, top, right, bottom)，物理像素；失败返回 None。

    工具自身在前台时返回 None（宁可截主屏也不能截到自己）。仅 Windows 实现，
    其他平台走主屏回退。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and pid.value == ctypes.windll.kernel32.GetCurrentProcessId():
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception as e:  # noqa: BLE001 —— 截屏兜底，不能拖垮主流程
        logger.info(f"取前台窗口矩形失败，回退主屏: {e}")
        return None


def _pick_screen(rect: Optional[Tuple[int, int, int, int]]):
    """按矩形中心选 QScreen；无矩形/失败回退主屏。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication

    if rect is not None:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        screen = QGuiApplication.screenAt(QPoint(cx, cy))
        if screen is not None:
            return screen
    return QGuiApplication.primaryScreen()


def _is_mostly_black(image, sample_w: int = 16, sample_h: int = 9) -> bool:
    """95% 以上采样点近黑视为黑帧（全屏独占/遮挡），避免把黑图送 OCR。"""
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return True
    dark = 0
    total = 0
    for iy in range(sample_h):
        y = min(image.height() - 1, int((iy + 0.5) * image.height() / sample_h))
        for ix in range(sample_w):
            x = min(image.width() - 1, int((ix + 0.5) * image.width() / sample_w))
            c = image.pixelColor(x, y)
            total += 1
            if c.red() + c.green() + c.blue() < 24:
                dark += 1
    return total > 0 and dark / total >= 0.95


def _grab_with_qt(
    rect: Optional[Tuple[int, int, int, int]], encode_png: bool = True
) -> Optional[Screenshot]:
    """Qt 主方案；失败返回 None（由 mss 回退）。

    ``encode_png=False`` 时返回 QImage（不编码），编码留给后台线程。
    """
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QGuiApplication
    except Exception as e:  # noqa: BLE001
        logger.info(f"Qt 截屏不可用: {e}")
        return None
    if QGuiApplication.instance() is None:
        return None
    screen = _pick_screen(rect)
    if screen is None:
        return None
    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        logger.warning("QScreen.grabWindow 返回空图")
        return None
    image = pixmap.toImage()
    if _is_mostly_black(image):
        logger.warning("Qt 截屏接近全黑（全屏独占/遮挡？）")
        return None
    if not encode_png:
        # 主线程不做编码：QImage 是不可变值对象，可安全交给后台线程读取
        return Screenshot(b"", image.width(), image.height(), "qt", image=image)
    buffer = QByteArray()
    buf = QBuffer(buffer)
    buf.open(QIODevice.WriteOnly)
    if not image.save(buf, "PNG"):
        logger.warning("QImage 编码 PNG 失败")
        return None
    return Screenshot(bytes(buffer.data()), image.width(), image.height(), "qt")


def _mss_monitor(rect: Optional[Tuple[int, int, int, int]]) -> int:
    """矩形中心所在显示器索引（mss 约定 1 起）；无矩形回主屏。"""
    if rect is None:
        return 1
    try:
        import mss

        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        with mss.mss() as sct:
            for idx, mon in enumerate(sct.monitors[1:], start=1):
                if (
                    mon["left"] <= cx < mon["left"] + mon["width"]
                    and mon["top"] <= cy < mon["top"] + mon["height"]
                ):
                    return idx
    except Exception as e:  # noqa: BLE001
        logger.info(f"mss 显示器枚举失败，回退主屏: {e}")
    return 1


def _grab_with_mss(
    rect: Optional[Tuple[int, int, int, int]], encode_png: bool = True
) -> Optional[Screenshot]:
    """mss 回退：按物理像素抓整块屏。未安装 mss 返回 None。

    ``encode_png=False`` 时把原始 BGRA 包成 QImage（不做 PNG 编码）。
    """
    try:
        import mss
        import mss.tools
    except Exception:  # noqa: BLE001
        return None
    try:
        idx = _mss_monitor(rect)
        with mss.mss() as sct:
            monitors = sct.monitors
            mon = monitors[idx] if 0 < idx < len(monitors) else monitors[0]
            shot = sct.grab(mon)
            if encode_png:
                data = mss.tools.to_png(shot.rgb, shot.size)
            else:
                data = b""
        if encode_png:
            return Screenshot(data, shot.size[0], shot.size[1], "mss")
        from PySide6.QtGui import QImage

        w, h = shot.size[0], shot.size[1]
        # 小端 Windows 上 BGRA 字节序即 QImage 的 ARGB32；copy() 脱离原始缓冲
        image = QImage(bytes(shot.bgra), w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        return Screenshot(b"", w, h, "mss", image=image)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mss 截屏失败: {e}")
        return None


def grab_game_screen(encode_png: bool = True) -> Screenshot:
    """抓取游戏前台所在屏幕（物理像素）。

    优先 Qt；Qt 失败/黑帧时回退 mss。全屏独占黑帧或无可用后端时抛
    `ScreenGrabError`，消息提示用户改用无边框窗口并把游戏置于前台。

    ``encode_png=False``（识别热路径）：只拿 QImage、不做 PNG 编码，
    编码/转 BGR 由后台 OCR 线程完成（2026-10-22 性能第一）。
    """
    rect = _foreground_rect()
    shot = _grab_with_qt(rect, encode_png=encode_png)
    if shot is None:
        shot = _grab_with_mss(rect, encode_png=encode_png)
    if shot is None:
        raise ScreenGrabError(
            "本机画面未被取到：请用无边框/窗口模式并置于前台，"
            "独占全屏不供像素。"
        )
    logger.info(f"截屏完成 source={shot.source} size={shot.width}x{shot.height}")
    return shot
