"""
事件处理引擎。

包含：
- EventNormalizer: 将 USN/watchdog 事件标准化为统一格式
- EventCoalescer: 合并同文件的重复事件
- ProtectionEngine: 消费事件队列，调度备份/删除/重命名操作
"""

from .event_normalizer import (
    EventType, NormalizedEvent, EventSource, EventNormalizer,
)
from .event_coalescer import EventCoalescer
from .protection_engine import ProtectionEngine

__all__ = [
    "EventType", "NormalizedEvent", "EventSource", "EventNormalizer",
    "EventCoalescer", "ProtectionEngine",
]
