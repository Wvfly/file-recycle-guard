"""
定期同步模块 - 主动扫描监控目录，将变更同步到备份目录。

解决 watchdog 无法可靠检测 SMB 网络共享变更的问题。

性能优化（亿级场景）：
- SQLite 持久化缓存替代内存 dict，避免上亿条记录 OOM
- 目录树持久化到 SQLite：os.walk() 仅首次/定期重建时执行，
  日常扫描从 SQLite 读取目录列表，避免每轮全量遍历
- 目录 mtime 持久化到 SQLite：替代内存 dict，避免百万目录 OOM
- 层次化扫描：先检查顶层目录 mtime，未变化的整个子树跳过
- os.scandir() 替代 os.listdir()：一次系统调用获取文件名+stat
- 批量 SQLite 查询：IN 子句替代逐个 SELECT
- 内存 LRU 热缓存：最近访问的文件 mtime/size 放内存，加速高频访问
- 单 writer thread 消费写入队列，避免多线程 SQLite 写锁竞争（P1-8）
"""

import os
import time
import sqlite3
import threading
import fnmatch
import hashlib
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple, Optional, List, Set, Any
from collections import OrderedDict
from .config import Config
from .backup import backup_file


# ── SQLite 持久化文件缓存 ──────────────────────────────────────

