"""外部功能组件包（runtime 模块）的本地安装/校验/切换（纯逻辑，不依赖 Qt）。

发布物分三层（见 打包更新说明 §8.8）：

    <exe目录>/runtime/<模块>/current.txt        # 单行：当前版本目录名
    <exe目录>/runtime/<模块>/<版本>/site-packages/...

模块按「重依赖边界」划分，各模块独立版本、独立签名、独立下载：
- ``ocr``：numpy/cv2/onnxruntime/rapidocr 等视觉识别栈（卡池识别、练度、排轴共用）
- ``training``：练度私有资产（模型/模板），依赖 ocr
- ``team_builder``：排轴私有依赖（supervision/scipy/av…），依赖 ocr

兼容旧布局 ``runtime/current.txt`` + ``runtime/<版本>/``（视作 ``ocr``）。

bootstrap 只在进程启动时挂载 runtime，因此**安装后必须重启**才生效。本模块
负责：校验下载包（sha256 + ed25519 签名，fail-safe）→ 安全解压到版本目录 →
原子切换 current.txt。所有写操作只发生在 ``<app_dir>/runtime/`` 内。

安全要点：
- 缺哈希/缺签名/验签失败一律拒绝，不留下半包；
- zip 成员做路径穿越校验（拒绝绝对路径、`..`、盘符外逃）；
- 解压到临时目录成功后才 rename，失败不会破坏现有版本；
- 清理旧版本时只删「合法版本号目录」，永不删当前版本与 legacy 布局。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple

from .paths import get_app_dir
from .signing import verify_update_signature

RUNTIME_DIR_NAME = "runtime"
POINTER_NAME = "current.txt"
DEFAULT_MODULE = "ocr"
MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_LEGACY = "legacy"
_OCR_MODULES = ("numpy", "cv2", "onnxruntime", "rapidocr_onnxruntime")

# 客户端展示名（服务端 version.json 可覆盖）
MODULE_LABELS: Dict[str, str] = {
    "ocr": "视觉识别组件",
    "training": "练度识别组件",
    "team_builder": "排轴组件",
}

_ocr_available: Optional[bool] = None


def _valid_version(version: str) -> bool:
    return (
        isinstance(version, str)
        and bool(VERSION_RE.match(version))
        and version not in (".", "..", _LEGACY)
    )


def _valid_module(module: str) -> bool:
    return isinstance(module, str) and bool(MODULE_RE.match(module))


def _version_like(name: str) -> bool:
    """目录名是否像版本号（以数字开头）。清理时据此避免误删 notes/cache 等。"""
    return bool(re.match(r"^\d[A-Za-z0-9._-]*$", name))


def module_label(module: str) -> str:
    """模块的默认中文名；未知模块回退模块名。"""
    return MODULE_LABELS.get(module, module)


def runtime_base(app_dir: str = "") -> str:
    """runtime 根目录（不创建）。"""
    return os.path.join(os.path.abspath(app_dir or get_app_dir()), RUNTIME_DIR_NAME)


def _legacy_root(base: str) -> bool:
    """是否为旧单包布局（runtime/current.txt 或 runtime/site-packages）。"""
    return os.path.isfile(os.path.join(base, POINTER_NAME)) or os.path.isdir(
        os.path.join(base, "site-packages")
    )


def module_base(module: str, app_dir: str = "") -> str:
    """模块根目录（不创建）。

    新布局 ``runtime/<模块>/``；``ocr`` 在尚未迁移的旧单包布局下返回
    ``runtime/`` 本身，读取旧指针/旧版本目录。安装永远写新布局
    ``runtime/<模块>/``，装完即优先于旧布局生效。
    """
    base = runtime_base(app_dir)
    if not _valid_module(module):
        return base
    if module != DEFAULT_MODULE:
        return os.path.join(base, module)
    new_root = os.path.join(base, module)
    # 新布局已有可用内容（指针/解压目录）才覆盖旧布局；只有空的运行时
    # 残留目录（例如上次安装中途失败）不能把旧布局“遮住”。
    if os.path.isfile(os.path.join(new_root, POINTER_NAME)) or os.path.isdir(
        os.path.join(new_root, "site-packages")
    ):
        return new_root
    if _legacy_root(base):
        return base
    return new_root


def read_pointer(module: str = DEFAULT_MODULE, app_dir: str = "") -> str:
    """读模块 current.txt；缺失/非法返回 ""。"""
    path = os.path.join(module_base(module, app_dir), POINTER_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            version = f.readline().strip()
    except OSError:
        return ""
    return version if _valid_version(version) else ""


def current_version(module: str = DEFAULT_MODULE, app_dir: str = "") -> str:
    """模块当前生效版本；未安装返回 ""，旧布局返回 "legacy"。"""
    root = module_base(module, app_dir)
    version = read_pointer(module, app_dir)
    if version and os.path.isdir(os.path.join(root, version, "site-packages")):
        return version
    if module == DEFAULT_MODULE and os.path.isdir(
        os.path.join(root, "site-packages")
    ):
        return _LEGACY
    return ""


def installed_versions(module: str = DEFAULT_MODULE, app_dir: str = "") -> List[str]:
    """列出模块下已解压的合法版本目录（不含 legacy 布局与临时目录）。"""
    root = module_base(module, app_dir)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for name in names:
        if not (_valid_version(name) and _version_like(name)):
            continue
        if os.path.isdir(os.path.join(root, name, "site-packages")):
            out.append(name)
    return sorted(out)


def installed_modules(app_dir: str = "") -> List[str]:
    """本地已安装（有当前版本）的模块名。"""
    base = runtime_base(app_dir)
    names = set()
    try:
        for name in os.listdir(base):
            if _valid_module(name) and os.path.isdir(os.path.join(base, name)):
                names.add(name)
    except OSError:
        pass
    if _legacy_root(base):
        names.add(DEFAULT_MODULE)
    return sorted(n for n in names if current_version(n, app_dir))


def update_available(
    module: str = DEFAULT_MODULE, remote_version: str = "", app_dir: str = ""
) -> bool:
    """服务端模块版本是否值得安装（未安装或比当前新）。"""
    remote_version = (remote_version or "").strip()
    if not _valid_module(module) or not _valid_version(remote_version):
        return False
    current = current_version(module, app_dir)
    if current in ("", _LEGACY):
        return True
    from .version import is_newer

    return is_newer(remote_version, current)


def mounted_info() -> Optional[dict]:
    """bootstrap 挂载 runtime 后写入的元信息；开发/未挂载为 None。"""
    info = getattr(sys, "_example-key_runtime", None)
    return info if isinstance(info, dict) else None


def mounted_modules() -> Dict[str, dict]:
    """bootstrap 已挂载的模块表（模块名 -> 元信息）；未挂载返回 {}。"""
    info = mounted_info() or {}
    modules = info.get("modules")
    if isinstance(modules, dict):
        return modules
    # 兼容旧版 bootstrap 的单 runtime 元信息
    if info.get("site_packages"):
        return {DEFAULT_MODULE: dict(info)}
    return {}


def ocr_available(refresh: bool = False) -> bool:
    """OCR 栈是否可导入（当前进程）。

    用 find_spec 探测，不真正加载模型；结果缓存，安装新 runtime 后本进程
    仍不可用（需重启），调用方应提示重启。测试可传 refresh=True。
    """
    global _ocr_available
    if _ocr_available is None or refresh:
        try:
            import importlib.util

            _ocr_available = all(
                importlib.util.find_spec(name) is not None for name in _OCR_MODULES
            )
        except Exception:  # 损坏的包/父包缺失等一律视为不可用
            _ocr_available = False
    return bool(_ocr_available)


def reset_ocr_cache() -> None:
    """清空可用性缓存（测试/安装后调用）。"""
    global _ocr_available
    _ocr_available = None


def module_available(module: str, refresh: bool = False) -> bool:
    """模块当前是否可使用。

    - ``ocr``：按 OCR 栈实际可导入判断（兼容开发态源码运行）；
    - 其他模块：以 bootstrap 是否已挂载为准（其依赖由 requires 保证）。
    """
    if module == DEFAULT_MODULE:
        return ocr_available(refresh=refresh)
    mounted = mounted_modules()
    info = mounted.get(module)
    if info is None:
        return False
    return all(dep in mounted for dep in info.get("requires") or ())


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """按成员逐个解压，拒绝绝对路径与 `..` 穿越。"""
    dest_abs = os.path.abspath(dest)
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if name.startswith("/") or not parts or any(p == ".." for p in parts):
            raise RuntimeError(f"runtime 包含非法路径: {info.filename!r}")
        target = os.path.abspath(os.path.join(dest_abs, *parts))
        if os.path.commonpath([dest_abs, target]) != dest_abs:
            raise RuntimeError(f"runtime 包路径越界: {info.filename!r}")
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    """读 runtime.json；缺失/非法返回 {}。"""
    try:
        raw = zf.read("runtime.json")
        data = json.loads(raw.decode("utf-8"))
    except (KeyError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def install_zip(
    module: str,
    zip_path: str,
    version: str,
    expected_sha256: str = "",
    expected_signature: str = "",
    app_dir: str = "",
) -> Tuple[bool, str]:
    """校验并安装模块 zip，成功后切换该模块的 current.txt。

    Args:
        module: 模块名（``ocr``/``training``/``team_builder``）
        zip_path: 已下载到本地的 zip
        version: 期望版本（同时校验 runtime.json）
        expected_sha256: 服务端下发哈希（必须提供且一致）
        expected_signature: ed25519 签名（base64，必须提供且有效）
        app_dir: 程序目录（默认 get_app_dir）

    Returns:
        (成功, 面向用户的说明)。失败时现有 runtime 不受影响。
    """
    module = (module or "").strip()
    label = module_label(module)
    if not _valid_module(module):
        return False, "组件模块名非法，已取消。"
    version = (version or "").strip()
    if not _valid_version(version):
        return False, f"{label}版本号非法，已取消。"
    if not os.path.isfile(zip_path):
        return False, f"{label}安装包不存在，已取消。"

    expected_sha256 = (expected_sha256 or "").strip().lower()
    expected_signature = (expected_signature or "").strip()
    if not expected_sha256 or not expected_signature:
        return False, f"{label}缺少校验信息，已拒绝安装（安全校验失败）。"

    if _sha256_file(zip_path) != expected_sha256:
        return False, f"{label}哈希校验失败，已取消。"

    with open(zip_path, "rb") as f:
        file_bytes = f.read()
    if not verify_update_signature(expected_signature, file_bytes):
        return False, f"{label}签名校验失败，已拒绝安装（安全校验失败）。"

    base = runtime_base(app_dir)
    root = os.path.join(base, module)  # 安装永远写新布局
    dest = os.path.join(root, version)
    pointer = os.path.join(root, POINTER_NAME)
    if read_pointer(module, app_dir) == version and os.path.isdir(
        os.path.join(dest, "site-packages")
    ):
        return True, f"{label} {version} 已安装。"

    try:
        with zipfile.ZipFile(zip_path) as zf:
            manifest = _read_manifest(zf)
            if manifest.get("module") not in (None, "", module):
                return False, f"{label}清单模块不符，已取消。"
            if manifest.get("module") is None and module != DEFAULT_MODULE:
                return False, f"{label}清单缺少模块标识，已取消。"
            if manifest.get("runtime_version") != version:
                return False, f"{label}清单版本不符，已取消。"
            names = {n.replace("\\", "/") for n in zf.namelist()}
            if "site-packages/" not in names and not any(
                n.startswith("site-packages/") for n in names
            ):
                return False, f"{label}内容不完整，已取消。"
            os.makedirs(root, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".install-", dir=root)
            try:
                _safe_extract(zf, tmp)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
    except zipfile.BadZipFile:
        return False, f"{label}压缩包损坏，已取消。"
    except (RuntimeError, ValueError) as e:
        return False, str(e)
    except OSError as e:
        return False, f"{label}解压失败：{e}"

    # 目标版本目录若存在（上次半途/同名旧包），先移除；绝不动当前生效版本
    if os.path.isdir(dest):
        if read_pointer(module, app_dir) == version:
            shutil.rmtree(tmp, ignore_errors=True)
            return True, f"{label} {version} 已安装。"
        shutil.rmtree(dest, ignore_errors=True)
    try:
        os.replace(tmp, dest)
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"{label}安装失败：{e}"

    try:
        pointer_tmp = pointer + ".tmp"
        with open(pointer_tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(version + "\n")
        os.replace(pointer_tmp, pointer)
    except OSError as e:
        return False, f"{label}切换失败：{e}"

    reset_ocr_cache()
    return True, f"{label} {version} 已安装，重启工具后生效。"


def _cleanup_root(root: str, keep: str) -> int:
    """清理单个模块根目录：临时目录 + 非当前版本目录。"""
    if not os.path.isdir(root):
        return 0
    current = keep
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            # 崩溃残留的半包（下载线程正常结束会自删，这里兜底）
            if os.path.isfile(path) and name.startswith(".download-"):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
            continue
        if name.startswith((".install-", ".download-")):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            continue
        if not current or name == current or name == _LEGACY:
            continue
        if not _version_like(name):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def cleanup_old_versions(modules: Optional[List[str]] = None, app_dir: str = "") -> int:
    """删除各模块非当前版本目录与安装残留；返回删除数量。

    只删合法版本号目录和 `.install-*`/`.download-*` 临时目录。某模块未安装
    （无 current.txt）时不动它的任何目录，避免误删用户文件。
    """
    base = runtime_base(app_dir)
    if not os.path.isdir(base):
        return 0
    if modules is None:
        modules = []
        try:
            for name in os.listdir(base):
                if _valid_module(name) and os.path.isdir(os.path.join(base, name)):
                    modules.append(name)
        except OSError:
            return 0
        if _legacy_root(base):
            modules.append(DEFAULT_MODULE)

    removed = 0
    legacy_done = False
    for module in modules:
        current = current_version(module, app_dir)
        if not current:
            continue
        root = module_base(module, app_dir)
        if module == DEFAULT_MODULE and root == base:
            # 旧单包布局：同时清掉根目录下的临时目录/旧版本
            if legacy_done:
                continue
            legacy_done = True
        removed += _cleanup_root(root, current)

    # 迁移到 runtime/ocr/ 新布局后，根目录下的旧单包（版本目录 + site-packages）
    # 已不再被 bootstrap 使用，回收磁盘；只删版本目录/旧 site-packages，
    # 模块目录名不以数字开头，不会误删。
    if (
        os.path.isdir(os.path.join(base, DEFAULT_MODULE))
        and read_pointer(DEFAULT_MODULE, app_dir)
        and _legacy_root(base)
    ):
        try:
            for name in os.listdir(base):
                path = os.path.join(base, name)
                if not os.path.isdir(path):
                    continue
                if name == "site-packages" or (
                    _version_like(name)
                    and os.path.isdir(os.path.join(path, "site-packages"))
                ):
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
        except OSError:
            pass
    return removed
