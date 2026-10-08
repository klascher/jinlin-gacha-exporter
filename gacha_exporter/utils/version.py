"""工具版本号解析与比较 — 无第三方依赖。

版本规则（打包更新说明 §11）：
- 基础版号 x.y.z（数字段），缺段按 0 补齐；`0.2.2` 是某条线内的「最小」版本。
- 可选热修后缀 `-YYYYMMDDrcN`：8 位日期 + "rc" + 数字，如 `0.2.2-20260817rc1`。
  有后缀必须严格此格式（见 `is_valid_version`）。
- 比较：先比基础版号（`0.2.3` > 任意 `0.2.2-*`）→ 同基础，未带后缀的 < 带后缀的
  （`0.2.2` 最小，所以 `0.2.2-xxx` 必然比 `0.2.2` 新，能触发用户更新）→
  同基础带后缀再比日期（日期大的大）→ 同日期比 rc。
- 非法/空输入解析为全 0，比较视为最旧，保证「无法判断的版本不触发更新」。

`parse_version` 只返回基础三段（兼容旧语义）；带后缀的完整比较见 `_version_key`，
由 `compare_versions`/`is_newer` 使用。打包脚本 `packaging/pack_release.py` 复用
本模块，保证构建端与客户端比较规则一致。
"""

import re
from typing import Tuple

# x.y.z 可选 -8位日期 rc 数字
_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<date>\d{8})rc(?P<rc>\d+))?$"
)


def is_valid_version(v) -> bool:
    """v 是否为合法版本串（x.y.z 或 x.y.z-YYYYMMDDrcN）。"""
    return isinstance(v, str) and bool(_VERSION_RE.fullmatch(v.strip()))


def parse_base_version(v) -> str:
    """返回去掉后缀的基础版号 x.y.z（PEP 440 安全，供 pyproject.toml 用）。"""
    if not isinstance(v, str):
        return ""
    m = _VERSION_RE.fullmatch(v.strip())
    if not m:
        return ""
    return f"{m.group('major')}.{m.group('minor')}.{m.group('patch')}"


def parse_version(v) -> Tuple[int, ...]:
    """解析基础 x.y.z 三段为数字元组（不含后缀）。

    "0.2.0" -> (0,2,0)；"1.2" 缺段补 0 -> (1,2,0)；非法/空 -> (0,0,0)。
    合法串（含热修后缀）直接取基础三段；否则回退宽松「按 . 切、缺段补 0」旧语义。
    """
    if not isinstance(v, str):
        return (0, 0, 0)
    v = v.strip()
    m = _VERSION_RE.fullmatch(v)
    if m:
        return (
            int(m.group("major")),
            int(m.group("minor")),
            int(m.group("patch")),
        )
    nums = []
    for part in v.split(".")[:3]:
        nums.append(int(part) if part.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _version_key(v) -> Tuple[int, ...]:
    """比较键：(maj,minor,patch, tier, date, rc)。tier=1 表示带热修后缀。"""
    if not isinstance(v, str):
        return (0, 0, 0, 0, 0, 0)
    v = v.strip()
    m = _VERSION_RE.fullmatch(v)
    if not m:
        return (0, 0, 0, 0, 0, 0)
    maj = int(m.group("major"))
    minor = int(m.group("minor"))
    patch = int(m.group("patch"))
    if m.group("date") is None:
        return (maj, minor, patch, 0, 0, 0)
    return (maj, minor, patch, 1, int(m.group("date")), int(m.group("rc")))


def compare_versions(a, b) -> int:
    """比较两个版本号：a<b -> -1；a>b -> 1；相等 -> 0。热修后缀纳入比较。"""
    pa, pb = _version_key(a), _version_key(b)
    return (pa > pb) - (pa < pb)


def is_newer(a, b) -> bool:
    """a 是否严格新于 b。"""
    return compare_versions(a, b) > 0