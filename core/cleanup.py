"""
定期清理模块 - 清理过期的回收站文件和孤立的备份文件
"""

import os
import time
import threading
from typing import Dict
from .config import Config
from .recycler import cleanup_expired, move_to_recycle
from .database import get_db

# 记录备份文件首次被发现"源文件不存在"的时刻，
# 宽限期从该时刻起算，避免旧备份被立即清理导致数据丢失
_missing_since: Dict[str, float] = {}
# 防止 _missing_since 无限增长：超过上限时淘汰最旧的条目
_MISSING_SINCE_MAX = 500_000


def _evict_missing_since():
    """当 _missing_since 超过上限时，淘汰最旧的一半条目"""
    if len(_missing_since) <= _MISSING_SINCE_MAX:
        return
    sorted_items = sorted(_missing_since.items(), key=lambda x: x[1])
    evict_count = len(sorted_items) // 2
    for key, _ in sorted_items[:evict_count]:
        del _missing_since[key]


def _source_exists(rel_path: str, config: Config) -> bool:
    """检查源文件在任一监控路径下是否存在"""
    for wp in config.watch_paths:
        if os.path.exists(os.path.join(wp, rel_path)):
            return True
    return False


def _build_source_file_set(config: Config, logger) -> set:
    """
    一次性遍历所有监控源目录，构建现有文件路径集合。
    用于批量对比替代逐条 os.path.exists 调用，大幅减少 SMB 开销。
    """
    existing = set()
    for wp in config.watch_paths:
        if not os.path.exists(wp):
            continue
        for root, dirs, files in os.walk(wp):
            dirs[:] = [d for d in dirs if d not in config.exclude_dirs]
            for f in files:
                abs_path = os.path.join(root, f)
                rel = os.path.relpath(abs_path, wp)
                existing.add(os.path.normcase(rel))
    logger.debug(f"源文件集合构建完成，共 {len(existing)} 个文件")
    return existing


def _cleanup_orphaned_backups(config: Config, logger):
    """
    清理备份目录中源目录已不存在的孤立文件。
    移入回收站而非直接删除，保留数据安全。

    性能优化（亿级场景）：
    - 当备份记录数较多时，先一次性遍历源目录构建文件集合，
      用集合查找替代逐条 os.path.exists，大幅减少 SMB 调用次数
    """
    if not os.path.exists(config.backup_dir):
        return 0

    cleaned = 0
    grace = config.mirror_cleanup.grace_period
    db = get_db()
    _BATCH_OPTIMIZE_THRESHOLD = 10_000  # 超过此数量启用批量优化

    if db is not None:
        try:
            # 估算备份数量，决定是否启用批量优化
            try:
                backup_count = db.count_backup_meta()
            except Exception:
                backup_count = 0

            use_batch = backup_count >= _BATCH_OPTIMIZE_THRESHOLD
            source_files = None
            if use_batch:
                logger.info(
                    f"备份数量 {backup_count}，启用源目录批量对比优化"
                )
                source_files = _build_source_file_set(config, logger)

            for batch in db.iter_backup_meta_batch(batch_size=5000):
                for meta in batch:
                    rel_path = meta.get("rel_path", "")
                    watch_root = meta.get("watch_root", "")
                    backup_path = os.path.join(config.backup_dir, rel_path)

                    key = os.path.normcase(backup_path)

                    # 检查源文件是否存在
                    if use_batch:
                        src_exists = os.path.normcase(rel_path) in source_files
                    else:
                        src_exists = _source_exists(rel_path, config)

                    if src_exists:
                        _missing_since.pop(key, None)
                        continue

                    now = time.time()
                    first_missing = _missing_since.setdefault(key, now)
                    if now - first_missing < grace:
                        continue

                    try:
                        src_path_for_recycle = os.path.join(watch_root, rel_path)
                        recycle_path = move_to_recycle(
                            src_path_for_recycle, False, config, logger
                        )
                        if recycle_path:
                            cleaned += 1
                            logger.debug(f"孤立备份已移入回收站: {rel_path}")
                            db.delete_backup_meta(watch_root, rel_path)
                        else:
                            if os.path.exists(backup_path):
                                os.remove(backup_path)
                            db.delete_backup_meta(watch_root, rel_path)
                            cleaned += 1
                            logger.debug(f"清理孤立备份: {rel_path}")
                    except OSError as e:
                        logger.error(f"清理孤立备份失败: {e}")
                    finally:
                        _missing_since.pop(key, None)

                # 每批处理后检查 _missing_since 大小
                _evict_missing_since()

            if cleaned > 0:
                logger.info(f"已清理 {cleaned} 个孤立备份文件")
            return cleaned
        except Exception:
            pass  # 回退到文件遍历方式

    # 回退：文件遍历方式（兼容旧数据）
    for root, dirs, files in os.walk(config.backup_dir, topdown=False):
        for file_name in files:
            if file_name.endswith(".meta"):
                continue  # 元信息文件跟随主文件一起处理

            backup_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(backup_path, config.backup_dir)

            key = os.path.normcase(backup_path)

            if _source_exists(rel_path, config):
                _missing_since.pop(key, None)  # 源文件还在，重置记录
                continue

            # 源文件已删除，从首次发现消失时起算宽限期
            now = time.time()
            first_missing = _missing_since.setdefault(key, now)
            if now - first_missing < grace:
                continue  # 还在宽限期内（删除事件可能尚未完成回收）

            # 清理：移入回收站而非直接删除，保留数据安全
            try:
                src_path_for_recycle = os.path.join(
                    config.watch_paths[0], rel_path
                )
                recycle_path = move_to_recycle(
                    src_path_for_recycle, False, config, logger
                )
                if recycle_path:
                    cleaned += 1
                    logger.debug(
                        f"孤立备份已移入回收站: {rel_path}"
                    )
                else:
                    # move_to_recycle 返回 None，直接清理残留
                    meta_path = backup_path + ".meta"
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                    if os.path.exists(meta_path):
                        os.remove(meta_path)
                    cleaned += 1
                    logger.debug(f"清理孤立备份: {rel_path}")
            except OSError as e:
                logger.error(f"清理孤立备份失败: {e}")
            finally:
                _missing_since.pop(key, None)

        # 清理空目录
        if root != config.backup_dir:
            try:
                remaining = os.listdir(root)
                if not remaining:
                    os.rmdir(root)
            except OSError:
                pass
            except FileNotFoundError:
                pass

    if cleaned > 0:
        logger.info(f"已清理 {cleaned} 个孤立备份文件")
    return cleaned


def _cleanup_thread(config: Config, logger, stop_event: threading.Event):
    """后台清理线程"""
    interval = config.mirror_cleanup.interval

    while not stop_event.is_set():
        try:
            # 清理过期回收站
            cleanup_expired(config, logger)

            # 清理孤立备份
            if config.mirror_cleanup.enabled:
                _cleanup_orphaned_backups(config, logger)
        except Exception as e:
            logger.error(f"清理任务异常: {e}")

        # 等待下一次执行
        stop_event.wait(interval)


def start_cleanup(config: Config, logger) -> tuple:
    """
    启动定期清理线程。

    返回 (thread, stop_event) 元组。
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_cleanup_thread,
        args=(config, logger, stop_event),
        daemon=True,
        name="CleanupThread"
    )
    thread.start()
    logger.info(f"定期清理任务已启动（间隔 {config.mirror_cleanup.interval} 秒）")
    return thread, stop_event
