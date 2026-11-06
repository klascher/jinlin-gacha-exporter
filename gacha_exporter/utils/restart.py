"""安装功能组件后自动重启工具。

新进程通过 ``--restart-after=<旧进程 PID>`` 标记启动，**等旧进程真正退出
后**才继续初始化：否则两个进程会同时抢占 Npcap 网卡与 data/ 下的历史文件。
旧进程走正常关窗流程（``closeEvent`` 停抓包线程、合并历史）后再退出。

冻结态下该标记由 ``packaging/bootstrap.py`` 在加载 app.zip 之前处理（必须
早于任何界面/业务导入）；源码态由 ``__main__.main()`` 开头处理。两边共用
同一标记格式与本模块的解析/等待实现（bootstrap 内联等价逻辑，不能 import
app.zip 里的模块）。
"""
import os
import subprocess
import sys
import time
from typing import Iterable, List, Optional

from .logger import get_logger
from .paths import get_app_dir

logger = get_logger(__name__)

RESTART_FLAG = "--restart-after="
# 旧进程关窗最多等抓包线程 2s、再合并历史；60s 是给慢磁盘/异常情况的余量。
DEFAULT_WAIT_TIMEOUT = 60.0


def restart_args(pid: int) -> List[str]:
    """新进程命令行（源码态用 -m 运行包；冻结态直接重开 exe）。"""
    flag = f"{RESTART_FLAG}{int(pid)}"
    if getattr(sys, "frozen", False):
        return [sys.executable, flag]
    return [sys.executable, "-m", "gacha_exporter", flag]


def parse_restart_pid(argv: Iterable[str]) -> Optional[int]:
    """从命令行解析旧进程 PID；无标记/非法值返回 None。"""
    for arg in argv:
        if not arg.startswith(RESTART_FLAG):
            continue
        raw = arg[len(RESTART_FLAG):]
        try:
            pid = int(raw)
        except ValueError:
            return None
        return pid if pid > 0 else None
    return None


def strip_restart_flag(argv: Iterable[str]) -> List[str]:
    """移除重启标记，避免传给 QApplication/业务参数解析。"""
    return [arg for arg in argv if not arg.startswith(RESTART_FLAG)]


def wait_for_exit(pid: int, timeout: float = DEFAULT_WAIT_TIMEOUT) -> bool:
    """等待进程退出；已退出/超时均返回（超时返回 False）。

    超时也继续启动：宁可极小概率双开，也不要让工具卡在启动阶段。
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        return _wait_windows(pid, timeout)
    return _wait_posix(pid, timeout)


def _wait_windows(pid: int, timeout: float) -> bool:
    import ctypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        # 打不开通常意味着进程已不存在（或同用户下无 SYNCHRONIZE 权限，
        # 此时无法可靠等待）：不阻塞启动。
        return True
    try:
        result = kernel32.WaitForSingleObject(handle, int(max(0, timeout) * 1000))
        return result in (wait_object_0, wait_abandoned)
    finally:
        kernel32.CloseHandle(handle)


def _wait_posix(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # 进程存在但属于其他用户：继续等
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def spawn_restart(pid: Optional[int] = None) -> bool:
    """拉起带等待标记的新进程；成功返回 True（调用方随后关闭旧进程）。

    新进程与旧进程**不共享**控制台/标准句柄：旧进程退出后新进程仍独立运行。
    """
    old_pid = os.getpid() if pid is None else int(pid)
    args = restart_args(old_pid)
    kwargs = {
        "cwd": get_app_dir(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：彻底脱离旧进程的控制台
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    except OSError as e:
        logger.error(f"自动重启失败: {e}")
        return False
    logger.info(f"已拉起重启进程（等待旧进程 {old_pid} 退出）：{args}")
    return True
