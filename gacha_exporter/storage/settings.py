"""全局 UI 偏好设置 - data/settings.json 读写。

含：保底视图偏好（pity_view）、保底面板视图（pity_panel_view，按卡池/按角色
流水）、最近抓包时间（last_capture）、自助发现的
抓包地址（per_profile_discovered_addresses，按账号隔离）、版本更新确认状态
（acknowledged_version / rejected_forced_version）。文件读写失败不影响程序
运行，回退默认值。
"""
import json
import os
from typing import Any, Dict

from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

SETTINGS_FILENAME = "settings.json"

# 抓包网卡显式槽的值统一语义："" = 智能（自动），落到 P2/P3（见 utils/net_iface）。
IFACE_AUTO = ""

VALID_PITY_VIEWS = ("shared", "per_pool")
DEFAULT_PITY_VIEW = "shared"


def _settings_path() -> str:
    """设置文件路径（与 exe 同级的 data/ 目录）。"""
    return os.path.join(get_data_dir(), SETTINGS_FILENAME)


def _load_all() -> Dict[str, Any]:
    """读取全部设置。文件缺失/损坏返回空 dict（不抛错）。"""
    path = _settings_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"读取 settings 失败，用默认值: {e}")
        return {}


def _save_all(data: Dict[str, Any]) -> None:
    """写入全部设置。失败仅告警。"""
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写 settings 失败（不影响运行）: {e}")


def load_pity_view() -> str:
    """读取保底视图偏好。缺省/非法返回 shared。"""
    v = _load_all().get("pity_view", DEFAULT_PITY_VIEW)
    return v if v in VALID_PITY_VIEWS else DEFAULT_PITY_VIEW


def save_pity_view(mode: str) -> None:
    """保存保底视图偏好（shared / per_pool）。"""
    if mode not in VALID_PITY_VIEWS:
        logger.warning(f"非法 pity_view '{mode}'，忽略")
        return
    data = _load_all()
    data["pity_view"] = mode
    _save_all(data)


VALID_PITY_PANEL_VIEWS = ("pool", "flow")
DEFAULT_PITY_PANEL_VIEW = "pool"


def load_pity_panel_view() -> str:
    """读取保底面板视图偏好（pool=按卡池, flow=按角色流水）。缺省/非法返回 pool。"""
    v = _load_all().get("pity_panel_view", DEFAULT_PITY_PANEL_VIEW)
    return v if v in VALID_PITY_PANEL_VIEWS else DEFAULT_PITY_PANEL_VIEW


def save_pity_panel_view(mode: str) -> None:
    """保存保底面板视图偏好（用户切换时调用，跨启动记忆）。"""
    if mode not in VALID_PITY_PANEL_VIEWS:
        logger.warning(f"非法 pity_panel_view '{mode}'，忽略")
        return
    data = _load_all()
    data["pity_panel_view"] = mode
    _save_all(data)


def load_last_capture(profile_id: str) -> str:
    """读取某账号上次抓包完成时间（ISO 字符串）。

    按 profile_id 隔离（多账号各自记录）。缺省/无记录返回 ""。
    """
    if not profile_id:
        return ""
    captures = _load_all().get("last_capture", {})
    if not isinstance(captures, dict):
        return ""
    val = captures.get(profile_id, "")
    return val if isinstance(val, str) else ""


def save_last_capture(profile_id: str, iso_str: str) -> None:
    """保存某账号上次抓包完成时间（ISO 字符串）。

    存于 settings.json 顶层 last_capture: {profile_id: iso_str}，按账号隔离。
    """
    if not profile_id:
        return
    data = _load_all()
    captures = data.get("last_capture")
    if not isinstance(captures, dict):
        captures = {}
    captures[profile_id] = iso_str
    data["last_capture"] = captures
    _save_all(data)


def _parse_addresses(raw) -> tuple:
    """把 JSON 存储值解析为 ((ip, port), ...) 元组，格式非法返回空 tuple。"""
    if not isinstance(raw, list):
        return ()
    out = []
    for item in raw:
        if (isinstance(item, (list, tuple)) and len(item) == 2
                and isinstance(item[0], str) and isinstance(item[1], int)):
            out.append((item[0], item[1]))
    return tuple(out)


