"""更新补丁下载线程 - 下载 app.zip.pending + 校验签名与 sha256 + 写 sidecar。

下载到 <app_dir>/app.zip.pending（与当前 app.zip 同目录同卷，供下次启动
原子替换），边下边算 sha256，与版本接口下发的 update_sha256 比对；再用
exe 内嵌公钥对 app.zip 字节做 ed25519 验签（独立信任根）：
- sha256 一致 且 签名有效 -> 写 app.zip.pending.sha256 sidecar，finished(True)
- 任一不一致 / 缺签名（fail-safe）-> 删除 pending，finished(False)（不留下坏文件）

下载请求带 HMAC 鉴权头（与远程拉取 role/announcements 同套 key/secret，
环境变量 JINLIN_KEY/JINLIN_SECRET 可覆盖）。
"""
import hashlib
import os

import requests
from PySide6.QtCore import QThread, Signal

from ..loaders.remote_data import DEFAULT_KEY, DEFAULT_SECRET
from ..utils.auth import build_auth_headers
from ..utils.logger import get_logger
from ..utils.paths import get_app_dir
from ..utils.signing import verify_update_signature

logger = get_logger(__name__)

PENDING_NAME = "app.zip.pending"
PENDING_SHA_NAME = "app.zip.pending.sha256"


class UpdateDownloader(QThread):
    """后台下载更新补丁。

    Attributes:
        progress(int): 下载进度 0-100
        finished(bool, str): 成功(True)/失败(False, 原因)。成功消息含"重启后生效"提示
    """

    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, url: str, expected_sha256: str,
                 expected_signature: str = "", app_dir: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.expected_sha256 = (expected_sha256 or "").strip().lower()
        # 版本接口下发的 ed25519 签名（base64，对 app.zip 完整字节）；空 = 未签名
        # （fail-safe：新 exe 只接受已签名补丁，空签名直接判失败）
        self.expected_signature = (expected_signature or "").strip()
        self.app_dir = app_dir or get_app_dir()
        self._tmp_path = os.path.join(self.app_dir, PENDING_NAME)
        self._sha_path = os.path.join(self.app_dir, PENDING_SHA_NAME)

    def run(self) -> None:
        try:
            headers = build_auth_headers(
                os.environ.get("JINLIN_KEY", DEFAULT_KEY),
                os.environ.get("JINLIN_SECRET", DEFAULT_SECRET),
            )
            with requests.get(self.url, headers=headers, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                hasher = hashlib.sha256()
                downloaded = 0
                with open(self._tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if total:
                            self.progress.emit(min(100, int(downloaded * 100 / total)))
            digest = hasher.hexdigest()
            if self.expected_sha256 and digest != self.expected_sha256:
                self._cleanup()
                logger.warning(f"补丁哈希校验失败: 期望 {self.expected_sha256}，实际 {digest}")
                self.finished.emit(False, "下载文件校验失败（哈希不一致），已取消。")
                return
            # 独立信任根验签：缺签名（旧服务端未签发）或验签失败一律拒绝。
            # 先读完整字节再验（app.zip 约 300KB，一次性读入开销可忽略）。
            with open(self._tmp_path, "rb") as f:
                file_bytes = f.read()
            if not self.expected_signature:
                self._cleanup()
                logger.error("补丁缺少签名（update_signature 为空），拒绝应用（fail-safe）")
                self.finished.emit(False, "更新包未签名，已取消（安全校验失败）。")
                return
            if not verify_update_signature(self.expected_signature, file_bytes):
                self._cleanup()
                logger.warning("补丁签名校验失败（私钥签名/公钥验签），拒绝应用")
                self.finished.emit(False, "更新包签名校验失败，已取消（安全校验失败）。")
                return
            # 校验通过：写 sidecar，供下次启动 bootstrap 校验 pending
            with open(self._sha_path, "w", encoding="utf-8") as f:
                f.write(digest)
            self.progress.emit(100)
            logger.info(f"更新补丁已就绪: {self._tmp_path}")
            self.finished.emit(True, "更新补丁已下载完成，重启工具后生效。")
        except Exception as e:
            self._cleanup()
            logger.exception(f"下载更新补丁失败: {e}")
            self.finished.emit(False, f"下载失败: {e}")

    def _cleanup(self) -> None:
        """失败时删除残留的 pending 与 sidecar。"""
        for p in (self._tmp_path, self._sha_path):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
