"""卡池视图感知层 — 解析切池/翻页请求与响应，跨视图去重。

依据 `卡池切换分析/分析结论.md`（抓包样本逐字节核对）：切换卡池
与翻页是同一种 C->S 请求帧，业务体 `08 <pool_id> 10 <offset>`；频繁错误是
8 字节截断响应（result=142）。帧头解析走 capture.frame_schema（结构层）。

本模块是「视图感知的跨卡池重复过滤器」，插在 parser.parse 之后、stats.add
之前，专门消除「同一条抽卡记录在具体池页和全部卡池页各传输一次」导致的重复
计入。**只做跨视图去重**，不碰同视图内的去重（十连同名 / 整批重发由冻结的
stats.add 按原规则处理）。只看记录自己的 key（同 GachaRecord.key()，
卡池信息天然含在 key 里），与页边界/批次划分完全无关。

按份数配对（不是「见过就丢」）：key K 从视图 V 到达时，看其他视图的未配对
份数 - 为正则这是其中一份的重传，丢弃并把那份配对掉（份数 -1）；否则接纳并
记为视图 V 的未配对份数 +1。同视图到达永不过滤。

兜底：抓不到请求/响应解析失败 -> 视图未知 (None) -> 该页不过滤，走现有逻辑
（宁可重复不可漏抓，与现状一致）。
"""
from typing import Dict, List, Optional, Set, Tuple

from ..capture.frame_schema import (
    MSG_GACHA,
    RESULT_REJECTED,
    SEQ_MOD,
    parse_header,
)
from ..models import GachaRecord
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 全部卡池的视图标识（与请求里 pool_id=0 对应）。记录的 pool_id 始终来自
# 记录自身（抓包已验证），全部卡池页里各条自带各自 pool_id，与 view 的 pool_id
# 无关 -> 过滤只看记录自己的 key，把它当独立维度即可。
_ALL_POOL_ID = "0"

# ---- 视图标识：pool_id + 列表类型 ----
# 请求帧业务体完整布局 `08 <pool_id> 10 <offset> 18 <list_type>`。list_type
# （field 3）用于区分"同为 pool_id=0 的不同卡池列表视图"——游戏新增的按契约
# 类型聚合页（遴选/活动）与「全部卡池」发的请求 pool_id 都是 0，早前只按
# pool_id 判视图，三者被当成同一视图，跨视图去重失效、记录重复计入。
# 视图标识统一为 "<pool_id>@<list_type>"；协议没有 field 3 时回退 "<pool_id>"
# （老协议/其他消息行为不变）。
VIEW_SEP = "@"
LIST_TYPE_ALL = 0
LIST_TYPE_LABELS = {0: "全部卡池", 3: "活动契约", 4: "遴选契约"}

# 每页记录条数（规则文件 B4）：日志实测「解析5条」占绝对多数，末页可少
# （521 条 = 104×5 + 1、792 条 = 158×5 + 2）。仅用于把「权威 total」换算成
# 「应有页的 offset 列表」，以便收尾时报告还缺哪几页。
PAGE_SIZE = 5


# H1 撞车预警（见 docs/去重归档/去重架构重构设计.md §13.5 / §16.2）：
# 「全部卡池」用 pool_id=0，而按契约类型聚合的汇总页（活动/遴选）与它**共用**
# pool_id=0，只靠 list_type(field 3) 区分。一旦协议不带 field 3，这些页面会一起
# 回退成同一个视图标识 "0" → **第①级视图身份不可用**（视图溯源与各视图 total
# 完整性校验会张冠李戴），跨视图重复只能降级到第②级（用响应 total 拆开同名页）。
# 注一：L1（filter_records）自 2026-09-11 步②起已直通、不再做跨视图去重，故本
#   回退**不再有丢数风险**；跨视图重复由事件账本按「事件 (pool_id, timestamp)」
#   判定兜住，不依赖视图身份。
# 注二：自 2026-09-10 21:04 起协议恒带 field 3（见设计文档 §16.5-A），因此本预警
#   在当前协议下**不会被真实流量触发**，属纯防御代码；有效性由
#   tests/test_pool_view.py 的 H1 用例保证。
# 帧内已无从区分，只能靠日志把「撞车」从静默变可见；同类警告一个进程只报一次，
# 避免逐帧刷屏。
_VIEW_COLLAPSE_WARNED = False