def migrate_global_discovered_addresses(profile_ids) -> None:
    """一次性迁移：旧版全局地址列表 -> 按账号隔离的 dict。

    旧版 discovered_addresses 为跨账号共享的 [[ip, port], ...]（顶层 list）。
    抽卡地址实际因账号而异，改为 {profile_id: [[ip, port], ...]}。迁移时把
    旧列表复制给当时已存在的全部账号（与旧版共享行为一致，不改变现状）；
    之后新建的账号无地址，由切换时的主动检查引导获取。已是 dict 则跳过。
    """
    data = _load_all()
    raw = data.get("discovered_addresses")
    if not isinstance(raw, list):
        return
    addresses = _parse_addresses(raw)
    per_profile = data.get("per_profile_discovered_addresses")
    if not isinstance(per_profile, dict):
        per_profile = {}
    for pid in profile_ids:
        if pid and pid not in per_profile:
            per_profile[pid] = [[ip, port] for ip, port in addresses]
    data["per_profile_discovered_addresses"] = per_profile
    del data["discovered_addresses"]
    _save_all(data)
    logger.info(f"已把全局抽卡地址迁移为按账号隔离（{len(profile_ids)} 个账号）")


def load_discovered_addresses(profile_id: str) -> tuple:
    """读取指定账号自助发现的抽卡服务器地址列表。

    存于 settings.json 顶层 per_profile_discovered_addresses:
    {profile_id: [[ip, port], ...]}。抽卡地址因账号而异，按 profile_id 隔离；
    地址变更后用户自助发现并保存。缺省/无记录/格式非法返回空 tuple
    （回退默认端口过滤）。
    """
    if not profile_id:
        return ()
    per_profile = _load_all().get("per_profile_discovered_addresses")
    if not isinstance(per_profile, dict):
        return ()
    return _parse_addresses(per_profile.get(profile_id))


def save_discovered_addresses(profile_id: str, addresses) -> None:
    """保存指定账号自助发现的抽卡服务器地址列表。addresses 为 (ip, port) 可迭代对象。"""
    if not profile_id:
        return
    data = _load_all()
    per_profile = data.get("per_profile_discovered_addresses")
    if not isinstance(per_profile, dict):
        per_profile = {}
    per_profile[profile_id] = [[ip, port] for ip, port in addresses]
    data["per_profile_discovered_addresses"] = per_profile
    _save_all(data)


def load_acknowledged_version() -> str:
    """读取用户已确认（提示后选择"稍后"）的版本号。

    用于提示不强制更新：同版本不再反复弹。缺省/非法返回空串。
    """
    v = _load_all().get("acknowledged_version", "")
    return v if isinstance(v, str) else ""


def save_acknowledged_version(version: str) -> None:
    """保存用户已确认（稍后）的版本号。"""
    data = _load_all()
    data["acknowledged_version"] = version
    _save_all(data)


def load_rejected_forced_version() -> str:
    """读取用户上次拒绝的强制更新版本号。

    用于强制更新同步闸门：拒绝过强更的用户下次启动直接禁用抓包
    （即使网络不可用也知道该强制）。缺省/非法返回空串。
    """
    v = _load_all().get("rejected_forced_version", "")
    return v if isinstance(v, str) else ""


def save_rejected_forced_version(version: str) -> None:
    """保存用户拒绝的强制更新版本号。"""
    data = _load_all()
    data["rejected_forced_version"] = version
    _save_all(data)


def clear_rejected_forced_version() -> None:
    """清除拒绝的强制更新版本号（用户已更新到最新后调用）。"""
    data = _load_all()
    if "rejected_forced_version" in data:
        del data["rejected_forced_version"]
        _save_all(data)


def load_capture_iface() -> str:
    """读取用户显式选择的抓包网卡（P1 槽）。

    存于 settings.json 顶层 capture_iface。"" / 缺失 = 智能（自动），落到 P2/P3。
    网卡是物理属性、非账号级，故为全局单值。
    """
    v = _load_all().get("capture_iface", "")
    return v if isinstance(v, str) and v else ""


def save_capture_iface(iface: str) -> None:
    """保存用户显式选择的抓包网卡（P1 槽）。

    仅由用户手动选择调用（铁护栏：自动解析/学习永不写本槽）。"" = 智能（自动）。
    存非空时校验仅存 NPF 名，避免写入无关值。
    """
    data = _load_all()
    data["capture_iface"] = iface if isinstance(iface, str) else ""
    _save_all(data)


def load_capture_iface_learned() -> str:
    """读取自动学习的抓包网卡（P2 槽，隐藏，用户不可直接编辑）。

    地址发现成功时自动记录"本次命中那块"，供下次在 P1 为空时复用并复查。
    存于 settings.json 顶层 capture_iface_learned。
    """
    v = _load_all().get("capture_iface_learned", "")
    return v if isinstance(v, str) and v else ""


def save_capture_iface_learned(iface: str) -> None:
    """保存自动学习的抓包网卡（P2 槽）。

    仅由地址发现成功流程调用；仅在 P1 为空时写入（见调用方护栏），不覆盖显式。
    """
    data = _load_all()
    data["capture_iface_learned"] = iface if isinstance(iface, str) else ""
    _save_all(data)
