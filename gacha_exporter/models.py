"""数据模型 — 抽卡记录、角色、卡池、会话上下文、异常。"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

_RUNTIME_MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_RUNTIME_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class GachaRecord:
    """单条抽卡记录。

    batch_seq / position_in_batch 仅为解析辅助字段，不参与去重。语义约定
    「越小越新」（shared_pity_tracking 冻结排序的 tiebreaker 依赖）：
    - 新数据（2026-08-18 起）：batch_seq=0 常量；position_in_batch = 页
      offset 回显 + 帧内序号 = 视图内绝对序号（0=最新一条），与翻页方向无关
    - 旧数据：帧到达序号，仅当抓包时「新->旧」翻页才满足越小越新
    """

    pool_id: str
    item_id: str
    timestamp: int  # 毫秒时间戳
    batch_seq: int = 0
    position_in_batch: int = 0

    def key(self) -> tuple:
        """单条记录关键字，用于历史合并去重。"""
        return (self.pool_id, self.item_id, self.timestamp)

    @staticmethod
    def batch_fingerprint(records: list) -> tuple:
        """批次指纹：整批记录的有序序列，用于批次级去重。

        同一页重复抓取时指纹相同，整批跳过；
        十连被拆分到不同页时指纹不同，各自全部保留。
        """
        return tuple((r.pool_id, r.item_id, r.timestamp) for r in records)


@dataclass
class Character:
    """角色信息。"""

    id: str
    name: str
    rarity: int
    limited: bool = False
    # 专属词条库（库序→名称+档位），来自 data/role-image.json（load_role_data
    # 装配）；缺失 = 无翻译表，练度展示回退 #序号（见 roster_capture.translate_entries）
    lib: Optional[List[dict]] = None

    @classmethod
    def default(cls, item_id: str) -> "Character":
        """构造未知角色的默认值。"""
        return cls(id=item_id, name=f"未知({item_id})", rarity=0, limited=False)


@dataclass
class Pool:
    """卡池信息。"""

    id: str
    name: str
    type: str  # 新手/常驻/UP/遴选
    order: int = 0
    rarity: int = 0
    up_character: str = ""
    pity_group: str = ""  # 所属保底分组 key（对应 PityGroup.id），空表示未配->兜底 solo

    @property
    def is_up(self) -> bool:
        """是否为 UP 池。"""
        return self.type == "UP"

    @property
    def is_featured(self) -> bool:
        """是否为有 UP 角色的池子（UP 或 遴选）。"""
        return self.type in ("UP", "遴选")

    @classmethod
    def default(cls, pool_id: str) -> "Pool":
        """构造未知卡池的默认值。"""
        return cls(id=pool_id, name=f"未知({pool_id})", type="未知", order=0)


@dataclass
class PityGroup:
    """保底分组 - 同组卡池共享保底计数与小/大保底状态。

    id 既是分组 key 也是显示用的内部标识；label 用于展示；hard_pity 为该组
    硬保底抽数；has_state=False 的组（如新手/常驻）不跑小/大保底状态机。
    """

    id: str
    label: str
    hard_pity: int = 70
    has_state: bool = False

    @classmethod
    def solo(cls, pool_id: str, label: str, hard_pity: int = 70) -> "PityGroup":
        """构造一个独立分组（不与任何池共享）。"""
        return cls(id="solo:" + pool_id, label=label, hard_pity=hard_pity, has_state=False)


@dataclass
class Profile:
    """账号档案 - 多账号隔离的分区单元。

    id 为内部稳定标识（uuid hex），用作 users/<id>.json 文件名与历史合并
    的分区键；name 仅作显示，可改。不绑定游戏 uid（抓包协议读不到账号
    标识），所有抓包结果归到当前选中的 profile。
    """

    id: str
    name: str
    created_at: str = ""


@dataclass
class CaptureConfig:
    """抓包配置。

    addresses 优先于 ports：发现抽卡服务器地址后按 (ip, port) 精确过滤；
    为空时回退按 ports 过滤。真实端口因账号/环境而异（2026-10-08 起不再
    内置真实端口），新账号须先走「自助获取抽卡地址」，识别成功后按
    (ip, port) 精确过滤并按账号持久化。
    pcap_enabled 控制原始抓包落盘：打包态恒 False（见 debug_flags），
    仅开发环境 JINLIN_DEBUG=1 时可开，避免生产把原始流量写盘。
    iface 指定要嗅探的网卡（NPF 接口名）；空串 = 由 net_iface 自动解析
    （P1 显式 → P2 学习 → P3 实时，见 utils/net_iface.resolve_capture_iface）。
    """

    ports: tuple = (12345,)  # 占位端口，无真实含义；实际地址靠 addresses（自助获取）
    addresses: tuple = ()  # 发现的 (ip, port) 列表，优先于 ports
    iface: str = ""  # 显式指定抓包网卡；空串 = 自动解析
    timeout: float = 1.0
    max_frame_len: int = 50000
    pcap_enabled: bool = False
    # 已知最短合法帧体 = 8 字节被拒（限流）响应：整个 S->C 固定头（类型2+extId2
    # +result2+seq回显1+ack1 = 10B 含长度头）无业务体，见 frame_schema。曾设
    # 10：被拒响应在切帧层被整帧丢弃、note_response 永远收不到（2026-08-17 由
    # 抓包样本字节对账定位）。9 字节 C->S 心跳随之正常送达。
    min_frame_len: int = 8


@dataclass
class SessionContext:
    """当前会话上下文。"""

    current_pool_id: str
    capture_start: datetime = field(default_factory=datetime.now)


@dataclass
class Announcement:
    """服务端公告 - 启动时拉取，未读的弹窗展示。

    id 为稳定标识，用于“不再提示”已读标记；title/content 为公告文本
    （content 支持 Markdown）；ts 为 Unix 秒时间戳，用于排序（最新在前）；
    type 区分 pinned（置顶/免责，强制“我已了解并同意”）/ update（更新）；
    visible 为 False 时服务端不返回（草稿/归档，客户端收不到）。
    """

    id: str
    title: str
    content: str
    ts: int = 0
    type: str = "update"
    visible: bool = True


@dataclass(frozen=True)
class RuntimeInfo:
    """一个外部功能组件模块（runtime）的下发信息。

    模块按重依赖边界拆开（``ocr`` / ``training`` / ``team_builder``），各自
    独立版本/地址/哈希/签名，只有变化的模块才会被客户端下载。
    ``requires`` 声明依赖模块（安装/挂载前须就绪）。
    """

    module: str
    version: str = ""
    url: str = ""
    sha256: str = ""
    signature: str = ""
    size: int = 0
    requires: Tuple[str, ...] = ()
    label: str = ""

    def display_name(self) -> str:
        """展示名：服务端 label 优先，其次模块名。"""
        return self.label or self.module


@dataclass(frozen=True)
class VersionInfo:
    """服务端下发的工具最新版本信息。

    latest_version 为最新工具版本（与客户端 __version__ 同格式 x.y.z）；
    update_type 为本次发布的更新策略：silent（静默，仅手动检查）/ prompt
    （提示不强制）/ force（强制，拒绝则禁用抓包）；
    update_url 为逻辑补丁 zip（full_exe=False）或新 exe 下载地址（full_exe=True）；
    update_sha256 为补丁期望哈希（配合 HMAC 鉴权响应防篡改）；
    runtimes 为外部功能组件模块表（模块名 -> RuntimeInfo），空表 = 不更新；
    ts 为 Unix 秒时间戳（排序/展示用）。
    """

    latest_version: str
    update_type: str = "prompt"
    changelog: str = ""
    update_url: str = ""
    update_sha256: str = ""
    update_signature: str = ""
    full_exe: bool = False
    runtimes: Dict[str, RuntimeInfo] = field(default_factory=dict)
    ts: int = 0

    def runtime(self, module: str) -> Optional[RuntimeInfo]:
        """按模块名取组件信息；未配置返回 None。"""
        return self.runtimes.get(module)


class DataNotReadyError(Exception):
    """角色/卡池数据未就绪时抛出。"""

    def __init__(self, message: str = "角色/卡池数据未就绪"):
        super().__init__(message)


def parse_character(item_id: str, data: Dict[str, Any]) -> Character:
    """从 JSON dict 构造 Character。

    词条库不随 role.json（走独立文件 role-image.json，见 loaders/role_image），
    lib 保持 None 由 load_role_data 装配。
    """
    return Character(
        id=item_id,
        name=data.get("name", f"未知({item_id})"),
        rarity=int(data.get("rarity", 0)),
        limited=bool(data.get("limited", False)),
    )


def parse_pool(pool_id: str, data: Dict[str, Any]) -> Pool:
    """从 JSON dict 构造 Pool。"""
    return Pool(
        id=pool_id,
        name=data.get("name", f"未知({pool_id})"),
        type=data.get("type", "未知"),
        order=int(data.get("order", 0)),
        rarity=int(data.get("rarity", 0)),
        up_character=data.get("up_character", ""),
        pity_group=data.get("pity_group", ""),
    )


def parse_pity_groups(data: Dict[str, Any]) -> Dict[str, PityGroup]:
    """从 role.json 顶层 pity_groups 段构造 {group_id: PityGroup}。

    缺失或格式错返回空 dict（由 pool_groups 兜底为 solo）。
    """
    if not isinstance(data, dict):
        return {}
    result: Dict[str, PityGroup] = {}
    for gid, gdata in data.items():
        if not isinstance(gdata, dict):
            continue
        result[gid] = PityGroup(
            id=gid,
            label=gdata.get("label", gid),
            hard_pity=int(gdata.get("hard_pity", 70)),
            has_state=bool(gdata.get("has_state", False)),
        )
    return result


def parse_announcement(aid: str, data: Dict[str, Any]) -> Announcement:
    """从 JSON dict 构造 Announcement。id 必填，其余缺省。"""
    return Announcement(
        id=aid,
        title=data.get("title", ""),
        content=data.get("content", ""),
        ts=int(data.get("ts", 0)),
        type=data.get("type", "update"),
        visible=bool(data.get("visible", True)),
    )


def _parse_runtime_info(module: str, data: Any) -> Optional[RuntimeInfo]:
    """解析单个 runtime 模块项；缺版本号/版本格式非法返回 None。"""
    if not isinstance(data, dict):
        return None

    def _str(key: str) -> str:
        value = data.get(key, "")
        return value.strip() if isinstance(value, str) else ""

    version = _str("version")
    if not version or not _RUNTIME_VERSION_RE.match(version) or version in (".", ".."):
        return None
    size = data.get("size", 0)
    if not isinstance(size, int) or isinstance(size, bool):
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
    if size < 0:
        size = 0
    requires_raw = data.get("requires", ())
    requires: Tuple[str, ...] = ()
    if isinstance(requires_raw, (list, tuple)):
        requires = tuple(
            name for name in requires_raw
            if isinstance(name, str) and _RUNTIME_MODULE_RE.match(name)
        )
    return RuntimeInfo(
        module=module,
        version=version,
        url=_str("url"),
        sha256=_str("sha256"),
        signature=_str("signature"),
        size=size,
        requires=requires,
        label=_str("label"),
    )


def parse_version_info(data: Dict[str, Any]) -> VersionInfo:
    """从服务端 /version 响应 dict 构造 VersionInfo。

    latest_version 必填（缺失视为无效，调用方应判空后按"无更新"处理）；
    其余字段逐个校验 + 缺省：update_type 非法 -> "prompt"（默认提示不强制），
    update_url 非 str -> ""，update_sha256 非 str -> ""，update_signature 非 str -> ""，
    full_exe 非 bool -> False；runtimes 为模块名 -> 组件项映射，非法模块名/
    缺版本号的条目直接跳过；旧单模块字段 runtime_* 兼容映射为 ocr 模块。
    """
    latest = data.get("latest_version", "")
    if not isinstance(latest, str) or not latest:
        return VersionInfo(latest_version="")
    utype = data.get("update_type", "prompt")
    if utype not in ("silent", "prompt", "force"):
        utype = "prompt"
    url = data.get("update_url", "")
    if not isinstance(url, str):
        url = ""
    sha = data.get("update_sha256", "")
    if not isinstance(sha, str):
        sha = ""
    signature = data.get("update_signature", "")
    if not isinstance(signature, str):
        signature = ""
    changelog = data.get("changelog", "")
    if not isinstance(changelog, str):
        changelog = ""
    full_exe = data.get("full_exe", False)
    if not isinstance(full_exe, bool):
        full_exe = False

    runtimes: Dict[str, RuntimeInfo] = {}
    raw_runtimes = data.get("runtimes")
    if isinstance(raw_runtimes, dict):
        for name, item in raw_runtimes.items():
            if not isinstance(name, str) or not _RUNTIME_MODULE_RE.match(name):
                continue
            parsed = _parse_runtime_info(name, item)
            if parsed is not None:
                runtimes[name] = parsed
    # 兼容旧单模块字段（runtime_version/... 视作 ocr 模块）
    if "ocr" not in runtimes:
        legacy_version = data.get("runtime_version", "")
        if isinstance(legacy_version, str) and legacy_version.strip():
            legacy = _parse_runtime_info("ocr", {
                "version": legacy_version,
                "url": data.get("runtime_url", ""),
                "sha256": data.get("runtime_sha256", ""),
                "signature": data.get("runtime_signature", ""),
                "size": data.get("runtime_size", 0),
            })
            if legacy is not None:
                runtimes["ocr"] = legacy

    ts = data.get("ts", 0)
    if not isinstance(ts, int):
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = 0
    return VersionInfo(
        latest_version=latest,
        update_type=utype,
        changelog=changelog,
        update_url=url,
        update_sha256=sha,
        update_signature=signature,
        full_exe=full_exe,
        runtimes=runtimes,
        ts=ts,
    )
