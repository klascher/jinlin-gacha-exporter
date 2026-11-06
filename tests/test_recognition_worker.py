# -*- coding: utf-8 -*-
"""识图线程 F4 计时遥测：成功路径也必须输出「OCR耗时」日志。

背景（项目进展 §3.199）：原先 run() 在首张截图校验通过时直接 return，
计时日志只在全部失败时执行 → 实机 soak 全是成功页却量不到 OCR 耗时。
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gacha_exporter.analysis.unknown_trigger import RecognitionTask  # noqa: E402
from gacha_exporter.gui import recognition_worker  # noqa: E402
from gacha_exporter.gui.recognition_worker import RecognitionWorker  # noqa: E402

app = QApplication.instance() or QApplication([])

_LOGGER = "gacha_exporter.gui.recognition_worker"


def _task() -> RecognitionTask:
    return RecognitionTask(
        view="0@0",
        offset=5,
        total=50,
        records=(),
        unknown_item_ids=("90000049",),
        unknown_pool_ids=(),
        created_at=0.0,
    )


def _run(worker: RecognitionWorker):
    done, failed = [], []
    worker.done.connect(done.append)
    worker.failed.connect(failed.append)
    with patch.object(recognition_worker, "_shot_to_bgr", return_value=None):
        worker.run()
    return done, failed


def test_success_logs_ocr_timing(caplog):
    ok = SimpleNamespace(ok=True)
    with patch.object(recognition_worker, "recognize_image", return_value=object()), \
            patch.object(recognition_worker, "validate_page", return_value=ok), \
            caplog.at_level(logging.INFO, logger=_LOGGER):
        done, failed = _run(RecognitionWorker(_task(), [object()], {}, set()))

    assert done == [ok]
    assert failed == []
    assert "OCR耗时" in caplog.text
    assert "page=2/10" in caplog.text
    assert "截图数=1" in caplog.text


def test_all_fail_logs_timing_and_failed(caplog):
    """每张都抛异常（无任何页结果）→ 计时照常输出 + failed 信号。"""
    with patch.object(recognition_worker, "_shot_to_bgr", return_value=None), \
            patch.object(recognition_worker, "recognize_image",
                         side_effect=RuntimeError("boom")), \
            caplog.at_level(logging.INFO, logger=_LOGGER):
        worker = RecognitionWorker(_task(), [object()], {}, set())
        done, failed = [], []
        worker.done.connect(done.append)
        worker.failed.connect(failed.append)
        worker.run()

    assert done == []
    assert failed == ["识图失败：所有截图都无法解析"]
    assert "OCR耗时" in caplog.text


def test_success_stops_after_first_valid_shot(caplog):
    """成功页不必再 OCR 兜底图：只尝试 1 张、日志里截图数仍是任务张数。"""
    ok = SimpleNamespace(ok=True)
    with patch.object(recognition_worker, "recognize_image", return_value=object()) as rec, \
            patch.object(recognition_worker, "validate_page", return_value=ok), \
            caplog.at_level(logging.INFO, logger=_LOGGER):
        done, _ = _run(RecognitionWorker(_task(), [object(), object()], {}, set()))

    assert done == [ok]
    assert rec.call_count == 1
    assert "截图数=2" in caplog.text
