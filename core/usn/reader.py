"""
USN Journal 卷级读取器。

职责：
1. 打开卷句柄
2. 查询 USN Journal 当前状态
3. 从 checkpoint.next_usn 开始读取新记录
4. 解析记录为 UsnEvent 列表
5. 维护 FRN -> 路径缓存
6. 检测 journal_id 变化和 USN gap（方案第四/五/六节）

增强（相比原 core/usn.py）：
- journal_id 变化检测 -> 返回 JOURNAL_RESET 信号
- checkpoint.next_usn < journal.first_usn 检测 -> 返回 JOURNAL_GAP 信号
- 增大输出缓冲区（1MB -> 可配置，默认 4MB）
- 记录解析按 64-bit 边界对齐（方案第十八节）
- 提供 query_journal_info() 用于 Health Check
"""

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum

from .record import (
    USN_RECORD_V3,
    USN_JOURNAL_DATA_V2,
    READ_USN_JOURNAL_DATA_V1,
    FSCTL_QUERY_USN_JOURNAL,
    FSCTL_READ_USN_JOURNAL,
    USN_REASON_MASK,
    USN_REASON_FILE_DELETE,
    kernel32,
    open_volume_handle,
    get_full_path_by_frn,
    get_volume_root_from_dos,
    align8,
    INVALID_HANDLE_VALUE,
)
from .checkpoint import UsnCheckpointStore, CheckpointInfo, JournalStatus
from .journal import JournalInfo, query_journal_info, create_journal
from .path_resolver import FrnPathCache


class ReadResult(Enum):
    """read_records() 的返回状态"""
    OK = "OK"                       # 正常读取
    JOURNAL_RESET = "JOURNAL_RESET"  # Journal 被重建
    JOURNAL_GAP = "JOURNAL_GAP"     # USN gap（记录被覆盖）
    ERROR = "ERROR"                 # 读取错误


@dataclass
class UsnEvent:
    """USN Journal 中解析出的一次文件变更事件"""
    usn: int
    timestamp: int  # 100ns intervals since 1601-01-01
    reason: int
    file_reference_number: int
    parent_frn: int
    file_name: str
    full_path: str = ""
    is_directory: bool = False

    @property
    def timestamp_sec(self) -> float:
        """转换为 Unix 时间戳（秒）"""
        return (self.timestamp / 10_000_000) - 11644473600.0

    def has_reason(self, mask: int) -> bool:
        return (self.reason & mask) != 0


@dataclass
class ReadRecordsResult:
    """read_records() 的完整返回"""
    status: ReadResult
    events: List[UsnEvent]
    last_usn: int  # 最后处理的 USN（用于更新 checkpoint）
    journal_info: Optional[JournalInfo] = None


