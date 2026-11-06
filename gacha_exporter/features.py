"""功能注册表：核心功能 / 扩展功能，以及运行时组件依赖门控。

分层原则（2026-10-21 用户定）：
- **核心**：抽卡抓包、统计、导出、卡池信息视觉识别——必须随基础包提供；
- **扩展**：角色练度识别、排轴辅助——默认关闭，玩家不开就不加载不解析；
- **运行时组件**：按重依赖边界拆成独立模块（``runtime/<模块>/``，见
  ``utils.runtime_pack``）：``ocr`` 视觉识别栈、``training`` 练度资产、
  ``team_builder`` 排轴依赖。功能按 ``requires_modules`` 声明依赖，缺失时
  降级为不可用并提示，不阻塞核心启动。

扩展功能的开关写入 ``data/settings.json``；UI 接线在功能实际接入时再做
（练度/排轴尚未移植进 app.zip，当前仅登记注册表与持久化）。
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .storage import settings
from .utils import host_env
from .utils.runtime_pack import module_available, module_label


@dataclass(frozen=True)
class Feature:
    """一个可开关的功能。

    ``requires_modules`` 为空表示不依赖外部组件；否则全部模块就绪才可用。
    ``can_disable``：组件更新不满足时，是否允许用户停用该功能来跳过安装。
    2026-10-26 用户定：卡池识别可停用；练度识别（training 线）一旦开启
    就必须更新组件，不能靠停用绕过（只允许下载失败后继续运行）。
    """

    key: str
    label: str
    default: bool
    requires_modules: Tuple[str, ...] = ()
    core: bool = False
    description: str = ""
    can_disable: bool = True

    @property
    def requires_runtime(self) -> bool:
        """是否依赖外部功能组件（兼容旧字段语义）。"""
        return bool(self.requires_modules)


FEATURES: Dict[str, Feature] = {
    "recognition": Feature(
        key="recognition_enabled",
        label="识别未知角色（仅截本机屏幕）",
        default=True,
        requires_modules=("ocr",),
        core=True,
        description="卡池信息视觉识别；只截本机屏幕、零上行",
    ),
    "training": Feature(
        key="training_enabled",
        label="角色练度识别",
        default=False,
        requires_modules=("ocr", "training"),
        description="扩展功能；默认关闭，不开启则不获取不解析",
        # 用户已开启的练度识别不许用「停用」绕过组件更新；失败时仅允许
        # 「继续运行，但该功能暂不可用」，下次启动继续强制。
        can_disable=False,
    ),
    "team_builder": Feature(
        key="team_builder_enabled",
        label="排轴辅助",
        default=False,
        requires_modules=("ocr", "team_builder"),
        description="扩展功能；默认关闭",
    ),
}


def get(name: str) -> Optional[Feature]:
    """按注册名取功能；未注册返回 None。"""
    return FEATURES.get(name)


def enabled(name: str) -> bool:
    """功能开关状态（注册表缺省值）。"""
    feature = FEATURES.get(name)
    if feature is None:
        return False
    return settings.load_bool_setting(feature.key, feature.default)


def set_enabled(name: str, value: bool) -> None:
    """持久化功能开关；未注册名忽略。"""
    feature = FEATURES.get(name)
    if feature is None:
        return
    settings.save_bool_setting(feature.key, bool(value))


def modules_for(names: Iterable[str]) -> List[str]:
    """一组功能所需的组件模块（去重、保持顺序）；未注册功能忽略。"""
    out: List[str] = []
    for name in names:
        feature = FEATURES.get(name)
        if feature is None:
            continue
        for module in feature.requires_modules:
            if module not in out:
                out.append(module)
    return out


def enabled_modules() -> List[str]:
    """当前已开启功能所需的组件模块。"""
    return modules_for(name for name in FEATURES if enabled(name))


def unskippable_modules() -> List[str]:
    """已开启且不可停用的功能所需模块（组件更新不能靠停用功能绕过）。"""
    return modules_for(
        name for name in FEATURES
        if not FEATURES[name].can_disable and enabled(name)
    )


def disable_features_for(modules: Iterable[str]) -> List[str]:
    """停用后能移除待装模块依赖的已开启功能（仅含可停用功能）。

    调用方须先用 :func:`can_skip_by_disabling` 确认跳过确实可行——若某模块
    同时被不可停用功能依赖，停用这些功能也不能避免安装。
    """
    pending = set(modules)
    return [
        name for name, feature in FEATURES.items()
        if feature.can_disable
        and enabled(name)
        and pending.intersection(feature.requires_modules)
    ]


def can_skip_by_disabling(modules: Iterable[str]) -> bool:
    """停用所有相关可停用功能后，是否不再需要这些待装模块。"""
    pending = set(modules)
    if not pending:
        return True
    return not pending.intersection(unskippable_modules())


def available(name: str) -> bool:
    """功能当前是否可运行（依赖的组件模块是否就绪）。"""
    feature = FEATURES.get(name)
    if feature is None:
        return False
    return all(module_available(module) for module in feature.requires_modules)


def unavailable_reason(name: str) -> str:
    """功能不可用的用户可读原因；可用返回 ""。"""
    feature = FEATURES.get(name)
    if feature is None:
        return "功能未注册"
    if feature.requires_modules and host_env.is_legacy_launcher():
        # 0.4.0 旧壳没有 runtime 挂载能力：不要引导用户下载组件（装完也不会生效）。
        return host_env.legacy_upgrade_message()
    missing = [
        module for module in feature.requires_modules
        if not module_available(module)
    ]
    if not missing:
        return ""
    names = "、".join(module_label(module) for module in missing)
    return f"缺少{names}，请下载/更新组件（安装完成后工具会自动重启）。"
