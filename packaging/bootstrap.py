"""瘦身 exe 入口 - 交换/修复 app.zip 状态机后，从 zip 动态加载业务包。

构建产物结构（同一目录）：
- 金鳞抽卡导出工具.exe（本文件 + PySide6 + 状态机，永不更新）
- app.zip（业务逻辑 .pyc 字节码，更新时整体替换）
- runtime/<模块>/<版本>/site-packages/（OCR 等功能组件，独立签名/独立更新）
- runtime/<模块>/current.txt（指向该模块当前版本目录）
  兼容旧单包布局 runtime/current.txt + runtime/<版本>/（视作 ocr 模块）
- data/（运行时生成：日志/缓存/账号历史，zip 升级天然保留）

Windows 关键约束：zipimport 会占用已导入 zip 的文件句柄（无
FILE_SHARE_DELETE），因此状态机的交换/修复必须先于任何 zip 导入。
"""
import importlib
import os
import re
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


# os.add_dll_directory 返回的句柄必须存活到进程结束，否则目录会被移出
# DLL 搜索路径；模块级列表就是它的持有者。
_DLL_DIR_HANDLES: list = []
_RUNTIME_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RUNTIME_MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# 启动器能力契约（app.zip 通过 sys._example-key_bootstrap 判断宿主）：
# - 1（缺省）= 0.4.0 旧壳：只加载 app.zip，不挂 runtime、不写 _example-key_runtime；
# - 2 = 支持 runtime/<模块>/ 挂载，始终写 sys._example-key_runtime（无模块时为空表，
#   用于区分“新壳但未装组件”和“旧壳不支持组件”）；1.0.0 首批测试壳；
# - 3 = 追加“自动重启等待”：识别 --restart-after=<PID>，等旧进程退出后再加载
#   （2026-10-26）。app.zip 用 host_env.supports_auto_restart() 判断，API<3 时
#   退回手动重启提示，避免旧壳不认识标记导致双开。
# 旧壳加载新 app.zip 时缺省为 1，业务层据此降级为核心模式。
BOOTSTRAP_API = 3
EXE_VERSION = "1.0.0"

# 组件安装后自动重启：新进程带 --restart-after=<旧 PID>，必须等旧进程真正
# 退出后再继续（否则双开抢网卡/数据文件）。标记须在 QApplication/业务
# import 之前移除，避免渗入 argv。与 gacha_exporter/utils/restart.py 同格式。
_RESTART_FLAG = "--restart-after="
_RESTART_WAIT_TIMEOUT = 60.0


def _parse_restart_pid(argv) -> int | None:
    for arg in argv:
        if not arg.startswith(_RESTART_FLAG):
            continue
        try:
            pid = int(arg[len(_RESTART_FLAG):])
        except ValueError:
            return None
        return pid if pid > 0 else None
    return None


def _wait_process_exit(pid: int, timeout: float) -> bool:
    """Windows 等旧进程退出（超时也放行，避免卡死启动）。"""
    if pid <= 0:
        return True
    if os.name != "nt":
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            time.sleep(0.1)
        return False
    import ctypes

    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return True
    try:
        # WAIT_OBJECT_0 / WAIT_ABANDONED 均表示已退出
        return kernel32.WaitForSingleObject(
            handle, int(max(0, timeout) * 1000)
        ) in (0x00000000, 0x00000080)
    finally:
        kernel32.CloseHandle(handle)


def _handle_restart_wait() -> None:
    """处理 --restart-after：等待旧进程退出并移除该参数。"""
    pid = _parse_restart_pid(sys.argv[1:])
    if pid is None:
        return
    _wait_process_exit(pid, _RESTART_WAIT_TIMEOUT)
    sys.argv = [sys.argv[0]] + [
        arg for arg in sys.argv[1:] if not arg.startswith(_RESTART_FLAG)
    ]


def _set_capabilities() -> None:
    """在加载 app.zip 之前声明启动器能力（旧壳没有这两个属性）。"""
    sys._example-key_bootstrap = BOOTSTRAP_API
    sys._example-key_exe_version = EXE_VERSION


def _resolve_module_root(root: str) -> tuple[str | None, str | None]:
    """解析模块根目录，返回 (版本, 版本目录)。

    布局：
      <root>/current.txt              # 内容为版本目录名（单行）
      <root>/<版本>/site-packages/    # 外部依赖
    兼容未分版本的 <root>/site-packages/（旧单包布局）。
    任何异常都返回 (None, None)：组件缺失只影响扩展功能，不影响核心启动。
    """
    try:
        pointer = os.path.join(root, "current.txt")
        if os.path.isfile(pointer):
            with open(pointer, "r", encoding="utf-8") as f:
                version = f.readline().strip()
            if not _RUNTIME_VERSION_RE.match(version) or version in (".", ".."):
                return None, None
            candidate = os.path.join(root, version)
            if os.path.isdir(os.path.join(candidate, "site-packages")):
                return version, candidate
            return None, None
        legacy = os.path.join(root, "site-packages")
        if os.path.isdir(legacy):
            return "legacy", root
        return None, None
    except OSError:
        return None, None


