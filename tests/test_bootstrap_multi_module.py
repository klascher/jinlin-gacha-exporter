# -*- coding: utf-8 -*-
"""bootstrap 多模块挂载测试：模块枚举、依赖拓扑、sys.path / 元信息。

不启动 exe，只验证 packaging/bootstrap.py 的纯函数；真实挂载冒烟在全量
环境（或临时 exe 目录）做。
"""
import importlib.util
import json
import os
import sys

import pytest

PACKAGING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging"
)


@pytest.fixture(scope="module")
def bootstrap():
    sys.path.insert(0, PACKAGING_DIR)
    spec = importlib.util.spec_from_file_location(
        "nf_bootstrap_under_test", os.path.join(PACKAGING_DIR, "bootstrap.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(PACKAGING_DIR)


def _install_module(app, module, version, requires=()):
    root = os.path.join(app, "runtime", module, version)
    os.makedirs(os.path.join(root, "site-packages"), exist_ok=True)
    with open(os.path.join(root, "runtime.json"), "w", encoding="utf-8") as f:
        json.dump({"module": module, "runtime_version": version,
                   "requires": list(requires)}, f)
    with open(os.path.join(app, "runtime", module, "current.txt"), "w",
              encoding="utf-8") as f:
        f.write(version + "\n")


def test_runtime_modules_reads_all_and_legacy(bootstrap, tmp_path):
    app = str(tmp_path)
    _install_module(app, "ocr", "1.0.0")
    _install_module(app, "training", "2.0.0", requires=["ocr"])

    mods = {m["module"]: m for m in bootstrap._runtime_modules(app)}
    assert set(mods) == {"ocr", "training"}
    assert mods["ocr"]["version"] == "1.0.0"
    assert mods["training"]["requires"] == ("ocr",)
    assert mods["training"]["root"].endswith(
        os.path.join("runtime", "training", "2.0.0")
    )


def test_legacy_single_pack_treated_as_ocr(bootstrap, tmp_path):
    app = str(tmp_path)
    os.makedirs(os.path.join(app, "runtime", "site-packages"))
    mods = {m["module"]: m for m in bootstrap._runtime_modules(app)}
    assert mods["ocr"]["version"] == "legacy"


def test_module_order_deps_first(bootstrap):
    mods = [
        {"module": "training", "requires": ("ocr",)},
        {"module": "team_builder", "requires": ("ocr",)},
        {"module": "ocr", "requires": ()},
    ]
    ordered = [m["module"] for m in bootstrap._order_modules(mods)]
    assert ordered.index("ocr") < ordered.index("training")
    assert ordered.index("ocr") < ordered.index("team_builder")
    assert set(ordered) == {"ocr", "training", "team_builder"}


def test_setup_runtime_mounts_all_and_sets_info(bootstrap, tmp_path, monkeypatch):
    app = str(tmp_path)
    _install_module(app, "ocr", "1.0.0")
    _install_module(app, "training", "2.0.0", requires=["ocr"])

    before = list(sys.path)
    monkeypatch.delattr(sys, "_example-key_runtime", raising=False)
    try:
        info = bootstrap._setup_runtime(app)
        added = [p for p in sys.path if p not in before]
        assert len(added) == 2
        assert any(p.endswith(os.path.join("runtime", "ocr", "1.0.0",
                                           "site-packages")) for p in added)
        assert any(p.endswith(os.path.join("runtime", "training", "2.0.0",
                                           "site-packages")) for p in added)
        assert set(info["modules"]) == {"ocr", "training"}
        # 兼容旧字段
        assert info["version"] == "1.0.0"
        assert sys._example-key_runtime is info
    finally:
        sys.path[:] = before


def test_setup_runtime_empty_sets_empty_info(bootstrap, tmp_path, monkeypatch):
    """新壳即使没装任何组件也写空 modules，用于区别于旧壳（无该属性）。"""
    monkeypatch.delattr(sys, "_example-key_runtime", raising=False)
    info = bootstrap._setup_runtime(str(tmp_path))
    assert info == {"modules": {}}
    assert sys._example-key_runtime is info


def test_capabilities_always_set(bootstrap, monkeypatch):
    """新壳在加载 app.zip 前声明 API 版本与 exe 版本。"""
    monkeypatch.delattr(sys, "_example-key_bootstrap", raising=False)
    monkeypatch.delattr(sys, "_example-key_exe_version", raising=False)
    bootstrap._set_capabilities()
    assert sys._example-key_bootstrap == bootstrap.BOOTSTRAP_API
    assert sys._example-key_exe_version == bootstrap.EXE_VERSION


# ---- 自动重启参数（2026-10-26） ----

def test_restart_flag_waits_and_is_stripped(bootstrap, monkeypatch):
    """新进程等待旧 PID 退出，标记不得留给 QApplication/业务导入。"""
    calls = []
    monkeypatch.setattr(
        bootstrap, "_wait_process_exit",
        lambda pid, timeout: calls.append((pid, timeout)) or True,
    )
    monkeypatch.setattr(
        bootstrap.sys, "argv",
        ["Jinlin.exe", "--restart-after=123", "--other"],
    )
    bootstrap._handle_restart_wait()
    assert calls == [(123, bootstrap._RESTART_WAIT_TIMEOUT)]
    assert bootstrap.sys.argv == ["Jinlin.exe", "--other"]


def test_restart_flag_absent_is_noop(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "argv", ["Jinlin.exe"])
    bootstrap._handle_restart_wait()  # 不应抛错/等待
    assert bootstrap.sys.argv == ["Jinlin.exe"]


def test_parse_restart_pid_rejects_invalid(bootstrap):
    assert bootstrap._parse_restart_pid(["--restart-after=7"]) == 7
    assert bootstrap._parse_restart_pid(["--restart-after=0"]) is None
    assert bootstrap._parse_restart_pid(["--restart-after=x"]) is None
    assert bootstrap._parse_restart_pid([]) is None