def make_view(pool_id: int, list_type: Optional[int]) -> str:
    """拼装视图标识：有 list_type 用 "pool@type"，否则仅 "pool"（兼容老协议）。

    返回语义与老协议完全一致；仅在 pool_id=0 且缺 list_type（视图标识退化为
    "0"、无法与其它同 pool_id 汇总页区分）时额外打一次撞车预警（H1）。
    """
    if list_type is None:
        _warn_view_collapse(pool_id)
        return str(pool_id)
    return f"{pool_id}{VIEW_SEP}{list_type}"


def _warn_view_collapse(pool_id: int) -> None:
    """H1：pool_id=0 的无类型回退无法区分多个汇总页 → 打一次 warning。

    只在 pool_id=0（「全部卡池」标识）时预警——非 0 的 pool_id 自带区分度，不会
    与其它视图撞车。同进程内只报一次。
    """
    global _VIEW_COLLAPSE_WARNED
    if pool_id != 0 or _VIEW_COLLAPSE_WARNED:
        return
    _VIEW_COLLAPSE_WARNED = True
    logger.warning(
        "视图标识回退为纯 pool_id=0（请求未带 list_type/field3）：无法区分 pool_id "
        "同为 0 的多个汇总页（全部卡池 / 活动契约 / 遴选契约），第①级视图身份不可用 "
        "→ 视图溯源与各视图 total 完整性校验会张冠李戴。跨视图重复仍由事件账本按"
        "「事件 (pool_id, timestamp)」判定兜住（L1 自 2026-09-11 步②起已直通、不再"
        "做跨视图去重，故无丢数风险）。请按设计文档 §16.2 降级到第②级（用响应 total "
        "拆开同名页），并抓包核对切池/翻页请求业务体是否带 field3。"
    )


def split_view(view: Optional[str]) -> Tuple[str, Optional[int]]:
    """拆分视图标识为 (pool_id, list_type)；无类型段时 list_type=None。"""
    if not view:
        return "", None
    pool_id, sep, raw_type = view.partition(VIEW_SEP)
    if not sep:
        return view, None
    try:
        return pool_id, int(raw_type)
    except ValueError:
        return pool_id, None


def is_all_pools(view: Optional[str]) -> bool:
    """该视图是否属于「全部卡池」（pool_id=0 且 list_type=全部/缺省）。

    **「仅采纳全部卡池数据」的运行约定**（见 `协议结构说明` B8）用它
    判定：全部卡池是其它所有视图（单池 / 活动契约 / 遴选契约汇总页）的**超集**
    —— 实测各视图的权威 total 与对应池的求和完全吻合（792 = 各池总和；
    421 = 30001~30006；70 = 20005+20007）。只保留它即可覆盖全部记录。

    这样做的价值：**只剩单一视图后，去重退化为「同视图 + 位置可比」的逐槽
    判定**，跨视图带来的全部歧义（页边界错位、首个观察起点歧义、翻页方向
    依赖、坐标系不可比）随之消失。

    老协议无 field3 时视图为裸 `"0"`，同样视为全部卡池。
    """
    if view is None:
        return False
    pool_id, list_type = split_view(view)
    return pool_id == "0" and list_type in (LIST_TYPE_ALL, None)


