"""
USN Journal 底层结构体、Windows API 常量与辅助函数。

从原 core/usn.py 提取，包含：
- Windows API 常量（USN_REASON_* 等）
- USN_RECORD_V3 / USN_JOURNAL_DATA_V2 / READ_USN_JOURNAL_DATA_V1 等结构体
- kernel32 API 函数声明
- 卷句柄打开、NT 路径转换等辅助函数
"""

import os
import ctypes
import threading
from ctypes import wintypes
from typing import Optional, Dict

# Python 3.10/3.11 兼容：wintypes 中部分 64 位类型可能不存在
if not hasattr(wintypes, 'DWORDLONG'):
    wintypes.DWORDLONG = ctypes.c_uint64
if not hasattr(wintypes, 'LONGLONG'):
    wintypes.LONGLONG = ctypes.c_longlong


# ═══════════════════════════════════════════════════════════════
# Windows API 常量
# ═══════════════════════════════════════════════════════════════

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004

OPEN_EXISTING = 3

FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900FB

FILE_OPEN_BY_FILE_ID = 0x00002000

VOLUME_NAME_DOS = 0
VOLUME_NAME_NT = 2

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

# ── USN Reason Flags ──────────────────────────────────────────

USN_REASON_DATA_OVERWRITE      = 0x00000001
USN_REASON_DATA_EXTEND         = 0x00000002
USN_REASON_DATA_TRUNCATION     = 0x00000004
USN_REASON_NAMED_DATA_OVERWRITE = 0x00000010
USN_REASON_NAMED_DATA_EXTEND   = 0x00000020
USN_REASON_NAMED_DATA_TRUNCATION = 0x00000040
USN_REASON_FILE_CREATE         = 0x00000100
USN_REASON_FILE_DELETE         = 0x00000200
USN_REASON_EA_CHANGE           = 0x00000400
USN_REASON_SECURITY_CHANGE     = 0x00000800
USN_REASON_RENAME_OLD_NAME     = 0x00001000
USN_REASON_RENAME_NEW_NAME     = 0x00002000
USN_REASON_INDEXABLE_CHANGE    = 0x00004000
USN_REASON_BASIC_INFO_CHANGE   = 0x00008000
USN_REASON_HARD_LINK_CHANGE    = 0x00010000
USN_REASON_COMPRESSION_CHANGE  = 0x00020000
USN_REASON_ENCRYPTION_CHANGE   = 0x00040000
USN_REASON_OBJECT_ID_CHANGE    = 0x00080000
USN_REASON_REPARSE_POINT_CHANGE = 0x00100000
USN_REASON_STREAM_CHANGE       = 0x00200000
USN_REASON_CLOSE               = 0x80000000

# 文件内容真正发生了变化的 reason 组合
USN_REASON_CONTENT_MODIFIED = (
    USN_REASON_DATA_OVERWRITE |
    USN_REASON_DATA_EXTEND |
    USN_REASON_DATA_TRUNCATION |
    USN_REASON_NAMED_DATA_OVERWRITE |
    USN_REASON_NAMED_DATA_EXTEND |
    USN_REASON_NAMED_DATA_TRUNCATION |
    USN_REASON_STREAM_CHANGE
)

# 只关心这些 reason 事件
USN_REASON_MASK = (
    USN_REASON_CONTENT_MODIFIED |
    USN_REASON_FILE_CREATE |
    USN_REASON_FILE_DELETE |
    USN_REASON_RENAME_OLD_NAME |
    USN_REASON_RENAME_NEW_NAME |
    USN_REASON_CLOSE
)


# ═══════════════════════════════════════════════════════════════
# Windows API 结构体
# ═══════════════════════════════════════════════════════════════

class USN_RECORD_V3(ctypes.Structure):
    """USN 记录（V3 版本，Windows 10+）"""
    _fields_ = [
        ("RecordLength",         wintypes.DWORD),
        ("MajorVersion",         wintypes.WORD),
        ("MinorVersion",         wintypes.WORD),
        ("FileReferenceNumber",  wintypes.DWORDLONG),
        ("ParentFileReferenceNumber", wintypes.DWORDLONG),
        ("Usn",                  wintypes.LONGLONG),
        ("TimeStamp",            wintypes.LONGLONG),
        ("Reason",               wintypes.DWORD),
        ("SourceInfo",           wintypes.DWORD),
        ("SecurityId",           wintypes.DWORD),
        ("FileAttributes",       wintypes.DWORD),
        ("FileNameLength",       wintypes.WORD),
        ("FileNameOffset",       wintypes.WORD),
        ("FileName",             ctypes.c_wchar * 1),
    ]


class USN_JOURNAL_DATA_V2(ctypes.Structure):
    """USN Journal 查询结果"""
    _fields_ = [
        ("UsnJournalID",          wintypes.DWORDLONG),
        ("FirstUsn",             wintypes.LONGLONG),
        ("NextUsn",              wintypes.LONGLONG),
        ("LowestValidUsn",       wintypes.LONGLONG),
        ("MaxUsn",               wintypes.LONGLONG),
        ("MaximumSize",          wintypes.DWORDLONG),
        ("AllocationDelta",      wintypes.DWORDLONG),
        ("MinSupportedMajorVersion", wintypes.WORD),
        ("MaxSupportedMajorVersion", wintypes.WORD),
        ("Flags",                wintypes.DWORD),
        ("RangeTrackChunkSize",  wintypes.DWORD),
        ("RangeTrackFileSizeThreshold", wintypes.LONGLONG),
    ]


