"""账号档案管理 - 多账号注册表与首次迁移。

维护 data/profiles.json 注册表（active_id + 显示顺序 + 各 profile 元信息），
每个账号的历史记录存放在 data/users/<id>.json。首次启动若无注册表，则创建
「默认账号」并把旧 data/history.json 迁移进去。

依赖：models、utils.paths、utils.logger、json
"""
import json
import os
import shutil
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from ..models import Profile
from ..utils.logger import get_logger
from ..utils.paths import get_data_dir

logger = get_logger(__name__)

_DEFAULT_NAME = "默认账号"


class ProfileManager:
    """账号档案管理器。"""

    def __init__(self):
        """初始化，确定路径并执行首次迁移。"""
        self.data_dir = get_data_dir()
        self.users_dir = os.path.join(self.data_dir, "users")
        os.makedirs(self.users_dir, exist_ok=True)
        self.profiles_file = os.path.join(self.data_dir, "profiles.json")
        self._registry: Dict[str, object] = {"active_id": "", "order": [], "profiles": {}}
        self._bootstrap()

    # ---- 启动 / 迁移 ----
    def _bootstrap(self) -> None:
        """首次启动创建默认账号并迁移旧 history.json；否则加载注册表。"""
        if os.path.exists(self.profiles_file):
            self._load()
            return
        default = Profile(id=self._new_id(), name=_DEFAULT_NAME, created_at=self._now())
        self._registry["order"] = [default.id]
        self._registry["profiles"][default.id] = self._profile_dict(default)
        self._registry["active_id"] = default.id
        old = os.path.join(self.data_dir, "history.json")
        if os.path.exists(old):
            self._migrate_old_history(old, default.id)
        self._persist()
        logger.info(f"首次启动：已创建默认账号 {default.id}")

    def _migrate_old_history(self, old_file: str, profile_id: str) -> None:
        """把旧 data/history.json 的记录迁入默认账号，旧文件改名留底。"""
        try:
            with open(old_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            records = payload.get("records", []) if isinstance(payload, dict) else []
        except Exception as e:
            logger.warning(f"读取旧 history.json 失败，跳过迁移: {e}")
            return
        if not records:
            logger.info("旧 history.json 无记录，跳过迁移")
            return
        target = os.path.join(self.users_dir, f"{profile_id}.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"records": records}, f, ensure_ascii=False, indent=2)
        migrated = old_file + ".migrated"
        shutil.move(old_file, migrated)
        logger.info(f"已把旧 history.json（{len(records)} 条）迁入账号 {profile_id}，旧文件留底为 {migrated}")

    # ---- 持久化 ----
    def _load(self) -> None:
        """加载注册表并自愈不一致。"""
        try:
            with open(self.profiles_file, "r", encoding="utf-8") as f:
                reg = json.load(f)
            self._registry = {
                "active_id": reg.get("active_id", ""),
                "order": list(reg.get("order", [])),
                "profiles": {pid: dict(p) for pid, p in reg.get("profiles", {}).items()},
            }
        except Exception as e:
            logger.warning(f"读取 profiles.json 失败，重建: {e}")
            self._registry = {"active_id": "", "order": [], "profiles": {}}
        # order 缺失时按 profiles 重建
        if not self._registry["order"]:
            self._registry["order"] = list(self._registry["profiles"].keys())
        # active_id 失效时回退到第一个
        aid = self._registry["active_id"]
        if not aid or aid not in self._registry["profiles"]:
            self._registry["active_id"] = self._registry["order"][0] if self._registry["order"] else ""

    def _persist(self) -> None:
        with open(self.profiles_file, "w", encoding="utf-8") as f:
            json.dump(self._registry, f, ensure_ascii=False, indent=2)

    # ---- 查询 ----
    def list_profiles(self) -> List[Profile]:
        """按显示顺序返回所有账号。"""
        return [
            self._profile_obj(pid)
            for pid in self._registry["order"]
            if pid in self._registry["profiles"]
        ]

    def get(self, profile_id: str) -> Optional[Profile]:
        p = self._registry["profiles"].get(profile_id)
        return self._profile_obj(profile_id) if p else None

    def active_id(self) -> str:
        """当前激活账号 id，失效时自愈回退到第一个。"""
        aid = self._registry.get("active_id", "")
        if aid and aid in self._registry["profiles"]:
            return aid
        if self._registry["order"]:
            aid = self._registry["order"][0]
            self._registry["active_id"] = aid
            self._persist()
            return aid
        return ""

    def active(self) -> Optional[Profile]:
        """当前激活账号。"""
        return self.get(self.active_id())

    # ---- 变更 ----
    def create(self, name: str) -> Profile:
        """新建账号并加入注册表尾部。"""
        name = (name or "").strip() or _DEFAULT_NAME
        pid = self._new_id()
        prof = Profile(id=pid, name=name, created_at=self._now())
        self._registry["profiles"][pid] = self._profile_dict(prof)
        self._registry["order"].append(pid)
        self._persist()
        logger.info(f"新建账号 {pid}({name})")
        return prof

    def rename(self, profile_id: str, name: str) -> None:
        """重命名账号（name 为空则保持原名）。"""
        p = self._registry["profiles"].get(profile_id)
        if not p:
            return
        new_name = (name or "").strip()
        if new_name:
            p["name"] = new_name
            self._persist()
            logger.info(f"账号 {profile_id} 改名为 {new_name}")

    def delete(self, profile_id: str) -> Optional[str]:
        """删除账号：归档其历史文件后从注册表移除。

        仅剩一个账号时拒绝删除。返回归档文件路径；被拒绝或无文件时返回 None。
        """
        if profile_id not in self._registry["profiles"]:
            return None
        if len(self._registry["profiles"]) <= 1:
            logger.warning("仅剩一个账号，拒绝删除")
            return None
        archive = None
        user_file = os.path.join(self.users_dir, f"{profile_id}.json")
        if os.path.exists(user_file):
            archive = self._unique_archive(profile_id)
            shutil.move(user_file, archive)
        self._registry["order"] = [pid for pid in self._registry["order"] if pid != profile_id]
        del self._registry["profiles"][profile_id]
        if self._registry["active_id"] == profile_id:
            self._registry["active_id"] = self._registry["order"][0] if self._registry["order"] else ""
        self._persist()
        logger.info(f"已删除账号 {profile_id}，历史归档到 {archive}")
        return archive

    def set_active(self, profile_id: str) -> None:
        """切换激活账号。"""
        if profile_id not in self._registry["profiles"]:
            return
        self._registry["active_id"] = profile_id
        self._persist()

    # ---- 辅助 ----
    @staticmethod
    def _new_id() -> str:
        """12 位 hex 账号 id（无下划线/连字符，避免与备份名冲突）。"""
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _now_stamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _unique_archive(self, profile_id: str) -> str:
        """生成不与现有备份冲突的归档文件名（与 history._backup 命名一致）。"""
        stamp = self._now_stamp()
        archive = os.path.join(self.users_dir, f"{profile_id}_{stamp}.json")
        n = 1
        while os.path.exists(archive):
            archive = os.path.join(self.users_dir, f"{profile_id}_{stamp}_{n}.json")
            n += 1
        return archive

    def _profile_dict(self, prof: Profile) -> dict:
        return {"id": prof.id, "name": prof.name, "created_at": prof.created_at}

    def _profile_obj(self, pid: str) -> Profile:
        p = self._registry["profiles"][pid]
        return Profile(id=p["id"], name=p["name"], created_at=p.get("created_at", ""))
