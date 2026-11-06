"""功能组件包（runtime 模块）下载线程 — 下载 + 校验 + 安装 + 切指针。

下载到 ``<app_dir>/runtime/<模块>/.download-<版本>.zip``，完成后交给
``utils.runtime_pack.install_zip`` 做 sha256/ed25519 校验与安全解压。
安装成功只切换该模块的 current.txt，**重启工具后由 bootstrap 挂载生效**。
取消/失败会删除半包，现有组件不受影响。
"""
import hashlib
import os

import requests
from PySide6.QtCore import QThread, Signal

from ..loaders.remote_data import DEFAULT_KEY, DEFAULT_SECRET
from ..utils.auth import build_auth_headers
from ..utils.logger import get_logger
from ..utils.paths import get_app_dir
from ..utils.runtime_pack import install_zip, module_label, runtime_base

logger = get_logger(__name__)


class RuntimeDownloader(QThread):
    """后台下载并安装视觉组件包。

    Signals:
        progress(int): 0-100（服务端未给 content-length 时保持 0）
        finished(bool, str): 成功/失败（消息可直接展示给用户）
    """

    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, module: str, url: str, version: str, expected_sha256: str,
                 expected_signature: str, app_dir: str = "", parent=None):
        super().__init__(parent)
        self.module = module
        self.label = module_label(module)
        self.url = url
        self.version = version
        self.expected_sha256 = expected_sha256
        self.expected_signature = expected_signature
        self.app_dir = app_dir or get_app_dir()
        self._tmp_path = os.path.join(
            runtime_base(self.app_dir), module, f".download-{version}.zip"
        )
        self._cancelled = False

    def cancel(self) -> None:
        """请求取消（下载循环下一块时退出）。"""
        self._cancelled = True

    def run(self) -> None:
        try:
            if not self.url:
                self.finished.emit(False, f"服务器未提供{self.label}下载地址。")
                return
            os.makedirs(os.path.dirname(self._tmp_path), exist_ok=True)
            headers = build_auth_headers(
                os.environ.get("JINLIN_KEY", DEFAULT_KEY),
                os.environ.get("JINLIN_SECRET", DEFAULT_SECRET),
            )
            with requests.get(self.url, headers=headers, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                hasher = hashlib.sha256()
                downloaded = 0
                with open(self._tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=262144):
                        if self._cancelled:
                            self._cleanup()
                            self.finished.emit(False, f"已取消下载{self.label}。")
                            return
                        if not chunk:
                            continue
                        f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if total:
                            self.progress.emit(min(100, int(downloaded * 100 / total)))
            if self._cancelled:
                self._cleanup()
                self.finished.emit(False, f"已取消下载{self.label}。")
                return
            self.progress.emit(100)
            ok, msg = install_zip(
                self.module,
                self._tmp_path,
                self.version,
                self.expected_sha256,
                self.expected_signature,
                self.app_dir,
            )
            self._cleanup()
            if ok:
                logger.info(f"{self.label}已安装: {self.version}")
            else:
                logger.warning(f"{self.label}安装失败: {msg}")
            self.finished.emit(ok, msg)
        except Exception as e:  # noqa: BLE001 —— 网络/磁盘异常统一回给界面
            self._cleanup()
            logger.exception(f"下载{self.label}失败: {e}")
            self.finished.emit(False, f"下载失败: {e}")

    def _cleanup(self) -> None:
        try:
            if os.path.isfile(self._tmp_path):
                os.remove(self._tmp_path)
        except OSError:
            pass
