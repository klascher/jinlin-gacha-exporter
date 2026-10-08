"""鉴权签名 — HMAC-SHA256，用于远程拉取角色/卡池信息。

当前状态：sign/build_auth_headers 供客户端发请求，verify_signature 供服务端
校验（算法同源）。服务端实现见 server/auth.py，由 tests 保证两者不漂移。
"""
import hashlib
import hmac
import time


def sign(key: str, secret: str, timestamp: int) -> str:
    """生成 HMAC-SHA256 签名。

    Args:
        key: 标识符（公开）
        secret: 密钥（保密）
        timestamp: Unix 秒级时间戳

    Returns:
        十六进制签名字符串
    """
    message = f"{key}{timestamp}"
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_auth_headers(key: str, secret: str) -> dict:
    """构建鉴权请求头。

    Args:
        key: 标识符
        secret: 密钥

    Returns:
        {"X-Auth-Key":..., "X-Auth-Timestamp":..., "X-Auth-Signature":...}
    """
    timestamp = int(time.time())
    signature = sign(key, secret, timestamp)
    return {
        "X-Auth-Key": key,
        "X-Auth-Timestamp": str(timestamp),
        "X-Auth-Signature": signature,
    }


def verify_signature(
    key: str,
    secret: str,
    timestamp: int,
    signature: str,
    max_skew_seconds: int = 300,
) -> bool:
    """校验签名与时间戳新鲜度（防重放）。

    与 sign() 同源：用相同 key/secret/timestamp 重算 HMAC，与给定签名比对。
    时间戳与当前时间偏差超过 max_skew_seconds 直接拒绝。

    Args:
        key: 标识符
        secret: 密钥
        timestamp: 请求时间戳（Unix 秒）
        signature: 待校验签名（十六进制）
        max_skew_seconds: 允许的时间偏差，默认 300 秒

    Returns:
        时间戳在范围内且签名匹配为 True
    """
    now = int(time.time())
    if abs(now - timestamp) > max_skew_seconds:
        return False
    expected = sign(key, secret, timestamp)
    return hmac.compare_digest(expected, signature)
