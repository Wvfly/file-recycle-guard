"""
备份镜像管理 - 在文件修改时同步到备份目录

返回值约定：
    backup_file() 返回字符串状态：
    - "backed_up"    : 备份成功，内容一致，缓存可安全更新
    - "skipped"      : 文件未变化，无需备份
    - "dirty"        : 备份完成但源文件在备份期间被并发修改，缓存不应更新
    - "source_gone"  : 备份完成但源文件已删除，备份已移入回收站
    - "failed"       : 备份失败

性能优化（亿级场景）：
- per-file 锁替代全局锁，允许多文件并行复制（大幅提升 SMB 吞吐）
- per-file 锁采用 LRU 淘汰，亿级文件不会 OOM
- 目录创建使用独立锁，避免 os.makedirs 并发冲突
- backup_full_tree 流式遍历，不一次性加载所有文件路径到内存
- 活跃备份跟踪机制，替代全局锁供 watcher 等待
"""

import os
import shutil
import fnmatch
import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Set
from .config import Config
from .database import get_db

# per-file 锁（固定大小锁池，消除 LRU 淘汰正确性风险）
# 4096 个锁 + 8 workers → 碰撞概率极低，碰撞仅导致串行化（不影响正确性）
_FILE_LOCK_POOL_SIZE = 4096
_file_locks: list = [threading.Lock() for _ in range(_FILE_LOCK_POOL_SIZE)]

# 目录创建锁（防止 os.makedirs 并发冲突）
_makedirs_lock = threading.Lock()

# 活跃备份跟踪（替代全局 _backup_lock，供 watcher 等待）
_active_backups: set = set()
_active_backups_cond = threading.Condition()

# ── MySQL 元信息写入缓冲（攒批替代逐条提交） ───────────────────
_meta_buffer_lock = threading.Lock()
_meta_buffer: list = []
_META_BUFFER_THRESHOLD = 50


def _flush_meta_buffer():
    """刷新 MySQL 元信息写入缓冲区（批量 upsert）"""
    with _meta_buffer_lock:
        if not _meta_buffer:
            return
        items = _meta_buffer[:]
        _meta_buffer.clear()
    try:
        db = get_db()
        if db is not None:
            db.batch_upsert_backup_meta(items)
    except Exception:
        pass


def _get_file_lock(path: str) -> threading.Lock:
    """获取指定文件的锁（基于路径哈希选槽，固定大小锁池无淘汰风险）"""
    norm = os.path.normcase(os.path.abspath(path))
    return _file_locks[hash(norm) % _FILE_LOCK_POOL_SIZE]


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


def _safe_stat(path: str):
    """
    安全获取文件 stat 信息。
    对于 SMB 网络共享上不支持 stat 的文件（WinError 50），静默返回 None。
    """
    try:
        return os.stat(path)
    except OSError:
        return None


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
        stat = _safe_stat(abs_src_path)
        if stat is None:
            return  # 无法获取 stat 信息，跳过元信息写入
        if file_hash is None:
            file_hash = _compute_file_hash(abs_src_path) or "unknown"

        db = get_db()
        if db is not None:
            items = None
            with _meta_buffer_lock:
                _meta_buffer.append({
                    "watch_root": watch_root,
                    "rel_path": relative,
                    "file_hash": file_hash,
                    "file_size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "source_path": abs_src_path,
                    "backup_time": time.time(),
                })
                if len(_meta_buffer) >= _META_BUFFER_THRESHOLD:
                    items = _meta_buffer[:]
                    _meta_buffer.clear()
            if items is not None:
                try:
                    db.batch_upsert_backup_meta(items)
                except Exception:
                    pass  # 元信息写入失败不阻塞主流程
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

    # per-file 锁：防止同一文件被并发备份
    flock = _get_file_lock(abs_src_path)
    if not flock.acquire(timeout=0.1):
        return "skipped"  # 另一个线程正在备份此文件

    # 注册为活跃备份（供 watcher 等待）
    norm_path = os.path.normcase(os.path.abspath(abs_src_path))
    with _active_backups_cond:
        _active_backups.add(norm_path)

    try:
        return _backup_file_inner(abs_src_path, watch_root, relative, config, logger)
    finally:
        flock.release()
        with _active_backups_cond:
            _active_backups.discard(norm_path)
            _active_backups_cond.notify_all()


