"""未知角色识图后台线程（卡池识别 P3）。

截屏在主线程完成（Qt QScreen 只能在 GUI 线程安全使用），本线程只做 OCR 与
页内配对；结果经信号回主线程。OCR 依赖惰性导入——未安装时给出可读失败消息，
不影响抓包主链路。
"""
from __future__ import annotations

import time
from typing import Iterable, Mapping, Sequence

from PySide6.QtCore import QThread, Signal

from ..analysis.gacha_history_ocr import decode_image, recognize_image
from ..analysis.unknown_trigger import RecognitionTask, validate_page
from ..capture.screen_grab import Screenshot
from ..models import Character
from ..utils.logger import get_logger

logger = get_logger(__name__)


def _shot_to_bgr(shot: Screenshot):
    """截图 → BGR ndarray。QImage 在此线程转换（主线程只负责抓像素）。"""
    image = getattr(shot, "image", None)
    if image is None:
        return decode_image(shot.data)
    import numpy as np
    from PySide6.QtGui import QImage

    img = image.convertToFormat(QImage.Format.Format_BGR888)
    w, h, stride = img.width(), img.height(), img.bytesPerLine()
    # constBits 的缓冲归 QImage 所有：必须 copy，函数返回后 QImage 即释放
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=stride * h)
    arr = np.array(buf.reshape(h, stride)[:, : w * 3], dtype=np.uint8, copy=True)
    return arr.reshape(h, w, 3)


class RecognitionWorker(QThread):
    """对一张截图执行 OCR + 校验；done 发 PageValidation，failed 发错误文案。"""

    done = Signal(object)  # PageValidation
    failed = Signal(str)

    def __init__(
        self,
        task: RecognitionTask,
        shots: Sequence[Screenshot],
        characters: Mapping[str, Character],
        known_pool_ids: Iterable[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._task = task
        # 一次任务可带多张截图（主图 + 渲染兜底图）：按顺序 OCR，第一张通过
        # 就停；都不过时优先返回「页号对得上」的那次失败，便于汇总里说明原因。
        self._shots = list(shots)
        # 快照映射：识别期间主线程可能更新 characters（并入/远端刷新）
        self._characters = dict(characters)
        self._known_pool_ids = set(known_pool_ids)

    def run(self) -> None:  # noqa: D401 —— QThread 入口
        last_result = None
        done_result = None
        t0 = time.monotonic()
        for idx, shot in enumerate(self._shots):
            try:
                page = recognize_image(_shot_to_bgr(shot))
                result = validate_page(
                    self._task, page, self._characters, self._known_pool_ids
                )
            except Exception as e:  # noqa: BLE001 —— 识图失败由确认流程兜底
                logger.warning(f"识图线程异常（第 {idx + 1} 张）: {e}")
                continue
            if result.ok:
                done_result = result
                break
            # 页号对得上的失败更能说明问题（行数/行序），优先保留
            if last_result is None or (
                result.page is not None
                and result.page.ok
                and result.page.page == self._task.page
            ):
                last_result = result
        # F4 计时遥测（2026-10-22）：OCR 耗时与试图数，供实机核对限线程后的
        # 后台吞吐，并作为「OCR 负载下截图延迟」测量的配套数据。
        # 成功路径也必须记录——否则 soak 里全是成功页，量不到任何 OCR 耗时。
        logger.info(
            f"识图计时 page={self._task.page}/{self._task.total_page} "
            f"OCR耗时={(time.monotonic() - t0) * 1000:.0f}ms "
            f"截图数={len(self._shots)}"
        )
        if done_result is not None:
            self.done.emit(done_result)
            return
        if last_result is None:
            self.failed.emit("识图失败：所有截图都无法解析")
            return
        self.done.emit(last_result)
