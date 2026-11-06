# -*- coding: utf-8 -*-
"""功能组件对话框的纯逻辑测试：模块状态、待安装列表、启动提示判定。

不创建 QDialog（无需显示），只测可用于启动决策的函数；下载/安装链路见
test_runtime_pack.py 与真实签名安装冒烟。
"""
from gacha_exporter.gui import runtime_dialog as rd
from gacha_exporter.models import RuntimeInfo, VersionInfo
from gacha_exporter.utils import runtime_pack as rp


def _info():
    return VersionInfo(
        latest_version="1.0.0",
        runtimes={
            "ocr": RuntimeInfo(module="ocr", version="2.0.0", label="视觉识别组件"),
            "training": RuntimeInfo(
                module="training", version="1.0.0", requires=("ocr",)
            ),
            "team_builder": RuntimeInfo(
                module="team_builder", version="0.5.0", requires=("ocr",)
            ),
        },
    )


def test_module_states_marks_missing_update_and_current(monkeypatch):
    """未安装/可更新/已最新三种状态。"""
    local = {"ocr": "1.0.0", "training": "1.0.0", "team_builder": ""}
    newer = {"ocr": True, "training": False, "team_builder": True}
    monkeypatch.setattr(rp, "current_version", lambda module, app_dir="": local[module])
    monkeypatch.setattr(
        rp, "update_available",
        lambda module, remote="", app_dir="": newer[module],
    )

    states = {s.info.module: s for s in rd.module_states(_info())}
    assert states["ocr"].needs_install is True
    assert "可更新" in states["ocr"].status and "1.0.0" in states["ocr"].status
    assert states["training"].needs_install is False
    assert "已是最新" in states["training"].status
    assert states["team_builder"].needs_install is True
    assert "未安装" in states["team_builder"].status


def test_pending_modules_respects_feature_module_filter(monkeypatch):
    """启动自动检查只列已开启功能所需模块；手动打开（None）列全部。"""
    monkeypatch.setattr(rp, "current_version", lambda module, app_dir="": "")
    monkeypatch.setattr(
        rp, "update_available", lambda module, remote="", app_dir="": True
    )
    info = _info()
    assert [r.module for r in rd.pending_modules(info)] == [
        "ocr", "training", "team_builder"
    ]
    assert [r.module for r in rd.pending_modules(info, ["ocr", "training"])] == [
        "ocr", "training"
    ]
    assert rd.needs_install(info) is True
    assert rd.needs_install(None) is False


def test_legacy_launcher_never_offers_runtime(monkeypatch):
    """0.4.0 旧壳不支持 runtime 挂载：自动/手动都不应提示下载。"""
    monkeypatch.setattr(rd.host_env, "is_legacy_launcher", lambda: True)
    monkeypatch.setattr(
        rp, "update_available", lambda module, remote="", app_dir="": True
    )
    info = _info()
    assert rd.pending_modules(info) == []
    assert rd.needs_install(info) is False
    assert rd.skippable_features(info) == []


def test_nothing_to_install(monkeypatch):
    monkeypatch.setattr(rp, "current_version", lambda module, app_dir="": "")
    monkeypatch.setattr(
        rp, "update_available", lambda module, remote="", app_dir="": False
    )
    assert rd.pending_modules(_info()) == []
    assert rd.skippable_features(_info()) == []
    assert rd.module_states(None) == []


# ---- 强制更新：哪些功能可以靠停用跳过（2026-10-26） ----

def _all_pending(monkeypatch):
    monkeypatch.setattr(rp, "current_version", lambda module, app_dir="": "")
    monkeypatch.setattr(
        rp, "update_available", lambda module, remote="", app_dir="": True
    )


def _features_enabled(monkeypatch, enabled):
    monkeypatch.setattr(
        rd.features, "enabled", lambda name: enabled.get(name, False)
    )


def test_skippable_only_recognition(monkeypatch):
    """只开识别：可以停用识别来跳过 ocr 安装。"""
    _all_pending(monkeypatch)
    _features_enabled(monkeypatch, {"recognition": True})
    assert rd.skippable_features(_info(), ["ocr"]) == ["recognition"]


def test_training_enabled_blocks_skip(monkeypatch):
    """练度开启后 ocr 不可跳过（ocr 同时被不可停用的练度依赖）。"""
    _all_pending(monkeypatch)
    _features_enabled(monkeypatch, {"recognition": True, "training": True})
    assert rd.skippable_features(_info(), ["ocr"]) == []
    assert rd.skippable_features(_info(), ["ocr", "training"]) == []


def test_team_builder_is_skippable(monkeypatch):
    """排轴是普通扩展功能，可以停用。"""
    _all_pending(monkeypatch)
    _features_enabled(monkeypatch, {"team_builder": True})
    assert rd.skippable_features(_info(), ["ocr", "team_builder"]) == [
        "team_builder"
    ]
