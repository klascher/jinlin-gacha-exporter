"""调试抓包开关行为测试（见 gacha_exporter/debug_flags.py）。

核心约束：打包（sys.frozen）态恒 False——即使设置 JINLIN_DEBUG=1 也无法
开启；仅开发环境可开。
"""
import sys

from gacha_exporter.debug_flags import is_debug_capture_enabled


def test_dev_no_env_disabled(monkeypatch):
    monkeypatch.delenv("JINLIN_DEBUG", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert is_debug_capture_enabled() is False


def test_dev_with_env_enabled(monkeypatch):
    monkeypatch.setenv("JINLIN_DEBUG", "1")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert is_debug_capture_enabled() is True


def test_frozen_always_disabled(monkeypatch):
    # 打包态即使设了 env 也恒 False（生产不产生取证类落盘）
    monkeypatch.setenv("JINLIN_DEBUG", "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert is_debug_capture_enabled() is False
