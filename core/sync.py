"""
定期同步模块 - 主动扫描监控目录，将变更同步到备份目录。

解决 watchdog 无法可靠检测 SMB 网络共享变更的问题。

性能优化：
- 内存缓存每个文件的 (mtime, size)，未变化的文件只需 1 次 stat 调用
- 只有检测到变化时才调用 backup_file（读取 meta、计算哈希等）
- 对于 10 万文件的稳定目录，每轮扫描从 ~20 万次 I/O 降至 ~10 万次 stat
"""

import os
import time
import threading
import fnmatch
from typing import Dict, Tuple, Optional
from .config import Config
from .backup import backup_file


# 文件状态缓存: normpath(abs_path) -> (mtime, size)
# 用于快速跳过未变化的文件，避免每次都读取 meta 文件
_file_cache: Dict[str, Tuple[float, int]] = {}


def _should_exclude(relative_path: str, config: Config) -> bool:
    """检查文件是否应该被排除"""
    basename = os.path.basename(relative_path)
    for pattern in config.exclude_patterns:
        if fnmatch.fnmatch(basename, pattern):
            return True
    parts = relative_path.replace("\\", "/").split("/")
    for part in parts:
        if part in config.exclude_dirs:
            return True
    return False


def _get_file_stat(path: str) -> Optional[Tuple[float, int]]:
    """获取文件的 (mtime, size)，失败返回 None"""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _sync_scan(config: Config, logger) -> Tuple[int, int, int]:
    """
    扫描所有监控目录，将新增或修改的文件同步到备份。

    返回 (备份数, 跳过数, 扫描总数) 三元组。
    """
    backed_up = 0
    skipped = 0
    scanned = 0
    seen_keys = set()

    for watch_path in config.watch_paths:
        if not os.path.exists(watch_path):
            continue

        for root, dirs, files in os.walk(watch_path):
            # 过滤排除的目录
            dirs[:] = [d for d in dirs if d not in config.exclude_dirs]

            for file_name in files:
                full_path = os.path.join(root, file_name)
                rel = os.path.relpath(full_path, watch_path)
                if _should_exclude(rel, config):
                    continue

                scanned += 1
                cache_key = os.path.normcase(full_path)
                seen_keys.add(cache_key)

                # 快速路径：对比内存缓存，未变化则跳过
                current_stat = _get_file_stat(full_path)
                if current_stat is None:
                    continue  # 文件可能在扫描期间被删除

                cached = _file_cache.get(cache_key)
                if cached == current_stat:
                    skipped += 1
                    continue  # mtime + size 都没变，跳过

                # 文件有变化（新增或修改），执行备份
                try:
                    result = backup_file(full_path, config, logger)
                    if result == "backed_up":
                        backed_up += 1
                        # 备份成功且内容一致，安全更新缓存
                        _file_cache[cache_key] = current_stat
                    elif result == "dirty":
                        # 备份期间文件被并发修改，不更新缓存
                        # 下一轮扫描会重新备份
                        _file_cache.pop(cache_key, None)
                    elif result == "source_gone":
                        # 备份后源文件已删除，已自动移入回收站
                        # 清除缓存条目，下轮不再扫描
                        _file_cache.pop(cache_key, None)
                    else:
                        # skipped / failed，也更新缓存避免重复尝试
                        _file_cache[cache_key] = current_stat
                except Exception as e:
                    logger.error(f"同步扫描备份失败 {full_path}: {e}")

    # 清理已不存在的文件的缓存条目
    stale_keys = set(_file_cache.keys()) - seen_keys
    for k in stale_keys:
        del _file_cache[k]

    return backed_up, skipped, scanned


def _sync_thread(config: Config, logger, stop_event: threading.Event):
    """后台同步线程"""
    interval = config.sync.interval

    while not stop_event.is_set():
        try:
            backed_up, skipped, scanned = _sync_scan(config, logger)
            if backed_up > 0:
                logger.info(
                    f"定期同步: 扫描 {scanned} 个文件, "
                    f"备份 {backed_up} 个, 跳过 {skipped} 个(未变化)"
                )
            else:
                logger.debug(
                    f"定期同步: 扫描 {scanned} 个文件, "
                    f"全部未变化, 跳过 {skipped} 个"
                )
        except Exception as e:
            logger.error(f"同步扫描异常: {e}")

        stop_event.wait(interval)


def start_sync(config: Config, logger) -> tuple:
    """
    启动定期同步线程。

    返回 (thread, stop_event) 元组。
    """
    if not config.sync.enabled:
        logger.info("定期同步已禁用")
        return None, None

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_sync_thread,
        args=(config, logger, stop_event),
        daemon=True,
        name="SyncThread"
    )
    thread.start()
    logger.info(f"定期同步任务已启动（间隔 {config.sync.interval} 秒）")
    return thread, stop_event
