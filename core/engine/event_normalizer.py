"""
事件标准化模块。

将来自不同源（USN Journal / watchdog）的文件系统变更事件
标准化为统一的 NormalizedEvent 格式。

USN Reason -> EventType 映射（方案第八节）：
- FILE_CREATE -> CREATE
- FILE_DELETE -> DELETE
- RENAME_OLD_NAME -> RENAME_OLD
- RENAME_NEW_NAME -> RENAME_NEW
- DATA_OVERWRITE / DATA_EXTEND / DATA_TRUNCATION + CLOSE -> MODIFY
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List

from core.usn.record import (
    USN_REASON_FILE_CREATE,
    USN_REASON_FILE_DELETE,
    USN_REASON_RENAME_OLD_NAME,
    USN_REASON_RENAME_NEW_NAME,
    USN_REASON_CONTENT_MODIFIED,
    USN_REASON_CLOSE,
)


class EventType(Enum):
    """标准化的事件类型"""
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    RENAME_OLD = "RENAME_OLD"
    RENAME_NEW = "RENAME_NEW"


class EventSource(Enum):
    """事件来源"""
    USN = "USN"           # USN Journal（source of truth）
    WATCHDOG = "WATCHDOG"  # watchdog（低延迟加速器）
    SCAN = "SCAN"          # 全量扫描（reconciliation）


@dataclass
class NormalizedEvent:
    """
    标准化的文件系统变更事件。

    所有来源的事件都标准化为此格式后，送入 EventCoalescer 合并，
    最终由 ProtectionEngine 消费执行。
    """
    event_type: EventType
    full_path: str
    file_reference_number: int = 0
    parent_frn: int = 0
    volume_id: str = ""
    usn: int = 0
    timestamp: float = 0.0
    source: EventSource = EventSource.USN
    state: str = "PENDING"  # PENDING / PROCESSING / DONE / FAILED

    # 内部字段（用于持久化到 fs_event 表）
    event_id: int = 0
    raw_reason: int = 0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


class EventNormalizer:
    """
    事件标准化器。

    将 USN 事件或 watchdog 事件转换为 NormalizedEvent。

    使用方式：
        normalizer = EventNormalizer()

        # 从 USN 事件转换
        events = normalizer.from_usn_event(usn_event)

        # 从 watchdog 事件转换
        events = normalizer.from_watchdog_event(watchdog_event)
    """

    def from_usn_event(self, usn_event) -> List[NormalizedEvent]:
        """
        将 USN 事件转换为标准化事件。

        一个 USN 事件可能产生多个标准化事件（如 RENAME 产生 OLD + NEW）。

        Args:
            usn_event: UsnEvent 实例（来自 core.usn.reader）

        Returns:
            NormalizedEvent 列表（可能为空）
        """
        reason = usn_event.reason
        results: List[NormalizedEvent] = []
        full_path = usn_event.full_path
        frn = usn_event.file_reference_number
        parent_frn = usn_event.parent_frn

        # 提取 volume_id（如 'E:'）
        volume_id = ""
        if full_path and len(full_path) >= 2:
            volume_id = full_path[:2]

        base_kwargs = {
            "full_path": full_path,
            "file_reference_number": frn,
            "parent_frn": parent_frn,
            "volume_id": volume_id,
            "usn": usn_event.usn,
            "timestamp": usn_event.timestamp_sec,
            "source": EventSource.USN,
            "raw_reason": reason,
        }

        # 文件删除
        if reason & USN_REASON_FILE_DELETE:
            results.append(NormalizedEvent(
                event_type=EventType.DELETE,
                **base_kwargs,
            ))
            return results

        # 文件重命名（源）
        if reason & USN_REASON_RENAME_OLD_NAME:
            results.append(NormalizedEvent(
                event_type=EventType.RENAME_OLD,
                **base_kwargs,
            ))
            return results

        # 文件重命名（目标）
        if reason & USN_REASON_RENAME_NEW_NAME:
            results.append(NormalizedEvent(
                event_type=EventType.RENAME_NEW,
                **base_kwargs,
            ))
            return results

        # 文件创建
        if reason & USN_REASON_FILE_CREATE:
            results.append(NormalizedEvent(
                event_type=EventType.CREATE,
                **base_kwargs,
            ))
            return results

        # CLOSE + 内容修改 -> MODIFY
        if reason & USN_REASON_CLOSE:
            if reason & USN_REASON_CONTENT_MODIFIED:
                results.append(NormalizedEvent(
                    event_type=EventType.MODIFY,
                    **base_kwargs,
                ))
            return results

        # 纯内容修改（无 CLOSE）
        if reason & USN_REASON_CONTENT_MODIFIED:
            results.append(NormalizedEvent(
                event_type=EventType.MODIFY,
                **base_kwargs,
            ))
            return results

        return results

    def from_watchdog_event(self, event_type: str, full_path: str,
                            is_directory: bool = False) -> Optional[NormalizedEvent]:
        """
        将 watchdog 事件转换为标准化事件。

        Args:
            event_type: watchdog 事件类型 ("created" / "modified" / "deleted" / "moved")
            full_path: 文件完整路径
            is_directory: 是否为目录

        Returns:
            NormalizedEvent 或 None（目录事件时）
        """
        if is_directory:
            return None

        # 提取 volume_id
        volume_id = ""
        if full_path and len(full_path) >= 2:
            volume_id = full_path[:2]

        type_map = {
            "created": EventType.CREATE,
            "modified": EventType.MODIFY,
            "deleted": EventType.DELETE,
            "moved": EventType.MODIFY,  # watchdog 的 moved 视为修改
        }

        std_type = type_map.get(event_type)
        if std_type is None:
            return None

        return NormalizedEvent(
            event_type=std_type,
            full_path=full_path,
            volume_id=volume_id,
            source=EventSource.WATCHDOG,
            timestamp=time.time(),
        )

    def from_scan_path(self, full_path: str,
                       event_type: EventType = EventType.MODIFY) -> NormalizedEvent:
        """
        将扫描发现的文件变更转换为标准化事件。

        用于 reconciliation 场景。
        """
        volume_id = ""
        if full_path and len(full_path) >= 2:
            volume_id = full_path[:2]

        return NormalizedEvent(
            event_type=event_type,
            full_path=full_path,
            volume_id=volume_id,
            source=EventSource.SCAN,
            timestamp=time.time(),
        )
