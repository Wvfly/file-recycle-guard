"""
事件合并器（Event Coalescer）。

解决文件连续写入时产生多条 USN 记录的问题（方案第九节）：
- 同文件（同 FRN）的多次 MODIFY 合并为一次
- USN_CLOSE 作为写入阶段结束的辅助信号
- 500ms debounce 窗口（可配置）

实现策略：
- 维护 pending dict: {(volume_id, frn): (event, first_seen_time)}
- 新事件到来时：
  - 如果同文件已有 pending 事件且类型兼容 -> 合并（更新为最新事件）
  - 否则 -> 加入 pending
- 定时 flush 超过 debounce 窗口的 pending 事件

事件合并规则：
- MODIFY + MODIFY -> MODIFY（保留最新的）
- CREATE + MODIFY -> CREATE（保留创建事件，路径以最新为准）
- MODIFY + DELETE -> DELETE
- CREATE + DELETE -> 取消（文件被创建后立即删除，无需处理）
- RENAME_OLD + RENAME_NEW -> 配对处理
"""

import threading
import time
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass

from .event_normalizer import NormalizedEvent, EventType


@dataclass
class _PendingEntry:
    """pending 队列中的条目"""
    event: NormalizedEvent
    first_seen: float  # 首次见到此事件的时间
    last_seen: float   # 最后一次更新的时间


class EventCoalescer:
    """
    事件合并器。

    将短时间内的多个同文件事件合并为一个，减少不必要的备份 IO。

    使用方式：
        coalescer = EventCoalescer(debounce_ms=500)

        # 提交事件
        ready_events = coalescer.submit(normalized_event)

        # 定期 flush 过期条目（由后台线程调用）
        expired_events = coalescer.flush_expired()
    """

    DEFAULT_DEBOUNCE_MS = 500  # 默认 debounce 窗口

    def __init__(self, debounce_ms: int = DEFAULT_DEBOUNCE_MS):
        self._debounce_sec = debounce_ms / 1000.0
        self._pending: Dict[Tuple[str, int], _PendingEntry] = {}
        self._lock = threading.Lock()

    def submit(self, event: NormalizedEvent) -> List[NormalizedEvent]:
        """
        提交一个标准化事件。

        如果同文件已有 pending 事件，尝试合并。
        返回可以立即处理的事件列表（通常为空，因为需要等待 debounce）。

        特殊规则：
        - DELETE 事件立即返回（不等待 debounce），因为删除操作需要尽快执行
        - CREATE 事件立即返回（首次创建，尽快备份）
        """
        now = time.time()

        # DELETE 事件不合并，立即返回
        if event.event_type == EventType.DELETE:
            # 取消同文件的 pending CREATE（如果有的话）
            key = self._make_key(event)
            with self._lock:
                self._pending.pop(key, None)
            return [event]

        # CREATE 事件：如果是首次创建，立即返回
        key = self._make_key(event)
        with self._lock:
            existing = self._pending.get(key)

            if existing is None and event.event_type == EventType.CREATE:
                # 首次创建，立即处理
                return [event]

            # 合并逻辑
            if existing is not None:
                merged = self._merge_events(existing.event, event)
                if merged is not None:
                    # 合并成功，更新 pending
                    existing.event = merged
                    existing.last_seen = now
                    return []
                else:
                    # 无法合并（如 CREATE + DELETE 已取消），移除 pending
                    self._pending.pop(key, None)
                    return []

            # 新的 pending 条目
            self._pending[key] = _PendingEntry(
                event=event,
                first_seen=now,
                last_seen=now,
            )

        return []

    def flush_expired(self) -> List[NormalizedEvent]:
        """
        flush 超过 debounce 窗口的 pending 事件。

        由后台线程定期调用（如每 100ms）。

        Returns:
            可以处理的 NormalizedEvent 列表
        """
        now = time.time()
        expired: List[NormalizedEvent] = []
        expired_keys: List[Tuple[str, int]] = []

        with self._lock:
            for key, entry in self._pending.items():
                if now - entry.last_seen >= self._debounce_sec:
                    expired.append(entry.event)
                    expired_keys.append(key)

            for key in expired_keys:
                del self._pending[key]

        return expired

    def flush_all(self) -> List[NormalizedEvent]:
        """强制 flush 所有 pending 事件（用于关闭时）"""
        with self._lock:
            events = [entry.event for entry in self._pending.values()]
            self._pending.clear()
        return events

    def _make_key(self, event: NormalizedEvent) -> Tuple[str, int]:
        """生成 pending 字典的 key"""
        # 使用 (volume_id, frn) 作为 key
        # 如果 frn 为 0（watchdog 事件），回退到路径
        if event.file_reference_number != 0:
            return (event.volume_id, event.file_reference_number)
        else:
            # watchdog 事件没有 FRN，使用路径的哈希
            return ("", hash(event.full_path))

    def _merge_events(self, existing: NormalizedEvent,
                      new_event: NormalizedEvent) -> Optional[NormalizedEvent]:
        """
        尝试合并两个事件。

        合并规则：
        - MODIFY + MODIFY -> MODIFY（保留最新的）
        - CREATE + MODIFY -> CREATE（保留创建事件）
        - MODIFY + DELETE -> DELETE（但 DELETE 已立即返回，这里不会遇到）
        - RENAME_OLD + RENAME_NEW -> 配对（特殊处理）

        Returns:
            合并后的事件，或 None（表示取消）
        """
        old_type = existing.event_type
        new_type = new_event.event_type

        # MODIFY + MODIFY -> MODIFY
        if old_type == EventType.MODIFY and new_type == EventType.MODIFY:
            return new_event  # 保留最新的

        # CREATE + MODIFY -> CREATE（保留创建事件，但更新路径）
        if old_type == EventType.CREATE and new_type == EventType.MODIFY:
            result = existing
            result.full_path = new_event.full_path  # 路径可能被重命名
            return result

        # MODIFY + CREATE -> CREATE（文件被删除后重建）
        if old_type == EventType.MODIFY and new_type == EventType.CREATE:
            return new_event

        # CREATE + CREATE -> CREATE（重复创建事件）
        if old_type == EventType.CREATE and new_type == EventType.CREATE:
            return new_event

        # RENAME_OLD + RENAME_NEW -> 合并为 RENAME（保留 NEW 的路径）
        if old_type == EventType.RENAME_OLD and new_type == EventType.RENAME_NEW:
            result = new_event
            result.event_type = EventType.MODIFY  # rename 完成后视为修改
            return result

        # 其他情况：无法合并，保留新事件
        return new_event

    @property
    def pending_count(self) -> int:
        """当前 pending 事件数量"""
        with self._lock:
            return len(self._pending)
