"""
USN 路径解析模块。

包含两个核心组件：
1. FrnPathCache: FRN -> 绝对路径的内存缓存（LRU 淘汰）
2. DirectoryIdentityCache: 基于 FRN parent chain 的目录身份缓存

DirectoryIdentityCache 的核心价值（方案第十四/十五节）：
- 目录 rename 时只需更新目录 FRN 的 name，子节点自动继承新路径
- 避免遍历百万文件来更新路径
- FRN -> (parent_frn, name) 的映射关系

注意：FRN 不是永久 UUID，删除后可能复用（方案第十二节）。
"""

import os
import ctypes
import threading
from ctypes import wintypes
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass, field
from collections import OrderedDict

from .record import (
    get_full_path_by_frn,
    kernel32,
    GENERIC_READ,
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE,
    OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    INVALID_HANDLE_VALUE,
)


# ═══════════════════════════════════════════════════════════════
# FRN -> 路径缓存（LRU）
# ═══════════════════════════════════════════════════════════════

FRN_PATH_CACHE_CAPACITY = 500_000  # P2-1: 50K→500K


class FrnPathCache:
    """
    文件引用号 -> 绝对路径的内存缓存。
    用于快速解析 USN 记录中的文件路径，避免每次 OpenFileById。
    P2-1 修复：使用 OrderedDict 标准 LRU 淘汰。
    """

    def __init__(self, max_size: int = FRN_PATH_CACHE_CAPACITY):
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, frn: int) -> Optional[str]:
        with self._lock:
            if frn in self._cache:
                self._cache.move_to_end(frn)
                return self._cache[frn]
        return None

    def set(self, frn: int, path: str):
        with self._lock:
            if frn in self._cache:
                self._cache.move_to_end(frn)
            self._cache[frn] = path
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def remove(self, frn: int):
        with self._lock:
            self._cache.pop(frn, None)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def __len__(self):
        with self._lock:
            return len(self._cache)


# ═══════════════════════════════════════════════════════════════
# 目录身份缓存（方案第十四/十五节）
# ═══════════════════════════════════════════════════════════════

@dataclass
class DirIdentity:
    """目录的 FRN 身份信息"""
    frn: int
    parent_frn: int
    name: str
    volume_id: str