def expected_page_offsets(total: int, size: int = PAGE_SIZE) -> List[int]:
    """由权威 total 推算该视图「应有页」的 offset 列表（每页 size 条，末页可少）。

    实测吻合：total=521 → 105 页（末页 offset=520）；total=792 → 159 页
    （末页 offset=790）。供收尾缺口报告指出「还缺哪几页」，用户可据此回翻。

    Args:
        total: 响应 field2 给出的服务器权威总抽数（<=0 时返回空）
        size: 每页条数（默认 PAGE_SIZE）

    Returns:
        升序 offset 列表；total<=0 或 size<=0 时为空列表。
    """
    if total <= 0 or size <= 0:
        return []
    full = total // size
    offsets = [i * size for i in range(full)]
    if total % size:
        offsets.append(full * size)
    return offsets


def parse_pool_list_type(body: bytes) -> Optional[int]:
    """解析 C->S 切池/翻页请求帧的卡池列表类型（field 3，标签 0x18）。

    独立成函数：`parse_pool_request` 的返回签名已被调用方与既有测试依赖，
    不改动它；类型字段仅用于细化视图标识。

    Returns:
        list_type，字段缺失或非切池/翻页请求时返回 None。
    """
    if parse_pool_request(body) is None:
        return None
    header = parse_header(body, server_to_client=False)
    if header is None:
        return None
    tail = body[header.payload_offset:]
    _pool_id, after_pool = _read_varint(tail, 1)
    if after_pool >= len(tail) or tail[after_pool] != 0x10:
        return None
    _offset, after_off = _read_varint(tail, after_pool + 1)
    if after_off >= len(tail) or tail[after_off] != 0x18:
        return None
    list_type, _ = _read_varint(tail, after_off + 1)
    return list_type


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    """读取 protobuf varint。Returns: (value, next_offset)。"""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if (byte & 0x80) == 0:
            break
        shift += 7
    return result, offset


def parse_pool_request(body: bytes) -> Optional[Tuple[int, int, int]]:
    """解析一条 C->S 切池/翻页请求帧（不含 4 字节长度头）。

    帧体布局（frame_schema / 抓包样本核对）：前 4 字节为大端 seq，其后
    2B 类型 + 2B extId（抽卡 = 0x000c/0x0800）、1B counter（= seq%127），
    业务体 `08 <varint pool_id> 10 <varint offset>`。pool_id=0 表示全部卡池。

    seq 用前 4 字节大端值与响应配对（响应回显 seq%127）。

    Args:
        body: C->S 帧体（不含长度头，含前 4 字节 seq）

    Returns:
        (seq, pool_id, offset) 或 None（非切池/翻页请求、结构不符）
    """
    header = parse_header(body, server_to_client=False)
    if header is None or not header.known or header.msg_type != MSG_GACHA:
        return None
    tail = body[header.payload_offset:]
    if len(tail) < 2 or tail[0] != 0x08:
        return None
    pool_id, after_pool = _read_varint(tail, 1)
    if after_pool >= len(tail) or tail[after_pool] != 0x10:
        return None
    offset, _ = _read_varint(tail, after_pool + 1)
    return header.seq, pool_id, offset


