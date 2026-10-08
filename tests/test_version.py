"""版本号规则单测 — 基础版号 + 可选热修后缀 -YYYYMMDDrcN。

规则（打包更新说明 §11）：
  先比基础版号（0.2.3 > 任意 0.2.2-*）→ 同基础无后缀 < 有后缀（0.2.2 最小）→
  有后缀比日期 → 同日期比 rc。打包脚本与客户端共用本模块，须保持该 Ordering。
"""
import pytest

from gacha_exporter.utils.version import (
    compare_versions,
    is_newer,
    is_valid_version,
    parse_base_version,
    parse_version,
)


def c(a, b):
    return compare_versions(a, b)


class TestParse:
    def test_plain(self):
        assert parse_version("0.2.2") == (0, 2, 2)
        assert parse_version("1.2") == (1, 2, 0)  # 缺段补 0
        assert parse_version("0") == (0, 0, 0)

    def test_illegal_is_oldest(self):
        assert parse_version("") == (0, 0, 0)
        assert parse_version("垃圾") == (0, 0, 0)
        assert parse_version(None) == (0, 0, 0)


class TestCompare:
    def test_x_y_z_basic(self):
        assert c("0.2.3", "0.2.2") == 1
        assert c("0.2.2", "0.2.2") == 0
        assert c("0.2.1", "0.2.2") == -1

    def test_base_dominates_suffix(self):
        # 0.2.3 > 任意 0.2.2-*（修复攒够发正式版）
        assert c("0.2.3", "0.2.2-20991231rc99") == 1
        assert c("0.2.2-20260817rc1", "0.2.3") == -1

    def test_unsuffixed_is_smallest_in_line(self):
        # 同基础：0.2.2 最小，任意后缀都比它新（能触发已装 0.2.2 的用户更新）
        assert c("0.2.2", "0.2.2-20260817rc1") == -1
        assert c("0.2.2-20260817rc1", "0.2.2") == 1

    def test_date_then_rc(self):
        assert c("0.2.2-20260817rc1", "0.2.2-20260816rc9") == 1  # 日期大优先
        assert c("0.2.2-20260817rc1", "0.2.2-20260817rc2") == -1  # 同日比 rc
        assert c("0.2.2-20260817rc2", "0.2.2-20260817rc2") == 0

    def test_illegal_is_oldest(self):
        assert c("垃圾", "0.2.2") == -1
        assert is_newer("0.2.2-20260817rc1", "0.2.2") is True


class TestValidity:
    @pytest.mark.parametrize("s,exp", [
        ("0.2.2", True),
        ("0.2.2-20260817rc1", True),
        ("0.2.2-20260817", False),       # 缺 rc
        ("0.2.2abc", False),              # 乱后缀
        ("0.2.2-2026081rc1", False),      # 日期只 7 位
        ("0.2.2-20260817rc1.1", False),   # 多一段
        ("0.2", False),                   # 少一段
    ])
    def test_validity(self, s, exp):
        assert is_valid_version(s) is exp


class TestBase:
    def test_base_strips_suffix(self):
        assert parse_base_version("0.2.2-20260817rc1") == "0.2.2"
        assert parse_base_version("0.2.2") == "0.2.2"