class PersistentFileCache:
    """
    基于 SQLite 的文件状态持久化缓存。
    存储每个文件的 (mtime, size)，避免内存中存上亿条记录。
    配合内存 LRU 热缓存使用。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB SQLite 页面缓存
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_cache (
                    path_hash TEXT PRIMARY KEY,
                    full_path TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_full_path
                ON file_cache(full_path)
            """)
            conn.commit()
            self._local.conn = conn
        return conn

    def get(self, path_hash: str) -> Optional[Tuple[float, int]]:
        """查询缓存：返回 (mtime, size) 或 None"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT mtime, size FROM file_cache WHERE path_hash=?",
            (path_hash,)
        )
        row = cur.fetchone()
        if row:
            return (row[0], row[1])
        return None

    def get_batch(self, path_hashes: List[str]) -> Dict[str, Tuple[float, int]]:
        """
        批量查询缓存（IN 子句），减少 SQLite 交互次数。
        返回 {path_hash: (mtime, size)} 字典。
        """
        if not path_hashes:
            return {}
        conn = self._get_conn()
        result = {}
        # SQLite IN 子句分批（每批 500 个）
        batch_size = 500
        for i in range(0, len(path_hashes), batch_size):
            batch = path_hashes[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"SELECT path_hash, mtime, size FROM file_cache "
                f"WHERE path_hash IN ({placeholders})",
                batch
            )
            for row in cur.fetchall():
                result[row[0]] = (row[1], row[2])
        return result

    def set(self, path_hash: str, full_path: str, mtime: float, size: int):
        """写入/更新缓存条目"""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO file_cache (path_hash, full_path, mtime, size) "
            "VALUES (?, ?, ?, ?)",
            (path_hash, full_path, mtime, size)
        )
        conn.commit()

    def set_batch(self, items: List[Tuple[str, str, float, int]]):
        """
        批量写入缓存条目。
        items: [(path_hash, full_path, mtime, size), ...]
        """
        if not items:
            return
        conn = self._get_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO file_cache "
            "(path_hash, full_path, mtime, size) VALUES (?, ?, ?, ?)",
            items
        )
        conn.commit()

    def delete(self, path_hash: str):
        """删除缓存条目"""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM file_cache WHERE path_hash=?",
            (path_hash,)
        )
        conn.commit()

    def delete_batch(self, path_hashes: List[str]):
        """批量删除缓存条目"""
        if not path_hashes:
            return
        conn = self._get_conn()
        conn.executemany(
            "DELETE FROM file_cache WHERE path_hash=?",
            [(h,) for h in path_hashes]
        )
        conn.commit()

    def count(self) -> int:
        """返回缓存条目总数"""
        conn = self._get_conn()
        cur = conn.execute("SELECT COUNT(*) FROM file_cache")
        return cur.fetchone()[0]

    def get_batch_set(self, path_hashes: List[str]) -> Set[str]:
        """
        批量查询已存在的 path_hash 集合。
        用于去重：避免对已在缓存中的文件重复写入。
        """
        if not path_hashes:
            return set()
        conn = self._get_conn()
        result = set()
        batch_size = 500
        for i in range(0, len(path_hashes), batch_size):
            batch = path_hashes[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"SELECT path_hash FROM file_cache "
                f"WHERE path_hash IN ({placeholders})",
                batch
            )
            for row in cur.fetchall():
                result.add(row[0])
        return result


# ── 目录树持久化缓存 ──────────────────────────────────────────

class DirectoryTreeCache:
    """
    目录树持久化缓存：将目录结构及其 mtime 存储在 SQLite 中。
    避免每轮扫描都执行 os.walk() 全量遍历目录树。

    策略：
    - 首次启动时 os.walk() 构建目录树并持久化
    - 后续启动时从 SQLite 读取目录列表
    - 定期（每 N 轮）或手动触发时重建目录树
    """

    # 每 100 轮（约 100 * interval 秒）重建一次目录树
    REBUILD_INTERVAL_ROUNDS = 100

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dir_tree (
                    path TEXT PRIMARY KEY,
                    mtime REAL NOT NULL DEFAULT 0,
                    parent TEXT,
                    depth INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_parent
                ON dir_tree(parent)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_depth
                ON dir_tree(depth)
            """)
            conn.commit()
            self._local.conn = conn
        return conn

    def get_all_dirs(self) -> List[str]:
        """从持久化缓存获取所有目录路径"""
        conn = self._get_conn()
        cur = conn.execute("SELECT path FROM dir_tree ORDER BY depth, path")
        return [row[0] for row in cur.fetchall()]

    def get_dir_mtime(self, dir_path: str) -> float:
        """获取目录的缓存 mtime"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT mtime FROM dir_tree WHERE path=?",
            (dir_path,)
        )
        row = cur.fetchone()
        return row[0] if row else 0.0

    def update_dir_mtime(self, dir_path: str, mtime: float):
        """更新目录的 mtime"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE dir_tree SET mtime=? WHERE path=?",
            (mtime, dir_path)
        )
        conn.commit()

    def get_changed_dirs(self, min_depth: int = 0,
                         max_depth: int = 999) -> List[Tuple[str, float]]:
        """
        获取指定深度范围内的目录及其缓存 mtime。
        返回 [(path, cached_mtime), ...]
        """
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT path, mtime FROM dir_tree "
            "WHERE depth >= ? AND depth <= ? "
            "ORDER BY depth, path",
            (min_depth, max_depth)
        )
        return cur.fetchall()

    def get_child_dirs(self, parent_prefix: str) -> List[str]:
        """获取某目录下的所有子目录（用于层次化扫描）"""
        conn = self._get_conn()
        # 匹配 parent_prefix 开头的目录
        cur = conn.execute(
            "SELECT path FROM dir_tree WHERE path LIKE ? ORDER BY depth",
            (parent_prefix + os.sep + "%",)
        )
        return [row[0] for row in cur.fetchall()]

    def build_from_walk(self, config: Config):
        """
        使用 os.walk() 重建目录树（仅在首次或定期重建时调用）。
        同时将目录的当前 mtime 一并存储。

        原子切换策略：先构建到临时表，完成后原子交换，
        避免重建期间新目录丢失的一致性窗口。
        """
        conn = self._get_conn()

        # 创建临时表
        conn.execute("DROP TABLE IF EXISTS dir_tree_new")
        conn.execute("""
            CREATE TABLE dir_tree_new (
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL DEFAULT 0,
                parent TEXT,
                depth INTEGER NOT NULL DEFAULT 0
            )
        """)

        for watch_path in config.watch_paths:
            if not os.path.exists(watch_path):
                continue
            norm_watch = os.path.normcase(os.path.abspath(watch_path))
            base_depth = norm_watch.count(os.sep)

            # 添加根目录
            root_st = _safe_stat(watch_path)
            root_mtime = root_st.st_mtime if root_st else 0.0
            conn.execute(
                "INSERT INTO dir_tree_new (path, mtime, parent, depth) "
                "VALUES (?, ?, ?, ?)",
                (watch_path, root_mtime, None, 0)
            )

            for root, dirs, files in os.walk(watch_path):
                dirs[:] = [d for d in dirs if d not in config.exclude_dirs]
                norm_root = os.path.normcase(os.path.abspath(root))
                parent_depth = norm_root.count(os.sep) - base_depth

                for d in dirs:
                    dir_path = os.path.join(root, d)
                    dir_st = _safe_stat(dir_path)
                    dir_mtime = dir_st.st_mtime if dir_st else 0.0
                    conn.execute(
                        "INSERT INTO dir_tree_new (path, mtime, parent, depth) "
                        "VALUES (?, ?, ?, ?)",
                        (dir_path, dir_mtime, root, parent_depth + 1)
                    )

        # 原子切换：删除旧表，重命名新表
        conn.execute("DROP TABLE dir_tree")
        conn.execute("ALTER TABLE dir_tree_new RENAME TO dir_tree")
        # 重建索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_parent
            ON dir_tree(parent)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_depth
            ON dir_tree(depth)
        """)
        conn.commit()

    def count(self) -> int:
        """返回目录总数"""
        conn = self._get_conn()
        cur = conn.execute("SELECT COUNT(*) FROM dir_tree")
        return cur.fetchone()[0]

    def needs_rebuild(self) -> bool:
        """检查目录树是否为空（需要重建）"""
        return self.count() == 0

    def get_dirs_page(self, offset: int, limit: int) -> List[str]:
        """
        分页获取目录路径列表（按 depth, path 排序）。
        用于游标分页扫描，避免一次性加载所有目录到内存。
        """
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT path FROM dir_tree ORDER BY depth, path "
            "LIMIT ? OFFSET ?",
            (limit, offset)
        )
        return [row[0] for row in cur.fetchall()]

    def get_dirs_mtime_batch(self, dir_paths: List[str]) -> Dict[str, float]:
        """
        批量查询目录的缓存 mtime（IN 子句），减少 SQLite 交互次数。
        返回 {dir_path: cached_mtime} 字典。
        """
        if not dir_paths:
            return {}
        conn = self._get_conn()
        result = {}
        batch_size = 500
        for i in range(0, len(dir_paths), batch_size):
            batch = dir_paths[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"SELECT path, mtime FROM dir_tree "
                f"WHERE path IN ({placeholders})",
                batch
            )
            for row in cur.fetchall():
                result[row[0]] = row[1]
        return result

    def get_dirs_with_mtime_change(self) -> List[str]:
        """
        获取磁盘 mtime 与缓存 mtime 不一致的目录列表（P1-3）。
        用于增量发现新子目录：只扫描这些目录的直接子目录。
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT path, mtime FROM dir_tree")
        changed = []
        for row in cur.fetchall():
            dir_path, cached_mtime = row
            try:
                actual_mtime = os.stat(dir_path).st_mtime
                if abs(actual_mtime - cached_mtime) > 0.5:
                    changed.append(dir_path)
            except OSError:
                pass
        return changed


