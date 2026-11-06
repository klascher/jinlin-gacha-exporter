# -*- coding: utf-8 -*-
"""启动器能力探测测试：旧壳 / 新壳 / 源码运行三种宿主。"""
import sys

from gacha_exporter.utils import host_env


def test_source_run_is_current(monkeypatch):
    """源码运行没有 bootstrap 标识，但本机依赖可用，按新壳处理。"""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delattr(sys, "_example-key_bootstrap", raising=False)
    assert host_env.bootstrap_api() == host_env.CURRENT_BOOTSTRAP_API
    assert host_env.supports_runtime_modules() is True
    assert host_env.supports_auto_restart() is True
    assert host_env.is_legacy_launcher() is False
    assert host_env.exe_version() == host_env.SOURCE_EXE_VERSION


def test_old_frozen_launcher_is_legacy(monkeypatch):
    """0.4.0 冻结壳没有能力属性 -> 不支持 runtime。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_example-key_bootstrap", raising=False)
    monkeypatch.delattr(sys, "_example-key_exe_version", raising=False)
    assert host_env.bootstrap_api() == host_env.LEGACY_BOOTSTRAP_API
    assert host_env.supports_runtime_modules() is False
    assert host_env.supports_auto_restart() is False
    assert host_env.is_legacy_launcher() is True
    assert host_env.exe_version() == "0.4.0"
    assert "1.0.0" in host_env.legacy_upgrade_message()


def test_runtime_api2_has_no_auto_restart(monkeypatch):
    """1.0.0 首批测试壳（API=2）认识 runtime，但不认识 --restart-after。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_example-key_bootstrap", 2, raising=False)
    monkeypatch.setattr(sys, "_example-key_exe_version", "1.0.0", raising=False)
    assert host_env.supports_runtime_modules() is True
    assert host_env.supports_auto_restart() is False
    assert host_env.is_legacy_launcher() is False


def test_new_frozen_launcher_is_current(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_example-key_bootstrap", 3, raising=False)
    monkeypatch.setattr(sys, "_example-key_exe_version", "1.0.0", raising=False)
    assert host_env.supports_runtime_modules() is True
    assert host_env.supports_auto_restart() is True
    assert host_env.is_legacy_launcher() is False
    assert host_env.exe_version() == "1.0.0"


def test_bad_capability_value_falls_back_to_legacy(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_example-key_bootstrap", "bad", raising=False)
    assert host_env.bootstrap_api() == host_env.LEGACY_BOOTSTRAP_API
    assert host_env.is_legacy_launcher() is True
