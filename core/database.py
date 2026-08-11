"""
MySQL 元数据存储模块

将备份元信息（hash/size/mtime/source/backup_time）和回收站元信息
从文件系统迁移到 MySQL，消除 .meta 和 .recycle.json 文件。

性能优化（亿级场景）：
- 分页查询替代全量 fetchall，避免 OOM
- 统计聚合（COUNT/SUM）直接在数据库完成，避免 Python 层遍历
- 批量 upsert 减少数据库交互次数
- 路径哈希索引提升区分度
- 连接断线自动重连
- 连接池复用 MySQL 连接（P1-7）
- 增量统计计数器：stats 表 + 内存缓存，O(1) 查询（P1-6）
"""

import time
import hashlib
import threading
import pymysql
from typing import Optional, Dict, List, Tuple
from pymysql.cursors import DictCursor


# ── MySQL 连接池（P1-7） ─────────────────────────────────────

class ConnectionPool:
    """
    简单的 MySQL 连接池。
    线程安全，支持连接健康检查和最大空闲连接数限制。
    """

    def __init__(self, max_size: int = 16, config: dict = None):
        self._pool: list = []
        self._max_size = max_size
        self._config = config or {}
        self._lock = threading.Lock()

    def acquire(self):
        """从池中获取连接，池空时创建新连接"""
        with self._lock:
            while self._pool:
                conn = self._pool.pop()
                try:
                    if conn.open:
                        conn.ping()
                        return conn
                except Exception:
                    pass
                self._safe_close(conn)
        return pymysql.connect(**self._config)

    def release(self, conn):
        """归还连接到池"""
        try:
            if conn and conn.open:
                with self._lock:
                    if len(self._pool) < self._max_size:
                        self._pool.append(conn)
                        return
        except Exception:
            pass
        self._safe_close(conn)

    def close_all(self):
        """关闭池中所有连接"""
        with self._lock:
            for conn in self._pool:
                self._safe_close(conn)
            self._pool.clear()

    @staticmethod
    def _safe_close(conn):
        try:
            conn.close()
        except Exception:
            pass


# 全局数据库实例
_db_instance: Optional['Database'] = None
_db_lock = threading.Lock()


def init_database(host: str, port: int, user: str, password: str,
                  database: str) -> 'Database':
    """初始化全局数据库实例并创建表结构"""
    global _db_instance
    with _db_lock:
        _db_instance = Database(host, port, user, password, database)
        _db_instance.init_tables()
    return _db_instance


def get_db() -> Optional['Database']:
    """获取全局数据库实例"""
    return _db_instance


def _path_hash(watch_root: str, rel_path: str) -> str:
    """计算路径组合的 SHA256 短哈希（用于唯一索引）"""
    return hashlib.sha256(f"{watch_root}|{rel_path}".encode("utf-8")).hexdigest()


