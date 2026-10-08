"""瘦身 exe 入口 - 交换/修复 app.zip 状态机后，从 zip 动态加载业务包。

构建产物结构（同一目录）：
- 金鳞抽卡导出工具.exe（本文件 + PySide6 + 状态机，永不更新）
- app.zip（业务逻辑 .pyc 字节码，更新时整体替换）
- data/（运行时生成：日志/缓存/账号历史，zip 升级天然保留）

Windows 关键约束：zipimport 会占用已导入 zip 的文件句柄（无
FILE_SHARE_DELETE），因此状态机的交换/修复必须先于任何 zip 导入。
"""
import importlib
import os
import sys
import types

import appzip_ops

# exe 内置签名公钥（独立信任根）。打包时由 PyInstaller 经 --paths packaging/
# 把 embedded_pubkey.py 打进来；开发直跑 bootstrap.py 时同目录可 import。
# 导入失败则视为无锚点（业务验签在开发态读 gacha_exporter/_pubkey.py 同值）。
try:
    from embedded_pubkey import PUBLIC_KEY_B64
except Exception:
    PUBLIC_KEY_B64 = ""

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except Exception:  # PySide6 缺失时仅影响错误弹窗，仍可用 stderr 提示
    QApplication = None
    QMessageBox = None


def _app_dir() -> str:
    """exe 所在目录（开发环境为仓库根）。不能 import gacha_exporter.utils.paths
    （业务包在 zip 里），此处内联等价逻辑。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _inject_pubkey() -> None:
    """把 exe 内嵌公钥预置为 sys.modules['gacha_exporter._pubkey']。

    import 机制先查 sys.modules，因此 app.zip 内同名模块会被本对象覆盖——
    即使 app.zip 被替换为带不同公钥的伪造包，验签锚点仍以 exe 内嵌为准。
    必须在任何 gacha_exporter 业务 import 之前调用（清残包之后、zipimport
    命中之前）。
    """
    if not PUBLIC_KEY_B64:
        return
    mod = types.ModuleType("gacha_exporter._pubkey")
    mod.PUBLIC_KEY_B64 = PUBLIC_KEY_B64
    sys.modules["gacha_exporter._pubkey"] = mod


def _load_from(zip_path: str):
    """从指定 zip 动态导入 gacha_exporter 包，返回其 __main__ 模块。

    sys.path.insert(0, zip_path) 让 PathFinder 在 PyInstaller FrozenImporter
    找不到（已 exclude）时命中 zipimport。字符串 import 规避 PyInstaller
    静态分析把业务包打进 exe。
    """
    # 清掉可能残留的残包（也会清掉旧注入），再注入 exe 公钥锚点，最后让 zip 优先
    for name in list(sys.modules):
        if name == "gacha_exporter" or name.startswith("gacha_exporter."):
            del sys.modules[name]
    _inject_pubkey()
    sys.path.insert(0, zip_path)
    return importlib.import_module("gacha_exporter.__main__")


def _dump_exc(app_dir: str, exc: BaseException) -> None:
    """把导入异常写到 <app_dir>/bootstrap_error.log。

    windowed 模式 sys.stderr 是空流，控制台看不到 traceback；落盘便于排查
    打包后启动失败（zipimport / 缺 hidden-import 等问题）。
    """
    import traceback

    try:
        with open(
            os.path.join(app_dir, "bootstrap_error.log"), "w", encoding="utf-8"
        ) as f:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass


def main() -> int:
    app_dir = _app_dir()

    # ① 修复上一轮失败/过期备份（此刻无 zip 被打开）
    appzip_ops.repair_after_failed_import(app_dir)
    # ② 处理待安装补丁（此刻无 zip 被打开）
    appzip_ops.swap_pending(app_dir)

    cur = appzip_ops.app_zip_path(app_dir)
    bak = appzip_ops.backup_path(app_dir)
    mod = None
    if os.path.isfile(cur):
        try:
            mod = _load_from(cur)
        except Exception as e:
            _dump_exc(app_dir, e)  # 主包导入异常落盘，便于排查
            # 当前包导入失败：有 .bak 则本会话回退运行，并留 .bad 标记
            if os.path.isfile(bak):
                try:
                    mod = _load_from(bak)
                    appzip_ops.mark_rollback(app_dir)
                except Exception:
                    mod = None

    if mod is None:
        import traceback

        traceback.print_exc(file=sys.stderr)
        if QMessageBox is not None:
            QApplication(sys.argv)
            QMessageBox.critical(
                None, "启动失败",
                "应用组件 (app.zip) 缺失或损坏。\n请重新下载程序包，或联系管理员。",
            )
        else:
            print("启动失败：应用组件 (app.zip) 缺失或损坏。", file=sys.stderr)
        return 1

    return mod.main()


if __name__ == "__main__":
    sys.exit(main())