@dataclass
class UsnJournalReader:
    """
    单个卷的 USN Journal 读取器。

    增强（相比原 core/usn.py 中的 UsnJournalReader）：
    - 返回 ReadRecordsResult 包含状态信息
    - journal_id 变化检测
    - FirstUsn gap 检测
    - 可配置缓冲区大小
    - 64-bit 对齐的记录解析
    """

    volume: str  # 卷路径，如 'E:'
    checkpoint_store: UsnCheckpointStore
    frn_cache: FrnPathCache = field(default_factory=FrnPathCache)
    buffer_size_mb: int = 4  # 读取缓冲区大小（MB）
    logger: object = None

    def __post_init__(self):
        self._volume_handle: Optional[int] = None
        self._journal_id: int = 0
        self._journal_info: Optional[JournalInfo] = None
        self._volume_root_dos: str = ""
        import threading
        self._lock = threading.Lock()

    def _log(self, level: str, msg: str):
        if self.logger:
            getattr(self.logger, level)(msg)

    def open(self) -> bool:
        """打开卷句柄并查询 Journal 信息"""
        with self._lock:
            h = open_volume_handle(self.volume)
            if h is None:
                self._log(
                    "warning",
                    f"无法打开卷 {self.volume}（可能需要管理员权限）"
                )
                return False

            self._volume_handle = h
            self._volume_root_dos = get_volume_root_from_dos(self.volume)

            # 查询 Journal 信息
            self._journal_info = query_journal_info(h)
            if self._journal_info is None or self._journal_info.journal_id == 0:
                self._log(
                    "warning",
                    f"卷 {self.volume} 未启用 USN Journal，尝试启用"
                )
                jid = create_journal(h)
                if jid == 0:
                    self._log("warning", f"卷 {self.volume} 启用 USN Journal 失败")
                    return False
                self._journal_id = jid
                self._journal_info = query_journal_info(h)
            else:
                self._journal_id = self._journal_info.journal_id

            self._log(
                "info",
                f"USN Journal 已打开: 卷 {self.volume}, "
                f"JournalID={self._journal_id}, "
                f"NextUsn={self._journal_info.next_usn if self._journal_info else 0}"
            )
            return True

    def close(self):
        """关闭卷句柄"""
        with self._lock:
            if self._volume_handle is not None:
                kernel32.CloseHandle(self._volume_handle)
                self._volume_handle = None

    @property
    def is_open(self) -> bool:
        return self._volume_handle is not None

    @property
    def journal_id(self) -> int:
        return self._journal_id

    @property
    def journal_info(self) -> Optional[JournalInfo]:
        return self._journal_info

    def validate_checkpoint(self) -> CheckpointInfo:
        """
        校验 checkpoint 与当前 Journal 状态的一致性。

        返回 CheckpointInfo（可能带有异常状态）。
        调用方应根据 cp.needs_reconciliation 决定是否触发 reconciliation。
        """
        if self._journal_info is None:
            return CheckpointInfo(
                volume=self.volume,
                journal_id=self._journal_id,
                next_usn=0,
                status=JournalStatus.ERROR,
            )

        return self.checkpoint_store.validate(
            self.volume,
            self._journal_info.journal_id,
            self._journal_info.first_usn,
        )

    def read_records(self, start_usn: int,
                     max_records: int = 10000) -> ReadRecordsResult:
        """
        从 USN Journal 读取新的变更记录。

        增强（方案第四/五/六节）：
        - 检测 journal_id 变化 -> JOURNAL_RESET
        - 检测 start_usn < first_usn -> JOURNAL_GAP

        Args:
            start_usn: 起始 USN（读取 > start_usn 的记录）
            max_records: 单次最多读取的记录数

        Returns:
            ReadRecordsResult 包含状态、事件列表和最后处理的 USN
        """
        if not self.is_open:
            return ReadRecordsResult(
                status=ReadResult.ERROR,
                events=[],
                last_usn=start_usn,
            )

        with self._lock:
            # 刷新 Journal 信息
            self._journal_info = query_journal_info(self._volume_handle)
            if self._journal_info is None:
                return ReadRecordsResult(
                    status=ReadResult.ERROR,
                    events=[],
                    last_usn=start_usn,
                )

            # 检测 journal_id 变化（方案第五节）
            if self._journal_info.journal_id != self._journal_id:
                self._log(
                    "warning",
                    f"USN JournalID 已变化: {self._journal_id} -> "
                    f"{self._journal_info.journal_id}，需要 RESCAN"
                )
                self._journal_id = self._journal_info.journal_id
                return ReadRecordsResult(
                    status=ReadResult.JOURNAL_RESET,
                    events=[],
                    last_usn=start_usn,
                    journal_info=self._journal_info,
                )

            # 检测 USN gap（方案第六节）
            if start_usn > 0 and start_usn < self._journal_info.first_usn:
                self._log(
                    "warning",
                    f"USN gap 检测: checkpoint.next_usn={start_usn} < "
                    f"journal.first_usn={self._journal_info.first_usn}，"
                    f"旧记录已被覆盖，需要 RESCAN"
                )
                return ReadRecordsResult(
                    status=ReadResult.JOURNAL_GAP,
                    events=[],
                    last_usn=start_usn,
                    journal_info=self._journal_info,
                )

            # 如果没有新记录，直接返回
            if start_usn >= self._journal_info.next_usn:
                return ReadRecordsResult(
                    status=ReadResult.OK,
                    events=[],
                    last_usn=start_usn,
                    journal_info=self._journal_info,
                )

            # 确保起始 USN 有效
            effective_start = max(start_usn + 1, self._journal_info.first_usn)

            # 读取记录
            all_events: List[UsnEvent] = []
            current_usn = effective_start
            buf_size = self.buffer_size_mb * 1024 * 1024

            while len(all_events) < max_records:
                events, next_usn = self._read_batch(
                    current_usn, buf_size,
                    max_records - len(all_events)
                )
                if not events:
                    break
                all_events.extend(events)
                current_usn = next_usn

                # 如果返回的事件数少于请求量，说明已读完
                if len(events) < max_records - len(all_events):
                    break

            last_usn = all_events[-1].usn if all_events else start_usn

            return ReadRecordsResult(
                status=ReadResult.OK,
                events=all_events,
                last_usn=last_usn,
                journal_info=self._journal_info,
            )

    def _read_batch(self, start_usn: int, buf_size: int,
                    max_records: int) -> tuple:
        """读取一批 USN 记录，返回 (events, next_usn)"""
        read_data = READ_USN_JOURNAL_DATA_V1()
        read_data.StartUsn = start_usn
        read_data.ReasonMask = USN_REASON_MASK
        read_data.ReturnOnlyOnClose = 0
        read_data.Timeout = 0
        read_data.BytesToWaitFor = 0
        read_data.UsnJournalID = self._journal_id
        read_data.MinMajorVersion = 2
        read_data.MaxMajorVersion = 3

        out_buffer = ctypes.create_string_buffer(buf_size)
        bytes_returned = wintypes.DWORD()

        result = kernel32.DeviceIoControl(
            self._volume_handle,
            FSCTL_READ_USN_JOURNAL,
            ctypes.byref(read_data), ctypes.sizeof(read_data),
            ctypes.byref(out_buffer), ctypes.sizeof(out_buffer),
            ctypes.byref(bytes_returned),
            None,
        )

        if not result:
            err = ctypes.get_last_error() if ctypes.get_last_error() else 0
            if err == 38:  # ERROR_HANDLE_EOF
                return [], start_usn
            # 回退尝试：某些情况下 get_last_error 不可靠
            if err == 0:
                return [], start_usn
            self._log("error", f"读取 USN Journal 失败 (err={err})")
            return [], start_usn

        events = self._parse_records(
            out_buffer, bytes_returned.value, max_records
        )

        # 计算下一批的起始 USN
        next_usn = events[-1].usn + 1 if events else start_usn

        return events, next_usn

    def _parse_records(self, buffer: ctypes.Array,
                       buf_len: int,
                       max_records: int) -> List[UsnEvent]:
        """
        解析输出缓冲区中的 USN 记录。

        增强：按 64-bit 边界对齐（方案第十八节）。
        """
        events: List[UsnEvent] = []
        offset = 0

        while offset < buf_len and len(events) < max_records:
            if offset + ctypes.sizeof(USN_RECORD_V3) > buf_len:
                break

            rec_struct = USN_RECORD_V3.from_buffer_copy(buffer, offset)
            record_len = rec_struct.RecordLength

            if record_len == 0 or record_len < ctypes.sizeof(USN_RECORD_V3):
                break

            # 读取文件名
            name_offset = offset + rec_struct.FileNameOffset
            name_len_chars = rec_struct.FileNameLength // 2
            if name_len_chars > 0 and name_offset + rec_struct.FileNameLength <= buf_len:
                file_name = ctypes.cast(
                    ctypes.addressof(buffer) + name_offset,
                    ctypes.POINTER(ctypes.c_wchar * name_len_chars)
                ).contents.value
            else:
                file_name = ""

            is_dir = bool(rec_struct.FileAttributes & 0x10)

            # 跳过目录变更（默认不监控目录本身）
            # 但记录目录的 FRN 信息到 DirectoryIdentityCache
            if is_dir:
                offset += align8(record_len)  # 64-bit 对齐
                continue

            event = UsnEvent(
                usn=rec_struct.Usn,
                timestamp=rec_struct.TimeStamp,
                reason=rec_struct.Reason,
                file_reference_number=rec_struct.FileReferenceNumber,
                parent_frn=rec_struct.ParentFileReferenceNumber,
                file_name=file_name,
                is_directory=is_dir,
            )
            events.append(event)

            # 64-bit 对齐（方案第十八节）
            offset += align8(record_len)

        return events

    def resolve_paths(self, events: List[UsnEvent]) -> List[UsnEvent]:
        """
        批量解析事件的文件路径。

        策略：
        1. 先查 FRN 路径缓存
        2. 缓存未命中时通过 OpenFileById 获取路径
        3. 解析成功后更新缓存
        4. DELETE 事件特殊处理
        """
        resolved = []

        for event in events:
            frn = event.file_reference_number
            cached = self.frn_cache.get(frn)

            if cached:
                event.full_path = cached
                resolved.append(event)
                continue

            # DELETE 事件的文件可能已不存在
            if event.has_reason(USN_REASON_FILE_DELETE):
                parent_path = self._resolve_parent_path(event.parent_frn)
                if parent_path:
                    event.full_path = os.path.join(parent_path, event.file_name)
                    resolved.append(event)
                continue

            # 通过 OpenFileById 获取路径
            path = get_full_path_by_frn(self._volume_handle, frn)
            if path:
                event.full_path = path
                self.frn_cache.set(frn, path)
                resolved.append(event)
            else:
                # 解析失败，尝试通过父 FRN 构建
                parent_path = self._resolve_parent_path(event.parent_frn)
                if parent_path:
                    event.full_path = os.path.join(parent_path, event.file_name)
                    resolved.append(event)

        return resolved

    def _resolve_parent_path(self, parent_frn: int) -> Optional[str]:
        """解析父目录的完整路径"""
        cached = self.frn_cache.get(parent_frn)
        if cached:
            return cached

        path = get_full_path_by_frn(self._volume_handle, parent_frn)
        if path:
            self.frn_cache.set(parent_frn, path)
            return path

        return None
