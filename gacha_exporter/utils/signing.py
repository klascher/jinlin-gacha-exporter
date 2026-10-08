"""更新补丁验签 — ed25519 独立信任根（私钥签名 + 公钥验签）。

信任模型：发版时 pack_release.py 在开发机用签名私钥对 app.zip 字节签名；
公钥打进 exe（bootstrap 预置 sys.modules["gacha_exporter._pubkey"]）与
app.zip 源码副本 gacha_exporter/_pubkey.py。下载的补丁先验签再落盘——
即使 HMAC 密钥泄露（软门槛，拆 app.zip 即可提取）、URL 被指向恶意服务器，
对方拿不到私钥，无法产出合法签名，客户端一律拒绝。相比纯 sha256（期望
哈希由同一台服务端下发，信任根与下载源同源），这是独立信任根。

公钥锚点解析顺序（见 签名密钥生成脚本）：
- 打包运行时：bootstrap 已把 exe 内嵌公钥注入 sys.modules，本模块
  `from .._pubkey import ...` 命中注入对象（app.zip 内同名模块被覆盖）；
- 开发运行时：读取 app.zip 源码 gacha_exporter/_pubkey.py（同值）。
"""
import base64

from .._pubkey import PUBLIC_KEY_B64


def verify_ed25519(pubkey_b64: str, sig_b64: str, data: bytes) -> bool:
    """用 ed25519 公钥验签（纯函数）。

    Args:
        pubkey_b64: base64 公钥（ed25519 原始 32 字节）
        sig_b64: base64 签名（ed25519 固定 64 字节）
        data: 被签名的原始字节（补丁 = app.zip 完整内容）

    Returns:
        签名有效为 True；公钥/签名 base64 非法、验签失败一律 False
        （fail-safe：任何异常都不放行）。
    """
    # cryptography 懒加载（引入开销小但避免启动期硬依赖；打包态恒内嵌 exe，
    # 仅开发环境缺依赖时命中 -> 返回 False 拒绝补丁）
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False

    try:
        pubkey = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        signature = base64.b64decode(sig_b64)
    except (ValueError, TypeError):
        return False
    try:
        pubkey.verify(signature, data)
        return True
    except Exception:  # 非法签名/长度不符等：一律视为无效
        return False


def verify_update_signature(sig_b64: str, data: bytes) -> bool:
    """用内置公钥验签补丁。公钥锚点缺失（空）时返回 False（fail-safe 拒绝）。"""
    if not PUBLIC_KEY_B64:
        return False
    return verify_ed25519(PUBLIC_KEY_B64, sig_b64, data)
