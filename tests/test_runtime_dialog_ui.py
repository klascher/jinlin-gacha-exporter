# -*- coding: utf-8 -*-
"""强制组件更新对话框的按钮语义（offscreen，不起下载线程）。

下载/验签由 test_runtime_pack 覆盖；这里只验证：强制模式不能 X/Esc 绕过、
可停用功能走「停用」出口、不可停用功能只能下载/退出/失败后继续、安装成功
请求立即重启。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from gacha_exporter import features  # noqa: E402
from gacha_exporter.gui import runtime_dialog as rd  # noqa: E402
from gacha_exporter.models import RuntimeInfo, VersionInfo  # noqa: E402
from gacha_exporter.storage import settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_settings_path",
                        lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(rd.runtime_pack, "current_version",
                        lambda module, app_dir="": "")
    monkeypatch.setattr(rd.runtime_pack, "update_available",
                        lambda module, remote="", app_dir="": True)
    monkeypatch.setattr(rd.host_env, "is_legacy_launcher", lambda: False)
    monkeypatch.setattr(rd.QMessageBox, "warning",
                        staticmethod(lambda *args, **kwargs: None))
    return tmp_path


def _info():
    return VersionInfo(
        latest_version="1.0.0",
        runtimes={
            "ocr": RuntimeInfo(module="ocr", version="2.0.0"),
            "training": RuntimeInfo(
                module="training", version="1.0.0", requires=("ocr",)
            ),
        },
    )


def _finished(dialog):
    seen = []
    dialog.finished.connect(seen.append)
    return seen


def test_force_skip_disables_recognition(qapp, env):
    """可停用功能：按钮为停用出口；X/Esc(reject) 不能静默绕过。"""
    dlg = rd.RuntimeUpdateDialog(_info(), modules=["ocr"], force=True)
    assert "停用相关功能" in dlg.btn_secondary.text()
    seen = _finished(dlg)
    dlg.reject()  # 模拟 X/Esc
    assert seen == [], "强制模式不得被 reject 绕过"
    dlg.btn_secondary.click()
    assert seen == [QDialog.Rejected]
    assert dlg.disabled_features == ["recognition"]
    assert features.enabled("recognition") is False


def test_force_training_cannot_skip(qapp, env):
    """练度开启后 ocr 不可跳过：只能退出/更新。"""
    features.set_enabled("training", True)
    dlg = rd.RuntimeUpdateDialog(_info(), modules=["ocr", "training"], force=True)
    assert dlg.btn_secondary.text() == "退出程序"
    seen = _finished(dlg)
    dlg.btn_secondary.click()
    assert seen == [QDialog.Rejected]
    assert dlg.exit_requested is True
    assert features.enabled("training") is True


def test_manual_dialog_can_close(qapp, env):
    dlg = rd.RuntimeUpdateDialog(_info(), manual=True)
    assert dlg.force is False
    assert dlg.btn_secondary.text() == "关闭"
    seen = _finished(dlg)
    dlg.btn_secondary.click()
    assert seen == [QDialog.Rejected]


def test_success_requests_restart(qapp, env):
    dlg = rd.RuntimeUpdateDialog(_info(), modules=["ocr"], force=True)
    seen = _finished(dlg)
    dlg._finish_all()
    assert dlg.restart_requested is True
    assert seen == [QDialog.Accepted]


def test_failure_with_unskippable_feature_allows_continue(qapp, env):
    """下载失败：不可停用时只允许继续运行（标出功能不可用）。"""
    features.set_enabled("training", True)
    dlg = rd.RuntimeUpdateDialog(_info(), modules=["ocr", "training"], force=True)
    dlg._failed = ["ocr：网络错误"]
    dlg._finish_all()
    assert dlg.restart_requested is False
    assert "继续运行" in dlg.btn_secondary.text()
    assert "暂不可用" in dlg.btn_secondary.text()
    seen = _finished(dlg)
    dlg.btn_secondary.click()
    assert seen == [QDialog.Accepted]


def test_failure_with_skippable_feature_offers_skip(qapp, env):
    dlg = rd.RuntimeUpdateDialog(_info(), modules=["ocr"], force=True)
    dlg._failed = ["ocr：网络错误"]
    dlg._finish_all()
    assert "停用相关功能" in dlg.btn_secondary.text()
    seen = _finished(dlg)
    dlg.btn_secondary.click()
    assert seen == [QDialog.Rejected]
    assert dlg.disabled_features == ["recognition"]


def test_api2_launcher_success_asks_manual_restart(qapp, env, monkeypatch):
    """API=2 壳不认识 --restart-after：文案改为手动重启，仍请求调用方处理。"""
    monkeypatch.setattr(rd.host_env, "supports_auto_restart", lambda: False)
    dlg = rd.RuntimeUpdateDialog(_info(), modules=["ocr"], force=True)
    dlg._finish_all()
    assert dlg.restart_requested is True
    assert "手动重启" in dlg.status.text()