# ── 内存 LRU 热缓存 ───────────────────────────────────────────

class MemoryLRUCache:
    """
    内存 LRU 热缓存：存储最近访问的文件 (mtime, size)。
    容量有限（默认 100 万条），超出时淘汰最久未使用的。
    命中则无需访问 SQLite。
    """

    def __init__(self, max_size: int = 1_000_000):
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Tuple[float, int]]:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def set(self, key: str, value: Tuple[float, int]):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def delete(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def __len__(self):
        with self._lock:
            return len(self._cache)


# ── 路径哈希工具 ───────────────────────────────────────────────

def _path_hash(path: str) -> str:
    """计算路径的短哈希（用于缓存 key）P2-2: MD5→SHA256截断"""
    return hashlib.sha256(os.path.normcase(path).encode("utf-8")).hexdigest()[:16]


# 不支持 stat 操作的错误码（SMB 网络共享上某些文件会返回）
_UNSUPPORTED_STAT_ERRORS = {50, 87, 123, 1920}


def _safe_stat(path: str, logger=None):
    """
    安全获取文件 stat 信息。
    返回 stat 对象，失败返回 None。
    对于 SMB 网络共享上不支持 stat 的文件（WinError 50），
    静默跳过，不输出错误日志。
    """
    try:
        return os.stat(path)
    except OSError as e:
        if hasattr(e, 'winerror') and e.winerror in _UNSUPPORTED_STAT_ERRORS:
            # SMB 共享上某些文件不支持 stat，静默跳过
            if logger:
                logger.debug(f"文件不支持 stat 操作，已跳过: {path} (WinError {e.winerror})")
        elif logger:
            logger.debug(f"获取文件 stat 失败: {path} ({e})")
        return None


def _safe_entry_stat(entry, logger=None):
    """
    安全获取 DirEntry 的 stat 信息。
    返回 stat 对象，失败返回 None。
    """
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as e:
        if hasattr(e, 'winerror') and e.winerror in _UNSUPPORTED_STAT_ERRORS:
            if logger:
                logger.debug(f"文件不支持 stat 操作，已跳过: {entry.path} (WinError {e.winerror})")
        elif logger:
            logger.debug(f"获取文件 stat 失败: {entry.path} ({e})")
        return None


# ── 全局缓存实例（延迟初始化） ─────────────────────────────────

_persistent_cache: Optional[PersistentFileCache] = None
_memory_cache: Optional[MemoryLRUCache] = None
_dir_cache: Optional[DirectoryTreeCache] = None
_cache_init_lock = threading.Lock()

# ── SQLite 持久化缓存写入缓冲（攒批替代逐条 commit） ───────────
_pcache_buffer_lock = threading.Lock()
_pcache_buffer: List[Tuple[str, str, float, int]] = []
_PCACHE_BUFFER_THRESHOLD = 100


def _flush_pcache_buffer(pcache: 'PersistentFileCache'):
    """刷新 SQLite 持久化缓存写入缓冲区（通过 writer 队列异步写入，P1-8）"""
    with _pcache_buffer_lock:
        if not _pcache_buffer:
            return
        items = _pcache_buffer[:]
        _pcache_buffer.clear()
    # 通过 writer 队列提交，避免多线程直接写 SQLite
    if _sqlite_writer_queue is not None:
        _sqlite_writer_queue.put(("set_batch", pcache, items))
    else:
        # writer 未启动，回退到直接写入
        try:
            pcache.set_batch(items)
        except Exception:
            pass


def _get_caches(cache_dir: str) -> Tuple[PersistentFileCache,
                                          MemoryLRUCache,
                                          DirectoryTreeCache]:
    """获取全局缓存实例（线程安全延迟初始化）"""
    global _persistent_cache, _memory_cache, _dir_cache
    if _persistent_cache is None or _memory_cache is None or _dir_cache is None:
        with _cache_init_lock:
            if _persistent_cache is None:
                os.makedirs(cache_dir, exist_ok=True)
                db_path = os.path.join(cache_dir, "file_cache.db")
                _persistent_cache = PersistentFileCache(db_path)
            if _memory_cache is None:
                _memory_cache = MemoryLRUCache(max_size=1_000_000)
            if _dir_cache is None:
                os.makedirs(cache_dir, exist_ok=True)
                dir_db_path = os.path.join(cache_dir, "dir_tree.db")
                _dir_cache = DirectoryTreeCache(dir_db_path)
    return _persistent_cache, _memory_cache, _dir_cache


# ── 目录扫描工具 ───────────────────────────────────────────────

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
    st = _safe_stat(path)
    if st is not None:
        return (st.st_mtime, st.st_size)
    return None


# ── 分片增量扫描器（亿级安全版） ─────────────────────────────────

_PAGE_SIZE = 5000  # 目录分页大小


class IncrementalScanner:
    """
    分片增量扫描器（亿级安全版）：

    修复 5 大卡死风险：
    1. 游标分页：不再一次性加载所有目录到内存
    2. 后台重建：目录树重建在后台线程进行，不阻塞扫描
    3. 异步备份：backup_file 提交到线程池，不阻塞扫描循环
    4. 待处理跟踪：防止同一文件重复提交备份
    5. 批量 SQLite 写入：缓存更新合并为一次 COMMIT
    """

    # 线程池队列上限：防止突发大量文件变更时任务积压
    _MAX_POOL_QUEUE = 2000

    def __init__(self, backup_workers: int = 8):
        self._cursor: int = 0
        self._round: int = 0
        self._dirs_built = False
        self._rebuilding = False
        self._total_dirs: int = 0

        # 异步备份线程池（不阻塞扫描循环）
        self._backup_pool = ThreadPoolExecutor(
            max_workers=backup_workers,
            thread_name_prefix="BackupWorker"
        )
        # 待处理备份跟踪（防止重复提交）
        self._in_flight: Set[str] = set()
        self._in_flight_cond = threading.Condition()

    def _ensure_dir_tree(self, config: Config,
                         dir_cache: DirectoryTreeCache, logger):
        """确保目录树已构建（仅首次调用时）"""
        if not self._dirs_built:
            if dir_cache.needs_rebuild():
                logger.info("首次构建目录树缓存...")
                dir_cache.build_from_walk(config)
                self._total_dirs = dir_cache.count()
                logger.info(f"目录树构建完成: {self._total_dirs} 个目录")
            else:
                self._total_dirs = dir_cache.count()
            self._dirs_built = True

    def _discover_new_directories(self, config: Config,
                                   dir_cache: DirectoryTreeCache, logger):
        """
        发现磁盘上存在但不在目录树缓存中的新目录。
        解决 watchdog 缓冲区溢出导致新目录创建事件丢失的问题。
        P1-3 修复：增量扫描替代全量 os.walk()。
        策略：(1) 扫描缓存中 mtime 变化的目录的直接子目录；
              (2) 检查 watch_path 根目录下的新增子目录。
        """
        new_dirs = []  # [(dir_path, watch_path), ...]
        for watch_path in config.watch_paths:
            if not os.path.exists(watch_path):
                continue
            norm_watch = os.path.normcase(os.path.abspath(watch_path))

            # 策略 1：扫描缓存中 mtime 变化的目录的直接子目录
            changed_dirs = dir_cache.get_dirs_with_mtime_change()
            for dir_path in changed_dirs:
                try:
                    entries = os.scandir(dir_path)
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            child = os.path.normcase(
                                os.path.abspath(entry.path)
                            )
                            if dir_cache.get_dir_mtime(child) == 0.0:
                                new_dirs.append((child, norm_watch))
                    entries.close()
                except OSError:
                    pass

            # 策略 2：检查 watch_path 根目录下的新增子目录
            try:
                for entry in os.scandir(watch_path):
                    if entry.is_dir(follow_symlinks=False):
                        child = os.path.normcase(
                            os.path.abspath(entry.path)
                        )
                        if dir_cache.get_dir_mtime(child) == 0.0:
                            new_dirs.append((child, norm_watch))
            except OSError:
                pass

        if new_dirs:
            logger.info(f"发现 {len(new_dirs)} 个新目录，更新目录树缓存...")
            conn = dir_cache._get_conn()
            for dir_path, norm_watch in new_dirs:
                parent = os.path.dirname(dir_path)
                depth = dir_path.count(os.sep) - norm_watch.count(os.sep)
                dir_st = _safe_stat(dir_path)
                mtime = dir_st.st_mtime if dir_st else 0.0
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO dir_tree "
                        "(path, mtime, parent, depth) VALUES (?, ?, ?, ?)",
                        (dir_path, mtime, parent, depth)
                    )
                except Exception:
                    pass
            conn.commit()
            self._total_dirs = dir_cache.count()
            logger.info(f"目录树已更新: {self._total_dirs} 个目录")

    def scan_batch(self, config: Config, logger,
                   pcache: PersistentFileCache,
                   mcache: MemoryLRUCache,
                   dir_cache: DirectoryTreeCache,
                   batch_dirs: int = 50000,
                   batch_files: int = 1000,
                   time_budget: float = 25.0) -> Tuple[int, int, int]:
        """
        执行一批扫描。

        亿级安全策略：
        - 游标分页获取目录（不加载全部到内存）
        - 批量查询目录 mtime（减少 SQLite 交互）
        - backup_file 提交到线程池（不阻塞扫描）
        - 缓存更新合并为批量写入（减少 COMMIT）

        返回 (backed_up, skipped, scanned) 三元组。
        """
        self._ensure_dir_tree(config, dir_cache, logger)

        # 每 10 轮检查一次是否有新目录（watchdog 事件丢失时的补偿）
        if self._round % 10 == 0 and self._cursor == 0:
            try:
                self._discover_new_directories(config, dir_cache, logger)
            except Exception as e:
                logger.error(f"新目录发现失败: {e}")

        # 检查是否需要重置游标（上一轮扫完）
        if self._cursor >= self._total_dirs and self._total_dirs > 0:
            self._cursor = 0
            self._round += 1

        # 定期触发后台目录树重建（不阻塞当前扫描）
        if (self._round > 0 and self._cursor == 0
                and self._round % DirectoryTreeCache.REBUILD_INTERVAL_ROUNDS == 0
                and not self._rebuilding):
            logger.info(f"第 {self._round} 轮，后台重建目录树缓存...")
            self._rebuilding = True

            def _bg_rebuild():
                try:
                    dir_cache.build_from_walk(config)
                    self._total_dirs = dir_cache.count()
                    logger.info(
                        f"目录树后台重建完成: {self._total_dirs} 个目录"
                    )
                except Exception as e:
                    logger.error(f"目录树后台重建失败: {e}")
                finally:
                    self._rebuilding = False

            threading.Thread(target=_bg_rebuild, daemon=True).start()

        # 获取第一批目录（游标分页，不加载全部）
        dirs_batch = dir_cache.get_dirs_page(self._cursor, _PAGE_SIZE)
        if not dirs_batch:
            self._cursor = 0
            return 0, 0, 0

        backed_up = 0
        skipped = 0
        scanned = 0
        dirs_skipped_by_mtime = 0
        batch_start = time.time()

        # 批量缓存写入已通过模块级 _pcache_buffer 攒批实现（见 L474-490）

        dirs_processed_in_page = 0

        while True:
            # 时间预算检查
            if (time.time() - batch_start) > time_budget:
                break

            # 批量查询本页目录的缓存 mtime（1 次 SQL 替代 N 次）
            cached_mtimes = dir_cache.get_dirs_mtime_batch(dirs_batch)

            for dir_path in dirs_batch:
                dirs_processed_in_page += 1

                # 快速路径：批量 mtime 检查
                cached_dir_mtime = cached_mtimes.get(dir_path, 0.0)
                if cached_dir_mtime > 0 and not _dir_has_changes(
                        dir_path, cached_dir_mtime):
                    dirs_skipped_by_mtime += 1
                    continue

                # 目录有变化，更新缓存 mtime
                dir_st = _safe_stat(dir_path)
                if dir_st is not None:
                    dir_cache.update_dir_mtime(
                        dir_path, dir_st.st_mtime
                    )

                # os.scandir() 高效扫描
                try:
                    entries = list(os.scandir(dir_path))
                except OSError:
                    continue

                # 收集文件信息
                file_infos = []
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    full_path = entry.path
                    try:
                        rel = os.path.relpath(
                            full_path,
                            config.find_watch_root(full_path) or dir_path
                        )
                    except ValueError:
                        continue
                    if _should_exclude(rel, config):
                        continue
                    st = _safe_entry_stat(entry, logger)
                    if st is None:
                        continue
                    file_stat = (st.st_mtime, st.st_size)
                    cache_key = _path_hash(full_path)
                    file_infos.append(
                        (full_path, rel, cache_key, file_stat)
                    )

                scanned += len(file_infos)
                if not file_infos:
                    continue

                # 批量查询 SQLite 缓存
                cache_keys = [fi[2] for fi in file_infos]
                batch_cached = pcache.get_batch(cache_keys)

                # 筛选需要备份的文件
                pending_backup = []
                for full_path, rel, cache_key, file_stat in file_infos:
                    # 1. 内存 LRU 缓存
                    mem_cached = mcache.get(cache_key)
                    if mem_cached is not None:
                        if mem_cached == file_stat:
                            skipped += 1
                            continue
                    # 2. SQLite 批量缓存
                    db_cached = batch_cached.get(cache_key)
                    if db_cached is not None:
                        if db_cached == file_stat:
                            mcache.set(cache_key, db_cached)
                            skipped += 1
                            continue
                    pending_backup.append(
                        (full_path, rel, cache_key, file_stat)
                    )

                # 异步提交备份（不阻塞扫描循环）
                for full_path, rel, cache_key, file_stat in pending_backup:
                    if backed_up >= batch_files:
                        break

                    # 队列积压保护：防止突发大量文件变更时任务积压
                    with self._in_flight_cond:
                        if len(self._in_flight) >= self._MAX_POOL_QUEUE:
                            break

                    # 跳过正在备份中的文件（防止重复提交）
                    with self._in_flight_cond:
                        if full_path in self._in_flight:
                            continue
                        self._in_flight.add(full_path)

                    def _on_backup_done(future, _fp=full_path,
                                        _ck=cache_key, _fs=file_stat,
                                        _pc=pcache):
                        try:
                            result = future.result()
                            if result == "backed_up":
                                mcache.set(_ck, _fs)
                                items = None
                                with _pcache_buffer_lock:
                                    _pcache_buffer.append(
                                        (_ck, _fp, _fs[0], _fs[1])
                                    )
                                    if len(_pcache_buffer) >= _PCACHE_BUFFER_THRESHOLD:
                                        items = _pcache_buffer[:]
                                        _pcache_buffer.clear()
                                if items is not None:
                                    # P1-8: 通过 writer 队列写入
                                    if _sqlite_writer_queue is not None:
                                        _sqlite_writer_queue.put(
                                            ("set_batch", _pc, items)
                                        )
                                    else:
                                        try:
                                            _pc.set_batch(items)
                                        except Exception:
                                            pass
                            elif result in ("dirty", "source_gone"):
                                mcache.delete(_ck)
                                # P1-8: 通过 writer 队列删除，避免多线程写 SQLite
                                if _sqlite_writer_queue is not None:
                                    _sqlite_writer_queue.put(
                                        ("delete", _pc, _ck)
                                    )
                                else:
                                    _pc.delete(_ck)
                        except Exception as e:
                            logger.error(
                                f"异步备份失败 {_fp}: {e}"
                            )
                        finally:
                            with self._in_flight_cond:
                                self._in_flight.discard(_fp)
                                self._in_flight_cond.notify()

                    fut = self._backup_pool.submit(
                        backup_file, full_path, config, logger
                    )
                    fut.add_done_callback(_on_backup_done)
                    backed_up += 1

                if backed_up >= batch_files:
                    break

            # 更新游标
            self._cursor += len(dirs_batch)

            # 获取下一页目录
            dirs_batch = dir_cache.get_dirs_page(
                self._cursor, _PAGE_SIZE
            )
            if not dirs_batch:
                self._cursor = 0
                self._round += 1
                break

        if dirs_skipped_by_mtime > 0:
            logger.debug(
                f"层次化扫描跳过 {dirs_skipped_by_mtime} 个未变化目录"
            )

        # 刷新残余 SQLite 缓存缓冲
        _flush_pcache_buffer(pcache)

        return backed_up, skipped, scanned

    @property
    def current_round(self) -> int:
        return self._round

    @property
    def progress(self) -> Tuple[int, int]:
        """返回 (当前进度, 总目录数)"""
        return self._cursor, self._total_dirs

    def shutdown(self):
        """关闭备份线程池 + 刷新 SQLite writer"""
        self._backup_pool.shutdown(wait=False)
        # P1-8: 等待 writer 线程处理完残余写入
        _flush_sqlite_writer()


