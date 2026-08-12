"""
USN Journal 查询、创建与健康信息管理。

职责：
- 查询卷的 USN Journal 当前状态（JournalID / FirstUsn / NextUsn 等）
- 创建/启用 USN Journal（如未启用）
- 提供 JournalHealthInfo 数据类（用于 Web Health Check）
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from .record import (
    USN_JOURNAL_DATA_V2,
    FSCTL_QUERY_USN_JOURNAL,
    GENERIC_READ, GENERIC_WRITE,
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE,
    OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS,
    INVALID_HANDLE_VALUE,
    kernel32,
    open_volume_handle,
    get_volume_root_from_dos,
)
from .checkpoint import JournalStatus


@dataclass
class JournalInfo:
    """USN Journal 的当前状态快照（对应 USN_JOURNAL_DATA_V2）"""
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int
    max_usn: int
    maximum_size: int
    allocation_delta: int


@dataclass
class JournalHealthInfo:
    """
    USN Journal 健康信息（用于 Web API /api/usn_health）。

    字段说明（方案第二十五节）：
    - volume: 卷标识（如 'E:'）
    - status: 健康状态枚举
    - journal_id: 当前 Journal ID
    - first_usn: Journal 中最早可用的 USN
    - current_usn: Journal 当前的最新 USN（NextUsn）
    - checkpoint_usn: 上次已处理的 USN
    - lag: current_usn - checkpoint_usn（未处理的记录数）
    - journal_size_bytes: Journal 总容量
    - journal_used_bytes: 已使用的容量估算
    - coverage_estimate: 覆盖率估算（基于变化速率）
    """
    volume: str
    status: JournalStatus
    journal_id: int
    first_usn: int
    current_usn: int
    checkpoint_usn: int
    lag: int
    journal_size_bytes: int = 0
    journal_used_bytes: int = 0
    coverage_estimate: str = ""


def query_journal_info(volume_handle: int) -> Optional[JournalInfo]:
    """
    查询卷的 USN Journal 当前状态。

    Args:
        volume_handle: 已打开的卷句柄

    Returns:
        JournalInfo 或 None（查询失败时）
    """
    journal_data = USN_JOURNAL_DATA_V2()
    bytes_returned = wintypes.DWORD()
    result = kernel32.DeviceIoControl(
        volume_handle,
        FSCTL_QUERY_USN_JOURNAL,
        None, 0,
        ctypes.byref(journal_data), ctypes.sizeof(journal_data),
        ctypes.byref(bytes_returned),
        None,
    )
    if not result:
        return None

    return JournalInfo(
        journal_id=journal_data.UsnJournalID,
        first_usn=journal_data.FirstUsn,
        next_usn=journal_data.NextUsn,
        lowest_valid_usn=journal_data.LowestValidUsn,
        max_usn=journal_data.MaxUsn,
        maximum_size=journal_data.MaximumSize,
        allocation_delta=journal_data.AllocationDelta,
    )


def create_journal(volume_handle: int,
                   maximum_size: int = 128 * 1024 * 1024,
                   allocation_delta: int = 4 * 1024 * 1024) -> int:
    """
    创建/启用 USN Journal。

    Args:
        volume_handle: 已打开的卷句柄
        maximum_size: Journal 最大容量（字节），默认 128 MB
        allocation_delta: 分配增量（字节），默认 4 MB

    Returns:
        新创建的 Journal ID，失败返回 0
    """
    create_data = ctypes.c_buffer(ctypes.sizeof(USN_JOURNAL_DATA_V2))
    create_journal = USN_JOURNAL_DATA_V2.from_buffer(create_data)
    create_journal.MaximumSize = maximum_size
    create_journal.AllocationDelta = allocation_delta
    create_journal.MinSupportedMajorVersion = 2
    create_journal.MaxSupportedMajorVersion = 3

    bytes_returned = wintypes.DWORD()
    result = kernel32.DeviceIoControl(
        volume_handle,
        FSCTL_QUERY_USN_JOURNAL,  # 写回以创建
        ctypes.byref(create_journal), ctypes.sizeof(create_journal),
        ctypes.byref(create_journal), ctypes.sizeof(create_journal),
        ctypes.byref(bytes_returned),
        None,
    )
    if result:
        return create_journal.UsnJournalID
    return 0


def compute_health(volume: str,
                   journal_info: Optional[JournalInfo],
                   checkpoint_usn: int,
                   status: JournalStatus) -> JournalHealthInfo:
    """
    计算 Journal 健康信息（方案第二十五节）。

    Args:
        volume: 卷标识
        journal_info: 当前 Journal 状态快照
        checkpoint_usn: 上次已处理的 USN
        status: 当前健康状态

    Returns:
        JournalHealthInfo 实例
    """
    if journal_info is None:
        return JournalHealthInfo(
            volume=volume,
            status=JournalStatus.ERROR,
            journal_id=0,
            first_usn=0,
            current_usn=0,
            checkpoint_usn=checkpoint_usn,
            lag=0,
        )

    lag = journal_info.next_usn - checkpoint_usn
    used_estimate = journal_info.next_usn - journal_info.first_usn

    # 覆盖率估算：基于 Journal 容量与当前使用量
    if journal_info.maximum_size > 0:
        coverage_ratio = used_estimate / journal_info.maximum_size
        coverage_str = f"{coverage_ratio:.0%}"
    else:
        coverage_str = "N/A"

    return JournalHealthInfo(
        volume=volume,
        status=status,
        journal_id=journal_info.journal_id,
        first_usn=journal_info.first_usn,
        current_usn=journal_info.next_usn,
        checkpoint_usn=checkpoint_usn,
        lag=max(0, lag),
        journal_size_bytes=journal_info.maximum_size,
        journal_used_bytes=used_estimate,
        coverage_estimate=coverage_str,
    )
