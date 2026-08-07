"""
备份镜像管理 - 在文件修改时同步到备份目录

返回值约定：
    backup_file() 返回字符串状态：
    - "backed_up"    : 备份成功，内容一致，缓存可安全更新
    - "skipped"      : 文件未变化，无需备份
    - "dirty"        : 备份完成但源文件在备份期间被并发修改，缓存不应更新
    - "source_gone"  : 备份完成但源文件已删除，备份已移入回收站
    - "failed"       : 备份失败
"""

import os
import shutil
import fnmatch
import hashlib
import time
import threading
from typing import List, Optional, Set
from .config import Config
from .database import get_db

# 全局锁，防止文件操作冲突
_backup_lock = threading.Lock()


def _should_exclude(relative_path: str, config: Config) -> bool:
    """检查文件是否应该被排除"""
    basename = os.path.basename(relative_path)

    # 检查排除的文件模式
    for pattern in config.exclude_patterns:
        if fnmatch.fnmatch(basename, pattern):
            return True

    # 检查排除的目录
    parts = relative_path.replace("\\", "/").split("/")
    for part in parts:
        if part in config.exclude_dirs:
            return True

    return False


def _get_relative_path(abs_path: str, watch_root: str) -> str:
    """获取相对于监控根目录的路径"""
    return os.path.relpath(abs_path, watch_root)


def _compute_file_hash(file_path: str) -> Optional[str]:
    """计算文件 SHA256 哈希，用于判断文件是否真的变化了"""
    try:
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()
    except (IOError, OSError, PermissionError):
        return None


def _compute_hash_for_backup(backup_path: str, watch_root: str = "",
                             relative: str = "") -> Optional[str]:
    """计算备份中已有文件的哈希（优先从数据库读取）"""
    db = get_db()
    if db is not None:
        try:
            meta = db.get_backup_meta(watch_root, relative)
            if meta and meta.get("file_hash"):
                return meta["file_hash"]
        except Exception:
            pass
    # 回退：从 .meta 文件读取（兼容旧数据）
    meta_path = backup_path + ".meta"
    try:
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("hash:"):
                        return line.split(":", 1)[1].strip()
    except (IOError, OSError):
        pass
    return None


def _read_meta(backup_path: str, watch_root: str = "",
               relative: str = "") -> dict:
    """读取备份文件的元信息（优先从数据库读取）"""
    db = get_db()
    if db is not None:
        try:
            meta = db.get_backup_meta(watch_root, relative)
            if meta:
                return {
                    "hash": meta.get("file_hash", ""),
                    "size": str(meta.get("file_size", -1)),
                    "mtime": str(meta.get("mtime", -1)),
                    "source": meta.get("source_path", ""),
                    "backup_time": str(meta.get("backup_time", 0)),
                }
        except Exception:
            pass
    # 回退：从 .meta 文件读取（兼容旧数据）
    meta_path = backup_path + ".meta"
    result = {}
    try:
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if ":" in line:
                        key, val = line.split(":", 1)
                        result[key] = val
    except (IOError, OSError):
        pass
    return result


def _write_meta(backup_path: str, abs_src_path: str,
                file_hash: Optional[str] = None,
                watch_root: str = "", relative: str = ""):
    """写入备份文件的元信息（优先写入数据库）"""
    try:
        stat = os.stat(abs_src_path)
        if file_hash is None:
            file_hash = _compute_file_hash(abs_src_path) or "unknown"

        db = get_db()
        if db is not None:
            db.upsert_backup_meta(
                watch_root=watch_root,
                rel_path=relative,
                file_hash=file_hash,
                file_size=stat.st_size,
                mtime=stat.st_mtime,
                source_path=abs_src_path,
                backup_time=time.time(),
            )
        else:
            # 回退：写入 .meta 文件（兼容旧模式）
            meta_path = backup_path + ".meta"
            with open(meta_path, "w", encoding="utf-8") as f:
                f.write(f"hash:{file_hash}\n")
                f.write(f"size:{stat.st_size}\n")
                f.write(f"mtime:{stat.st_mtime}\n")
                f.write(f"source:{abs_src_path}\n")
                f.write(f"backup_time:{time.time()}\n")
    except (IOError, OSError) as e:
        pass  # 元信息写入失败不阻塞主流程


def _delete_file_safe(path: str) -> bool:
    """安全删除文件"""
    try:
        if os.path.isfile(path):
            os.remove(path)
        return True
    except (IOError, OSError):
        return False


