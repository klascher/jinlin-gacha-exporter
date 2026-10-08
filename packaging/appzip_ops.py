"""app.zip 磁盘状态机 - 纯文件操作，供 bootstrap（瘦身 exe）在启动最前调用。

不 import PySide6 / gacha_exporter（会被 pytest 直接测、会被 PyInstaller
打进 exe）。所有函数只操作 <app_dir>（exe 所在目录）下的五个文件：

| 文件                     | 含义                                            |
|--------------------------|-------------------------------------------------|
| app.zip                  | 当前生效包                                      |
| app.zip.pending          | 已下载+校验、待下次启动替换的新包               |
| app.zip.pending.sha256   | pending 期望哈希 sidecar                        |
| app.zip.bak              | 交换前的旧包（保留到下次成功启动）              |
| app.zip.bad              | 标记：上轮导入失败，.bak 才是好的               |

关键约束（Windows zipimport 占用文件句柄）：本模块的任何「交换/删除」动作
都必须在进程最开头、任何 zip 导入之前完成。
"""
import hashlib
import os

APP_ZIP = "app.zip"
PENDING = "app.zip.pending"
PENDING_SHA = "app.zip.pending.sha256"
BACKUP = "app.zip.bak"
BAD_MARK = "app.zip.bad"

_CHUNK = 65536


# ---------- 路径 ----------

def app_zip_path(app_dir: str) -> str:
    return os.path.join(app_dir, APP_ZIP)


def pending_path(app_dir: str) -> str:
    return os.path.join(app_dir, PENDING)


def pending_sha_path(app_dir: str) -> str:
    return os.path.join(app_dir, PENDING_SHA)


def backup_path(app_dir: str) -> str:
    return os.path.join(app_dir, BACKUP)


def bad_mark_path(app_dir: str) -> str:
    return os.path.join(app_dir, BAD_MARK)


# ---------- 校验 ----------

def sha256_file(path: str) -> str:
    """计算文件 sha256（十六进制小写）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_pending(app_dir: str) -> bool:
    """pending 与 sidecar 均存在且哈希一致才算有效。"""
    p = pending_path(app_dir)
    s = pending_sha_path(app_dir)
    if not (os.path.isfile(p) and os.path.isfile(s)):
        return False
    try:
        with open(s, encoding="utf-8") as f:
            expected = f.read().strip().lower()
    except OSError:
        return False
    if not expected:
        return False
    try:
        actual = sha256_file(p)
    except OSError:
        return False
    return actual == expected


# ---------- 状态机 ----------

def swap_pending(app_dir: str) -> bool:
    """处理待安装补丁（须在任何 zip 导入之前调用）。

    - 校验通过：当前包 -> .bak，pending -> app.zip（删 sidecar），返回 True
    - 校验失败：删 pending + sidecar（不动当前包），返回 False
    - 无 pending：直接返回 False
    """
    if not os.path.isfile(pending_path(app_dir)):
        return False
    if not verify_pending(app_dir):
        cleanup_pending(app_dir)
        return False

    cur = app_zip_path(app_dir)
    bak = backup_path(app_dir)
    # 先清掉可能残留的旧 .bak，避免 replace 目标已存在
    if os.path.isfile(bak):
        try:
            os.remove(bak)
        except OSError:
            pass
    if os.path.isfile(cur):
        try:
            os.replace(cur, bak)
        except OSError:
            pass
    try:
        os.replace(pending_path(app_dir), cur)
    except OSError:
        # 替换失败：尝试把 .bak 还回去，避免当前包缺失
        if os.path.isfile(bak) and not os.path.isfile(cur):
            try:
                os.replace(bak, cur)
            except OSError:
                pass
        return False
    try:
        os.remove(pending_sha_path(app_dir))
    except OSError:
        pass
    return True


def repair_after_failed_import(app_dir: str) -> None:
    """启动最前调用，处理上一轮的失败/过期备份。必须先于 swap_pending。

    - 有 .bad 且 .bak 存在：删坏 app.zip，.bak 升回 app.zip，删 .bad
    - 仅 .bak 存在（上轮成功，无失败标记）：.bak 为过期备份，直接清理
    """
    cur = app_zip_path(app_dir)
    bak = backup_path(app_dir)
    bad = bad_mark_path(app_dir)
    if os.path.isfile(bad):
        if os.path.isfile(bak):
            if os.path.isfile(cur):
                try:
                    os.remove(cur)
                except OSError:
                    pass
            try:
                os.replace(bak, cur)
            except OSError:
                pass
        try:
            os.remove(bad)
        except OSError:
            pass
    elif os.path.isfile(bak):
        try:
            os.remove(bak)
        except OSError:
            pass


def mark_rollback(app_dir: str) -> None:
    """导入失败回退到 .bak 后，留下 .bad 标记（下次启动清理坏包）。"""
    with open(bad_mark_path(app_dir), "w", encoding="utf-8") as f:
        f.write("rollback")


def cleanup_pending(app_dir: str) -> None:
    """删除未通过校验的 pending 与 sidecar。"""
    for p in (pending_path(app_dir), pending_sha_path(app_dir)):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
