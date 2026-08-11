"""
文件系统监控模块 - 使用 watchdog 监控共享文件夹变更
"""

import os
import time
import fnmatch
import threading
from collections import deque
from typing import Dict, Tuple
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from .config import Config
from .backup import backup_file, backup_full_tree
from .recycler import move_to_recycle

# 删除事件确认延迟（秒）：等待这么久后文件仍未重建，才视为真删除
_DELETE_CONFIRM_DELAY = 2.0

# 等待备份完成的最大时间（秒）
_BACKUP_WAIT_TIMEOUT = 5


class RecycleGuardHandler(FileSystemEventHandler):
    """
    文件系统事件处理器。

    策略：
    - on_created / on_modified → 备份到镜像目录
    - on_deleted → 从镜像目录移动到回收站
    """

    def __init__(self, config: Config, logger):
        super().__init__()
        self.config = config
        self.logger = logger
        # 去重字典：同一文件同一事件 2 秒内只处理一次（O(1) 查找替代原 O(n) deque 扫描）
        # key: (src_path, event_type) → value: timestamp
        self._recent_events: Dict[Tuple[str, str], float] = {}
        self._recent_events_lock = threading.Lock()
        # 待确认的删除事件：(路径, 是否目录, 事件时间)
        self._pending_deletes: deque = deque()
        # 用 set 加速路径查找（O(1) 替代 O(n) 线性搜索）
        self._pending_paths: set = set()
        self._pending_lock = threading.Lock()
        # 单一工作线程处理延迟删除，避免每个删除事件新建线程
        self._delete_worker = threading.Thread(
            target=self._delete_worker_loop,
            daemon=True,
            name="PendingDeleteWorker"
        )
        self._delete_worker.start()
        # 批量延迟备份：on_created 只记录路径，后台线程定期批量处理
        # 解决大批量文件拷贝时线程池队列积压导致 watchdog 事件丢失的问题
        self._pending_created: set = set()  # 待备份的创建文件路径
        self._created_sizes: Dict[str, int] = {}  # 上次检查的文件大小
        self._created_lock = threading.Lock()
        # 失败重试跟踪：path → (retry_count, next_retry_time)
        self._retry_queue: Dict[str, Tuple[int, float]] = {}
        self._batch_backup_worker = threading.Thread(
            target=self._batch_backup_loop,
            daemon=True,
            name="BatchBackupWorker"
        )
        self._batch_backup_worker.start()

    def _cleanup_file_tracking(self, src_path: str):
        """清除文件的所有跟踪条目（P2-15: 防止 _created_sizes 内存泄漏）"""
        self._created_sizes.pop(src_path, None)
        self._created_sizes.pop(f"__stable__{src_path}", None)
        self._created_sizes.pop(f"__first_seen__{src_path}", None)
        self._retry_queue.pop(src_path, None)

    def _is_duplicate(self, src_path: str, event_type: str) -> bool:
        """去重：同一文件同一事件 2 秒内只处理一次（O(1) dict 查找）。
        
        关键改进：不同事件类型之间不互斥（如 created → modified 应放行），
        因为 on_created 时文件可能未写完，需要 on_modified 补上正确备份。
        """
        now = time.time()
        key = (src_path, event_type)
        with self._recent_events_lock:
            # 检查是否重复（仅同类型事件才拦截）
            prev_time = self._recent_events.get(key)
            if prev_time is not None and now - prev_time < 2:
                return True
            # 记录本次事件
            self._recent_events[key] = now
            # 定期清理过期项（超过 1000 条时触发，避免字典无限增长）
            if len(self._recent_events) > 1000:
                expired = [
                    k for k, t in self._recent_events.items()
                    if now - t > 3
                ]
                for k in expired:
                    del self._recent_events[k]
        return False

    def _is_pending_delete(self, src_path: str) -> bool:
        """检查该路径是否已在待删除队列中（O(1) set 查找）"""
        with self._pending_lock:
            return src_path in self._pending_paths

    def _is_under(self, path: str, base: str) -> bool:
        """判断 path 是否位于 base 目录内（归一化比较，避免前缀误匹配）"""
        try:
            norm_path = os.path.normcase(os.path.abspath(path))
            norm_base = os.path.normcase(os.path.abspath(base))
        except (OSError, ValueError):
            return False
        return norm_path == norm_base or norm_path.startswith(norm_base + os.sep)

    def _should_exclude(self, path: str) -> bool:
        """检查路径是否应该被排除"""
        basename = os.path.basename(path)

        # 排除备份目录和回收站本身
        if self._is_under(path, self.config.backup_dir):
            return True
        if self._is_under(path, self.config.recycle_dir):
            return True

        for pattern in self.config.exclude_patterns:
            if fnmatch.fnmatch(basename, pattern):
                return True

        # 检查父级目录名
        parts = path.replace("\\", "/").split("/")
        for part in parts:
            if part in self.config.exclude_dirs:
                return True

        return False

    def on_created(self, event: FileSystemEvent):
        if event.is_directory or self._should_exclude(event.src_path):
            return

        if self._is_duplicate(event.src_path, "created"):
            return

        self.logger.debug(f"文件创建: {event.src_path}")
        # 只记录路径，不阻塞事件分发线程
        # 后台批量处理线程会定期检查文件稳定性并备份
        with self._created_lock:
            self._pending_created.add(event.src_path)
            self._created_sizes.pop(event.src_path, None)  # 重置大小记录

    def _batch_backup_loop(self):
        """
        后台批量备份工作线程。
        
        每隔 _BATCH_BACKUP_INTERVAL 秒扫描一次待备份文件，
        对已稳定的文件执行备份，不稳定的继续等待。
        
        优势：
        - on_created 只记录路径（O(1)），不阻塞 watchdog 事件队列
        - 单线程轮询所有待备份文件，无线程池队列积压问题
        - 大文件和小文件互不阻塞
        """
        _BATCH_BACKUP_INTERVAL = 3.0  # 扫描间隔（秒）
        _STABLE_THRESHOLD = 2  # 连续稳定次数
        _MAX_AGE = 600.0  # 文件最大等待时间（秒），超时强制备份

        while True:
            try:
                self._process_pending_created(
                    _STABLE_THRESHOLD, _MAX_AGE
                )
            except Exception as e:
                self.logger.error(f"批量备份循环异常: {e}")

            time.sleep(_BATCH_BACKUP_INTERVAL)

    def _process_pending_created(self, stable_threshold: int,
                                  max_age: float):
        """处理一批待备份的创建文件"""
        now = time.time()
        to_backup = []
        still_waiting = []

        with self._created_lock:
            for src_path in list(self._pending_created):
                try:
                    if not os.path.isfile(src_path):
                        # 文件已不存在，清除所有跟踪条目（P2-15）
                        self._cleanup_file_tracking(src_path)
                        continue

                    # 优先检查重试延迟：未到重试时间的文件继续等待
                    retry_info = self._retry_queue.get(src_path)
                    if retry_info is not None:
                        _, retry_time = retry_info
                        if now < retry_time:
                            still_waiting.append(src_path)
                            continue
                        # 重试时间已到，清除重试记录，正常处理
                        self._retry_queue.pop(src_path, None)
                        # 重置稳定计数，防止之前的高计数导致立即备份
                        self._created_sizes[f"__stable__{src_path}"] = 0

                    current_size = os.path.getsize(src_path)
                    prev_size = self._created_sizes.get(src_path, -1)
                    first_seen = self._created_sizes.get(
                        f"__first_seen__{src_path}", now
                    )

                    if current_size == prev_size and current_size > 0:
                        # 大小未变，增加稳定计数
                        stable = self._created_sizes.get(
                            f"__stable__{src_path}", 0
                        ) + 1
                        self._created_sizes[f"__stable__{src_path}"] = stable

                        if stable >= stable_threshold:
                            to_backup.append(src_path)
                            continue
                    else:
                        # 大小变了，重置稳定计数
                        self._created_sizes[f"__stable__{src_path}"] = 0
                        self._created_sizes[f"__first_seen__{src_path}"] = now

                    self._created_sizes[src_path] = current_size

                    # 检查是否超时
                    if now - first_seen >= max_age:
                        to_backup.append(src_path)
                    else:
                        still_waiting.append(src_path)

                except OSError:
                    # 文件不可访问，继续等待
                    still_waiting.append(src_path)

            # 更新待处理集合
            self._pending_created.clear()
            for p in still_waiting:
                self._pending_created.add(p)

        # 批量执行备份（在锁外执行，避免阻塞事件记录）
        backed_up = 0
        failed_retry = []
        for src_path in to_backup:
            try:
                result = backup_file(src_path, self.config, self.logger)
                if result == "backed_up":
                    backed_up += 1
                    # 备份成功，清除所有跟踪条目（P2-15: 防止内存泄漏）
                    self._cleanup_file_tracking(src_path)
                elif result in ("failed", "source_gone"):
                    # 失败文件加入重试队列（最多 3 次，间隔递增）
                    retry_count, _ = self._retry_queue.get(src_path, (0, 0))
                    retry_count += 1
                    if retry_count <= 3:
                        # 指数退避：5s, 15s, 45s
                        delay = 5 * (3 ** (retry_count - 1))
                        self._retry_queue[src_path] = (
                            retry_count, now + delay
                        )
                        failed_retry.append(src_path)
                        # 重置稳定计数，防止下次循环立即再次备份
                        with self._created_lock:
                            self._created_sizes[
                                f"__stable__{src_path}"
                            ] = 0
                        self.logger.warning(
                            f"批量备份失败，第{retry_count}次重试"
                            f"({delay}s后): {src_path}"
                        )
                    else:
                        self.logger.error(
                            f"批量备份最终失败（已重试3次）: {src_path}"
                        )
                        # 最终失败，清除所有跟踪条目（P2-15: 防止内存泄漏）
                        self._cleanup_file_tracking(src_path)
                # "skipped" 和 "dirty" 是正常情况，不记录
            except Exception as e:
                self.logger.error(f"批量备份异常: {e} - {src_path}")

        # 处理重试队列：失败文件放回待处理集合
        with self._created_lock:
            for src_path in failed_retry:
                self._pending_created.add(src_path)

        if backed_up > 0:
            self.logger.info(
                f"批量备份完成: {backed_up}/{len(to_backup)} 个文件"
            )
        elif to_backup:
            self.logger.debug(
                f"批量备份完成: 0/{len(to_backup)} 个文件"
            )

    def _wait_file_stable(self, src_path: str,
                          check_interval: float = 2.0,
                          stable_count: int = 2,
                          timeout: float = 600.0) -> bool:
        """
        等待文件大小稳定（连续 stable_count 次大小不变）。
        保留此方法供其他模块（如 sync）使用。
        """
        start_time = time.time()
        last_size = -1
        stable = 0

        while time.time() - start_time < timeout:
            try:
                if not os.path.isfile(src_path):
                    return False
                current_size = os.path.getsize(src_path)
            except OSError:
                time.sleep(check_interval)
                continue

            if current_size == last_size and current_size > 0:
                stable += 1
                if stable >= stable_count:
                    return True
            else:
                stable = 0
                last_size = current_size

            time.sleep(check_interval)

        self.logger.debug(
            f"文件稳定等待超时({timeout}s)，尝试备份: {src_path}"
        )
        return os.path.isfile(src_path)

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory or self._should_exclude(event.src_path):
            return

        if self._is_duplicate(event.src_path, "modified"):
            return

        self.logger.debug(f"文件修改: {event.src_path}")
        # P2-14: 与 on_created 合并为统一的批量备份路径，
        # 避免同步调用 backup_file 阻塞 watchdog 事件分发线程
        with self._created_lock:
            self._pending_created.add(event.src_path)
            self._created_sizes.pop(event.src_path, None)  # 重置大小记录

    def on_deleted(self, event: FileSystemEvent):
        if self._should_exclude(event.src_path):
            return

        # 去重：避免同一文件的多个删除事件重复处理
        if self._is_duplicate(event.src_path, "deleted"):
            return

        self.logger.info(f"文件/目录被删除: {event.src_path} (目录: {event.is_directory})")

        # 延迟处理：因为有些程序（如 Office）会先删除再创建临时文件
        # 加入待确认队列，由工作线程在延迟后确认文件未被重建再回收
        with self._pending_lock:
            # O(1) set 检查替代 O(n) 线性搜索
            if event.src_path not in self._pending_paths:
                self._pending_deletes.append(
                    (event.src_path, event.is_directory, time.time())
                )
                self._pending_paths.add(event.src_path)

    def _delete_worker_loop(self):
        """后台工作线程：处理到期的待确认删除事件"""
        while True:
            item = None
            with self._pending_lock:
                if self._pending_deletes:
                    src_path, is_directory, ts = self._pending_deletes[0]
                    if time.time() - ts >= _DELETE_CONFIRM_DELAY:
                        item = self._pending_deletes.popleft()
                        self._pending_paths.discard(src_path)
            if item is None:
                time.sleep(0.1)
                continue
            src_path, is_directory, _ = item
            try:
                self._handle_pending_delete(src_path, is_directory)
            except Exception as e:
                self.logger.error(f"处理删除事件失败 {src_path}: {e}")

    def _handle_pending_delete(self, src_path: str, is_directory: bool):
        """确认删除事件：文件确实没被重新创建后才移入回收站"""
        # 如果文件又被重新创建了，说明这是"保存覆盖"操作，不是真删除
        if os.path.exists(src_path):
            self.logger.debug(f"删除事件取消（文件已重建）: {src_path}")
            # P2-14: 记录到批量备份队列，避免同步阻塞 delete worker
            if not is_directory:
                with self._created_lock:
                    self._pending_created.add(src_path)
                    self._created_sizes.pop(src_path, None)
            return

        # 确认真删除：等待正在进行的备份完成，再移入回收站
        # 避免备份还没做完就把空备份移入回收站
        self._wait_for_backup(src_path)
        result = move_to_recycle(src_path, is_directory, self.config, self.logger)

        # 如果第一次失败（备份不存在），再重试一次
        if result is None and not is_directory:
            self.logger.debug(f"首次移入回收站失败，等待后重试: {src_path}")
            time.sleep(2)
            if not os.path.exists(src_path):  # 确认文件没有重建
                move_to_recycle(src_path, is_directory, self.config, self.logger)

    def _wait_for_backup(self, src_path: str):
        """等待指定文件的备份操作完成"""
        from .backup import _active_backups, _active_backups_cond
        norm = os.path.normcase(os.path.abspath(src_path))
        with _active_backups_cond:
            if norm in _active_backups:
                self.logger.debug(f"等待备份操作完成: {src_path}")
                _active_backups_cond.wait_for(
                    lambda: norm not in _active_backups,
                    timeout=_BACKUP_WAIT_TIMEOUT
                )
                # 额外等待让文件系统操作完成
                time.sleep(0.5)

    def on_moved(self, event: FileSystemEvent):
        """处理重命名/移动事件"""
        if self._should_exclude(event.dest_path):
            return

        self.logger.debug(f"文件移动/重命名: {event.src_path} -> {event.dest_path}")

        # P2-14: 目标文件记录到批量备份队列，避免同步阻塞 watchdog 事件线程
        if not event.is_directory and os.path.isfile(event.dest_path):
            with self._created_lock:
                self._pending_created.add(event.dest_path)
                self._created_sizes.pop(event.dest_path, None)

        # 目标仍在监控目录内：只是重命名/移动，不算删除
        if self.config.find_watch_root(event.dest_path) is not None:
            return

        # 源文件被移出监控目录：视为被删除，移入回收站
        if self._should_exclude(event.src_path):
            return
        self.logger.info(f"文件被移出监控目录（视为删除）: {event.src_path}")
        with self._pending_lock:
            if event.src_path not in self._pending_paths:
                self._pending_deletes.append(
                    (event.src_path, event.is_directory, time.time())
                )
                self._pending_paths.add(event.src_path)


def start_watcher(config: Config, logger):
    """
    启动文件监控。

    返回 (observer, handler) 元组。
    """
    handler = RecycleGuardHandler(config, logger)
    observer = Observer()

    for watch_path in config.watch_paths:
        if not os.path.exists(watch_path):
            os.makedirs(watch_path, exist_ok=True)
            logger.info(f"创建监控目录: {watch_path}")

        observer.schedule(handler, watch_path, recursive=True)
        logger.info(f"开始监控: {watch_path}")

    observer.start()

    # 初始备份
    logger.info("执行初始全量备份...")
    threading.Thread(
        target=backup_full_tree,
        args=(config, logger),
        daemon=True
    ).start()

    return observer, handler