def backup_file(abs_src_path: str, config: Config, logger) -> str:
    """
    将源文件备份到备份镜像目录。
    只在文件确实变化时才复制（通过比较哈希值）。

    返回状态字符串（详见模块文档）。
    """
    if not os.path.isfile(abs_src_path):
        return "skipped"

    watch_root = config.find_watch_root(abs_src_path)
    if watch_root is None:
        return "skipped"

    relative = _get_relative_path(abs_src_path, watch_root)

    if _should_exclude(relative, config):
        return "skipped"

    backup_path = os.path.join(config.backup_dir, relative)

    # 快速路径：如果文件大小和修改时间都没变，跳过备份
    try:
        src_stat = os.stat(abs_src_path)
        src_size = src_stat.st_size
        src_mtime = src_stat.st_mtime
    except OSError:
        return "skipped"

    if os.path.exists(backup_path):
        meta = _read_meta(backup_path, watch_root, relative)
        try:
            bak_size = int(meta.get("size", -1))
            bak_mtime = float(meta.get("mtime", -1))
            if bak_size == src_size and abs(bak_mtime - src_mtime) < 0.001:
                return "skipped"
        except (ValueError, TypeError):
            pass

    # 在锁外计算源文件哈希，避免大文件哈希计算长时间持有全局锁
    src_hash = _compute_file_hash(abs_src_path)
    if src_hash is None:
        return "failed"

    with _backup_lock:
        try:
            # 如果备份已存在，比较哈希
            if os.path.exists(backup_path):
                backup_hash = _compute_hash_for_backup(backup_path, watch_root, relative)
                if backup_hash and backup_hash == src_hash:
                    return "skipped"

                # 内容变了，删除旧备份及其元信息
                _delete_file_safe(backup_path)
                _delete_file_safe(backup_path + ".meta")

            # 复制到备份目录
            backup_dir = os.path.dirname(backup_path)
            os.makedirs(backup_dir, exist_ok=True)

            shutil.copy2(abs_src_path, backup_path)
            _write_meta(backup_path, abs_src_path, src_hash, watch_root, relative)

            # ── 竞态校验：备份后重新 stat 源文件 ──────────────
            # 检测备份期间是否有并发写入或删除
            try:
                post_stat = os.stat(abs_src_path)
                post_size = post_stat.st_size
                post_mtime = post_stat.st_mtime
            except OSError:
                # 源文件在备份期间被删除 → 将备份移入回收站
                try:
                    from .recycler import move_to_recycle
                    rp = move_to_recycle(
                        abs_src_path, False, config, logger
                    )
                    if rp:
                        logger.info(
                            f"备份后源文件已删除，已移入回收站: {relative}"
                        )
                    else:
                        logger.warning(
                            f"备份后源文件已删除，移入回收站失败: {relative}"
                        )
                except Exception as exc:
                    logger.error(f"移入回收站失败 {relative}: {exc}")
                return "source_gone"

            if (post_size != src_size or
                    abs(post_mtime - src_mtime) > 0.001):
                # 文件在备份期间被并发修改，备份内容可能不一致
                # 返回 dirty，让调用方不更新缓存，下轮重新备份
                logger.debug(
                    f"备份期间文件被并发修改: {relative}，将在下轮重新备份"
                )
                return "dirty"

            logger.debug(f"备份: {relative}")
            return "backed_up"

        except Exception as e:
            logger.error(f"备份失败 {relative}: {e}")
            return "failed"


def remove_backup(abs_src_path: str, config: Config, logger) -> bool:
    """
    从备份镜像中移除对应文件（当源文件被正常删除时调用）。
    注意：deleted 逻辑会将文件移动到回收站，这里是从备份目录清理残留。
    """
    watch_root = config.find_watch_root(abs_src_path)
    if watch_root is None:
        return False

    relative = _get_relative_path(abs_src_path, watch_root)

    if _should_exclude(relative, config):
        return False

    backup_path = os.path.join(config.backup_dir, relative)

    with _backup_lock:
        try:
            if os.path.exists(backup_path):
                return _delete_file_safe(backup_path)
            return True
        except Exception as e:
            logger.error(f"移除备份失败 {relative}: {e}")
            return False


def get_backup_path(abs_src_path: str, config: Config) -> Optional[str]:
    """获取源文件对应的备份文件路径"""
    watch_root = config.find_watch_root(abs_src_path)
    if watch_root is None:
        return None
    relative = _get_relative_path(abs_src_path, watch_root)
    backup_path = os.path.join(config.backup_dir, relative)
    if os.path.exists(backup_path):
        return backup_path
    return None


def backup_full_tree(config: Config, logger) -> int:
    """
    递归备份整个监控目录树（初始化时调用）。
    返回备份的文件数量。
    """
    count = 0
    for watch_path in config.watch_paths:
        if not os.path.exists(watch_path):
            logger.warning(f"监控路径不存在: {watch_path}")
            continue

        for root, dirs, files in os.walk(watch_path):
            # 过滤排除的目录
            dirs[:] = [
                d for d in dirs
                if d not in config.exclude_dirs
            ]

            for file_name in files:
                full_path = os.path.join(root, file_name)
                if backup_file(full_path, config, logger) == "backed_up":
                    count += 1

    logger.info(f"初始备份完成，共备份 {count} 个文件")
    return count
