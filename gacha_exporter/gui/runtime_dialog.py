"""功能组件（外部 runtime 模块）更新对话框。

组件按重依赖边界拆成多个模块（``ocr`` / ``training`` / ``team_builder``），
各自独立版本与签名；本对话框只列出「有变化」的模块并逐个下载安装。

两种入口：
- 启动强制检查（``force=True``）：已开启功能所需的组件缺失/有更新时必须处理。
  卡池识别可停用（停用后待装模块若不再被需要，才允许跳过）；练度识别
  不可停用，只能下载成功后自动重启，或下载失败后继续运行（功能暂不可用）。
- 「更多 → 功能组件…」手动打开：显示全部模块状态，可关闭稍后再来。

只下载不上传；安装只切换对应模块的 ``current.txt``，随后自动重启，
由 bootstrap 挂载生效。抓包中禁止更新由调用方守卫（见 MainWindow）。
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .. import features
from ..loaders.remote_data import resolve_runtime_url
from ..models import RuntimeInfo, VersionInfo
from ..utils import runtime_pack
from ..utils import host_env
from ..utils.logger import get_logger
from .runtime_downloader import RuntimeDownloader

logger = get_logger(__name__)


def _display_name(info: RuntimeInfo) -> str:
    return info.label or runtime_pack.module_label(info.module)


def _size_text(size: int) -> str:
    if not size:
        return "大小以服务器为准"
    return f"约 {size / 1048576:.1f} MB"


@dataclass(frozen=True)
class ComponentState:
    """一个模块的本地状态（供 UI/启动判断，纯逻辑可测）。"""

    info: RuntimeInfo
    local_version: str
    needs_install: bool
    status: str


def module_states(
    info: Optional[VersionInfo], modules: Optional[Sequence[str]] = None
) -> List[ComponentState]:
    """列出版本信息中的模块状态；modules 为空表示全部模块。"""
    if info is None:
        return []
    names = list(modules) if modules else list(info.runtimes)
    states: List[ComponentState] = []
    for name in names:
        runtime = info.runtimes.get(name)
        if runtime is None:
            continue
        local = runtime_pack.current_version(name)
        needs = runtime_pack.update_available(name, runtime.version)
        if needs:
            status = "未安装" if not local else f"可更新（本地 {local}）"
        else:
            status = f"已是最新（{local}）"
        states.append(ComponentState(runtime, local, needs, status))
    return states


def pending_modules(
    info: Optional[VersionInfo], modules: Optional[Sequence[str]] = None
) -> List[RuntimeInfo]:
    """需要安装/更新的模块（保持服务端顺序）。

    0.4.0 旧启动器不支持 runtime 挂载：永远返回空表，避免下载后无法生效。
    """
    if host_env.is_legacy_launcher():
        return []
    return [state.info for state in module_states(info, modules) if state.needs_install]


def needs_install(
    info: Optional[VersionInfo], modules: Optional[Sequence[str]] = None
) -> bool:
    """是否存在未安装/可更新的模块。"""
    return bool(pending_modules(info, modules))


def skippable_features(
    info: Optional[VersionInfo], modules: Optional[Sequence[str]] = None
) -> List[str]:
    """停用后即可跳过本次安装的已开启功能（空表=不能靠停用跳过）。"""
    pending = [runtime.module for runtime in pending_modules(info, modules)]
    if not features.can_skip_by_disabling(pending):
        return []
    return features.disable_features_for(pending)


def _pending_feature_labels(pending_names: Sequence[str], *, can_disable: bool) -> str:
    """依赖待装模块、且（不）可停用的已开启功能名（用于对话框说明）。"""
    pending = set(pending_names)
    labels = []
    for name, feature in features.FEATURES.items():
        if feature.can_disable is not can_disable or not features.enabled(name):
            continue
        if pending.intersection(feature.requires_modules):
            labels.append(feature.label)
    return "、".join(labels)


class RuntimeUpdateDialog(QDialog):
    """功能组件下载/更新对话框（可一次安装多个模块）。

    安装成功后调用方应立即重启（``restart_requested``）；强制模式下用户
    只能选择更新、停用相关功能（仅当确实能跳过）或退出，不能用 X/Esc 绕过。
    """

    def __init__(self, info: VersionInfo, manual: bool = False,
                 modules: Optional[Sequence[str]] = None, parent=None,
                 force: bool = False):
        super().__init__(parent)
        self.info = info
        self.manual = manual
        self.force = bool(force)
        self.modules = list(modules) if modules else None
        self._worker: Optional[RuntimeDownloader] = None
        self._downloading = False
        self._queue: List[RuntimeInfo] = []
        self._failed: List[str] = []
        self._done = 0
        self._total = 0
        self._allow_close = False
        self._secondary_action = "close"
        self._skip_features: List[str] = []
        self._can_skip = False
        self.installed: List[str] = []  # 本次成功安装的模块名
        self.disabled_features: List[str] = []  # 因跳过更新而停用的功能
        self.restart_requested = False
        self.exit_requested = False
        self.setWindowTitle("功能组件")
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        states = module_states(self.info, self.modules)
        pending = [s for s in states if s.needs_install]
        pending_names = [s.info.module for s in pending]
        self._can_skip = self.force and features.can_skip_by_disabling(pending_names)
        if self._can_skip:
            self._skip_features = features.disable_features_for(pending_names)

        title = QLabel("必须更新功能组件" if self.force and pending else "功能组件")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        if not states:
            msg = "服务器未配置功能组件。核心抽卡抓包与导出不受影响。"
        elif pending and self.force:
            msg = (
                "已开启的功能需要以下组件的最新版本（只下载到本机，"
                "不采集、不上传任何信息）："
            )
        elif pending:
            msg = (
                "以下组件需要安装/更新（只下载到本机，不采集、不上传任何信息）："
            )
        else:
            msg = "全部组件已就绪，无需更新。识别过程只读取本机屏幕，不保存截图、不上传。"
        desc = QLabel(msg)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        for state in states:
            row = QLabel(
                f"• {_display_name(state.info)}  {state.info.version}"
                f"（{_size_text(state.info.size)}）—— {state.status}"
            )
            row.setWordWrap(True)
            layout.addWidget(row)

        restart_text = (
            "安装完成后工具会自动重启"
            if host_env.supports_auto_restart()
            else "安装完成后请手动重启工具"
        )
        if pending and self.force:
            if self._can_skip:
                labels = _pending_feature_labels(pending_names, can_disable=True)
                hint = QLabel(
                    f"也可以停用「{labels}」并继续——核心抓包、统计与导出不受影响；"
                    "下次重新开启时会再次要求更新组件。"
                )
            else:
                labels = _pending_feature_labels(pending_names, can_disable=False)
                hint = QLabel(
                    f"「{labels}」不可停用：必须下载组件，{restart_text}；"
                    "若下载/安装失败，可选择继续运行，但该功能暂不可用，下次启动仍会要求更新。"
                )
            hint.setStyleSheet("color: #b9770e;")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        elif self.manual and pending:
            hint = QLabel(f"提示：{restart_text}。")
            hint.setStyleSheet("color: #b9770e;")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        row = QHBoxLayout()
        self.btn_install = QPushButton(
            "下载并更新" if self.force else "下载并安装"
        )
        self.btn_secondary = QPushButton()
        self.btn_cancel = QPushButton("取消下载")
        self.btn_cancel.setVisible(False)
        row.addWidget(self.btn_install)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_secondary)
        row.addStretch()
        layout.addLayout(row)

        if self.force and pending:
            if self._can_skip:
                self._set_secondary("停用相关功能并继续", "skip")
            else:
                self._set_secondary("退出程序", "exit")
        else:
            self._set_secondary("关闭", "close")

        self.btn_install.setVisible(bool(pending))
        self.btn_install.clicked.connect(self._start_install)
        self.btn_cancel.clicked.connect(self._cancel_download)
        self.btn_secondary.clicked.connect(self._on_secondary)

    def _set_secondary(self, text: str, action: str, enabled: bool = True) -> None:
        self.btn_secondary.setText(text)
        self.btn_secondary.setEnabled(enabled)
        self._secondary_action = action

    # ---- 下载队列 ----

    def _start_install(self) -> None:
        self._queue = pending_modules(self.info, self.modules)
        self._failed = []
        self._done = 0
        self._total = len(self._queue)
        self.installed = []
        self._downloading = True
        self.btn_install.setEnabled(False)
        self.btn_secondary.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._start_next()

    def _start_next(self) -> None:
        if not self._queue:
            self._finish_all()
            return
        runtime = self._queue.pop(0)
        label = _display_name(runtime)
        self.status.setText(
            f"正在准备 {label} {runtime.version}（{self._done + 1}/{self._total}）"
        )
        self.progress.setValue(0)

        url = resolve_runtime_url(runtime.module, runtime.url)
        if not url:
            self._failed.append(f"{label}：服务器未提供下载地址")
            self._done += 1
            self._start_next()
            return
        if not runtime.sha256 or not runtime.signature:
            # fail-safe：缺校验信息一律不下载（避免白下大包）
            self._failed.append(f"{label}：组件包缺少校验信息，已拒绝安装")
            self._done += 1
            self._start_next()
            return
        self._worker = RuntimeDownloader(
            runtime.module,
            url,
            runtime.version,
            runtime.sha256,
            runtime.signature,
            parent=self,
        )
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_finished(self, ok: bool, msg: str) -> None:
        worker = self._worker
        self._worker = None
        self._done += 1
        if ok:
            self.status.setText(msg)
            if worker is not None:
                self.installed.append(worker.module)
        else:
            self._failed.append(msg)
        self._start_next()

    def _finish_all(self) -> None:
        self._downloading = False
        self.btn_cancel.setVisible(False)
        if self._failed:
            self.progress.setVisible(False)
            self.btn_install.setEnabled(True)
            self.btn_install.setText("重试下载")
            QMessageBox.warning(
                self, "组件更新未完成",
                "以下组件未能安装：\n" + "\n".join(self._failed)
                + "\n\n可重试；已成功的组件会在下次启动生效。",
            )
            if self.force:
                # 停用选择须按「剩余待装模块」重算：部分模块可能已装好
                remaining = [r.module for r in pending_modules(self.info, self.modules)]
                self._can_skip = features.can_skip_by_disabling(remaining)
                self._skip_features = (
                    features.disable_features_for(remaining) if self._can_skip else []
                )
                if self._can_skip:
                    self._set_secondary("停用相关功能并继续", "skip")
                else:
                    self._set_secondary(self._continue_label(remaining), "continue")
            else:
                self._set_secondary("关闭", "close")
            return
        # 安装成功：交给调用方立即重启（旧进程退出后新进程挂载新组件）。
        # API<3 的旧壳不认识 --restart-after，调用方会转成「请手动重启」。
        self.restart_requested = True
        self.status.setText(
            "组件已安装，正在重启工具…"
            if host_env.supports_auto_restart()
            else "组件已安装，请手动重启工具后生效。"
        )
        self._allow_close = True
        self.accept()

    @staticmethod
    def _continue_label(pending_names: Sequence[str]) -> str:
        """下载失败后「继续运行」的按钮文案（区分功能是否完全不可用）。"""
        if any(not runtime_pack.current_version(name) for name in pending_names):
            return "继续运行（相关功能暂不可用）"
        return "继续运行（使用当前版本）"

    def _cancel_download(self) -> None:
        if self._worker is not None:
            self.btn_cancel.setEnabled(False)
            self._worker.cancel()

    def _on_secondary(self) -> None:
        action = self._secondary_action
        if action == "skip":
            for name in self._skip_features:
                features.set_enabled(name, False)
                logger.info(f"用户停用功能 {name} 以跳过组件更新")
            self.disabled_features = list(self._skip_features)
            self._allow_close = True
            self.reject()
        elif action == "exit":
            self.exit_requested = True
            self._allow_close = True
            self.reject()
        elif action == "continue":
            # 下载失败后的唯一放行路径：本次不更新，下次启动继续强制
            self.accept()
        else:
            self._allow_close = True
            self.reject()

    def reject(self) -> None:  # noqa: D401 —— 覆盖 QDialog.reject
        """强制模式下 X/Esc 不得绕过（必须显式更新/停用/退出）。"""
        if self.force and not self._allow_close:
            self.status.setText("请选择更新组件、停用相关功能或退出程序。")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        """下载中不允许关闭（避免线程随对话框销毁）；先点「取消下载」。"""
        if self._downloading:
            event.ignore()
            return
        if self.force and not self._allow_close:
            event.ignore()
            self.status.setText("请选择更新组件、停用相关功能或退出程序。")
            return
        super().closeEvent(event)
