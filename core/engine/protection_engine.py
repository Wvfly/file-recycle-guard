"""
保护引擎（Protection Engine）。

消费合并后的事件并调度操作：
- CREATE / MODIFY -> backup_file
- DELETE -> move_to_recycle
- RENAME -> 更新路径

架构定位（方案第二十六节）：
- 从 EventCoalescer 接收已合并的事件
- 将事件持久化到 SQLite fs_event 表（durable queue）
- 后台 worker 线程消费 PENDING 事件
- 操作完成后更新事件状态 + 推进 checkpoint

关键可靠性保证（方案第二十节）：
- checkpoint 只在事件成功写入 fs_event 后才推进
- crash recovery：启动时将 PROCESSING 状态的事件重置为 PENDING
"""

import os
import threading
import time
import logging
from typing import List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .event_normalizer import NormalizedEvent, EventType, EventSource


class ProtectionEngine:
    """
    保护引擎。

    职责：
    1. 接收已合并的标准化事件
    2. 将事件持久化到 SQLite（durable event queue）
    3. 后台 worker 消费事件并执行备份/删除/重命名
    4. 更新事件状态 + 推进 checkpoint

    使用方式：
        engine = ProtectionEngine(config, logger, db)
        engine.start()

        # 提交事件（由 EventCoalescer flush 后调用）
        engine.submit_events(events)

        # 关闭时
        engine.stop()
    """

    # worker 线程数（默认值，实际使用时优先读取 config.usn.protection_workers）
    WORKER_COUNT = 4

    # 批量处理大小
    BATCH_SIZE = 100

    def __init__(self, config, logger, event_store=None):
        """
        Args:
            config: Config 实例
            logger: Logger 实例
            event_store: UsnEventStore 实例（可选，用于持久化事件到 SQLite）
        """
        self.config = config
        self.logger = logger
        self.event_store = event_store

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._backup_pool: Optional[ThreadPoolExecutor] = None

        # 事件队列（内存缓冲，定期 flush 到 SQLite）
        self._pending_events: List[NormalizedEvent] = []
        self._pending_lock = threading.Lock()
        self._pending_cond = threading.Condition(self._pending_lock)

        # 统计
        self.stats = {
            "total_submitted": 0,
            "total_processed": 0,
            "total_failed": 0,
            "total_backup": 0,
            "total_delete": 0,
            "total_rename": 0,
        }

        # 回调（用于通知外部事件处理结果）
        self._on_event_done: Optional[Callable] = None

    def set_on_event_done(self, callback: Callable):
        """设置事件处理完成的回调"""
        self._on_event_done = callback

    def start(self):
        """启动保护引擎"""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        # 从配置读取 worker 线程数
        worker_count = self.WORKER_COUNT
        if hasattr(self.config, 'usn') and hasattr(self.config.usn, 'protection_workers'):
            worker_count = self.config.usn.protection_workers

        self._backup_pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="ProtectionWorker"
        )

        # 恢复 PROCESSING 状态的事件（crash recovery）
        self._recover_processing_events()

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="ProtectionEngine"
        )
        self._worker_thread.start()

        if self.logger:
            self.logger.info("保护引擎已启动")

    def stop(self):
        """停止保护引擎"""
        self._stop_event.set()

        # 唤醒 worker
        with self._pending_cond:
            self._pending_cond.notify_all()

        if self._worker_thread:
            self._worker_thread.join(timeout=10)

        if self._backup_pool:
            self._backup_pool.shutdown(wait=False)

        if self.logger:
            self.logger.info("保护引擎已停止")

    def submit_events(self, events: List[NormalizedEvent]):
        """
        提交已合并的事件到保护引擎。

        事件会被加入内存队列，由 worker 线程异步处理。
        同时持久化到 SQLite fs_event 表（durable queue）。
        """
        if not events:
            return

        # 先持久化到 SQLite（保证 crash recovery）
        if self.event_store is not None:
            self._persist_events(events)

        with self._pending_cond:
            self._pending_events.extend(events)
            self.stats["total_submitted"] += len(events)
            self._pending_cond.notify()

    def _persist_events(self, events: List[NormalizedEvent]):
        """将事件持久化到 SQLite fs_event 表"""
        if self.event_store is None:
            return

        try:
            self.event_store.batch_insert_fs_events([
                {
                    "volume_id": e.volume_id,
                    "usn": e.usn,
                    "file_reference": e.file_reference_number,
                    "parent_reference": e.parent_frn,
                    "reason": e.raw_reason,
                    "path": e.full_path,
                    "event_type": e.event_type.value,
                    "state": "PENDING",
                    "created_at": e.timestamp,
                }
                for e in events
            ])
        except Exception as e:
            if self.logger:
                self.logger.error(f"事件持久化失败: {e}")

    def _recover_processing_events(self):
        """
        crash recovery：将 PROCESSING 状态的事件重置为 PENDING。

        在启动时调用，确保因崩溃而中断的事件能被重新处理。
        """
        if self.event_store is None:
            return

        try:
            count = self.event_store.reset_processing_events()
            if count > 0 and self.logger:
                self.logger.info(
                    f"crash recovery: 重置 {count} 个 PROCESSING 事件为 PENDING"
                )
        except Exception as e:
            if self.logger:
                self.logger.error(f"crash recovery 失败: {e}")

    def _worker_loop(self):
        """worker 主循环：消费事件队列"""
        while not self._stop_event.is_set():
            events = []

            with self._pending_cond:
                if not self._pending_events:
                    # 等待新事件
                    self._pending_cond.wait(timeout=1.0)

                if self._pending_events:
                    # 取出一批事件
                    events = self._pending_events[:self.BATCH_SIZE]
                    self._pending_events = self._pending_events[self.BATCH_SIZE:]

            if events:
                self._process_batch(events)

    def _process_batch(self, events: List[NormalizedEvent]):
        """处理一批事件"""
        # 按事件类型分组
        backup_events = []
        delete_events = []
        rename_events = []

        for event in events:
            if event.event_type in (EventType.CREATE, EventType.MODIFY):
                backup_events.append(event)
            elif event.event_type == EventType.DELETE:
                delete_events.append(event)
            elif event.event_type in (EventType.RENAME_OLD, EventType.RENAME_NEW):
                rename_events.append(event)

        # 并行处理
        futures = []

        # 备份事件
        for event in backup_events:
            fut = self._backup_pool.submit(
                self._handle_backup, event
            )
            futures.append(fut)

        # 删除事件（顺序处理，避免并发问题）
        for event in delete_events:
            self._handle_delete(event)

        # 重命名事件
        for event in rename_events:
            self._handle_rename(event)

        # 等待备份完成
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                if self.logger:
                    self.logger.error(f"备份任务异常: {e}")

    def _handle_backup(self, event: NormalizedEvent):
        """处理备份事件"""
        from core.backup import backup_file

        full_path = event.full_path
        if not full_path or not os.path.isfile(full_path):
            return

        try:
            result = backup_file(full_path, self.config, self.logger)

            if result == "backed_up":
                self.stats["total_backup"] += 1
                self._mark_event_done(event, "DONE")
            elif result == "skipped":
                self._mark_event_done(event, "DONE")
            elif result == "source_gone":
                # 源文件已删除，尝试移入回收站
                self._handle_delete(event)
                self._mark_event_done(event, "DONE")
            else:
                self.stats["total_failed"] += 1
                self._mark_event_done(event, "FAILED")

        except Exception as e:
            if self.logger:
                self.logger.error(f"备份失败 {full_path}: {e}")
            self.stats["total_failed"] += 1
            self._mark_event_done(event, "FAILED")

    def _handle_delete(self, event: NormalizedEvent):
        """处理删除事件"""
        from core.recycler import move_to_recycle

        full_path = event.full_path
        if not full_path:
            return

        # 确认文件确实不存在
        if os.path.exists(full_path):
            # 文件又出现了（可能是 save-as 操作），改为备份
            self._handle_backup(event)
            return

        try:
            result = move_to_recycle(full_path, False, self.config, self.logger)
            if result:
                self.stats["total_delete"] += 1
                self._mark_event_done(event, "DONE")
            else:
                self.stats["total_failed"] += 1
                self._mark_event_done(event, "FAILED")
        except Exception as e:
            if self.logger:
                self.logger.error(f"删除处理失败 {full_path}: {e}")
            self.stats["total_failed"] += 1
            self._mark_event_done(event, "FAILED")

    def _handle_rename(self, event: NormalizedEvent):
        """处理重命名事件"""
        # 重命名事件通常与 RENAME_OLD + RENAME_NEW 配对
        # 在 EventCoalescer 中已合并为 MODIFY
        # 这里作为后备处理
        self._handle_backup(event)

    def _mark_event_done(self, event: NormalizedEvent, state: str):
        """标记事件处理完成"""
        event.state = state
        self.stats["total_processed"] += 1

        # 更新 SQLite 中的事件状态
        if self.event_store is not None and event.event_id > 0:
            try:
                self.event_store.update_fs_event_state(event.event_id, state)
            except Exception:
                pass

        # 通知回调
        if self._on_event_done:
            try:
                self._on_event_done(event)
            except Exception:
                pass

    def get_stats(self) -> dict:
        """获取保护引擎统计信息"""
        return {
            **self.stats,
            "pending_events": len(self._pending_events),
        }