class Database:
    """MySQL 数据库管理器，提供连接池和线程安全的元数据操作"""

    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str):
        self._config = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
            "charset": "utf8mb4",
            "autocommit": True,
        }
        self._local = threading.local()
        self._lock = threading.Lock()
        # P1-7: 连接池
        self._pool = ConnectionPool(max_size=16, config=self._config)
        # P1-6: 增量统计内存缓存
        self._stats_lock = threading.Lock()
        self._stats_backup_count: Optional[int] = None
        self._stats_backup_size: Optional[int] = None
        self._stats_recycle_count: Optional[int] = None
        self._stats_recycle_size: Optional[int] = None

    # ── 连接管理 ────────────────────────────────────────────

    def _get_conn(self):
        """获取当前线程的数据库连接（从连接池获取 + 健康检查）"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                if conn.open:
                    conn.ping()
                    return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
            self._local.conn = None
        # 从连接池获取
        conn = self._pool.acquire()
        self._local.conn = conn
        return conn

    def _retry_on_disconnect(self, func, *args, **kwargs):
        """连接断开时自动重连并重试一次"""
        try:
            return func(*args, **kwargs)
        except pymysql.err.OperationalError:
            self._local.conn = None
            return func(*args, **kwargs)

    def close(self):
        """关闭当前线程的连接（归还到连接池）"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            self._pool.release(conn)

    # ── 初始化 ──────────────────────────────────────────────

    def init_tables(self):
        """创建所需的表结构（幂等操作），并自动迁移旧表"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            # 备份元信息表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS backup_meta (
                    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
                    rel_path    VARCHAR(1024) NOT NULL COMMENT '相对于监控根目录的路径',
                    watch_root  VARCHAR(1024) NOT NULL COMMENT '所属监控根目录',
                    file_hash   VARCHAR(128)  DEFAULT NULL COMMENT 'SHA256 哈希',
                    file_size   BIGINT        DEFAULT 0,
                    mtime       DOUBLE        DEFAULT 0 COMMENT '源文件修改时间',
                    source_path VARCHAR(1024) DEFAULT NULL COMMENT '源文件绝对路径',
                    backup_time DOUBLE        DEFAULT 0 COMMENT '备份时间戳',
                    path_hash   VARCHAR(64)     NOT NULL COMMENT 'watch_root+rel_path 的 SHA256 hex',
                    created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_path_hash (path_hash),
                    INDEX idx_watch_root (watch_root(255))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COMMENT='备份文件元信息'
            """)
            # 回收站元信息表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recycle_meta (
                    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                    recycle_path    VARCHAR(1024) NOT NULL COMMENT '回收站中的相对路径（相对于 recycle_dir）',
                    original_path   VARCHAR(1024) DEFAULT NULL COMMENT '原始绝对路径',
                    relative_path   VARCHAR(1024) DEFAULT NULL COMMENT '原始相对路径',
                    watch_root      VARCHAR(1024) DEFAULT NULL,
                    is_directory    TINYINT(1)    DEFAULT 0,
                    deletion_time   DOUBLE        DEFAULT 0 COMMENT '删除时间戳',
                    deletion_time_str VARCHAR(32) DEFAULT NULL,
                    file_size       BIGINT        DEFAULT 0,
                    file_hash       VARCHAR(128)  DEFAULT NULL,
                    original_mtime  DOUBLE        DEFAULT 0,
                    created_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_recycle_path (recycle_path(512)),
                    INDEX idx_deletion_time (deletion_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COMMENT='回收站文件元信息'
            """)
            # P1-6: 增量统计表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    name  VARCHAR(64) PRIMARY KEY,
                    value BIGINT NOT NULL DEFAULT 0
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            conn.commit()

        # 自动迁移：为旧表添加 path_hash 列
        self._migrate_add_path_hash(conn)

    def _migrate_add_path_hash(self, conn):
        """
        自动迁移：为旧版 backup_meta 表添加 path_hash 列并回填数据。
        如果列已存在但类型不对（如 BINARY(32)），则修复为 VARCHAR(64)。
        """
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'backup_meta'
                  AND COLUMN_NAME = 'path_hash'
            """)
            row = cur.fetchone()

        if row is not None:
            data_type = row[0]
            # 如果已经是 VARCHAR(64)，无需迁移
            if data_type == 'varchar':
                return
            # 列存在但类型不对（如 binary），需要修改类型
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE backup_meta
                    MODIFY COLUMN path_hash VARCHAR(64) DEFAULT NULL
                    COMMENT 'watch_root+rel_path 的 SHA256 hex'
                """)
            # 回填数据（可能之前未成功回填）
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE backup_meta
                    SET path_hash = SHA2(CONCAT(watch_root, '|', rel_path), 256)
                    WHERE path_hash IS NULL OR LENGTH(path_hash) != 64
                """)
            # 改为 NOT NULL
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE backup_meta
                    MODIFY COLUMN path_hash VARCHAR(64) NOT NULL
                    COMMENT 'watch_root+rel_path 的 SHA256 hex'
                """)
        else:
            # 列不存在，全新添加
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE backup_meta
                    ADD COLUMN path_hash VARCHAR(64) DEFAULT NULL
                    COMMENT 'watch_root+rel_path 的 SHA256 hex'
                """)

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE backup_meta
                    SET path_hash = SHA2(CONCAT(watch_root, '|', rel_path), 256)
                """)

            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE backup_meta
                    MODIFY COLUMN path_hash VARCHAR(64) NOT NULL
                    COMMENT 'watch_root+rel_path 的 SHA256 hex'
                """)

        # 删除旧唯一索引，添加新唯一索引
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'backup_meta'
                  AND INDEX_NAME = 'uk_path'
            """)
            if cur.fetchone()[0] > 0:
                cur.execute("ALTER TABLE backup_meta DROP INDEX uk_path")

            cur.execute("""
                SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'backup_meta'
                  AND INDEX_NAME = 'uk_path_hash'
            """)
            if cur.fetchone()[0] == 0:
                cur.execute("""
                    ALTER TABLE backup_meta
                    ADD UNIQUE INDEX uk_path_hash (path_hash)
                """)

        conn.commit()

    # ── 备份 meta 操作 ──────────────────────────────────────

    def get_backup_meta(self, watch_root: str, rel_path: str) -> Optional[Dict]:
        """查询备份元信息，返回 dict 或 None"""
        ph = _path_hash(watch_root, rel_path)
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                "SELECT file_hash, file_size, mtime, source_path, backup_time "
                "FROM backup_meta WHERE path_hash=%s",
                (ph,)
            )
            return cur.fetchone()

    def upsert_backup_meta(self, watch_root: str, rel_path: str,
                           file_hash: str, file_size: int, mtime: float,
                           source_path: str, backup_time: float):
        """插入或更新备份元信息（使用路径哈希索引 + 增量统计）"""
        ph = _path_hash(watch_root, rel_path)
        conn = self._get_conn()

        # P1-6: 查询旧值以计算增量
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_size FROM backup_meta WHERE path_hash=%s",
                (ph,)
            )
            old_row = cur.fetchone()

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backup_meta
                    (watch_root, rel_path, file_hash, file_size, mtime,
                     source_path, backup_time, path_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    file_hash   = VALUES(file_hash),
                    file_size   = VALUES(file_size),
                    mtime       = VALUES(mtime),
                    source_path = VALUES(source_path),
                    backup_time = VALUES(backup_time)
            """, (watch_root, rel_path, file_hash, file_size, mtime,
                  source_path, backup_time, ph))
        conn.commit()

        # P1-6: 增量更新统计
        if old_row is None:
            self._adjust_backup_stats(1, file_size)
        else:
            old_size = old_row[0] or 0
            self._adjust_backup_stats(0, file_size - old_size)

    def batch_upsert_backup_meta(self, items: List[Dict]):
        """
        批量插入/更新备份元信息，减少数据库交互次数。
        items: [{"watch_root", "rel_path", "file_hash", "file_size",
                 "mtime", "source_path", "backup_time"}, ...]
        """
        if not items:
            return
        conn = self._get_conn()

        # P1-6: 查询已存在的 path_hash 及其 file_size（用于增量统计）
        all_phs = [
            _path_hash(it["watch_root"], it["rel_path"]) for it in items
        ]
        old_sizes: Dict[str, int] = {}
        for i in range(0, len(all_phs), 500):
            batch_phs = all_phs[i:i + 500]
            placeholders = ",".join("%s" for _ in batch_phs)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT path_hash, file_size FROM backup_meta "
                    f"WHERE path_hash IN ({placeholders})",
                    batch_phs
                )
                for row in cur.fetchall():
                    old_sizes[row[0]] = row[1] or 0

        # 执行批量 upsert
        batch_size = 100
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            placeholders = []
            values = []
            for item in batch:
                ph = _path_hash(item["watch_root"], item["rel_path"])
                placeholders.append(
                    "(%s, %s, %s, %s, %s, %s, %s, %s)"
                )
                values.extend([
                    item["watch_root"], item["rel_path"],
                    item["file_hash"], item["file_size"],
                    item["mtime"], item["source_path"],
                    item["backup_time"], ph,
                ])
            sql = (
                "INSERT INTO backup_meta "
                "(watch_root, rel_path, file_hash, file_size, mtime, "
                "source_path, backup_time, path_hash) VALUES "
                + ", ".join(placeholders)
                + " ON DUPLICATE KEY UPDATE "
                "file_hash=VALUES(file_hash), file_size=VALUES(file_size), "
                "mtime=VALUES(mtime), source_path=VALUES(source_path), "
                "backup_time=VALUES(backup_time)"
            )
            with conn.cursor() as cur:
                cur.execute(sql, values)
        conn.commit()

        # P1-6: 增量更新统计
        new_count = 0
        size_delta = 0
        for item in items:
            ph = _path_hash(item["watch_root"], item["rel_path"])
            new_size = item["file_size"]
            if ph in old_sizes:
                size_delta += new_size - old_sizes[ph]
            else:
                new_count += 1
                size_delta += new_size
        if new_count != 0 or size_delta != 0:
            self._adjust_backup_stats(new_count, size_delta)

    def delete_backup_meta(self, watch_root: str, rel_path: str):
        """删除备份元信息（+ 增量统计）"""
        ph = _path_hash(watch_root, rel_path)
        conn = self._get_conn()

        # 查询旧值以计算增量
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_size FROM backup_meta WHERE path_hash=%s",
                (ph,)
            )
            old_row = cur.fetchone()

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM backup_meta WHERE path_hash=%s",
                (ph,)
            )
        conn.commit()

        if old_row is not None:
            self._adjust_backup_stats(-1, -(old_row[0] or 0))

    def list_all_backup_meta(self) -> List[Dict]:
        """列出所有备份元信息（用于清理模块）
        注意：亿级数据量下应使用 iter_backup_meta() 流式遍历
        """
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                "SELECT rel_path, watch_root, file_hash, file_size, mtime, "
                "source_path, backup_time FROM backup_meta"
            )
            return cur.fetchall()

    def iter_backup_meta_batch(self, batch_size: int = 5000):
        """
        流式分批遍历所有备份元信息（避免亿级数据 OOM）。
        生成器，每次 yield 一批 List[Dict]。
        """
        conn = self._get_conn()
        last_id = 0
        while True:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT id, rel_path, watch_root, file_hash, file_size, "
                    "mtime, source_path, backup_time FROM backup_meta "
                    "WHERE id > %s ORDER BY id LIMIT %s",
                    (last_id, batch_size)
                )
                rows = cur.fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
            yield rows

    # ── 统计聚合（P1-6: 增量计数器，O(1) 查询） ────────────

    def _adjust_backup_stats(self, count_delta: int, size_delta: int):
        """原子更新 MySQL stats 表 + 内存缓存（备份统计）"""
        with self._stats_lock:
            if self._stats_backup_count is not None:
                self._stats_backup_count += count_delta
                self._stats_backup_size += size_delta
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                if count_delta != 0:
                    cur.execute(
                        "INSERT INTO stats (name, value) VALUES "
                        "('backup_count', %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "value = value + %s",
                        (count_delta, count_delta)
                    )
                if size_delta != 0:
                    cur.execute(
                        "INSERT INTO stats (name, value) VALUES "
                        "('backup_size', %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "value = value + %s",
                        (size_delta, size_delta)
                    )
            conn.commit()
        except Exception:
            # 统计更新失败不影响主流程，下次访问时会从 DB 重新加载
            with self._stats_lock:
                self._stats_backup_count = None

    def _ensure_stats_loaded(self):
        """确保内存统计缓存已加载（从 stats 表或回退到全表扫描）"""
        with self._stats_lock:
            if (self._stats_backup_count is not None
                    and self._stats_recycle_count is not None):
                return
        try:
            conn = self._get_conn()
            stats_map = {}
            with conn.cursor() as cur:
                cur.execute("SELECT name, value FROM stats")
                for row in cur.fetchall():
                    stats_map[row[0]] = row[1]

            with self._stats_lock:
                if "backup_count" in stats_map:
                    self._stats_backup_count = stats_map["backup_count"]
                    self._stats_backup_size = stats_map.get("backup_size", 0)
                else:
                    # stats 表为空，从全表计算并写入
                    self._recompute_stats_from_tables()

                if self._stats_recycle_count is None:
                    if "recycle_count" in stats_map:
                        self._stats_recycle_count = stats_map["recycle_count"]
                        self._stats_recycle_size = stats_map.get(
                            "recycle_size", 0
                        )
                    else:
                        self._recompute_stats_from_tables()
        except Exception:
            self._recompute_stats_from_tables()

    def _recompute_stats_from_tables(self):
        """从全表重新计算统计（仅在首次启动或 stats 表为空时调用）"""
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), IFNULL(SUM(file_size),0) "
                            "FROM backup_meta")
                bc, bs = cur.fetchone()
                cur.execute("SELECT COUNT(*), IFNULL(SUM(file_size),0) "
                            "FROM recycle_meta")
                rc, rs = cur.fetchone()

            # 写入 stats 表
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stats (name, value) VALUES "
                    "('backup_count', %s), ('backup_size', %s), "
                    "('recycle_count', %s), ('recycle_size', %s) "
                    "ON DUPLICATE KEY UPDATE value = VALUES(value)",
                    (bc, bs, rc, rs)
                )
            conn.commit()

            with self._stats_lock:
                self._stats_backup_count = bc
                self._stats_backup_size = bs
                self._stats_recycle_count = rc
                self._stats_recycle_size = rs
        except Exception:
            with self._stats_lock:
                self._stats_backup_count = 0
                self._stats_backup_size = 0
                self._stats_recycle_count = 0
                self._stats_recycle_size = 0

    def count_backup_meta(self) -> int:
        """统计备份元信息总数（P1-6: O(1) 内存查询）"""
        self._ensure_stats_loaded()
        with self._stats_lock:
            return self._stats_backup_count or 0

    def sum_backup_size(self) -> int:
        """统计所有备份文件的总大小（P1-6: O(1) 内存查询）"""
        self._ensure_stats_loaded()
        with self._stats_lock:
            return self._stats_backup_size or 0

    def count_recycle_meta(self) -> int:
        """统计回收站元信息总数（P1-6: O(1) 内存查询）"""
        self._ensure_stats_loaded()
        with self._stats_lock:
            return self._stats_recycle_count or 0

    def sum_recycle_size(self) -> int:
        """统计回收站文件总大小（P1-6: O(1) 内存查询）"""
        self._ensure_stats_loaded()
        with self._stats_lock:
            return self._stats_recycle_size or 0

    # ── 回收站 meta 操作 ────────────────────────────────────

    def insert_recycle_meta(self, recycle_path: str, original_path: str,
                            relative_path: str, watch_root: str,
                            is_directory: bool, deletion_time: float,
                            deletion_time_str: str, file_size: int,
                            file_hash: str, original_mtime: float):
        """插入回收站元信息（+ P1-6 增量统计）"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO recycle_meta
                    (recycle_path, original_path, relative_path, watch_root,
                     is_directory, deletion_time, deletion_time_str,
                     file_size, file_hash, original_mtime)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (recycle_path, original_path, relative_path, watch_root,
                  int(is_directory), deletion_time, deletion_time_str,
                  file_size, file_hash, original_mtime))
        conn.commit()
        # P1-6: 增量更新回收站统计
        self._adjust_recycle_stats(1, file_size)

    def get_recycle_meta(self, recycle_path: str) -> Optional[Dict]:
        """查询单条回收站元信息"""
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                "SELECT * FROM recycle_meta WHERE recycle_path=%s",
                (recycle_path,)
            )
            return cur.fetchone()

    def list_all_recycle_meta(self) -> List[Dict]:
        """列出所有回收站元信息
        注意：大量数据下应使用 list_recycle_meta_paged()
        """
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute("SELECT * FROM recycle_meta ORDER BY deletion_time DESC")
            return cur.fetchall()

    def list_recycle_meta_paged(self, page: int = 1, page_size: int = 20,
                                search: str = "") -> Tuple[List[Dict], int]:
        """
        分页查询回收站元信息。
        返回 (records, total_count) 元组。
        """
        conn = self._get_conn()
        where_clause = ""
        params = []

        if search:
            where_clause = (
                "WHERE relative_path LIKE %s OR original_path LIKE %s "
                "OR recycle_path LIKE %s"
            )
            like = f"%{search}%"
            params = [like, like, like]

        # 查总数
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM recycle_meta {where_clause}",
                params
            )
            total = cur.fetchone()[0]

        # 查分页数据
        offset = (page - 1) * page_size
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                f"SELECT * FROM recycle_meta {where_clause} "
                f"ORDER BY deletion_time DESC LIMIT %s OFFSET %s",
                params + [page_size, offset]
            )
            rows = cur.fetchall()

        return rows, total

    def delete_recycle_meta(self, recycle_path: str):
        """删除回收站元信息（+ P1-6 增量统计）"""
        conn = self._get_conn()
        # 查询旧值
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_size FROM recycle_meta WHERE recycle_path=%s",
                (recycle_path,)
            )
            old_row = cur.fetchone()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recycle_meta WHERE recycle_path=%s",
                (recycle_path,)
            )
        conn.commit()
        if old_row is not None:
            self._adjust_recycle_stats(-1, -(old_row[0] or 0))

    def delete_expired_recycle_meta(self, cutoff: float,
                                     batch_size: int = 1000):
        """
        分批删除过期的回收站元信息（生成器），避免百万级记录一次性加载到内存。
        每次 yield 一批被删除的记录 List[Dict]，调用方逐批处理即可。
        """
        conn = self._get_conn()
        total_deleted = 0
        while True:
            # 每批先查询被删记录的 file_size（用于增量统计）
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT recycle_path, relative_path, file_size "
                    "FROM recycle_meta WHERE deletion_time < %s LIMIT %s",
                    (cutoff, batch_size)
                )
                rows = cur.fetchall()
            if not rows:
                break
            recycle_paths = [r["recycle_path"] for r in rows]
            batch_size_sum = sum(r.get("file_size", 0) or 0 for r in rows)
            # 分批 DELETE
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM recycle_meta WHERE deletion_time < %s LIMIT %s",
                    (cutoff, batch_size)
                )
            conn.commit()
            # P1-6: 增量更新回收站统计
            self._adjust_recycle_stats(-len(rows), -batch_size_sum)
            total_deleted += len(rows)
            yield rows
            if len(rows) < batch_size:
                break

    # ── P1-6: 回收站统计增量更新 ─────────────────────────────

    def _adjust_recycle_stats(self, count_delta: int, size_delta: int):
        """原子更新 MySQL stats 表 + 内存缓存（回收站统计）"""
        with self._stats_lock:
            if self._stats_recycle_count is not None:
                self._stats_recycle_count += count_delta
                self._stats_recycle_size += size_delta
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                if count_delta != 0:
                    cur.execute(
                        "INSERT INTO stats (name, value) VALUES "
                        "('recycle_count', %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "value = value + %s",
                        (count_delta, count_delta)
                    )
                if size_delta != 0:
                    cur.execute(
                        "INSERT INTO stats (name, value) VALUES "
                        "('recycle_size', %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "value = value + %s",
                        (size_delta, size_delta)
                    )
            conn.commit()
        except Exception:
            with self._stats_lock:
                self._stats_recycle_count = None