def _read_requires(root: str) -> tuple:
    """读模块 runtime.json 的 requires（缺失/非法返回空元组）。"""
    import json

    try:
        with open(os.path.join(root, "runtime.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ()
    requires = data.get("requires") if isinstance(data, dict) else None
    if not isinstance(requires, list):
        return ()
    return tuple(
        name for name in requires
        if isinstance(name, str) and _RUNTIME_MODULE_RE.match(name)
    )


def _runtime_modules(app_dir: str) -> list[dict]:
    """枚举已安装的 runtime 模块（含依赖声明）；旧单包布局视作 ocr。"""
    base = os.path.join(app_dir, "runtime")
    modules: list[dict] = []
    try:
        names = os.listdir(base)
    except OSError:
        return modules
    for name in names:
        if not _RUNTIME_MODULE_RE.match(name):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        version, root = _resolve_module_root(path)
        if not root:
            continue
        modules.append({
            "module": name,
            "version": version,
            "root": root,
            "requires": _read_requires(root),
        })
    if not any(m["module"] == "ocr" for m in modules):
        version, root = _resolve_module_root(base)
        if root:
            modules.append({
                "module": "ocr",
                "version": version,
                "root": root,
                "requires": (),
            })
    return modules


def _order_modules(modules: list[dict]) -> list[dict]:
    """按依赖拓扑排序（依赖先挂载，依赖方后挂载从而在 sys.path 更前面）。"""
    by_name = {m["module"]: m for m in modules}
    ordered: list[dict] = []
    done: set = set()
    visiting: set = set()

    def visit(name: str) -> None:
        if name in done or name in visiting:
            return
        mod = by_name.get(name)
        if mod is None:
            return
        visiting.add(name)
        for dep in mod["requires"]:
            visit(dep)
        visiting.discard(name)
        done.add(name)
        ordered.append(mod)

    for mod in modules:
        visit(mod["module"])
    return ordered


def _add_dll_dirs(root: str, site_packages: str) -> None:
    """把模块内已知 DLL 目录挂到 Windows 搜索路径（句柄模块级持有）。"""
    if not hasattr(os, "add_dll_directory"):
        return
    candidates = [root, site_packages]
    for rel in (
        "numpy.libs", "cv2", "cv2.libs", "onnxruntime/capi",
        "rapidocr_onnxruntime", "shapely", "shapely.libs", "pyclipper",
        "PIL", "skimage", "scipy.libs", "av", "matplotlib",
    ):
        candidates.append(os.path.join(site_packages, *rel.split("/")))
    try:
        # 未来依赖可能带新的 *.libs 目录（如 numpy 2.x 的 OpenBLAS），一并挂上
        for name in os.listdir(site_packages):
            if name.endswith(".libs"):
                candidates.append(os.path.join(site_packages, name))
    except OSError:
        pass
    for path in candidates:
        if not os.path.isdir(path):
            continue
        try:
            _DLL_DIR_HANDLES.append(os.add_dll_directory(path))
        except OSError:
            pass


def _setup_runtime(app_dir: str) -> dict:
    """把各功能组件模块挂到 sys.path / DLL 搜索路径，返回元信息。

    必须在任何业务 import 之前调用（app.zip 里的 OCR 模块是函数内懒加载，
    但一旦触发就要能找到这些包）。组件里的包优先于 exe 内置包：OCR 栈
    （numpy/cv2/onnxruntime/rapidocr）不随 exe 发布，只能来自这里。
    按依赖拓扑顺序挂载；缺失某个模块只影响依赖它的功能。

    **即使没有任何模块也写入 sys._example-key_runtime（空 modules）**：
    业务层用它区分“新启动器但组件未安装”与“旧启动器不支持组件”。
    """
    modules = _runtime_modules(app_dir)

    mounted: dict = {}
    for mod in _order_modules(modules) if modules else ():
        root = os.path.abspath(mod["root"])
        site_packages = os.path.join(root, "site-packages")
        # 放在最前：外部依赖优先于 exe 内置同名包（例如未来组件升级 numpy）。
        # app.zip 稍后会被插到更前面，业务代码仍优先于组件。
        if site_packages not in sys.path:
            sys.path.insert(0, site_packages)
        # 可选的完整标准库（当前 exe 已内置超集，仅作为未来兜底通道）
        stdlib = os.path.join(root, "stdlib")
        if os.path.isdir(stdlib) and stdlib not in sys.path:
            sys.path.append(stdlib)
        _add_dll_dirs(root, site_packages)
        mounted[mod["module"]] = {
            "version": mod["version"],
            "root": root,
            "site_packages": site_packages,
            "requires": list(mod["requires"]),
        }

    info: dict = {"modules": mounted}
    ocr = mounted.get("ocr")
    if ocr:  # 旧字段兼容（业务层曾按单 runtime 读取）
        info.update({
            "version": ocr["version"],
            "root": ocr["root"],
            "site_packages": ocr["site_packages"],
        })
    # 业务层（app.zip）通过 sys._example-key_runtime 判断扩展是否可用；
    # 不写入环境变量，避免泄露到子进程/日志。
    sys._example-key_runtime = info
    return info


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
    # ⓪-1 自动重启：等旧进程退出（其正停抓包/合并历史），避免双开
    _handle_restart_wait()
    # ⓪ 声明启动器能力（必须早于任何 app.zip import；旧壳没有这一步）
    _set_capabilities()

    # ① 修复上一轮失败/过期备份（此刻无 zip 被打开）
    appzip_ops.repair_after_failed_import(app_dir)
    # ② 处理待安装补丁（此刻无 zip 被打开）
    appzip_ops.swap_pending(app_dir)
    # ③ 挂载外部 runtime（OCR 重依赖）；缺失不阻断核心，业务层自行降级
    _setup_runtime(app_dir)

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
