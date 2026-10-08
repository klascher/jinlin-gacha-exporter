"""统一日志配置 - 控制台 + 文件双输出。

文件日志写入 get_logs_dir()/app.log（data/logs/ 子目录）。
使用约定：
- __main__.py 启动时调 setup_logging() 一次
- 各模块顶部 logger = get_logger(__name__)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(level: int = logging.INFO) -> None:
    """初始化全局日志配置（仅 __main__.py 调用一次）。

    Args:
        level: 日志级别，开发时可设 logging.DEBUG
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件输出（写入 data 目录，与 exe 同级）
    try:
        from .paths import get_logs_dir
        log_file = os.path.join(get_logs_dir(), "app.log")
        file_handler = RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as e:
        # 文件日志初始化失败不影响程序运行，但打印警告
        print(f"[WARNING] 文件日志初始化失败: {e}")


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger。

    Args:
        name: 通常传 __name__

    Returns:
        logging.Logger 实例
    """
    return logging.getLogger(name)