def parse_pool_response(body: bytes) -> Optional[Tuple[int, int, int, bool]]:
    """解析一条 S->C 切池/翻页响应帧（不含 4 字节长度头）。

    帧体布局（frame_schema / 抓包样本核对，无前置 seq）：
    2B 类型 + 2B extId（抽卡 = 0x000c/0x0800）+ 2B result（0 成功 / 142 被拒）
    + 1B seq 回显（= 请求 seq%127）+ 1B ack，业务体
    `08 <offset回显> 10 <total>` + 记录。
    result=142 且 8 字节截断无业务体 = 请求被限流拒绝。

    Args:
        body: S->C 帧体（不含长度头）

    Returns:
        (seq, offset, total, rejected) 或 None（结构不符）
        - rejected=True 表示该请求被限流拒绝（该次响应无数据，客户端随后自动重发）
        - offset/total 仅正常响应有意义，被拒时返回 0
    """
    header = parse_header(body, server_to_client=True)
    if header is None or not header.known or header.msg_type != MSG_GACHA:
        return None
    seq = header.seq_echo  # = 请求 seq % SEQ_MOD，与 track_request 登记键同维度
    rejected = header.result == RESULT_REJECTED and len(body) == 8
    if rejected:
        logger.info(f"切池响应被限流拒绝 (seq=0x{seq:02x})，该次响应无数据，客户端将自动重发")
        return seq, 0, 0, True
    # 正常响应：业务体 field1 标签 0x08 起，varint offset 回显。offset 是
    # varint：深翻页 offset>=130 为双字节编码，不能按单字节读（曾按单字节读
    # 导致深翻页响应全部解析失败、回退 current_view、total 丢失）。
    # total 跟在 offset varint 之后的 0x10 标签处。
    payload = body[header.payload_offset:]
    if len(payload) < 4 or payload[0] != 0x08:
        return None
    offset, after_offset = _read_varint(payload, 1)
    if after_offset >= len(payload) or payload[after_offset] != 0x10:
        return None
    total, _ = _read_varint(payload, after_offset + 1)
    return seq, offset, total, False


def _request_fail_reason(body: bytes) -> str:
    """一段 C->S 帧为何无法被 parse_pool_request 解析，返回简短原因（诊断用）。"""
    header = parse_header(body, server_to_client=False)
    if header is None:
        return f"过短/无法解析帧头(仅{len(body)}B)"
    if not header.known:
        return (f"未知信封(类型=0x{header.msg_type:04x} ext=0x{header.ext_id:04x}"
                f"{'' if header.counter == -1 else f' counter=0x{header.counter:02x} 不自洽'})")
    if header.msg_type != MSG_GACHA:
        return "非抽卡消息(如心跳)"
    return "业务体结构不符(无 0x08 pool / 0x10 offset)"


