"""
配置加载模块
"""

import os
import sys
import yaml
from typing import List, Dict, Optional
from dataclasses import dataclass, field


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = "logs/recycle_guard.log"
    max_days: int = 90


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8088
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class MirrorCleanupConfig:
    enabled: bool = True
    interval: int = 3600
    grace_period: int = 300


@dataclass
class SyncConfig:
    """定期同步配置 - 主动扫描监控目录并备份变更"""
    enabled: bool = True
    interval: int = 30  # 扫描间隔（秒）


@dataclass
class DatabaseConfig:
    """MySQL 数据库配置 - 存储备份和回收站元信息"""
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = "123456"
    database: str = "file_recycle_guard"


@dataclass
class Config:
    watch_paths: List[str] = field(default_factory=list)

    def find_watch_root(self, abs_path: str) -> Optional[str]:
        """
        查找包含 abs_path 的监控根目录（多个匹配时取最长匹配）。
        找不到返回 None。
        """
        try:
            norm_path = os.path.normcase(os.path.abspath(abs_path))
        except (OSError, ValueError):
            return None
        best = None
        best_len = -1
        for wp in self.watch_paths:
            wp_norm = os.path.normcase(os.path.abspath(wp))
            if norm_path == wp_norm or norm_path.startswith(wp_norm + os.sep):
                if len(wp_norm) > best_len:
                    best, best_len = wp, len(wp_norm)
        return best

    backup_dir: str = "backup_mirror"
    recycle_dir: str = "recycle_bin"
    retention_days: int = 30
    exclude_patterns: List[str] = field(default_factory=lambda: [
        "~$*", "*.tmp", "*.lock", "Thumbs.db"
    ])
    exclude_dirs: List[str] = field(default_factory=lambda: [
        "$RECYCLE.BIN", "System Volume Information"
    ])
    log: LogConfig = field(default_factory=LogConfig)
    web: WebConfig = field(default_factory=WebConfig)
    mirror_cleanup: MirrorCleanupConfig = field(default_factory=MirrorCleanupConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)


def get_exe_dir() -> str:
    """获取程序所在目录

    使用 sys.argv[0] 定位 exe 所在目录。
    开发模式下就是项目根目录。
    """
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _resolve_config_path(config_path: str) -> str:
    """解析配置文件路径

    优先级：
    1. 绝对路径（直接使用）
    2. exe 所在目录（打包模式下 config.yaml 与 exe 同目录）
    3. CWD 相对路径
    4. 脚本所在目录（开发模式）
    """
    # 绝对路径直接返回
    if os.path.isabs(config_path) and os.path.exists(config_path):
        return config_path
    # 1. exe 所在目录
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        candidate = os.path.join(exe_dir, os.path.basename(config_path))
        if os.path.exists(candidate):
            return candidate
    except Exception:
        pass
    # 2. CWD 相对路径
    cwd_candidate = os.path.abspath(config_path)
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    # 3. 脚本所在目录（开发模式）
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_dir = os.path.dirname(os.path.dirname(script_dir))
        candidate = os.path.join(project_dir, os.path.basename(config_path))
        if os.path.exists(candidate):
            return candidate
    except Exception:
        pass
    # 4. 返回原路径（由调用方处理不存在的情况）
    return config_path


def load_config(config_path: str = "config.yaml") -> Config:
    """从 YAML 文件加载配置"""
    config_path = _resolve_config_path(config_path)
    if not os.path.exists(config_path):
        print(f"配置文件 {config_path} 不存在，使用默认配置")
        return Config()

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = Config()

    if "watch_paths" in raw:
        config.watch_paths = raw["watch_paths"]
    if "backup_dir" in raw:
        config.backup_dir = raw["backup_dir"]
    if "recycle_dir" in raw:
        config.recycle_dir = raw["recycle_dir"]
    if "retention_days" in raw:
        config.retention_days = raw["retention_days"]
    if "exclude_patterns" in raw:
        config.exclude_patterns = raw["exclude_patterns"]
    if "exclude_dirs" in raw:
        config.exclude_dirs = raw["exclude_dirs"]

    if "log" in raw:
        log_raw = raw["log"]
        if "level" in log_raw:
            config.log.level = log_raw["level"]
        if "file" in log_raw:
            config.log.file = log_raw["file"]
        if "max_days" in log_raw:
            config.log.max_days = raw["log"]["max_days"]

    if "web" in raw:
        web_raw = raw["web"]
        if "enabled" in web_raw:
            config.web.enabled = web_raw["enabled"]
        if "host" in web_raw:
            config.web.host = web_raw["host"]
        if "port" in web_raw:
            config.web.port = web_raw["port"]
        if "username" in web_raw:
            config.web.username = web_raw["username"]
        if "password" in web_raw:
            config.web.password = web_raw["password"]

    if "mirror_cleanup" in raw:
        mc_raw = raw["mirror_cleanup"]
        if "enabled" in mc_raw:
            config.mirror_cleanup.enabled = mc_raw["enabled"]
        if "interval" in mc_raw:
            config.mirror_cleanup.interval = mc_raw["interval"]
        if "grace_period" in mc_raw:
            config.mirror_cleanup.grace_period = mc_raw["grace_period"]

    if "sync" in raw:
        sync_raw = raw["sync"]
        if "enabled" in sync_raw:
            config.sync.enabled = sync_raw["enabled"]
        if "interval" in sync_raw:
            config.sync.interval = sync_raw["interval"]

    if "database" in raw:
        db_raw = raw["database"]
        if "host" in db_raw:
            config.database.host = db_raw["host"]
        if "port" in db_raw:
            config.database.port = db_raw["port"]
        if "user" in db_raw:
            config.database.user = db_raw["user"]
        if "password" in db_raw:
            config.database.password = db_raw["password"]
        if "database" in db_raw:
            config.database.database = db_raw["database"]

    return config
