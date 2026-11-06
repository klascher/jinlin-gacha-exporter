# -*- coding: utf-8 -*-
"""resolve_appzip_url 补丁下载地址推导（本地联调跟随 JINLIN_ROLE_URL）。"""
import pytest

from gacha_exporter.loaders.remote_data import resolve_appzip_url

PROD_URL = "https://example.com/v1/app.zip"


def _clear_env(monkeypatch):
    for var in ("JINLIN_APPZIP_URL", "JINLIN_ROLE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_no_env_uses_server_url(monkeypatch):
    """未设环境变量（生产用户）：直接用服务端下发的 update_url。"""
    _clear_env(monkeypatch)
    assert resolve_appzip_url(PROD_URL) == PROD_URL


def test_role_url_derives_same_base(monkeypatch):
    """设了 JINLIN_ROLE_URL（本地联调）：同 base 换末段 -> app.zip。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("JINLIN_ROLE_URL", "http://127.0.0.1:5000/example-key/v1/role")
    assert resolve_appzip_url(PROD_URL) == "http://127.0.0.1:5000/example-key/v1/app.zip"


def test_appzip_url_env_wins(monkeypatch):
    """JINLIN_APPZIP_URL 显式覆盖优先于 ROLE_URL 推导。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("JINLIN_ROLE_URL", "http://127.0.0.1:5000/example-key/v1/role")
    monkeypatch.setenv("JINLIN_APPZIP_URL", "http://192.168.1.5:9000/other/app.zip")
    assert resolve_appzip_url(PROD_URL) == "http://192.168.1.5:9000/other/app.zip"


def test_role_url_to_prod_domain_derives_prod(monkeypatch):
    """ROLE_URL 指向生产域名时推导结果与生产 update_url 一致（无副作用）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("JINLIN_ROLE_URL", "https://example.com/v1/role")
    assert resolve_appzip_url(PROD_URL) == PROD_URL


# ---- 功能组件包地址（按模块，2026-10-21 多模块化） ----

from gacha_exporter.loaders.remote_data import resolve_runtime_url

RUNTIME_URL = "https://example.com/v1/runtime/ocr.zip"


def _clear_runtime_env(monkeypatch):
    for var in ("JINLIN_RUNTIME_URL", "JINLIN_ROLE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_runtime_no_env_uses_server_url(monkeypatch):
    """未设环境变量：用 version.json 下发的该模块 url（各模块可各自指向）。"""
    _clear_runtime_env(monkeypatch)
    assert resolve_runtime_url("ocr", RUNTIME_URL) == RUNTIME_URL
    assert resolve_runtime_url("training", "https://cdn/x/training.zip") == \
        "https://cdn/x/training.zip"


def test_runtime_role_url_derives_same_base(monkeypatch):
    """本地联调：同 base 推导 runtime/<模块>.zip，不会去生产下 76MB。"""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("JINLIN_ROLE_URL", "http://127.0.0.1:5000/example-key/v1/role")
    assert resolve_runtime_url("ocr", RUNTIME_URL) == \
        "http://127.0.0.1:5000/example-key/v1/runtime/ocr.zip"
    assert resolve_runtime_url("training", RUNTIME_URL) == \
        "http://127.0.0.1:5000/example-key/v1/runtime/training.zip"


def test_runtime_url_env_wins(monkeypatch):
    """JINLIN_RUNTIME_URL 优先；完整地址对所有模块生效（单包联调）。"""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("JINLIN_ROLE_URL", "http://127.0.0.1:5000/example-key/v1/role")
    monkeypatch.setenv("JINLIN_RUNTIME_URL", "http://192.168.1.5:9000/r.zip")
    assert resolve_runtime_url("ocr", RUNTIME_URL) == "http://192.168.1.5:9000/r.zip"
    assert resolve_runtime_url("training", RUNTIME_URL) == "http://192.168.1.5:9000/r.zip"


def test_runtime_url_env_directory_appends_module(monkeypatch):
    """环境变量以 / 结尾视为目录：按模块拼 <模块>.zip。"""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("JINLIN_RUNTIME_URL", "http://192.168.1.5:9000/runtime/")
    assert resolve_runtime_url("ocr", RUNTIME_URL) == \
        "http://192.168.1.5:9000/runtime/ocr.zip"
    assert resolve_runtime_url("team_builder", "") == \
        "http://192.168.1.5:9000/runtime/team_builder.zip"