def _dir_has_changes(dir_path: str, cached_mtime: float) -> bool:
    """
    检查目录是否有变化（通过目录 mtime）。
    目录 mtime 变化意味着其下有文件增删改。
    若 stat 失败（如 SMB 不支持），保守地认为有变化。
    """
    st = _safe_stat(dir_path)
    if st is None:
        return True  # stat 失败，保守地认为有变化
    return st.st_mtime > cached_mtime


# ── SQLite 单 writer 线程（P1-8） ────────────────────────────

_sqlite_writer_queue: Optional['queue.Queue'] = None
_sqlite_writer_thread: Optional[threading.Thread] = None
_writer_lock = threading.Lock()


def _start_sqlite_writer():
    """启动 SQLite 单 writer 线程（P1-8）"""
    global _sqlite_writer_queue, _sqlite_writer_thread
    with _writer_lock:
        if _sqlite_writer_thread is not None:
            return
        _sqlite_writer_queue = queue.Queue()

        def _writer_loop():
            q = _sqlite_writer_queue
            while True:
                try:
                    item = q.get()
                    if item is None:  # 关闭信号
                        break
                    op = item[0]
                    try:
                        if op == "set_batch":
                            # item = (op, pcache, items_list)
                            item[1].set_batch(item[2])
                        elif op == "delete":
                            # item = (op, pcache, path_hash)
                            item[1].delete(item[2])
                        elif op == "delete_batch":
                            # item = (op, pcache, path_hashes_list)
                            item[1].delete_batch(item[2])
                        elif op == "flush":
                            # 强制 commit（writer 的 set_batch 已自带 commit）
                            pass
                        elif op == "done":
                            # 通知调用方刷新完成
                            item[1].set()
                    except Exception:
                        pass
                except Exception:
                    pass

        _sqlite_writer_thread = threading.Thread(
            target=_writer_loop,
            daemon=True,
            name="SQLiteWriter"
        )
        _sqlite_writer_thread.start()


