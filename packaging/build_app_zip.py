"""构建逻辑补丁 app.zip - 把 gacha_exporter/ 编译为 legacy .pyc 布局后打包。

布局（zipimport 最先尝试、最稳）：
    gacha_exporter/__init__.pyc
    gacha_exporter/__main__.pyc
    gacha_exporter/models.pyc
    gacha_exporter/gui/main_window.pyc
    ...

规则：
- 不污染源码树：先编译到临时 staging 目录，再打包
- 排除 gacha_exporter/data/ 子树（运行时数据，不入包；见 打包更新说明）
- 不带 .py、不带 __pycache__/；zip 内路径用 `/`
- 产物 dist/app.zip（与 dist/金鳞抽卡导出工具.exe 同目录发布）
"""
import os
import shutil
import tempfile
import zipfile

import py_compile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(PROJECT_ROOT, "gacha_exporter")
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
OUT_ZIP = os.path.join(DIST_DIR, "app.zip")
# 分发目录：打包完成后把 app.zip 同步到这里（用户直接从此目录取件/上传）
DIST_TARGET_DIR = os.path.join(PROJECT_ROOT, "抽卡工具")
# 排除的顶层子树（gacha_exporter/<name>）
EXCLUDED_SUBTREES = ("data",)


def _iter_py_files():
    """遍历包内所有 .py（跳过排除子树）。"""
    for root, dirs, files in os.walk(PKG_DIR):
        # 剪枝：跳过被排除的顶层子树
        rel_root = os.path.relpath(root, PKG_DIR)
        if rel_root != ".":
            first = rel_root.split(os.sep)[0]
            if first in EXCLUDED_SUBTREES:
                dirs[:] = []
                continue
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _sync_to_dist_folder() -> str:
    """把构建好的 dist/app.zip 复制到分发目录 抽卡工具/，返回目标路径。"""
    os.makedirs(DIST_TARGET_DIR, exist_ok=True)
    target = os.path.join(DIST_TARGET_DIR, "app.zip")
    shutil.copyfile(OUT_ZIP, target)
    return target


def build() -> int:
    os.makedirs(DIST_DIR, exist_ok=True)
    if os.path.isfile(OUT_ZIP):
        os.remove(OUT_ZIP)

    with tempfile.TemporaryDirectory(prefix="appzip_stage_") as staging:
        compiled = 0
        for src in _iter_py_files():
            # 相对项目根的路径保留 gacha_exporter/ 前缀 -> zip 顶层目录
            rel = os.path.relpath(src, PROJECT_ROOT)
            # legacy .pyc：gui/main_window.py -> gacha_exporter/gui/main_window.pyc
            pyc_dst = os.path.join(staging, os.path.splitext(rel)[0] + ".pyc")
            os.makedirs(os.path.dirname(pyc_dst), exist_ok=True)
            py_compile.compile(src, cfile=pyc_dst, doraise=True)
            compiled += 1

        with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(staging):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    arc = os.path.relpath(full, staging).replace(os.sep, "/")
                    zf.write(full, arc)
        size_mb = os.path.getsize(OUT_ZIP) / 1024 / 1024
        print(f"app.zip 已生成: {OUT_ZIP}（{compiled} 个模块，{size_mb:.2f} MB）")

    # 同步到分发目录（用户从 抽卡工具/ 取件）
    target = _sync_to_dist_folder()
    print(f"已同步到分发目录: {target}（{os.path.getsize(target)/1024/1024:.2f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
