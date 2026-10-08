"""PyInstaller 打包脚本 - 输出瘦身 exe：金鳞抽卡导出工具.exe。

瘦身架构（见 打包更新说明）：
- 入口为 bootstrap.py（含 PySide6 + app.zip 状态机），业务逻辑在 app.zip
- --exclude-module gacha_exporter 级联排除业务包（exe 永不更新）
- zip 内代码动态 import 的第三方库（scapy/openpyxl/requests）静态分析看不到，
  必须 --hidden-import 显式带上
- 本脚本只负责 exe；app.zip 由 build_app_zip.py 生成（两者同放 dist/ 发布）
"""
import os
import shutil
import sys

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
        "--distpath", DIST_DIR,
        "--workpath", BUILD_DIR,
        "--specpath", PROJECT_ROOT,
    ]

    PyInstaller.__main__.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(build())
