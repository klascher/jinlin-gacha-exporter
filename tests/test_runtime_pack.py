# -*- coding: utf-8 -*-
"""外部功能组件（runtime 模块）安装通道测试。

覆盖：sha256/签名 fail-safe、清单模块/版本、zip 路径穿越、原子切换指针、
多模块独立安装、legacy 单包布局识别、旧版本清理。签名用 monkeypatch 替身
（真实验签见 test_signing.py），所有写操作只落在 tmp_path。
"""
import hashlib
import json
import os
import pathlib
import zipfile

import pytest

from gacha_exporter.utils import runtime_pack as rp


def _make_zip(
    tmp_path,
    version="1.1.0",
    manifest_version=None,
    entries=None,
    module="ocr",
    manifest_module="auto",
):
    """生成 runtime zip，返回 (路径, sha256)。默认含 runtime.json + site-packages/。"""
    path = tmp_path / f"runtime-{module}-{version}.zip"
    entries = entries or {}
    manifest = {"runtime_version": manifest_version or version}
    if manifest_module == "auto":
        if module:
            manifest["module"] = module
    elif manifest_module is not None:
        manifest["module"] = manifest_module
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("runtime.json", json.dumps(manifest))
        z.writestr("site-packages/demo/__init__.py", "X = 1\n")
        for name, data in entries.items():
            z.writestr(name, data)
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把签名验签替换成固定口令；返回 app_dir。"""
    monkeypatch.setattr(rp, "verify_update_signature", lambda sig, data: sig == "good")
    monkeypatch.setattr(rp, "ocr_available", lambda refresh=False: False)
    app = tmp_path / "app"
    app.mkdir()
    return str(app)


def _install(env, zip_path, sha, version="1.1.0", sig="good", module="ocr"):
    return rp.install_zip(module, zip_path, version, sha, sig, env)


def test_install_success_and_pointer(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp))
    ok, msg = _install(env, z, sha)
    assert ok, msg
    assert rp.current_version("ocr", env) == "1.1.0"
    assert os.path.isfile(os.path.join(
        env, "runtime", "ocr", "1.1.0", "site-packages", "demo", "__init__.py"
    ))
    assert rp.read_pointer("ocr", env) == "1.1.0"
    assert not [n for n in os.listdir(os.path.join(env, "runtime", "ocr"))
                if n.startswith(".install-")]


def test_missing_sha_or_signature_rejected(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp))
    assert _install(env, z, "", "good")[0] is False
    assert _install(env, z, sha, "")[0] is False
    assert rp.current_version("ocr", env) == ""


def test_hash_mismatch_rejected(env):
    tmp = os.path.dirname(env)
    z, _sha = _make_zip(pathlib.Path(tmp))
    ok, msg = _install(env, z, "0" * 64)
    assert ok is False and "哈希" in msg
    assert rp.current_version("ocr", env) == ""


def test_bad_signature_rejected(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp))
    ok, msg = _install(env, z, sha, sig="bad")
    assert ok is False and "签名" in msg
    assert rp.current_version("ocr", env) == ""


def test_manifest_version_mismatch_rejected(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp), manifest_version="9.9.9")
    ok, msg = _install(env, z, sha)
    assert ok is False and "清单" in msg
    assert rp.current_version("ocr", env) == ""


def test_manifest_module_mismatch_rejected(env):
    """模块 A 的包不能装进模块 B 的目录（防止错包覆盖）。"""
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp), module="training")
    ok, msg = _install(env, z, sha, module="ocr")
    assert ok is False and "模块不符" in msg
    assert rp.current_version("ocr", env) == ""


def test_non_ocr_requires_manifest_module(env):
    """非 ocr 模块必须带 module 标识（旧单包兼容只给 ocr）。"""
    tmp = os.path.dirname(env)
    z, sha = _make_zip(
        pathlib.Path(tmp), module="training", manifest_module=None
    )
    ok, msg = _install(env, z, sha, module="training")
    assert ok is False and "模块标识" in msg
    assert rp.current_version("training", env) == ""


def test_install_two_modules_independent(env):
    """各模块独立版本/指针：更新一个不动另一个。"""
    tmp = pathlib.Path(os.path.dirname(env))
    z1, sha1 = _make_zip(tmp, module="ocr", version="1.1.0")
    z2, sha2 = _make_zip(tmp, module="training", version="1.0.0")
    assert _install(env, z1, sha1)[0]
    assert _install(env, z2, sha2, version="1.0.0", module="training")[0]

    assert rp.current_version("ocr", env) == "1.1.0"
    assert rp.current_version("training", env) == "1.0.0"
    assert rp.installed_modules(env) == ["ocr", "training"]

    z3, sha3 = _make_zip(tmp, module="training", version="1.0.1")
    assert _install(env, z3, sha3, version="1.0.1", module="training")[0]
    assert rp.current_version("ocr", env) == "1.1.0"
    assert rp.current_version("training", env) == "1.0.1"


def test_zip_slip_rejected(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(
        pathlib.Path(tmp), entries={"../evil.txt": "oops"}
    )
    ok, msg = _install(env, z, sha)
    assert ok is False and "路径" in msg
    assert not os.path.exists(os.path.join(env, "evil.txt"))
    assert rp.current_version("ocr", env) == ""


def test_same_version_idempotent(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp))
    assert _install(env, z, sha)[0]
    ok, msg = _install(env, z, sha)
    assert ok and "已安装" in msg


def test_legacy_layout_and_update_available(env):
    base = os.path.join(env, "runtime", "site-packages")
    os.makedirs(base)
    assert rp.current_version("ocr", env) == "legacy"
    assert rp.update_available("ocr", "1.0.0", env) is True

    assert rp.update_available("ocr", "", env) is False
    assert rp.update_available("ocr", "not a version!", env) is False
    # 旧布局只视作 ocr，其他模块仍是未安装
    assert rp.current_version("training", env) == ""


def test_update_available_compares_versions(env):
    tmp = os.path.dirname(env)
    z, sha = _make_zip(pathlib.Path(tmp), version="1.1.0")
    assert _install(env, z, sha)[0]
    assert rp.update_available("ocr", "1.2.0", env) is True
    assert rp.update_available("ocr", "1.1.0", env) is False
    assert rp.update_available("ocr", "1.0.0", env) is False
    # 未安装的模块任何合法远端版本都值得装
    assert rp.update_available("training", "1.0.0", env) is True


def test_cleanup_keeps_current_and_removes_old(env):
    tmp = pathlib.Path(os.path.dirname(env))
    z1, sha1 = _make_zip(tmp, version="1.0.0")
    assert _install(env, z1, sha1, version="1.0.0")[0]
    z2, sha2 = _make_zip(tmp, version="1.1.0")
    assert _install(env, z2, sha2, version="1.1.0")[0]

    root = os.path.join(env, "runtime", "ocr")
    stale = os.path.join(root, ".install-stale")
    os.makedirs(stale)
    keep = os.path.join(root, "notes")  # 非版本目录：不动
    os.makedirs(keep)

    removed = rp.cleanup_old_versions(app_dir=env)
    assert removed == 2
    assert rp.installed_versions("ocr", env) == ["1.1.0"]
    assert os.path.isdir(keep)


def test_cleanup_noop_without_pointer(env):
    assert rp.cleanup_old_versions(app_dir=env) == 0


def test_invalid_pointer_ignored(env):
    os.makedirs(os.path.join(env, "runtime", "ocr", "1.0.0", "site-packages"))
    with open(os.path.join(env, "runtime", "ocr", "current.txt"), "w",
              encoding="utf-8") as f:
        f.write("../escape\n")
    assert rp.read_pointer("ocr", env) == ""
    assert rp.current_version("ocr", env) == ""
