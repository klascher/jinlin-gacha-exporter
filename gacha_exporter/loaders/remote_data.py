"""角色/卡池数据源 - 优先远程 HTTP GET 拉取，失败回退本地缓存。

回退链：远程 -> 本地缓存(role_cache.json) -> DataNotReadyError。
- 远程成功 -> 解析返回 + 写缓存，remote_ok=True
- 远程任何失败（含 503/网络/401/超时/格式错）-> 回退缓存，remote_ok=False
  （由调用方弹窗让用户确认是否用缓存数据继续；本函数不弹窗）
- 缓存无效/缺失 -> 抛 DataNotReadyError（无任何可用数据）

数据源在 server/data/role.json，客户端不内置 role 数据，运行时拉取 + 缓存。
"""
import json
import os
import urllib.parse
from typing import Dict, List, Tuple

import requests

from ..models import Announcement, Character, DataNotReadyError, PityGroup, Pool, VersionInfo, parse_announcement, parse_character, parse_pity_groups, parse_pool, parse_version_info
from ..utils.auth import build_auth_headers
from ..utils.logger import get_logger
from ..utils.paths import get_cache_dir

logger = get_logger(__name__)

DEFAULT_REMOTE_URL = "https://example.com/v1/role"
DEFAULT_KEY = "example-key"
DEFAULT_SECRET = "example-secret-not-real"

CACHE_FILENAME = "role_cache.json"

# ==================== 服务端健康探测 + 整体回退门（2026-08-24 第二批 D） ====================
# 启动先调一次 check_health（GET /health，无鉴权、秒回），结论缓存到
# _remote_health_cache；三个远程加载器（role/公告/版本）经 _remote_available()
# 读取——不可用时直接走各自回退（缓存 / [] / None），不再逐接口 10s 串行超时。
# 设置页「手动检查」会先 set_remote_available(None) 重置，重新探测。
_remote_health_cache: bool | None = None


def check_health(remote_url: str = "", timeout: float = 3.0) -> bool:
    """服务端健康探测：GET /health 是否 200（无鉴权、秒回）。

    /health 是服务端**根路径**（nginx `location = /health` 反代，与业务接口的
    /example-key/v1|test 前缀无关），故从 role URL 取 scheme://host 拼 /health，
    不能拼到 /v1 base 下（那会 404）。不可达返回 False，调用方应整体回退。
    """
    role_url = remote_url or os.environ.get("JINLIN_ROLE_URL", DEFAULT_REMOTE_URL)
    health_url = ""
    try:
        parts = urllib.parse.urlsplit(role_url)
        health_url = f"{parts.scheme}://{parts.netloc}/health"
        resp = requests.get(health_url, timeout=timeout)
        return resp.status_code == 200
    except Exception as e:
        logger.info(f"健康检查失败（远程将整体回退）: {health_url or role_url}: {e}")
        return False


def set_remote_available(ok: bool | None) -> None:
    """设置远程可用性缓存。启动时传 True/False（一次探测的结论），
    传 None 则清除缓存（设置页手动检查时重新探测）。"""
    global _remote_health_cache
    _remote_health_cache = ok


def _remote_available() -> bool:
    """远程是否可用（懒探测一次并缓存）。不可用则各加载器跳过网络分支。"""
    global _remote_health_cache
    if _remote_health_cache is None:
        _remote_health_cache = check_health()
    return _remote_health_cache


def _cache_path() -> str:
    """本地缓存路径（data/cache/ 子目录）。"""
    return os.path.join(get_cache_dir(), CACHE_FILENAME)


def _dump_compact(obj, _level: int = 1) -> str:
    """紧凑但可读的 JSON：叶子容器（内容全为标量）内联成一行，非叶子按层缩进。

    stdlib `json.dump(..., indent=2)` 只会全量展开（每键一行），读起来
    server/data/role.json 的手写紧凑风格（顶层缩进、条目内联）更省眼。
    这里复刻该风格：纯标量对象/数组原地内联，含嵌套容器时才按层换行缩进。
    语义与 json.dumps 等价，仅供缓存文件维护/阅读便利。

    `_level` 约定该容器「内部元素」的缩进层级（step=2 空格）；容器自身
    （开/闭括号）对齐在 `_level-1` 层。根调用 _level=1 -> 内容 2 空格、括号顶格。
    """
    step = 2
    if isinstance(obj, dict):
        if all(not isinstance(v, (dict, list)) for v in obj.values()):
            items = [
                f"{json.dumps(k, ensure_ascii=False)}: {_dump_compact(v)}"
                for k, v in obj.items()
            ]
            return "{" + ", ".join(items) + "}"
        pad_open = " " * ((_level - 1) * step)
        pad_item = " " * (_level * step)
        lines = [
            f"{pad_item}{json.dumps(k, ensure_ascii=False)}: {_dump_compact(v, _level + 1)}"
            for k, v in obj.items()
        ]
        return "{\n" + ",\n".join(lines) + "\n" + pad_open + "}"
    if isinstance(obj, list):
        if all(not isinstance(v, (dict, list)) for v in obj):
            items = [_dump_compact(v) for v in obj]
            return "[" + ", ".join(items) + "]"
        pad_open = " " * ((_level - 1) * step)
        pad_item = " " * (_level * step)
        lines = [f"{pad_item}{_dump_compact(v, _level + 1)}" for v in obj]
        return "[\n" + ",\n".join(lines) + "\n" + pad_open + "]"
    return json.dumps(obj, ensure_ascii=False)


