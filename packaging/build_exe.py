"""PyInstaller 打包脚本 - 输出瘦身 exe：金鳞抽卡导出工具.exe。

瘦身架构（见 打包更新说明）：
- 入口为 bootstrap.py（含 PySide6 + app.zip 状态机），业务逻辑在 app.zip
- --exclude-module gacha_exporter 级联排除业务包（exe 永不更新）
- zip 内代码动态 import 的第三方库（scapy/openpyxl/requests）静态分析
  看不到，必须 --hidden-import 显式带上
- OCR 栈（numpy/cv2/onnxruntime/rapidocr）不进 exe：由同目录 runtime/
  独立发布（build_runtime.py），bootstrap 启动时挂 sys.path + DLL 目录
- 本脚本只负责 exe；app.zip 由 build_app_zip.py 生成（两者同放 dist/ 发布）
"""
import os
import shutil
import sys
import sysconfig

import PyInstaller.__main__

# packaging/ 的上层才是项目根
PACKAGING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGING_DIR)
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")
# PyInstaller 入口（bootstrap 与 appzip_ops 同目录，经 --paths 加入搜索）
BOOTSTRAP_ENTRY = os.path.join(PACKAGING_DIR, "bootstrap.py")


def clean() -> None:
    """清理旧的打包产物（保留 dist/app.zip，它由 build_app_zip.py 管理）。"""
    for d in (BUILD_DIR,):
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"已清理 {d}")
    # 仅删 exe 产物，不动 app.zip
    exe = os.path.join(DIST_DIR, "金鳞抽卡导出工具.exe")
    if os.path.isfile(exe):
        os.remove(exe)
        print(f"已清理 {exe}")


def _prepare_icon() -> str | None:
    """从 app_icon.py 的内嵌 PNG 生成多尺寸 .ico，返回其路径。

    单源原则：exe 文件图标与应用内窗口图标同出一源（app_icon.py 的
    _ICON_PNG_B64），换图标只改 app_icon.py 一处、两端同步生效。
    Pillow 缺失或生成失败时返回 None（exe 退回默认图标，不阻断打包）。
    """
    try:
        import base64 as _b64
        import io as _io

        from PIL import Image

        # 项目根入搜索路径，直接 import 业务包常量（构建机有 PySide6，可加载）
        sys.path.insert(0, PROJECT_ROOT)
        from gacha_exporter.gui.app_icon import _ICON_PNG_B64

        png = _b64.b64decode("".join(_ICON_PNG_B64.split()))
        img = Image.open(_io.BytesIO(png))
        os.makedirs(BUILD_DIR, exist_ok=True)
        ico_path = os.path.join(BUILD_DIR, "app_icon.ico")
        img.save(
            ico_path,
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
        print(f"已生成 exe 图标: {ico_path}")
        return ico_path
    except Exception as e:  # noqa: BLE001 —— 图标失败不应挡发版
        print(f"警告：exe 图标生成失败（{e}），本次打包无自定义图标")
        return None


# 被 runtime 外部依赖间接使用、但业务包静态图里不出现的标准库子模块。
# PyInstaller 只收集静态可达模块；把标准库整体作为超集钉进 exe（纯 pyc，
# 实测 69→76MB），换取「runtime 里任意新依赖都能找到标准库」的稳定性。
# 排除项都是 GUI/开发/测试用途，OCR/数值依赖不会触达。
_STDLIB_EXCLUDES = {
    "tkinter", "idlelib", "turtle", "turtledemo", "test", "ensurepip",
    "lib2to3", "venv", "antigravity", "this", "__phello__", "pydoc_data",
}


def _stdlib_hidden_imports() -> list[str]:
    names = getattr(sys, "stdlib_module_names", ())
    if not names:  # 理论不会发生（构建固定 Py3.11）；退化为不追加
        return []
    return sorted(n for n in names if n not in _STDLIB_EXCLUDES)


def _python3_dll() -> str | None:
    """稳定 ABI 转发层 python3.dll（cv2 5.x 等 wheel 链接它，而非 python311.dll）。

    实测缺失时外部 cv2 import 报「DLL load failed: 找不到指定的模块」。
    """
    stdlib_dir = sysconfig.get_paths().get("stdlib")
    if not stdlib_dir:
        return None
    path = os.path.join(os.path.dirname(stdlib_dir), "python3.dll")
    return path if os.path.isfile(path) else None


def build() -> int:
    """执行 PyInstaller 打包。"""
    clean()
    icon_path = _prepare_icon()

    args = [
        BOOTSTRAP_ENTRY,
        # bootstrap 的同目录（packaging/）放 appzip_ops，显式加入搜索路径
        "--paths", PACKAGING_DIR,
        "--name", "金鳞抽卡导出工具",
        "--onefile",
        "--windowed",
        "--icon", icon_path or "NONE",
        # 级联排除业务包：name.startswith("gacha_exporter.") 全被排除，
        # 运行时由 bootstrap 动态从 app.zip 导入。
        "--exclude-module", "gacha_exporter",
        # 以下只被 zip 内代码 import，静态分析看不到，必须显式带上。
        # 注意：--exclude-module gacha_exporter 让 PyInstaller 看不到业务包内的
        # 任何 import，因此业务包用到的「仅它才引用」的依赖都要在这里补——
        # 包括第三方库，也包括标准库子模块（如 logging.handlers，除业务包外
        # 没人 import，实测缺失会导致启动失败）。新增依赖时须复查本清单。
        "--hidden-import", "logging.handlers",
        "--hidden-import", "scapy.all",
        "--hidden-import", "scapy.layers.all",
        "--hidden-import", "openpyxl",
        "--hidden-import", "requests",
        # ed25519 验签：业务包 utils/signing.py 只被 zip 内代码 import，
        # 静态分析看不到，必须显式带上（PyInstaller 自带 cryptography hooks）
        "--hidden-import", "cryptography",
        # webbrowser：main_window/update_dialog/__main__ 顶层 import（打开下载页/公告链接），
        # 仅 zip 内代码引用、标准库纯 py 模块，静态分析看不到也不被其它依赖传递带入
        #（2026-10-08 实测缺失 → 启动即 ModuleNotFoundError，弹「app.zip 损坏」）
        "--hidden-import", "webbrowser",
        # 截屏回退（Qt 失败/黑帧时用）：mss 只在 zip 内代码里动态 import
        "--hidden-import", "mss",
        "--hidden-import", "mss.tools",
        # OCR 栈不再进 exe（2026-10-21）：由 runtime/ 独立更新，见 build_runtime.py。
        # 但外部包会用到业务包静态图之外的标准库模块，必须把标准库超集带上。
        "--distpath", DIST_DIR,
        "--workpath", BUILD_DIR,
        "--specpath", PROJECT_ROOT,
    ]

    for name in _stdlib_hidden_imports():
        args += ["--hidden-import", name]

    python3_dll = _python3_dll()
    if python3_dll:
        args += ["--add-binary", f"{python3_dll}{os.pathsep}."]
    else:
        print("警告：未找到 python3.dll，外部 cv2 可能在运行时加载失败")

    PyInstaller.__main__.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(build())