def _backup_file_inner(abs_src_path: str, watch_root: str,
                       relative: str, config: Config, logger) -> str:
    """备份核心逻辑（已在 per-file 锁内）"""
    backup_path = os.path.join(config.backup_dir, relative)

    # 快速路径：如果文件大小和修改时间都没变，跳过备份
    src_stat = _safe_stat(abs_src_path)
    if src_stat is None:
        return "skipped"  # 无法获取文件元数据（如 SMB WinError 50）
    src_size = src_stat.st_size
    src_mtime = src_stat.st_mtime

    if os.path.exists(backup_path):
        meta = _read_meta(backup_path, watch_root, relative)
        try:
            bak_size = int(meta.get("size", -1))
            bak_mtime = float(meta.get("mtime", -1))
            if bak_size == src_size and abs(bak_mtime - src_mtime) < 0.001:
                return "skipped"
        except (ValueError, TypeError):
            pass

    # 在锁外计算源文件哈希，避免大文件哈希计算长时间持有锁
    src_hash = _compute_file_hash(abs_src_path)
    if src_hash is None:
        return "failed"

    # per-file 锁已保证同一文件不会并发备份，不同文件可并行复制
    try:
        # 如果备份已存在，比较哈希
        if os.path.exists(backup_path):
            backup_hash = _compute_hash_for_backup(backup_path, watch_root, relative)
            if backup_hash and backup_hash == src_hash:
                return "skipped"

            # 内容变了，删除旧备份及其元信息
            _delete_file_safe(backup_path)
            _delete_file_safe(backup_path + ".meta")

        # 复制到备份目录（目录创建用独立锁保护）
        backup_dir = os.path.dirname(backup_path)
        with _makedirs_lock:
            os.makedirs(backup_dir, exist_ok=True)

        shutil.copy2(abs_src_path, backup_path)
        _write_meta(backup_path, abs_src_path, src_hash, watch_root, relative)

        # ── 竞态校验：备份后重新 stat 源文件 ──────────────
        post_stat = _safe_stat(abs_src_path)
        if post_stat is None:
            # 源文件在备份期间被删除或不可访问
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

        post_size = post_stat.st_size
        post_mtime = post_stat.st_mtime

        if (post_size != src_size or
                abs(post_mtime - src_mtime) > 0.001):
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
    """
    watch_root = config.find_watch_root(abs_src_path)
    if watch_root is None:
        return False

    relative = _get_relative_path(abs_src_path, watch_root)

    if _should_exclude(relative, config):
        return False

    backup_path = os.path.join(config.backup_dir, relative)

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


def backup_full_tree(config: Config, logger, max_workers: int = 8) -> int:
    """
    递归备份整个监控目录树（初始化时调用）。

    流式处理（亿级优化）：
    - 边遍历边备份，每攒够一批（2000 个）就提交线程池
    - 不会将所有文件路径一次性加载到内存
    - 8 线程并行复制（移除全局锁后真正并行）
    - 信号量限制最大在途任务数，防止 pending_futures 无限增长导致 OOM

    返回备份的文件数量。
    """
    count = 0
    failed = 0
    total_found = 0
    _BATCH_SIZE = 2000
    # 最大在途任务数：防止亿级文件时 pending_futures 无限增长
    _MAX_IN_FLIGHT = max_workers * 500  # 8 workers → 4000 在途

    for watch_path in config.watch_paths:
        if not os.path.exists(watch_path):
            logger.warning(f"监控路径不存在: {watch_path}")
            continue

        logger.info(f"初始备份: 开始流式遍历 {watch_path} (workers={max_workers})")

        batch = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            # 信号量：限制在途任务数，防止内存无限增长
            in_flight_sem = threading.Semaphore(_MAX_IN_FLIGHT)
            pending_futures = []

            for root, dirs, files in os.walk(watch_path):
                dirs[:] = [
                    d for d in dirs
                    if d not in config.exclude_dirs
                ]
                for file_name in files:
                    full_path = os.path.join(root, file_name)
                    rel = os.path.relpath(full_path, watch_path)
                    if not _should_exclude(rel, config):
                        batch.append(full_path)

                    if len(batch) >= _BATCH_SIZE:
                        # 获取信号量：超出限制时阻塞等待已完成任务释放
                        for _ in batch:
                            in_flight_sem.acquire()
                        futures = [
                            pool.submit(backup_file, fp, config, logger)
                            for fp in batch
                        ]
                        for f in futures:
                            f.add_done_callback(lambda _: in_flight_sem.release())
                        pending_futures.extend(futures)
                        total_found += len(batch)
                        batch.clear()

                        # 收割已完成的 future
                        done_futures = [f for f in pending_futures if f.done()]
                        for f in done_futures:
                            try:
                                result = f.result()
                                if result == "backed_up":
                                    count += 1
                                elif result == "failed":
                                    failed += 1
                            except Exception as e:
                                failed += 1
                                logger.error(f"初始备份异常: {e}")
                        pending_futures = [
                            f for f in pending_futures if not f.done()
                        ]

                        done_total = count + failed
                        if done_total % 5000 < _BATCH_SIZE:
                            logger.info(
                                f"初始备份进度: 已发现 {total_found} 个文件, "
                                f"已备份 {count}, 失败 {failed}, "
                                f"在途 {len(pending_futures)}"
                            )

            # 处理最后一批
            if batch:
                for _ in batch:
                    in_flight_sem.acquire()
                futures = [
                    pool.submit(backup_file, fp, config, logger)
                    for fp in batch
                ]
                for f in futures:
                    f.add_done_callback(lambda _: in_flight_sem.release())
                pending_futures.extend(futures)
                total_found += len(batch)
                batch.clear()

            # 收割所有剩余 future
            for future in as_completed(pending_futures):
                try:
                    result = future.result()
                    if result == "backed_up":
                        count += 1
                    elif result == "failed":
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"初始备份异常: {e}")

        _flush_meta_buffer()

    logger.info(
        f"初始备份完成: 发现 {total_found} 个文件, "
        f"共备份 {count} 个, 失败 {failed} 个"
    )
    return count