class READ_USN_JOURNAL_DATA_V1(ctypes.Structure):
    """FSCTL_READ_USN_JOURNAL 输入参数"""
    _fields_ = [
        ("StartUsn",         wintypes.LONGLONG),
        ("ReasonMask",       wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout",          wintypes.DWORDLONG),
        ("BytesToWaitFor",   wintypes.DWORDLONG),
        ("UsnJournalID",     wintypes.DWORDLONG),
        ("MinMajorVersion",  wintypes.WORD),
        ("MaxMajorVersion",  wintypes.WORD),
    ]


class FILE_ID_DESCRIPTOR(ctypes.Structure):
    """OpenFileById 的文件标识符"""
    _fields_ = [
        ("dwSize",       wintypes.DWORD),
        ("Type",         wintypes.DWORD),
        ("FileReferenceNumber", wintypes.DWORDLONG),
    ]


# ═══════════════════════════════════════════════════════════════
# Windows API 函数声明
# ═══════════════════════════════════════════════════════════════

kernel32 = ctypes.windll.kernel32

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPVOID,
]
kernel32.DeviceIoControl.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.OpenFileById.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
kernel32.OpenFileById.restype = wintypes.HANDLE

kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
]
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def open_volume_handle(volume: str) -> Optional[int]:
    """打开卷句柄，用于读取 USN Journal。失败返回 None。"""
    if not volume.endswith("\\"):
        volume += "\\"
    volume_clean = volume.rstrip('\\')
    handle = kernel32.CreateFileW(
        f"\\\\.\\{volume_clean}",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle == 0:
        return None
    return handle


def get_volume_for_path(path: str) -> str:
    """获取路径所在的卷（如 'E:\\'）"""
    norm = os.path.abspath(path)
    return os.path.splitdrive(norm)[0] + "\\"


def get_full_path_by_frn(volume_handle: int,
                         file_reference_number: int) -> Optional[str]:
    """
    通过 File Reference Number 获取文件的完整路径。
    先尝试 OpenFileById -> GetFinalPathNameByHandle 获取全路径。
    """
    fid = FILE_ID_DESCRIPTOR()
    fid.dwSize = ctypes.sizeof(FILE_ID_DESCRIPTOR)
    fid.Type = 0  # FileIdType
    fid.FileReferenceNumber = file_reference_number

    h_file = kernel32.OpenFileById(
        volume_handle,
        ctypes.byref(fid),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
    )
    if h_file == INVALID_HANDLE_VALUE or h_file == 0:
        return None

    try:
        buf = ctypes.create_unicode_buffer(32768)
        ret = kernel32.GetFinalPathNameByHandleW(
            h_file, buf, 32768, VOLUME_NAME_NT
        )
        if ret > 0 and ret < 32768:
            raw = buf.value
            return nt_to_dos_path(raw)
    finally:
        kernel32.CloseHandle(h_file)

    return None


# ── NT 路径转换 ───────────────────────────────────────────────

_drive_map_cache: Dict[str, str] = {}
_drive_map_lock = threading.Lock()


def nt_to_dos_path(nt_path: str) -> str:
    """将 NT 设备路径转换为 DOS 盘符路径"""
    if not nt_path or not nt_path.startswith("\\Device\\"):
        return nt_path

    with _drive_map_lock:
        if not _drive_map_cache:
            _build_drive_map_cache()
        for nt_prefix, dos_prefix in _drive_map_cache.items():
            if nt_path.startswith(nt_prefix):
                return dos_prefix + nt_path[len(nt_prefix):]

    # 回退：尝试用 QueryDosDevice 转换
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{c}:"
        buf = ctypes.create_unicode_buffer(1024)
        ret = kernel32.QueryDosDeviceW(drive, buf, 1024)
        if ret > 0:
            nt_prefix = buf.value
            if nt_path.startswith(nt_prefix):
                rest = nt_path[len(nt_prefix):]
                return drive + rest

    return nt_path


def _build_drive_map_cache():
    """构建 NT 设备路径 -> DOS 盘符的映射缓存"""
    _drive_map_cache.clear()
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{c}:"
        buf = ctypes.create_unicode_buffer(1024)
        ret = kernel32.QueryDosDeviceW(drive, buf, 1024)
        if ret > 0 and buf.value:
            _drive_map_cache[buf.value] = drive


def get_volume_root_from_dos(dos_path: str) -> str:
    """从 DOS 路径提取卷根路径（如 'E:\\'）"""
    return os.path.splitdrive(dos_path)[0] + "\\"


def align8(n: int) -> int:
    """将数值向上对齐到 8 字节边界（USN 记录按 64-bit 对齐）"""
    return (n + 7) & ~7
