"""健康检查 + 整体回退门测试（第二批 D，见 remote_data.py）。

核心行为：
- check_health 从 role URL 取 **scheme://host** 拼 /health（服务端根路径，
  nginx `location = /health` 反代；拼到 /v1 base 下会 404）——实测
  https://example.com/health 200、/example-key/v1/health 404。
- 健康不可用 -> 三个远程加载器（role/公告/版本）整体回退，不再逐接口 10s 串行超时。
- 设置页手动检查会 set_remote_available(None) 重置缓存重新探测。
"""
import pytest

from gacha_exporter.loaders import remote_data
from gacha_exporter.loaders.remote_data import (
    _remote_available,
    check_health,
    load_announcements,
    load_role_data,
    load_version_info,
    set_remote_available,
)


@pytest.fixture(autouse=True)
def _reset_health_cache():
    """每个用例前后清空健康缓存，避免模块级状态串扰。"""
    set_remote_available(None)
    yield
    set_remote_available(None)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"characters": {}, "pools": {}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_check_health_uses_server_origin_not_base(monkeypatch):
    """/health 在服务端根路径：从 role URL 取 origin 拼 /health，不是 /v1 base。"""
    captured = {}

    def fake_get(url, timeout=None):
        captured["url"] = url
        return _Resp(status_code=200)

    monkeypatch.setattr(remote_data.requests, "get", fake_get)
    assert check_health(remote_url="https://example.com/test/role") is True
    assert captured["url"] == "https://example.com/health"


def test_check_health_non_200_is_false(monkeypatch):
    monkeypatch.setattr(remote_data.requests, "get",
                        lambda url, timeout=None: _Resp(status_code=503))
    assert check_health(remote_url="https://a.test/example-key/v1/role") is False


def test_check_health_network_error_is_false(monkeypatch):
    def boom(url, timeout=None):
        raise ConnectionError("down")

    monkeypatch.setattr(remote_data.requests, "get", boom)
    assert check_health(remote_url="https://a.test/example-key/v1/role") is False


def test_load_role_data_skips_remote_when_unavailable(monkeypatch):
    """健康不可用：load_role_data 不发远程请求，直接缓存回退，remote_ok=False。"""
    set_remote_available(False)
    calls = []

    def boom(url, timeout=None):
        calls.append(url)
        raise AssertionError("健康不可用时不应发起远程请求")

    monkeypatch.setattr(remote_data.requests, "get", boom)
    monkeypatch.setattr(remote_data, "_load_cache_or_raise",
                        lambda: {"characters": {}, "pools": {}})
    chars, pools, pity, ok = load_role_data(prefer_remote=True)
    assert ok is False
    assert calls == []


def test_load_version_info_skips_when_unavailable(monkeypatch):
    set_remote_available(False)
    calls = []

    def boom(url, timeout=None):
        calls.append(url)
        raise AssertionError("健康不可用时不应发起远程请求")

    monkeypatch.setattr(remote_data.requests, "get", boom)
    assert load_version_info() is None
    assert calls == []


def test_load_announcements_skips_when_unavailable(monkeypatch):
    set_remote_available(False)
    calls = []

    def boom(url, timeout=None):
        calls.append(url)
        raise AssertionError("健康不可用时不应发起远程请求")

    monkeypatch.setattr(remote_data.requests, "get", boom)
    assert load_announcements() == []
    assert calls == []


def test_load_role_data_uses_remote_when_available(monkeypatch):
    """健康可用：load_role_data 照常远程拉取，remote_ok=True。"""
    set_remote_available(True)
    monkeypatch.setattr(remote_data, "_write_cache", lambda data: None)
    monkeypatch.setattr(remote_data.requests, "get",
                        lambda url, timeout=None, **kw: _Resp(status_code=200))
    chars, pools, pity, ok = load_role_data(prefer_remote=True)
    assert ok is True


def test_set_remote_available_none_reprobes(monkeypatch):
    """None 清缓存后 _remote_available 重新探测（设置页手动检查路径）。"""
    monkeypatch.setattr(remote_data, "check_health", lambda *a, **k: True)
    set_remote_available(None)
    assert _remote_available() is True
