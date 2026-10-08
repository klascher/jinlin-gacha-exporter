"""ed25519 独立信任根验签测试（见 签名密钥生成脚本）。

verify_ed25519 为纯函数：公钥/签名/数据任一不符即 False；公钥或签名 base64
非法（含空串/垃圾/字节数不对）返回 False（fail-safe）。verify_update_signature
用仓库内置公钥锚点验签——锚点本身来自 签名密钥生成脚本 生成的双位置公钥。
"""
import base64
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gacha_exporter.models import parse_version_info
from gacha_exporter.utils.signing import (
    PUBLIC_KEY_B64,
    verify_ed25519,
    verify_update_signature,
)

DATA = b"app.zip payload bytes for signing test"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 私钥默认在仓库父目录 pem/（相对仓库根推算，跨机器基路径不同也能找到）
PRIV_KEY_PATH = os.path.normpath(os.path.join(PROJECT_ROOT, "..", "pem", "signing.key"))


def _new_key():
    """生成临时密钥对，返回 (Ed25519PrivateKey, 公钥base64)。"""
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    return key, pub


def _sign(key, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode("ascii")


def test_valid_signature_verifies():
    key, pub = _new_key()
    assert verify_ed25519(pub, _sign(key, DATA), DATA) is True


def test_tampered_data_rejected():
    key, pub = _new_key()
    assert verify_ed25519(pub, _sign(key, DATA), DATA + b"x") is False


def test_tampered_signature_rejected():
    key, pub = _new_key()
    sig = _sign(key, DATA)
    bad_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert verify_ed25519(pub, bad_sig, DATA) is False


def test_different_key_rejected():
    key_a, pub_a = _new_key()
    _, pub_b = _new_key()
    assert verify_ed25519(pub_a, _sign(key_a, DATA), DATA) is True
    assert verify_ed25519(pub_b, _sign(key_a, DATA), DATA) is False


def test_empty_signature_rejected():
    key, pub = _new_key()
    assert verify_ed25519(pub, "", DATA) is False


def test_garbage_base64_rejected():
    assert verify_ed25519("!!!not-base64!!!", "!!!", DATA) is False


def test_wrong_length_signature_rejected():
    key, pub = _new_key()
    short_sig = base64.b64encode(b"short").decode("ascii")  # 非 64 字节
    assert verify_ed25519(pub, short_sig, DATA) is False


def test_anchor_pubkey_parses_to_32_bytes():
    """仓库内置公钥锚点可解析为 ed25519 原始 32 字节（签名密钥生成脚本 已生成）。"""
    assert PUBLIC_KEY_B64, "内置公钥锚点缺失：请运行 py -3.11 签名密钥生成脚本"
    assert len(base64.b64decode(PUBLIC_KEY_B64)) == 32


@pytest.mark.skipif(not os.path.isfile(PRIV_KEY_PATH),
                    reason="签名私钥仅开发机有（gitignore，不入库）")
def test_anchor_roundtrip_with_real_private_key():
    """私钥签名 + 内置公钥验签配对：防公钥双位置与真实私钥漂移。

    pack_release 用同一私钥签名，此用例相当于"发版签名 -> 客户端验签"的
    离线端到端：签名有效放行、篡改数据拒绝。
    """
    with open(PRIV_KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    sig = _sign(key, DATA)
    assert verify_update_signature(sig, DATA) is True
    assert verify_update_signature(sig, DATA + b"x") is False


def test验证脚本_update_signature_rejects_empty_anchor_signature():
    """签名缺失（空串）→ fail-safe 拒绝。"""
    key, pub = _new_key()
    # 用非空签名但空锚点语义：直接验证空签名路径
    assert verify_update_signature("", DATA) is False


def test_parse_version_info_roundtrips_update_signature():
    """version.json 含 update_signature 字段时能被完整解析回 VersionInfo。"""
    sig = "Dw1kDvvYHQtm5l1GDNMFA2u1dAKKgNcH32hq2xjKp0Y="  # 任意 base64，只测透传
    info = parse_version_info({
        "latest_version": "0.2.3",
        "update_type": "prompt",
        "update_sha256": "abc",
        "update_signature": sig,
        "ts": 1787231354,
    })
    assert info.update_signature == sig
    # 缺省/非 str -> ""（兼容旧服务端未签发）
    info2 = parse_version_info({"latest_version": "0.2.3"})
    assert info2.update_signature == ""
    info3 = parse_version_info({"latest_version": "0.2.3", "update_signature": 123})
    assert info3.update_signature == ""
