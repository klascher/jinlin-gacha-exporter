# -*- coding: utf-8 -*-
"""自动重启工具测试：参数解析、命令行构建、等待旧进程退出。

真实重启（旧进程关窗→新进程启动→组件挂载）在全量环境实测；这里覆盖
容易出错的参数/平台分支，并起一个真实短命子进程验证等待逻辑。
"""
import os
import subprocess
import sys

from gacha_exporter.utils import restart


def test_parse_and_strip_restart_flag():
    assert restart.parse_restart_pid(["--restart-after=42", "--x"]) == 42
    assert restart.parse_restart_pid(["--restart-after=0"]) is None
    assert restart.parse_restart_pid(["--restart-after=abc"]) is None
    assert restart.parse_restart_pid(["--other=1"]) is None
    assert restart.strip_restart_flag(
        ["--restart-after=42", "--x"]
    ) == ["--x"]


def test_restart_args_source_mode(monkeypatch):
    monkeypatch.setattr(restart.sys, "frozen", False, raising=False)
    monkeypatch.setattr(restart.sys, "executable", "python.exe")
    assert restart.restart_args(9) == [
        "python.exe", "-m", "gacha_exporter", "--restart-after=9"
    ]


def test_restart_args_frozen_mode(monkeypatch):
    monkeypatch.setattr(restart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(restart.sys, "executable", "Jinlin.exe")
    assert restart.restart_args(9) == ["Jinlin.exe", "--restart-after=9"]


def test_spawn_restart_detaches_from_old_process(monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(restart.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        restart, "restart_args", lambda pid: ["app.exe", f"--restart-after={pid}"]
    )
    monkeypatch.setattr(restart, "get_app_dir", lambda: "C:/app")

    assert restart.spawn_restart(55) is True
    assert captured["args"] == ["app.exe", "--restart-after=55"]
    assert captured["kwargs"]["cwd"] == "C:/app"
    assert captured["kwargs"]["close_fds"] is True
    if os.name == "nt":
        flags = captured["kwargs"]["creationflags"]
        assert flags & 0x00000008 and flags & 0x00000200
    else:
        assert captured["kwargs"]["start_new_session"] is True


def test_spawn_restart_returns_false_on_oserror(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no exe")

    monkeypatch.setattr(restart.subprocess, "Popen", boom)
    monkeypatch.setattr(restart, "get_app_dir", lambda: "C:/app")
    assert restart.spawn_restart(55) is False


def test_wait_for_exit_returns_true_when_child_exits():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)"]
    )
    try:
        assert restart.wait_for_exit(proc.pid, timeout=10) is True
    finally:
        proc.wait(timeout=10)


def test_wait_for_exit_times_out_on_live_child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    try:
        assert restart.wait_for_exit(proc.pid, timeout=0.05) is False
    finally:
        proc.kill()
        proc.wait(timeout=10)