def _flush_sqlite_writer():
    """等待 writer 线程处理完所有待处理的写入"""
    if _sqlite_writer_queue is None or _sqlite_writer_thread is None:
        return
    done_event = threading.Event()
    _sqlite_writer_queue.put(("done", done_event))
    done_event.wait(timeout=30)


def _stop_sqlite_writer():
    """停止 writer 线程"""
    global _sqlite_writer_queue, _sqlite_writer_thread
    if _sqlite_writer_queue is not None:
        _sqlite_writer_queue.put(None)
    if _sqlite_writer_thread is not None:
        _sqlite_writer_thread.join(timeout=10)
        _sqlite_writer_thread = None
        _sqlite_writer_queue = None


# ── 同步线程 ───────────────────────────────────────────────────

def _sync_thread(config: Config, logger, stop_event: threading.Event):
    """
    后台同步线程 - 自适应增量分片扫描。

    策略：每次扫描使用时间预算（interval 秒），
    在预算内尽量多处理目录。扫描完成后等待剩余时间。
    """
    interval = config.sync.interval
    # 时间预算 = interval - 5秒（留 5 秒给备份操作）
    time_budget = max(5.0, interval - 5.0)

    # 初始化缓存目录：优先使用配置值，否则自动推导（backup_dir 的父目录 + .cache）
    if config.sync.cache_dir:
        cache_dir = config.sync.cache_dir
    else:
        cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(config.backup_dir)),
            ".cache"
        )
    pcache, mcache, dir_cache = _get_caches(cache_dir)
    backup_workers = config.sync.backup_workers
    scanner = IncrementalScanner(backup_workers=backup_workers)

    logger.info(
        f"增量同步引擎已启动: 间隔 {interval}s, "
        f"时间预算 {time_budget:.0f}s, "
        f"备份线程池 {backup_workers} workers, 缓存目录 {cache_dir}"
    )

    try:
        while not stop_event.is_set():
            scan_start = time.time()
            try:
                backed_up, skipped, scanned = scanner.scan_batch(
                    config, logger, pcache, mcache, dir_cache,
                    time_budget=time_budget
                )
                cursor, total_dirs = scanner.progress
                scan_elapsed = time.time() - scan_start

                if backed_up > 0:
                    logger.info(
                        f"定期同步: 扫描 {scanned} 个文件, "
                        f"提交备份 {backed_up} 个, "
                        f"跳过 {skipped} 个(未变化) | "
                        f"进度 {cursor}/{total_dirs} 目录, "
                        f"第 {scanner.current_round} 轮, "
                        f"耗时 {scan_elapsed:.1f}s"
                    )
                else:
                    logger.debug(
                        f"定期同步: 扫描 {scanned} 个文件, "
                        f"全部未变化 | 进度 {cursor}/{total_dirs} 目录, "
                        f"耗时 {scan_elapsed:.1f}s"
                    )
            except Exception as e:
                logger.error(f"同步扫描异常: {e}")

            # 等待剩余时间（保证总周期 = interval）
            elapsed = time.time() - scan_start
            remaining = max(0, interval - elapsed)
            if remaining > 0:
                stop_event.wait(remaining)
    finally:
        scanner.shutdown()
        # P1-8: 停止 SQLite writer 线程
        _stop_sqlite_writer()


def start_sync(config: Config, logger) -> tuple:
    """
    启动定期同步线程。

    返回 (thread, stop_event) 元组。
    """
    if not config.sync.enabled:
        logger.info("定期同步已禁用")
        return None, None

    # P1-8: 启动 SQLite 单 writer 线程
    _start_sqlite_writer()
    logger.info("SQLite 单 writer 线程已启动")

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
