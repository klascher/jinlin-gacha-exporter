"""启动器（exe/bootstrap）能力探测。

三层发布架构（2026-10-25 起）：exe 只做壳 + app.zip 业务 + runtime 组件。
0.4.0 旧壳没有 runtime 挂载能力，也不写任何能力标识；新壳由
``packaging/bootstrap.py`` 在加载 app.zip 前写入：

- ``sys._example-key_bootstrap``：启动器 API 版本（旧壳缺省 = 1）；
- ``sys._example-key_exe_version``：壳版本（旧壳缺省 = 0.4.0）；
- ``sys._example-key_runtime``：已挂载组件表（新壳无模块时为空表）。

源码运行（``python -m gacha_exporter``）没有 bootstrap 流程，但本机
Python 自带 OCR 栈、组件门控按 ``find_spec`` 判断，因此按“新壳”处理。
"""
import sys

LEGACY_BOOTSTRAP_API = 1
RUNTIME_MODULES_API = 2   # 开始支持 runtime/<模块>/ 挂载
AUTO_RESTART_API = 3      # 开始识别 --restart-after=<PID> 并等待旧进程退出
CURRENT_BOOTSTRAP_API = 3
LEGACY_EXE_VERSION = "0.4.0"
SOURCE_EXE_VERSION = "dev"

# 官网下载页（旧壳用户升级整包入口；官网页面已有）
LAUNCHER_DOWNLOAD_URL = "https://github.com/klascher/jinlin-gacha-exporter/issues"


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bootstrap_api() -> int:
    """启动器能力版本；源码运行视为当前版本。"""
    if not getattr(sys, "frozen", False):
        # 源码运行 / 开发环境：没有 bootstrap 标识，但本机依赖由源码环境提供
        return CURRENT_BOOTSTRAP_API
    return _int_or(
        getattr(sys, "_example-key_bootstrap", LEGACY_BOOTSTRAP_API),
        LEGACY_BOOTSTRAP_API,
    )


def supports_runtime_modules() -> bool:
    """当前宿主是否支持 runtime/<模块>/ 挂载。"""
    return bootstrap_api() >= RUNTIME_MODULES_API


def supports_auto_restart() -> bool:
    """当前宿主是否认识 --restart-after 并等待旧进程退出（API≥3）。

    API=2 的 1.0.0 首批测试壳会立即启动，无法安全等待旧进程；此时 app.zip
    应退回「组件已安装，请手动重启」提示，避免双开。
    """
    return bootstrap_api() >= AUTO_RESTART_API


def is_legacy_launcher() -> bool:
    """是否为 0.4.0 旧壳（只兼容核心功能，不支持功能组件）。"""
    return not supports_runtime_modules()


def exe_version() -> str:
    """启动器版本；源码运行返回 dev，旧壳缺省 0.4.0。"""
    if not getattr(sys, "frozen", False):
        return SOURCE_EXE_VERSION
    value = getattr(sys, "_example-key_exe_version", LEGACY_EXE_VERSION)
    return value if isinstance(value, str) and value else LEGACY_EXE_VERSION


def legacy_upgrade_message() -> str:
    """旧壳下功能组件不可用的用户可读说明。"""
    return (
        "当前启动器版本过旧，无法加载功能组件。识别功能需下载 "
        f"1.0.0 新版整包：{LAUNCHER_DOWNLOAD_URL}"
    )
