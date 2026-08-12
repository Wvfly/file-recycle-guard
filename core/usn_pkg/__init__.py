"""
USN Journal 变更监控模块 - 基于 NTFS USN Journal 的零遗漏文件变更检测。

这是 core/usn/ 子包的入口，导出所有公共接口。

架构定位（方案第一节）：
- USN Journal = 增量变更检测的主通道（source of truth）
- watchdog = 低延迟辅助通道（accelerator）
- Full Scan = 最终 reconciliation

优势：
- 零事件遗漏：USN Journal 由 NTFS 内核维护
- 低开销：定期轮询 Journal，不需要为每个目录注册监听器
- 支持断线追补：持久化 checkpoint，重启后自动补齐
- 全变更类型：创建、修改、删除、重命名

注意：读取 USN Journal 需要管理员权限。
"""

import os
import fnmatch
import threading
import time
from typing import List, Dict, Callable, Optional, Tuple
from dataclasses import dataclass, field

# 子模块导出
from .record import (
    # 常量
    USN_REASON_DATA_OVERWRITE, USN_REASON_DATA_EXTEND,
    USN_REASON_DATA_TRUNCATION, USN_REASON_FILE_CREATE,
    USN_REASON_FILE_DELETE, USN_REASON_RENAME_OLD_NAME,
    USN_REASON_RENAME_NEW_NAME, USN_REASON_CLOSE,
    USN_REASON_CONTENT_MODIFIED, USN_REASON_MASK,
    # 结构体
    USN_RECORD_V3, USN_JOURNAL_DATA_V2, READ_USN_JOURNAL_DATA_V1,
    # 辅助函数
    open_volume_handle, get_volume_for_path, get_full_path_by_frn,
    nt_to_dos_path, align8,
)

from .checkpoint import (
    JournalStatus, CheckpointInfo, UsnCheckpointStore,
)

from .journal import (
    JournalInfo, JournalHealthInfo,
    query_journal_info, create_journal, compute_health,
)

from .reader import (
    UsnEvent, UsnJournalReader, ReadResult, ReadRecordsResult,
)

from .path_resolver import (
    FrnPathCache, DirectoryIdentityCache, DirIdentity,
)


# ═══════════════════════════════════════════════════════════════
# USN Journal 监控器（整合所有卷的读取器）
# ═══════════════════════════════════════════════════════════════