def _write_cache(data: dict) -> None:
    """远程拉取成功后写本地缓存，供后续离线/失败回退。写失败仅告警不影响本次。

    用 `_dump_compact` 写成紧凑可读风格（与 server/data/role.json 一致），
    取代 json.dump(indent=2) 的全展开。
    """
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            f.write(_dump_compact(data) + "\n")
        logger.info(f"已更新本地缓存: {_cache_path()}")
    except OSError as e:
        logger.warning(f"写缓存失败（不影响本次使用）: {e}")


def _load_cache() -> Dict[str, any] | None:
    """读本地缓存。合法返回 dict，否则返回 None（不抛错，由调用方决定回退）。"""
    path = _cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取本地缓存失败: {e}")
        return None
    if isinstance(data, dict) and "characters" in data and "pools" in data:
        return data
    logger.warning("本地缓存格式错误")
    return None


def _load_cache_or_raise() -> Dict[str, any]:
    """远程失败后的回退：读本地缓存。缓存无效/缺失抛 DataNotReadyError。"""
    cache = _load_cache()
    if cache is None:
        raise DataNotReadyError("无法加载角色/卡池数据（远程失败且无可用缓存）")
    logger.info(f"使用本地缓存角色/卡池数据 (v{cache.get('version')})")
    return cache


def load_role_data(
    remote_url: str = "",
    key: str = "",
    secret: str = "",
    prefer_remote: bool = True,
) -> Tuple[Dict[str, Character], Dict[str, Pool], Dict[str, PityGroup], bool]:
    """加载角色与卡池映射。

    Args:
        remote_url: 远程接口地址，缺省从环境变量 JINLIN_ROLE_URL 读取
        key: 鉴权标识，缺省从环境变量 JINLIN_KEY 读取
        secret: 鉴权密钥，缺省从环境变量 JINLIN_SECRET 读取
        prefer_remote: 是否优先尝试远程

    Returns:
        (characters, pools, pity_groups, remote_ok)。remote_ok=True 表示拿到了最新远程数据；
        False 表示远程失败、用了缓存回退（调用方应弹窗让用户确认是否继续）。
        pity_groups 为保底分组配置；旧数据无该段时返回空 dict（由 pool_groups 兜底 solo）。

    Raises:
        DataNotReadyError: 远程失败且缓存无效/缺失（无任何可用数据）
    """
    remote_url = remote_url or os.environ.get("JINLIN_ROLE_URL", DEFAULT_REMOTE_URL)
    key = key or os.environ.get("JINLIN_KEY", DEFAULT_KEY)
    secret = secret or os.environ.get("JINLIN_SECRET", DEFAULT_SECRET)

    raw: Dict[str, any] = {}
    remote_ok = False

    if prefer_remote and _remote_available():  # D：健康检查未通过则跳过远程，直接缓存回退
        try:
            headers = build_auth_headers(key, secret)
            logger.info(f"尝试远程拉取角色/卡池数据: {remote_url}")
            resp = requests.get(remote_url, headers=headers, timeout=10)
            resp.raise_for_status()  # 503/4xx/5xx 在此抛 HTTPError，落入下面 except -> 回退
            data = resp.json()
            if not isinstance(data, dict) or "characters" not in data or "pools" not in data:
                raise ValueError("远程返回数据格式错误")
            raw = data
            _write_cache(data)
            remote_ok = True
            logger.info("远程拉取成功，已更新本地缓存")
        except Exception as e:
            # 任何远程失败（含 503/网络/401/超时/格式错）都回退缓存
            logger.warning(f"远程拉取失败，尝试缓存回退: {e}")

    if not raw:
        raw = _load_cache_or_raise()

    characters = {cid: parse_character(cid, cdata) for cid, cdata in raw.get("characters", {}).items()}
    pools = {pid: parse_pool(pid, pdata) for pid, pdata in raw.get("pools", {}).items()}
    pity_groups = parse_pity_groups(raw.get("pity_groups", {}))

    logger.info(
        f"加载角色 {len(characters)} 个，卡池 {len(pools)} 个，保底分组 {len(pity_groups)} 个，remote_ok={remote_ok}"
    )
    return characters, pools, pity_groups, remote_ok


