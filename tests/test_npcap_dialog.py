"""随包 Npcap 安装包探测测试 - find_bundled_npcap_installer 候选目录覆盖。

分发目录固定结构（2026-08-18）：安装包位于 辅助工具/npcap-*.exe；
开发环境为仓库根 redist/；两者与 exe 同目录任一命中即可。
"""
import os

import pytest

from gacha_exporter.gui import npcap_dialog
from gacha_exporter.gui.npcap_dialog import find_bundled_npcap_installer


@pytest.fixture
def app_base(tmp_path, monkeypatch):
    """伪造 exe 目录，重定向 get_app_dir。"""
    base = tmp_path / "抽卡工具"
    base.mkdir()
    monkeypatch.setattr(npcap_dialog, "get_app_dir", lambda: str(base))
    return base


def _make_installer(folder, name="npcap-1.88.exe"):
    folder.mkdir(parents=True, exist_ok=True)
    installer = folder / name
    installer.write_bytes(b"fake")
    return installer


def test_found_in_dist_aux_tools(app_base):
    """分发目录固定结构：辅助工具/npcap-*.exe 应被识别。"""
    installer = _make_installer(app_base / "辅助工具")
    assert find_bundled_npcap_installer() == str(installer)


def test_found_in_redist(app_base):
    """开发环境：redist/ 子目录应被识别。"""
    installer = _make_installer(app_base / "redist")
    assert find_bundled_npcap_installer() == str(installer)


def test_found_next_to_exe(app_base):
    """exe 同目录的 npcap-*.exe 应被识别。"""
    installer = _make_installer(app_base)
    assert find_bundled_npcap_installer() == str(installer)


def test_not_found(app_base):
    """无安装包时返回 None（按钮置灰、引导官网下载）。"""
    assert find_bundled_npcap_installer() is None


def test_ignores_non_npcap_exe(app_base):
    """辅助工具/ 里非 npcap 的 exe 不算安装包。"""
    _make_installer(app_base / "辅助工具", name="翻页助手.exe")
    assert find_bundled_npcap_installer() is None
