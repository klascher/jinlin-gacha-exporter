"""启动路径导入回归：import main_window/excel 不得把重依赖（scapy/openpyxl）拉进内存。

背景：启动慢（仅开发环境 ~3.67s 导入）大头是 scapy.all（~3s）+ openpyxl（~0.2s），
通过 main_window → capture_worker→engine / discovery_dialog→address_discovery / excel
在模块加载期被全量导入。已改为函数内懒加载（engine/_open_pcap、helpers 有同款先例），
本测试用子进程做干净导入后断言 sys.modules 无 scapy / openpyxl，防止将来又在
启动路径顶层 import 重依赖导致回归。
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_CHECK_CODE = (
    "import sys\n"
    "import gacha_exporter.gui.main_window\n"
    "import gacha_exporter.exporters.excel\n"
    "print(any('scapy.' in m or m == 'scapy' for m in sys.modules))\n"
    "print('openpyxl' in sys.modules)\n"
)

# 无 cryptography 时：signing.py 内 crypto 懒导入，启动链不得崩，验签 fail-safe 返回 False
_FAILSAFE_CODE = (
    "import builtins\n"
    "_orig = builtins.__import__\n"
    "def _block(name, *a, **k):\n"
    "    if name == 'cryptography':\n"
    "        raise ModuleNotFoundError('blocked for test')\n"
    "    return _orig(name, *a, **k)\n"
    "builtins.__import__ = _block\n"
    "import gacha_exporter.gui.main_window\n"
    "from gacha_exporter.utils.signing import verify_update_signature, verify_ed25519\n"
    "assert verify_update_signature('AAAA', b'data') is False\n"
    "assert verify_ed25519('AAAA', 'AAAA', b'data') is False\n"
    "print('ok')\n"
)


def _assert_clean_import() -> str:
    """干净子进程导入启动路径，返回 (scapy拉取?, openpyxl拉取?) 两行输出。"""
    proc = subprocess.run(
        [sys.executable, "-c", _CHECK_CODE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"子进程导入失败\nstderr={proc.stderr}"
    return proc.stdout


def test_main_window_import_does_not_pull_scapy_or_openpyxl() -> None:
    out = _assert_clean_import()
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    assert lines == ["False", "False"], f"启动路径仍导入重依赖，输出={out!r}"


def test_startup_survives_missing_cryptography() -> None:
    """无 cryptography 时仍能完整 import 启动链，验签 fail-safe 拒绝（回归：启动曾硬崩）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _FAILSAFE_CODE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"无 cryptography 启动链崩溃\nstderr={proc.stderr}"
    assert "ok" in proc.stdout