class UsnJournalMonitor:
    """
    USN Journal 监控器，整合所有卷的读取器，统一轮询和事件分发。

    工作流程：
    1. 启动时为每个监控路径所在的卷创建一个 UsnJournalReader
    2. 定期（默认 1 秒）轮询每个卷的 USN Journal
    3. 读取新记录 -> 解析路径 -> 过滤 -> 去重 -> 分发事件
    4. 持久化 checkpoint（与事件 commit 绑定）
    5. 检测 journal gap / reset -> 触发 reconciliation 回调

    增强（相比原 core/usn.py）：
    - 使用 SQLite checkpoint 替代 JSON
    - 返回 ReadRecordsResult 包含状态信息
    - 支持 reconciliation 回调（gap/reset 时触发）
    """

    DEDUP_WINDOW_SEC = 3.0

    def __init__(self, watch_paths: List[str],
                 checkpoint_db: str = ".usn_state/usn_checkpoint.db",
                 poll_interval: float = 1.0,
                 buffer_size_mb: int = 4,
                 max_records_per_read: int = 10000):
        self.watch_paths: List[str] = [os.path.abspath(p) for p in watch_paths]
        self.poll_interval = poll_interval
        self.buffer_size_mb = buffer_size_mb
        self.max_records_per_read = max_records_per_read

        self._checkpoint_store = UsnCheckpointStore(checkpoint_db)
        self._readers: Dict[str, UsnJournalReader] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callbacks: Dict[str, List[Callable]] = {
            "on_created": [],
            "on_modified": [],
            "on_deleted": [],
            "on_renamed": [],
            "on_journal_gap": [],     # 新增：gap 检测回调
            "on_journal_reset": [],   # 新增：reset 检测回调
        }
        # 去重跟踪
        self._recent_events: Dict[Tuple[str, str], Tuple[float, int]] = {}
        self.logger = None

        # 统计
        self.stats = {
            "total_records_read": 0,
            "total_events_dispatched": 0,
            "total_duplicates_skipped": 0,
            "last_poll_time": 0.0,
            "last_poll_records": 0,
        }

    def on(self, event_type: str, callback: Callable):
        """注册事件回调"""
        if event_type in self._callbacks:
            self._callbacks[event_type].append(callback)

    def _trigger(self, event_type: str, *args):
        """触发事件回调"""
        for cb in self._callbacks.get(event_type, []):
            try:
                cb(*args)
            except Exception:
                pass

    def start(self, logger=None):
        """启动监控线程"""
        if self._thread and self._thread.is_alive():
            return

        self.logger = logger
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="UsnMonitor"
        )
        self._thread.start()

    def stop(self):
        """停止监控"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        for reader in self._readers.values():
            reader.close()
        self._checkpoint_store.close()

    def _get_readers(self) -> Dict[str, UsnJournalReader]:
        """为所有监控路径所在的卷创建/获取读取器"""
        if not self._readers:
            volumes = set()
            for wp in self.watch_paths:
                vol = get_volume_for_path(wp).rstrip('\\')
                volumes.add(vol)

            for vol in volumes:
                reader = UsnJournalReader(
                    volume=vol,
                    checkpoint_store=self._checkpoint_store,
                    frn_cache=FrnPathCache(),
                    buffer_size_mb=self.buffer_size_mb,
                    logger=self.logger,
                )
                if reader.open():
                    self._readers[vol] = reader
                else:
                    if self.logger:
                        self.logger.warning(
                            f"无法打开卷 {vol}，该卷的监控将不可用"
                        )
        return self._readers

    def _is_path_watched(self, full_path: str) -> bool:
        """检查路径是否在监控范围内"""
        norm = os.path.normcase(os.path.abspath(full_path))
        for wp in self.watch_paths:
            wp_norm = os.path.normcase(os.path.abspath(wp))
            if norm.startswith(wp_norm + os.sep) or norm == wp_norm:
                return True
        return False

    def _should_exclude(self, full_path: str,
                        exclude_patterns: List[str] = None,
                        exclude_dirs: List[str] = None) -> bool:
        """检查路径是否应被排除"""
        if not exclude_patterns:
            exclude_patterns = []
        if not exclude_dirs:
            exclude_dirs = []

        basename = os.path.basename(full_path)
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(basename, pattern):
                return True

        parts = full_path.replace("\\", "/").split("/")
        for part in parts:
            if part in exclude_dirs:
                return True

        return False

    def _is_duplicate(self, full_path: str, reason: str) -> bool:
        """检查事件是否为短期内重复事件"""
        key = (os.path.normcase(full_path), reason)
        now = time.time()

        # 清理过期的去重记录
        expired = [
            k for k, (t, _) in self._recent_events.items()
            if now - t > self.DEDUP_WINDOW_SEC * 2
        ]
        for k in expired:
            del self._recent_events[k]

        if key in self._recent_events:
            t, _ = self._recent_events[key]
            if now - t < self.DEDUP_WINDOW_SEC:
                return True

        return False

    def _mark_seen(self, full_path: str, reason: str, usn: int):
        """标记事件已处理"""
        key = (os.path.normcase(full_path), reason)
        self._recent_events[key] = (time.time(), usn)

    def set_exclude_rules(self, patterns: List[str], dirs: List[str]):
        """设置排除规则"""
        self._exclude_patterns = patterns
        self._exclude_dirs = dirs

    def _monitor_loop(self):
        """监控主循环"""
        if self.logger:
            self.logger.info(
                f"USN Journal 监控已启动: "
                f"{len(self.watch_paths)} 个监控路径, "
                f"轮询间隔 {self.poll_interval}s"
            )

        readers = self._get_readers()
        if not readers:
            if self.logger:
                self.logger.error(
                    "没有可用的 USN Journal 卷，切换回 watchdog 模式"
                )
            return

        # 启动时校验所有 checkpoint
        for vol, reader in readers.items():
            cp = reader.validate_checkpoint()
            if cp.needs_reconciliation:
                if self.logger:
                    self.logger.warning(
                        f"卷 {vol} checkpoint 状态: {cp.status.value}，"
                        f"需要 reconciliation"
                    )
                if cp.status == JournalStatus.JOURNAL_GAP:
                    self._trigger("on_journal_gap", vol, cp)
                elif cp.status in (JournalStatus.JOURNAL_RESET,
                                   JournalStatus.RESCAN_REQUIRED):
                    self._trigger("on_journal_reset", vol, cp)

        while not self._stop_event.is_set():
            poll_start = time.time()
            total_records = 0

            for vol, reader in readers.items():
                try:
                    cp = self._checkpoint_store.load(vol)
                    start_usn = cp.next_usn if cp else 0

                    result = reader.read_records(
                        start_usn,
                        max_records=self.max_records_per_read,
                    )

                    # 处理异常状态
                    if result.status == ReadResult.JOURNAL_RESET:
                        if self.logger:
                            self.logger.warning(
                                f"卷 {vol} Journal 被重建，触发 RESCAN"
                            )
                        self._checkpoint_store.update_status(
                            vol, JournalStatus.JOURNAL_RESET
                        )
                        self._trigger("on_journal_reset", vol, cp)
                        continue

                    if result.status == ReadResult.JOURNAL_GAP:
                        if self.logger:
                            self.logger.warning(
                                f"卷 {vol} USN gap 检测，触发 RESCAN"
                            )
                        self._checkpoint_store.update_status(
                            vol, JournalStatus.JOURNAL_GAP
                        )
                        self._trigger("on_journal_gap", vol, cp)
                        continue

                    if result.status == ReadResult.ERROR:
                        continue

                    events = result.events
                    if not events:
                        continue

                    total_records += len(events)
                    self.stats["total_records_read"] += len(events)

                    # 解析路径
                    resolved = reader.resolve_paths(events)

                    # 分派事件
                    for event in resolved:
                        self._dispatch_event(event)

                    # 更新 checkpoint（在事件处理完成后）
                    if result.last_usn > start_usn:
                        self._checkpoint_store.update_next_usn(
                            vol,
                            result.last_usn,
                            reader.journal_id,
                            JournalStatus.HEALTHY,
                        )

                except Exception as e:
                    if self.logger:
                        self.logger.error(
                            f"轮询卷 {vol} USN Journal 异常: {e}"
                        )

            self.stats["last_poll_time"] = time.time() - poll_start
            self.stats["last_poll_records"] = total_records

            # 等待下一轮
            elapsed = time.time() - poll_start
            remaining = max(0.05, self.poll_interval - elapsed)
            self._stop_event.wait(remaining)

        if self.logger:
            self.logger.info("USN Journal 监控已停止")

    def _dispatch_event(self, event: UsnEvent):
        """根据 USN 事件类型分派到对应的回调"""
        full_path = event.full_path
        if not full_path or not os.path.isabs(full_path):
            return

        if not self._is_path_watched(full_path):
            return

        if self._should_exclude(
            full_path,
            getattr(self, '_exclude_patterns', []),
            getattr(self, '_exclude_dirs', []),
        ):
            return

        reason = event.reason

        # 文件删除
        if reason & USN_REASON_FILE_DELETE:
            if self._is_duplicate(full_path, "delete"):
                self.stats["total_duplicates_skipped"] += 1
                return
            self._mark_seen(full_path, "delete", event.usn)
            self._trigger("on_deleted", full_path)
            self.stats["total_events_dispatched"] += 1
            return

        # 文件重命名（源）
        if reason & USN_REASON_RENAME_OLD_NAME:
            if self._is_duplicate(full_path, "rename_old"):
                self.stats["total_duplicates_skipped"] += 1
                return
            self._mark_seen(full_path, "rename_old", event.usn)
            self._trigger("on_renamed", full_path, None)
            self.stats["total_events_dispatched"] += 1
            return

        # 文件重命名（目标）
        if reason & USN_REASON_RENAME_NEW_NAME:
            if self._is_duplicate(full_path, "rename_new"):
                self.stats["total_duplicates_skipped"] += 1
                return
            self._mark_seen(full_path, "rename_new", event.usn)
            self._trigger("on_created", full_path)
            self.stats["total_events_dispatched"] += 1
            return

        # 文件创建
        if reason & USN_REASON_FILE_CREATE:
            if self._is_duplicate(full_path, "create"):
                self.stats["total_duplicates_skipped"] += 1
                return
            self._mark_seen(full_path, "create", event.usn)
            self._trigger("on_created", full_path)
            self.stats["total_events_dispatched"] += 1
            return

        # CLOSE + 内容修改
        if reason & USN_REASON_CLOSE:
            if reason & USN_REASON_CONTENT_MODIFIED:
                if self._is_duplicate(full_path, "close_modify"):
                    self.stats["total_duplicates_skipped"] += 1
                    return
                self._mark_seen(full_path, "close_modify", event.usn)
                self._trigger("on_modified", full_path)
                self.stats["total_events_dispatched"] += 1
            return

        # 纯内容修改
        if reason & USN_REASON_CONTENT_MODIFIED:
            if self._is_duplicate(full_path, "modify"):
                self.stats["total_duplicates_skipped"] += 1
                return
            self._mark_seen(full_path, "modify", event.usn)
            self._trigger("on_modified", full_path)
            self.stats["total_events_dispatched"] += 1
            return

    def get_stats(self) -> dict:
        """获取监控统计信息"""
        return {
            **self.stats,
            "volumes": list(self._readers.keys()),
            "readers_open": sum(1 for r in self._readers.values()
                                if r.is_open),
        }

    def get_health(self) -> List[JournalHealthInfo]:
        """获取所有卷的 Journal 健康信息（方案第二十五节）"""
        result = []
        for vol, reader in self._readers.items():
            cp = self._checkpoint_store.load(vol)
            checkpoint_usn = cp.next_usn if cp else 0
            status = cp.status if cp else JournalStatus.HEALTHY

            health = compute_health(
                vol,
                reader.journal_info,
                checkpoint_usn,
                status,
            )
            result.append(health)
        return result


# ═══════════════════════════════════════════════════════════════
# 便捷工具
# ═══════════════════════════════════════════════════════════════

def create_usn_watcher(config, logger):
    """
    创建并配置 USN Journal 监控器，返回 UsnJournalMonitor 实例。

    如果在 Windows 上成功初始化，返回 monitor 实例；
    否则返回 None（调用方应回退到 watchdog）。
    """
    if os.name != 'nt':
        logger.info("非 Windows 系统，USN Journal 不可用")
        return None

    if hasattr(config, 'usn') and not config.usn.enabled:
        logger.info("USN Journal 已在配置中禁用，使用 watchdog 模式")
        return None

    watch_paths = config.watch_paths
    if not watch_paths:
        return None

    poll_interval = 1.0
    checkpoint_db = '.usn_state/usn_checkpoint.db'
    buffer_size_mb = 4
    max_records = 10000

    if hasattr(config, 'usn'):
        poll_interval = config.usn.poll_interval
        buffer_size_mb = getattr(config.usn, 'buffer_size_mb', 4)
        max_records = getattr(config.usn, 'max_records_per_read', 10000)

    monitor = UsnJournalMonitor(
        watch_paths=watch_paths,
        checkpoint_db=checkpoint_db,
        poll_interval=poll_interval,
        buffer_size_mb=buffer_size_mb,
        max_records_per_read=max_records,
    )

    monitor._exclude_patterns = config.exclude_patterns
    monitor._exclude_dirs = config.exclude_dirs

    readers = monitor._get_readers()
    if not readers:
        logger.warning(
            "USN Journal 初始化失败（所有卷均无法打开），回退到 watchdog"
        )
        return None

    monitor.logger = logger
    return monitor