class PoolViewTracker:
    """视图感知层（步②后：**只做视图归属，不做去重判定**）。

    维护：
    - seq -> (view, offset)：请求/响应配对确定每条响应来自哪个视图；offset 供
      被拒响应定位被拒的是哪一页
    - current_view：最近一次请求的 pool_id（"0"=全部卡池，具体池为该池 id）
    - rejected_pages：被限流拒绝的 (view, offset) 列表（该次响应无数据，客户端随后自动重发）

    **去重已不在本类**：跨视图/跨会话重复由「位置对齐」方案处理
    （`analysis/pos_align.py` + `storage/history.merge`，2026-09-13）——B8 之后
    只剩「全部卡池」单一视图，`position_in_batch` 唯一可比，判重按
    `(timestamp, pos − Δ)`；内容比对（旧账本的 SEEN/EXTEND/OVERLAP）已全部退役。
    此前的「key 按份数跨视图配对过滤」在多视图轮换场景会误丢十连内同名份
    （实测 20001 三事件各丢 1 条）。本类现仅提供：
    - 视图归属（resolve_view，溯源标签/日志用）
    - 限流被拒页识别（note_response / rejected_pages）
    - 各视图权威 total（view_totals，完整性校验 + 收尾缺口报告）
    - 已见页集合（seen_pages，收尾缺口报告）
    """

    def __init__(self) -> None:
        """初始化：无已知 seq->视图映射，当前视图未知。"""
        self._seq_to_req: Dict[int, Tuple[str, int]] = {}
        self._current_view: Optional[str] = None
        self._filtered_count: int = 0  # 兼容保留：步②后恒为 0（L1 不再过滤）
        self._rejected_pages: List[Tuple[str, int]] = []  # 被限流拒绝的页
        # 视图 -> 服务器权威总抽数（响应 field2，如 全部卡池 618 / 常驻 260），
        # 供完整性硬校验：已抓条数 < total 即还有窟窿
        self._view_totals: Dict[str, int] = {}
        # ---- 旧账本 B6「翻页方向」信号（2026-09-14 注释保留）----
        # 视图 -> 翻页方向（+1 往旧翻 / -1 往新翻 / 0 未知）。**消歧信号**：
        # 旧 event_ledger 据此判断「首个观察是事件的开头段还是结尾段」——仅靠
        # 内容+位置无法区分（b=0 与 b=cap-len(est) 两种对齐评分相同，2026-09-13
        # 实测），见 协议结构说明 缺口 3（B6 端点规则）。
        # 位置对齐方案下位置直接可比、不再需要方向消歧，故整条信号链路已退役：
        # `_update_direction` / `direction_of` 及其消费方均已注释。保留注释便于将来
        # 需要方向语义时有据可依，也便于回滚旧账本。
        # self._view_direction: Dict[str, int] = {}
        # # 视图 -> 上一次请求的 offset（用于比较得出方向）
        # self._view_last_offset: Dict[str, int] = {}
        # 已成功拿到数据的 (视图, offset) 页集合。**收尾缺口报告**的依据：
        # 与 view_totals 推算的「应有页」相减即得还缺哪几页（被拒页无数据、
        # 不计入，否则缺口会被漏报）。见 missing_pages()。
        self._seen_pages: Set[Tuple[str, int]] = set()

    @property
    def current_view(self) -> Optional[str]:
        """最近一次请求的视图（None 表示尚抓到任何切池请求）。"""
        return self._current_view

    @property
    def filtered_count(self) -> int:
        """累计被跨视图过滤掉的条数。"""
        return self._filtered_count

    def track_request(self, body: bytes) -> Optional[Tuple[int, int]]:
        """处理一条 C->S 请求帧，登记 seq->视图并更新当前视图。

        配对键 = seq % SEQ_MOD（127）：响应只回显该回绕值（seq 127->0x00、
        128->0x01，capture_20260817_214949.pcap 逐帧验证；早期文档误记为
        「低字节」，seq>=127 的长会话曾因此配对全空、只能靠 current_view
        兜底）。窗口内未完成请求数远达不到 127，碰撞可忽略，见方案 §6。

        Args:
            body: C->S 帧体（不含长度头，含前 4 字节 seq）

        Returns:
            (pool_id, offset) 或 None（非切池/翻页请求）。
            pool_id 用作视图标识（"0"=全部卡池）。
        """
        parsed = parse_pool_request(body)
        if parsed is None:
            # 诊断采集：抓包监听端口上收到的 C->S 完整帧若无法按切池/翻页结构解析，
            # 记一条（长度+帧头 hex+原因）。配合 main_window 的 request_debug.log
            # （每个 C->S 帧的完整 hex），用于确认「切池/翻页请求究竟是没发出（缓
            # 存省请求）还是发出了但结构对不上/流重组失步」。
            logger.info(
                "未识别的 C->S 请求帧 len=%d head=%s %s",
                len(body), body[:16].hex(), _request_fail_reason(body),
            )
            return None
        seq, pool_id, offset = parsed
        # 视图 = pool_id + 列表类型：同为 pool_id=0 的「全部卡池」与按契约类型
        # 聚合的汇总页（遴选/活动）靠 list_type 区分，否则会被当作同一视图而
        # 漏掉跨视图去重。类型字段缺失时回退为纯 pool_id（老协议行为不变）。
        list_type = parse_pool_list_type(body)
        view = make_view(pool_id, list_type)
        # seq % SEQ_MOD 作配对键，与响应回显维度对齐；同时记 offset 供被拒响应
        # 定位丢失的页
        self._seq_to_req[seq % SEQ_MOD] = (view, offset)
        self._current_view = view
        # self._update_direction(view, offset)  # 旧账本 B6 方向信号已注释（见 __init__）
        logger.info(
            f"切池/翻页请求 seq=0x{seq:02x} view={view} offset={offset}"
            f" list_type={list_type}"
        )
        return pool_id, offset

    # ---- 旧账本 B6「翻页方向」方法（2026-09-14 注释保留，见 __init__ 说明）----
    # def _update_direction(self, view: str, offset: int) -> None:
    #     """更新该视图的翻页方向：+1 往旧翻 / -1 往新翻 / 0 未知。
    #
    #     判据：与**同视图**上一次请求的 offset 比较。offset 0 为「最新一页」，
    #     故递增 = 由新往旧翻（常规操作）；递减 = 由旧往新翻（跳到末页后往前翻）。
    #
    #     offset 回到 0 视为重新从头部开始（切回该视图/重新进入），按正向记——
    #     否则「从末页切回第一页」会被误判成往新翻。
    #
    #     本方法是**消歧信号**：账本用它判断首个观察是事件的开头段还是结尾段
    #     （B6 端点规则）。方向未知时账本退回按内容评分，不会比现状更糟。
    #     """
    #     last = self._view_last_offset.get(view)
    #     if last is not None and offset != last:
    #         if offset > last:
    #             self._view_direction[view] = 1
    #         else:
    #             self._view_direction[view] = 1 if offset == 0 else -1
    #     self._view_last_offset[view] = offset
    #
    # def direction_of(self, view: Optional[str]) -> int:
    #     """该视图当前的翻页方向：+1 往旧翻 / -1 往新翻 / 0 未知。
    #
    #     **保留但暂无人消费**：旧的账本用它消歧「首个观察是开头段还是结尾段」，
    #     位置对齐方案下不再需要（位置直接可比）。保留采集便于将来需要方向语义时
    #     有据可依，也便于回滚。
    #     """
    #     if view is None:
    #         return 0
    #     return self._view_direction.get(view, 0)

    def resolve_view(self, body: bytes) -> Optional[str]:
        """由响应帧回显的 seq 查出该页所属视图。

        优先用响应回显的 seq 反查 seq->视图；若未登记（请求没被监听到）则回退
        到当前视图；两者都未知则返回 None。

        Args:
            body: S->C 帧体（不含长度头）

        Returns:
            视图标识（pool_id 字符串）或 None
        """
        parsed = parse_pool_response(body)
        if parsed is not None:
            seq, offset, total, rejected = parsed
            if seq in self._seq_to_req:
                view = self._seq_to_req[seq][0]
                logger.info(
                    f"响应 seq=0x{seq:02x} off={offset} total={total} -> 视图 {view}（seq 配对）"
                )
                return view
            # 被拒响应 seq 回显仍可配对；未被拒但 seq 未登记说明请求没抓到
            if rejected:
                logger.info(f"响应被拒 seq=0x{seq:02x} -> 回退当前视图 {self._current_view}")
                return self._current_view
            logger.info(f"响应 seq=0x{seq:02x} 未登记 seq->视图 -> 回退当前视图 {self._current_view}")
        else:
            logger.info(f"响应未解析出 seq -> 回退当前视图 {self._current_view}")
        return self._current_view

    def filter_records(
        self, view: Optional[str], records: List[GachaRecord]
    ) -> Tuple[List[GachaRecord], int]:
        """原样放行（步②：跨视图去重统一由事件账本处理）。

        此前的「key 按份数跨视图配对过滤」已移除——它会误丢十连内同名份
        （2026-09-11 实测 20001 三事件各丢 1 条），且滤剩的残片会骗过账本
        SEEN 判定造成 MERGED 重复（实测 30001 事件 14 条）。签名与返回值
        保持兼容（`(保留记录, 丢弃条数)`，丢弃恒为 0），调用方无需改接线。

        Args:
            view: 该页所属视图（仅作语义占位，不再参与任何判定）
            records: 该页解析出的记录（已过 parser.parse）

        Returns:
            (全部记录, 0)
        """
        return list(records), 0

    @property
    def rejected_pages(self) -> List[Tuple[str, int]]:
        """被限流拒绝的页列表 [(view, offset), ...]，该次响应无数据（客户端随后自动重发补齐）。"""
        return list(self._rejected_pages)

    def note_response(self, body: bytes) -> Optional[Tuple[str, int]]:
        """登记服务器响应：记录权威 total，识别被限流拒绝的响应。

        正常响应：把响应中的 total（field2，服务器权威总抽数）按视图存入
        view_totals（如 全部卡池 618），供完整性校验--已抓条数 < total
        即说明还有页面没拿到；返回 None。

        被拒响应：返回被拒页的 (view, offset)。

        **必须在调用方「无记录提前返回」之前调用**：被拒响应是 8 字节截断帧、
        不含任何记录，若放在 parser.parse 之后按记录数早退就永远检测不到--
        频繁限流导致某页本次无响应时用户无感知（此前 on_packet 正是这种接法，
        parse_pool_response 的 rejected 分支实际不可达）。

        被拒页定位：优先用响应回显 seq 反查请求登记的 (view, offset)；请求没
        被监听到则退回 (current_view, -1)（-1 表示 offset 未知）。
        """
        parsed = parse_pool_response(body)
        if parsed is None:
            return None
        seq, resp_offset, total, rejected = parsed
        view = self._seq_to_req[seq][0] if seq in self._seq_to_req else self._current_view
        if not rejected:
            # 正常响应：记录该视图的服务器权威总抽数（total 恒定，重复覆盖无害），
            # 并登记「这一页已拿到数据」——收尾缺口报告据此与应有页相减。
            if view is not None:
                if total > 0:
                    self._view_totals[view] = total
                if resp_offset >= 0:
                    self._seen_pages.add((view, resp_offset))
            return None
        if view is None:
            view = "?"
        offset = self._seq_to_req[seq][1] if seq in self._seq_to_req else -1
        self._rejected_pages.append((view, offset))
        logger.warning(
            f"切池/翻页被限流拒绝: 视图={view} offset={offset}（该次响应无数据，客户端将自动重发该页）"
        )
        return view, offset

    @property
    def view_totals(self) -> Dict[str, int]:
        """各视图的服务器权威总抽数（视图 "0" = 全部卡池，如 618）。"""
        return dict(self._view_totals)

    @property
    def seen_pages(self) -> Set[Tuple[str, int]]:
        """已成功拿到数据的 `(视图, offset)` 页集合（被限流拒绝的页不计入）。"""
        return set(self._seen_pages)

    def missing_pages(
        self, view: Optional[str] = None
    ) -> List[Tuple[str, List[int], int, int]]:
        """缺页报告 `[(视图, 缺失的 offset 列表, 已见页数, 应有页数), ...]`。

        应由「权威 total + 每页 5 条」（B4）推算应有页，再减去已成功响应的页。
        用途：收尾时明确告诉用户「还缺哪几页」，可直接回翻补齐——这是「仅采纳
        全部卡池」（B8）后失去多视图冗余的唯一兜底（见规则文件 B8 小节）。

        Args:
            view: 只报告该视图；`None` 表示报告全部有 total 的视图。

        Returns:
            按视图标识排序的报告列表；无缺口（或该视图没有权威 total）时为空。
        """
        report: List[Tuple[str, List[int], int, int]] = []
        for v, total in sorted(self._view_totals.items()):
            if view is not None and v != view:
                continue
            expected = expected_page_offsets(total)
            if not expected:
                continue
            seen = {o for vv, o in self._seen_pages if vv == v}
            expected_set = set(expected)
            missing = [o for o in expected if o not in seen]
            if missing:
                report.append((v, missing, len(seen & expected_set), len(expected)))
        return report

    def reset(self) -> None:
        """清空状态（账号切换/清空时复用）。"""
        self._seq_to_req.clear()
        self._current_view = None
        self._filtered_count = 0
        self._rejected_pages.clear()
        self._view_totals.clear()
        # self._view_direction.clear()      # 旧账本 B6 方向信号已注释（见 __init__）
        # self._view_last_offset.clear()    # 旧账本 B6 方向信号已注释（见 __init__）
        self._seen_pages.clear()