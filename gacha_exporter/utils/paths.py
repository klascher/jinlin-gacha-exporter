"""路径管理 - 兼容开发环境和 PyInstaller 打包后的数据路径。

关键：
- get_data_dir 用于可写数据，始终放在 exe/代码同级目录
- get_app_dir 返回 exe 所在目录或开发时的项目根
- 子目录函数 get_logs_dir/get_cache_dir/get_users_dir 按性质分类可写文件
"""
import os
import sys


def get_app_dir() -> str:
    """获取程序根目录（exe 所在目录 或 开发时的项目根目录）。

    Returns:
        绝对路径
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：exe 所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发环境：gacha_exporter/utils/ -> gacha_exporter/ -> 项目根
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_data_dir() -> str:
    """获取可写数据目录（与 exe/代码同级，不放 C 盘）。

    打包后：<exe 目录>/data/
    开发时：<项目根>/gacha_exporter/data/

    Returns:
        数据目录绝对路径（自动创建）
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_logs_dir() -> str:
    """获取日志子目录 <data>/logs/。

    app.log 与 frame_debug.log 都落这里，与配置/缓存分离，方便整体清理。

    Returns:
        日志目录绝对路径（自动创建）
    """
    d = os.path.join(get_data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def get_cache_dir() -> str:
    """获取缓存子目录 <data>/cache/。

    远程数据缓存（role_cache.json）落这里，可整体删除重建不影响功能。

    Returns:
        缓存目录绝对路径（自动创建）
    """
    d = os.path.join(get_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def get_users_dir() -> str:
    """获取账号历史子目录 <data>/users/。

    各账号抽卡历史 <id>.json 及写前备份 <id>_<时间戳>.json 落这里。

    Returns:
        账号历史目录绝对路径（自动创建）
    """
    d = os.path.join(get_data_dir(), "users")
    os.makedirs(d, exist_ok=True)
    return d


def migrate_legacy_flat_files() -> None:
    """一次性把旧平铺布局的可写文件迁到子目录（幂等）。

    旧版本把日志/缓存平铺在 data/ 根，现按性质分 logs/、cache/ 子目录。
    启动时调用（须早于 setup_logging，避免 app.log 被 RotatingFileHandler
    句柄占用导致移动失败）：源文件存在且目标不存在则移动，已迁或目标已存在
    则跳过，移动失败静默忽略（下次启动再试）。users/ 本就是子目录，不动。
    日志的轮转副本（app.log.1/.2/.3 等）一并迁移。
    """
    data = get_data_dir()
    # (文件名, 目标子目录)：主文件 + 轮转副本 .1/.2/.3
    for name, subdir in (
        ("app.log", "logs"),
        ("frame_debug.log", "logs"),
        ("role_cache.json", "cache"),
    ):
        dst_dir = os.path.join(data, subdir)
        os.makedirs(dst_dir, exist_ok=True)
        for candidate in (name, f"{name}.1", f"{name}.2", f"{name}.3"):
            src = os.path.join(data, candidate)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(dst_dir, candidate)
            if os.path.exists(dst):
                continue  # 目标已存在，不覆盖
            try:
                os.replace(src, dst)
            except OSError:
                pass  # 被占用等，下次再试