def _version_url() -> str:
    """版本检查接口 URL：环境变量优先，否则从 ROLE_URL 同 base 推导。

    JINLIN_VERSION_URL 优先；否则用 JINLIN_ROLE_URL（或默认）
    的同 base 换末段 role -> version，与公告接口推导一致。
    """
    env = os.environ.get("JINLIN_VERSION_URL")
    if env:
        return env
    role_url = os.environ.get("JINLIN_ROLE_URL", DEFAULT_REMOTE_URL)
    base = role_url.rsplit("/", 1)[0]  # 去掉末段（/role）
    return base + "/version"


def resolve_appzip_url(update_url: str) -> str:
    """补丁下载地址：环境变量优先，否则从 ROLE_URL 同 base 推导，最后用服务端下发地址。

    JINLIN_APPZIP_URL 优先；否则若设置了 JINLIN_ROLE_URL（本地联调），
    用同 base 换末段 role -> app.zip，避免本地联调时补丁仍打到生产域名；
    未设置任何环境变量时用 /version 接口下发的 update_url（生产用户）。
    """
    env = os.environ.get("JINLIN_APPZIP_URL")
    if env:
        return env
    role_url = os.environ.get("JINLIN_ROLE_URL")
    if role_url:
        base = role_url.rsplit("/", 1)[0]  # 去掉末段（/role）
        return base + "/app.zip"
    return update_url


def load_version_info() -> VersionInfo | None:
    """拉取工具最新版本信息。失败返回 None（静默，不抛错）。

    纯服务端远程：任何失败（网络/401/503/超时/格式错）都返回 None，
    由调用方按"无更新"处理，不影响主程序启动。latest_version 为空串
    视为无效也返回 None。
    """
    if not _remote_available():  # D：健康检查未通过则跳过远程，按"无更新"处理
        logger.info("服务端不可用（健康检查未通过），跳过版本检查")
        return None
    url = _version_url()
    key = os.environ.get("JINLIN_KEY", DEFAULT_KEY)
    secret = os.environ.get("JINLIN_SECRET", DEFAULT_SECRET)
    try:
        headers = build_auth_headers(key, secret)
        logger.info(f"尝试拉取版本信息: {url}")
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            logger.info("版本返回格式错误，忽略")
            return None
        info = parse_version_info(data)
        if not info.latest_version:
            logger.info("版本返回缺 latest_version，忽略")
            return None
        logger.info(f"最新版本 {info.latest_version}，类型 {info.update_type}")
        return info
    except Exception as e:
        logger.info(f"版本拉取失败（不影响使用）: {e}")
        return None


def _announcement_url() -> str:
    """公告接口 URL：环境变量优先，否则从 ROLE_URL 同 base 推导。

    JINLIN_ANNOUNCEMENT_URL 优先；否则用 JINLIN_ROLE_URL（或默认）
    的同 base 换末段 role -> announcements。用户调试时只设一个
    JINLIN_ROLE_URL=http://ip:5000/example-key/v1/role，公告自动同 base。
    """
    env = os.environ.get("JINLIN_ANNOUNCEMENT_URL")
    if env:
        return env
    role_url = os.environ.get("JINLIN_ROLE_URL", DEFAULT_REMOTE_URL)
    base = role_url.rsplit("/", 1)[0]  # 去掉末段（/role）
    return base + "/announcements"


def load_announcements() -> List[Announcement]:
    """拉取公告列表。失败/格式错返回空列表（静默，不抛错）。

    纯服务端远程：任何失败（网络/401/503/超时/格式错）都返回 []，不影响
    主程序启动。已读状态由 storage/announcements 单独管理。无 id 的条目
    跳过（无法标记已读）。
    """
    if not _remote_available():  # D：健康检查未通过则跳过远程，按无公告处理
        logger.info("服务端不可用（健康检查未通过），跳过公告拉取")
        return []
    url = _announcement_url()
    key = os.environ.get("JINLIN_KEY", DEFAULT_KEY)
    secret = os.environ.get("JINLIN_SECRET", DEFAULT_SECRET)
    try:
        headers = build_auth_headers(key, secret)
        logger.info(f"尝试拉取公告: {url}")
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "announcements" not in data:
            logger.info("公告返回格式错误，忽略")
            return []
        anns: List[Announcement] = []
        for a in data.get("announcements", []):
            if not isinstance(a, dict):
                continue
            aid = a.get("id")
            if not aid:
                continue  # 无 id 无法标记已读，跳过
            anns.append(parse_announcement(str(aid), a))
        logger.info(f"拉取公告 {len(anns)} 条")
        return anns
    except Exception as e:
        logger.info(f"公告拉取失败（不影响使用）: {e}")
        return []