class DirectoryIdentityCache:
    """
    目录 FRN -> (parent_frn, name, volume_id) 的内存缓存。

    核心价值（方案第十四/十五节）：
    - 通过 parent chain 递归解析完整路径
    - 目录 rename 时只需更新该目录的 name/parent_frn，
      所有子目录通过 parent chain 自动继承新路径
    - 避免遍历子树下百万文件来更新路径

    线程安全：使用读写锁保护。
    """

    CAPACITY = 500_000  # P2-1: 100K→500K

    def __init__(self):
        # frn -> DirIdentity，使用 OrderedDict 标准 LRU
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, frn: int) -> Optional[DirIdentity]:
        """查询目录的 FRN 身份信息"""
        with self._lock:
            if frn in self._cache:
                self._cache.move_to_end(frn)
                return self._cache[frn]
        return None

    def set(self, frn: int, parent_frn: int, name: str, volume_id: str):
        """添加/更新目录的 FRN 身份信息"""
        with self._lock:
            if frn in self._cache:
                self._cache.move_to_end(frn)
            self._cache[frn] = DirIdentity(
                frn=frn,
                parent_frn=parent_frn,
                name=name,
                volume_id=volume_id,
            )
            while len(self._cache) > self.CAPACITY:
                self._cache.popitem(last=False)

    def update_rename(self, frn: int, new_name: str,
                      new_parent_frn: Optional[int] = None):
        """
        处理目录 rename（方案第十五节）。

        只需更新该目录 FRN 的 name（和可选的 parent_frn），
        所有子目录通过 parent chain 自动继承新路径。
        不需要遍历子树中的任何文件。
        """
        with self._lock:
            identity = self._cache.get(frn)
            if identity is not None:
                identity.name = new_name
                if new_parent_frn is not None:
                    identity.parent_frn = new_parent_frn

    def resolve_path(self, frn: int, volume_handle: int,
                     volume_id: str) -> Optional[str]:
        """
        通过 parent chain 递归解析 FRN 对应的完整路径。

        策略：
        1. 从缓存中查找 FRN 的身份信息
        2. 如果缓存未命中，尝试通过 OpenFileById 获取路径
        3. 如果 OpenFileById 也失败，尝试通过父 FRN 递归构建

        Args:
            frn: 文件/目录的 FRN
            volume_handle: 卷句柄（用于 OpenFileById 回退）
            volume_id: 卷标识（如 'E:'）

        Returns:
            完整路径或 None
        """
        # 先尝试通过 parent chain 构建
        parts = []
        visited = set()  # 防止循环引用
        current_frn = frn

        while current_frn != 0 and current_frn not in visited:
            visited.add(current_frn)
            identity = self.get(current_frn)
            if identity is None:
                # 缓存未命中，尝试 OpenFileById
                path = get_full_path_by_frn(volume_handle, current_frn)
                if path:
                    # 成功获取路径，缓存结果
                    parts.insert(0, path)
                    return os.path.join(*parts) if parts else None
                # 无法解析
                return None

            parts.insert(0, identity.name)
            current_frn = identity.parent_frn

        if parts:
            # 构建完整路径：volume_root + 各层目录名
            volume_root = volume_id.rstrip(':') + ":" + os.sep
            # 移除第一层（根目录名，如 "share"），因为 volume_root 已包含盘符
            if len(parts) > 1:
                return volume_root + os.sep.join(parts[1:])
            else:
                return volume_root + parts[0]

        return None

    def remove(self, frn: int):
        """删除目录的 FRN 身份信息（文件被删除时调用）"""
        with self._lock:
            self._cache.pop(frn, None)

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    def __len__(self):
        with self._lock:
            return len(self._cache)

    def build_from_walk(self, watch_paths: List[str], volume_handle: int,
                        volume_id: str):
        """
        首次构建目录身份缓存。

        通过 os.walk 遍历目录树，对每个目录执行 OpenFileById 获取 FRN，
        建立 FRN -> (parent_frn, name) 的映射。

        注意：此操作仅在首次启动或 reconciliation 时执行。
        """
        for watch_path in watch_paths:
            if not os.path.exists(watch_path):
                continue

            for root, dirs, _ in os.walk(watch_path):
                # 为当前目录获取 FRN
                root_frn = self._get_frn_for_path(root, volume_handle)
                if root_frn is None:
                    continue

                for d in dirs:
                    dir_path = os.path.join(root, d)
                    dir_frn = self._get_frn_for_path(dir_path, volume_handle)
                    if dir_frn is not None:
                        self.set(dir_frn, root_frn, d, volume_id)

    def _get_frn_for_path(self, path: str,
                          volume_handle: int) -> Optional[int]:
        """
        获取路径的 FRN（通过 file index）。

        使用 Windows BY_HANDLE_FILE_INFORMATION 获取 FileIndex。
        """
        try:
            handle = kernel32.CreateFileW(
                path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if handle == INVALID_HANDLE_VALUE or handle == 0:
                return None

            try:
                class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
                    _fields_ = [
                        ("dwFileAttributes", wintypes.DWORD),
                        ("ftCreationTime", ctypes.c_uint64),
                        ("ftLastAccessTime", ctypes.c_uint64),
                        ("ftLastWriteTime", ctypes.c_uint64),
                        ("dwVolumeSerialNumber", wintypes.DWORD),
                        ("nFileSizeHigh", wintypes.DWORD),
                        ("nFileSizeLow", wintypes.DWORD),
                        ("nNumberOfLinks", wintypes.DWORD),
                        ("nFileIndexHigh", wintypes.DWORD),
                        ("nFileIndexLow", wintypes.DWORD),
                    ]

                info = BY_HANDLE_FILE_INFORMATION()
                result = kernel32.GetFileInformationByHandle(
                    handle, ctypes.byref(info)
                )
                if result:
                    frn = (info.nFileIndexHigh << 32) | info.nFileIndexLow
                    return frn
            finally:
                kernel32.CloseHandle(handle)

        except Exception:
            pass

        return None
