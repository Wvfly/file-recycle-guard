"""
文件系统监控模块 - 使用 watchdog 监控共享文件夹变更
"""

import os
import time
import fnmatch
import threading
from collections import deque
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
        # 去重队列：避免短时间内重复事件（Windows 上 modify 可能触发多次）
        self._recent_events: deque = deque(maxlen=500)
        # 待确认的删除事件：(路径, 是否目录, 事件时间)
        self._pending_deletes: deque = deque()
        self._pending_lock = threading.Lock()
        # 单一工作线程处理延迟删除，避免每个删除事件新建线程
        self._delete_worker = threading.Thread(
            target=self._delete_worker_loop,
            daemon=True,
            name="PendingDeleteWorker"
        )
        self._delete_worker.start()

    def _is_duplicate(self, src_path: str, event_type: str) -> bool:
        """简单去重：同一文件同一事件 2 秒内只处理一次"""
        now = time.time()
        key = (src_path, event_type)
        # 清理过期项
        while self._recent_events and self._recent_events[0][0] < now - 3:
            self._recent_events.popleft()
        # 检查是否重复
        for t, k in self._recent_events:
            if k == key and now - t < 2:
                return True
        self._recent_events.append((now, key))
        return False

    def _is_pending_delete(self, src_path: str) -> bool:
        """检查该路径是否已在待删除队列中（避免重复加入）"""
        with self._pending_lock:
            return any(p == src_path for p, _, _ in self._pending_deletes)

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
        # 不在事件分发线程中 sleep；若文件尚未写入完成，
        # 后续 modified 事件会通过哈希比较自动补上正确内容
        backup_file(event.src_path, self.config, self.logger)

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory or self._should_exclude(event.src_path):
            return

        if self._is_duplicate(event.src_path, "modified"):
            return

        self.logger.debug(f"文件修改: {event.src_path}")
        backup_file(event.src_path, self.config, self.logger)

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
            # 检查是否已在队列中，避免重复
            already_pending = any(p == event.src_path for p, _, _ in self._pending_deletes)
            if not already_pending:
                self._pending_deletes.append(
                    (event.src_path, event.is_directory, time.time())
                )

    def _delete_worker_loop(self):
        """后台工作线程：处理到期的待确认删除事件"""
        while True:
            item = None
            with self._pending_lock:
                if self._pending_deletes:
                    src_path, is_directory, ts = self._pending_deletes[0]
                    if time.time() - ts >= _DELETE_CONFIRM_DELAY:
                        item = self._pending_deletes.popleft()
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
            # 新文件已创建，触发备份
            if not is_directory:
                backup_file(src_path, self.config, self.logger)
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
        """等待正在进行的备份操作完成"""
        from .backup import _backup_lock
        if _backup_lock.locked():
            self.logger.debug(f"等待备份操作完成: {src_path}")
            acquired = _backup_lock.acquire(timeout=_BACKUP_WAIT_TIMEOUT)
            if acquired:
                _backup_lock.release()
                # 额外等待让文件系统操作完成
                time.sleep(0.5)

    def on_moved(self, event: FileSystemEvent):
        """处理重命名/移动事件"""
        if self._should_exclude(event.dest_path):
            return

        self.logger.debug(f"文件移动/重命名: {event.src_path} -> {event.dest_path}")

        # 目标文件：备份
        if not event.is_directory and os.path.isfile(event.dest_path):
            backup_file(event.dest_path, self.config, self.logger)

        # 目标仍在监控目录内：只是重命名/移动，不算删除
        if self.config.find_watch_root(event.dest_path) is not None:
            return

        # 源文件被移出监控目录：视为被删除，移入回收站
        if self._should_exclude(event.src_path):
            return
        self.logger.info(f"文件被移出监控目录（视为删除）: {event.src_path}")
        with self._pending_lock:
            already_pending = any(p == event.src_path for p, _, _ in self._pending_deletes)
            if not already_pending:
                self._pending_deletes.append(
                    (event.src_path, event.is_directory, time.time())
                )


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
