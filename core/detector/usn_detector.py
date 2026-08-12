"""
USN 事件检测器。

封装 UsnJournalMonitor 的生命周期管理和事件分发：
- 管理 UsnJournalReader 的创建和轮询
- 检测 journal gap / reset -> 触发 Reconciler
- 将 USN 事件通过 EventNormalizer 标准化后送入 EventCoalescer
- 最终由 ProtectionEngine 消费执行

架构定位：
    UsnDetector
        -> UsnJournalMonitor (读取 USN Journal)
        -> EventNormalizer (标准化事件)
        -> EventCoalescer (合并事件)
        -> ProtectionEngine (执行备份/删除)
"""

import os
import threading
import time
from typing import List, Optional, Callable

from core.usn import (
    UsnJournalMonitor,
    UsnJournalReader,
    UsnCheckpointStore,
    FrnPathCache,
    ReadResult,
    JournalStatus,
    CheckpointInfo,
)
from core.usn.reader import UsnEvent
from core.engine.event_normalizer import EventNormalizer, NormalizedEvent, EventType
from core.engine.event_coalescer import EventCoalescer
from core.engine.protection_engine import ProtectionEngine


class UsnDetector:
    """
    USN 事件检测器。

    整合 USN 读取 + 事件标准化 + 事件合并 + 保护引擎，
    提供统一的启动/停止接口。

    使用方式：
        detector = UsnDetector(config, logger, event_store)
        detector.start()

        # 运行中...

        detector.stop()
    """

    def __init__(self, config, logger, event_store=None):
        """
        Args:
            config: Config 实例
            logger: Logger 实例
            event_store: UsnEventStore 实例（用于持久化事件）
        """
        self.config = config
        self.logger = logger
        self.event_store = event_store

        # 事件处理管道
        self.normalizer = EventNormalizer()
        self.coalescer = EventCoalescer(
            debounce_ms=getattr(config, 'coalesce_debounce_ms', 500)
        )
        self.protection_engine = ProtectionEngine(
            config=config,
            logger=logger,
            event_store=event_store,
        )

        # USN 监控器
        self._monitor: Optional[UsnJournalMonitor] = None

        # Coalescer flush 线程
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Reconciler 回调
        self._on_reconcile: Optional[Callable] = None

    def set_on_reconcile(self, callback: Callable):
        """
        设置 reconciliation 回调。

        当检测到 journal gap / reset 时调用。
        callback(volume: str, reason: str)
        """
        self._on_reconcile = callback

    def start(self) -> bool:
        """
        启动 USN 检测管道。

        Returns:
            True 如果启动成功，False 如果 USN 不可用
        """
        if os.name != 'nt':
            self.logger.info("非 Windows 系统，USN Journal 不可用")
            return False

        if hasattr(self.config, 'usn') and not self.config.usn.enabled:
            self.logger.info("USN Journal 已在配置中禁用")
            return False

        watch_paths = self.config.watch_paths
        if not watch_paths:
            return False

        # 创建 USN 监控器
        usn_cfg = getattr(self.config, 'usn', None)
        poll_interval = usn_cfg.poll_interval if usn_cfg else 1.0
        buffer_size_mb = usn_cfg.buffer_size_mb if usn_cfg else 4
        max_records = usn_cfg.max_records_per_read if usn_cfg else 10000
        state_dir = usn_cfg.state_dir if usn_cfg else ".usn_state"

        checkpoint_db = os.path.join(state_dir, "usn_checkpoint.db")

        self._monitor = UsnJournalMonitor(
            watch_paths=watch_paths,
            checkpoint_db=checkpoint_db,
            poll_interval=poll_interval,
            buffer_size_mb=buffer_size_mb,
            max_records_per_read=max_records,
            event_store=self.event_store,  # N4: checkpoint 与 fs_event 绑定
        )

        # 设置排除规则
        self._monitor.set_exclude_rules(
            getattr(self.config, 'exclude_patterns', []),
            getattr(self.config, 'exclude_dirs', []),
        )

        # 注册 gap/reset 回调
        self._monitor.on("on_journal_gap", self._handle_journal_gap)
        self._monitor.on("on_journal_reset", self._handle_journal_reset)

        # 尝试打开卷
        readers = self._monitor._get_readers()
        if not readers:
            self.logger.warning("USN Journal 初始化失败，所有卷均无法打开")
            return False

        self._monitor.logger = self.logger

        # 启动保护引擎
        self.protection_engine.start()

        # 启动 coalescer flush 线程
        self._stop_event.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="CoalescerFlush"
        )
        self._flush_thread.start()

        # 启动 USN 监控（使用自定义事件分发）
        self._monitor.start(logger=self.logger)

        self.logger.info(
            f"USN 检测器已启动: {len(readers)} 个卷, "
            f"轮询间隔 {poll_interval}s"
        )
        return True

    def stop(self):
        """停止 USN 检测管道"""
        self._stop_event.set()

        if self._monitor:
            self._monitor.stop()

        # flush 剩余事件
        remaining = self.coalescer.flush_all()
        if remaining:
            self.protection_engine.submit_events(remaining)

        self.protection_engine.stop()

        if self._flush_thread:
            self._flush_thread.join(timeout=5)

        self.logger.info("USN 检测器已停止")

    def submit_watchdog_event(self, event_type: str, full_path: str,
                              is_directory: bool = False):
        """
        提交 watchdog 事件到检测管道。

        watchdog 事件经过 EventNormalizer 标准化后，
        与 USN 事件统一处理。

        Args:
            event_type: watchdog 事件类型 ("created"/"modified"/"deleted"/"moved")
            full_path: 文件完整路径
            is_directory: 是否为目录
        """
        normalized = self.normalizer.from_watchdog_event(
            event_type, full_path, is_directory
        )
        if normalized is None:
            return

        # 通过 coalescer 合并
        ready = self.coalescer.submit(normalized)
        if ready:
            self.protection_engine.submit_events(ready)

    def get_health(self) -> list:
        """获取 USN Journal 健康信息"""
        if self._monitor:
            return self._monitor.get_health()
        return []

    def get_stats(self) -> dict:
        """获取检测器统计信息"""
        stats = {
            "coalescer_pending": self.coalescer.pending_count,
        }
        if self._monitor:
            stats["monitor"] = self._monitor.get_stats()
        stats["protection"] = self.protection_engine.get_stats()
        return stats

    def _flush_loop(self):
        """定期 flush coalescer 中过期的事件"""
        while not self._stop_event.is_set():
            try:
                expired = self.coalescer.flush_expired()
                if expired:
                    self.protection_engine.submit_events(expired)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Coalescer flush 异常: {e}")

            self._stop_event.wait(timeout=0.1)  # 100ms flush 间隔

    def _handle_journal_gap(self, volume: str, checkpoint: CheckpointInfo):
        """处理 USN gap 事件"""
        self.logger.warning(
            f"USN gap 检测: 卷 {volume}, "
            f"checkpoint.next_usn={checkpoint.next_usn}"
        )
        if self._on_reconcile:
            self._on_reconcile(volume, "JOURNAL_GAP")

    def _handle_journal_reset(self, volume: str, checkpoint: CheckpointInfo):
        """处理 Journal reset 事件"""
        self.logger.warning(
            f"USN Journal reset: 卷 {volume}, "
            f"状态={checkpoint.status.value}"
        )
        if self._on_reconcile:
            self._on_reconcile(volume, "JOURNAL_RESET")
