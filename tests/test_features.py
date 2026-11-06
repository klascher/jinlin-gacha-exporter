# -*- coding: utf-8 -*-
"""功能注册表与扩展开关测试：默认值、持久化、runtime 模块依赖门控。"""
import pytest

from gacha_exporter import features
from gacha_exporter.storage import settings


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """settings.json 落在 tmp_path；各组件模块可用性可控（默认全可用）。"""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_settings_path", lambda: str(path))
    available = {"ocr": True, "training": True, "team_builder": True}
    monkeypatch.setattr(
        features, "module_available",
        lambda module, refresh=False: available.get(module, False),
    )
    return available


def test_defaults(isolated):
    """核心识别默认开；练度/排轴默认关。"""
    assert features.enabled("recognition") is True
    assert features.enabled("training") is False
    assert features.enabled("team_builder") is False
    assert features.get("nope") is None


def test_toggle_persists(isolated):
    assert features.enabled("training") is False
    features.set_enabled("training", True)
    assert features.enabled("training") is True
    features.set_enabled("training", False)
    assert features.enabled("training") is False


def test_runtime_gate(isolated, monkeypatch):
    monkeypatch.setattr(features, "module_available", lambda module, refresh=False: False)
    assert features.available("recognition") is False
    assert features.available("training") is False
    assert features.available("team_builder") is False
    assert "组件" in features.unavailable_reason("recognition")
    assert features.unavailable_reason("team_builder") != ""


def test_gate_requires_all_declared_modules(isolated):
    """练度/排轴依赖 ocr + 各自模块，只装一个不够。"""
    isolated["ocr"] = True
    isolated["training"] = False
    assert features.available("training") is False
    assert "练度识别组件" in features.unavailable_reason("training")

    isolated["training"] = True
    assert features.available("training") is True
    assert features.unavailable_reason("training") == ""

    isolated["ocr"] = False
    assert features.available("training") is False


def test_legacy_launcher_points_to_new_package(isolated, monkeypatch):
    """0.4.0 旧壳：不要把用户引向无效的 runtime 下载。"""
    monkeypatch.setattr(features, "module_available", lambda module, refresh=False: False)
    monkeypatch.setattr(features.host_env, "is_legacy_launcher", lambda: True)
    reason = features.unavailable_reason("recognition")
    assert "1.0.0" in reason and "新版整包" in reason
    assert "功能组件…" not in reason


def test_modules_for_enabled_features(isolated):
    """启动检查只针对已开启功能所需的模块（去重、保持注册顺序）。"""
    assert features.enabled_modules() == ["ocr"]  # 默认只开识别
    features.set_enabled("training", True)
    features.set_enabled("team_builder", True)
    assert features.enabled_modules() == ["ocr", "training", "team_builder"]

    features.set_enabled("recognition", False)
    assert features.modules_for(["training", "team_builder"]) == ["ocr", "training", "team_builder"]
    assert features.modules_for(["nope"]) == []


def test_unknown_feature(isolated):
    assert features.enabled("nope") is False
    assert features.available("nope") is False
    assert features.unavailable_reason("nope") == "功能未注册"
    features.set_enabled("nope", True)  # 不抛错、不写
    assert features.enabled("nope") is False


# ---- 「停用功能以跳过组件更新」判定（2026-10-26） ----

def test_can_disable_flags():
    """卡池识别可停用；练度识别不可（用户已开启时必须更新组件）。"""
    assert features.get("recognition").can_disable is True
    assert features.get("training").can_disable is False
    assert features.get("team_builder").can_disable is True


def test_can_skip_by_disabling_recognition_only(isolated):
    """只开识别：待装 ocr 可通过停用识别跳过。"""
    assert features.can_skip_by_disabling(["ocr"]) is True
    assert features.disable_features_for(["ocr"]) == ["recognition"]


def test_training_cannot_be_skipped_by_disabling_recognition(isolated):
    """练度用 ocr：即使停用识别，ocr 仍被不可停用的练度依赖。"""
    features.set_enabled("training", True)
    assert features.unskippable_modules() == ["ocr", "training"]
    assert features.can_skip_by_disabling(["ocr", "training"]) is False
    # 停用列表只含可停用功能（练度不在其中）
    assert features.disable_features_for(["ocr", "training"]) == ["recognition"]


def test_training_only_cannot_skip(isolated):
    features.set_enabled("recognition", False)
    features.set_enabled("training", True)
    assert features.can_skip_by_disabling(["ocr"]) is False
    assert features.disable_features_for(["ocr"]) == []


def test_team_builder_can_be_skipped(isolated):
    """排轴当前仍视为可停用（用户只点名练度线不可停）。"""
    features.set_enabled("recognition", False)
    features.set_enabled("team_builder", True)
    assert features.can_skip_by_disabling(["ocr", "team_builder"]) is True
    assert features.disable_features_for(["ocr", "team_builder"]) == ["team_builder"]


def test_empty_pending_never_blocks(isolated):
    assert features.can_skip_by_disabling([]) is True
    assert features.disable_features_for([]) == []
