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
"""

import time
import hashlib
import threading
import pymysql
from typing import Optional, Dict, List, Tuple
from pymysql.cursors import DictCursor


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

    # ── 连接管理 ────────────────────────────────────────────

    def _get_conn(self):
        """获取当前线程的数据库连接（惰性创建 + 健康检查 + 断线重连）"""
        conn = getattr(self._local, "conn", None)
        conn_time = getattr(self._local, "conn_time", 0)
        # 连接健康检查：ping 检测 + 最大空闲时间回收
        if conn is not None:
            if not conn.open:
                conn = None
            elif time.time() - conn_time > 3600:  # 1小时回收
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            else:
                try:
                    conn.ping()
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
        if conn is None:
            try:
                conn = pymysql.connect(**self._config)
            except pymysql.err.OperationalError:
                # 短暂等待后重试一次
                time.sleep(0.5)
                conn = pymysql.connect(**self._config)
            self._local.conn = conn
            self._local.conn_time = time.time()
        return conn

    def _retry_on_disconnect(self, func, *args, **kwargs):
        """连接断开时自动重连并重试一次"""
        try:
            return func(*args, **kwargs)
        except pymysql.err.OperationalError:
            self._local.conn = None
            return func(*args, **kwargs)

    def close(self):
        """关闭当前线程的连接"""
        conn = getattr(self._local, "conn", None)
        if conn and conn.open:
            conn.close()
            self._local.conn = None

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
        """插入或更新备份元信息（使用路径哈希索引）"""
        ph = _path_hash(watch_root, rel_path)
        conn = self._get_conn()
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

    def batch_upsert_backup_meta(self, items: List[Dict]):
        """
        批量插入/更新备份元信息，减少数据库交互次数。
        items: [{"watch_root", "rel_path", "file_hash", "file_size",
                 "mtime", "source_path", "backup_time"}, ...]
        """
        if not items:
            return
        conn = self._get_conn()
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

    def delete_backup_meta(self, watch_root: str, rel_path: str):
        """删除备份元信息"""
        ph = _path_hash(watch_root, rel_path)
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM backup_meta WHERE path_hash=%s",
                (ph,)
            )
        conn.commit()

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

    # ── 统计聚合（数据库层完成，避免 Python 遍历） ────────────

    def count_backup_meta(self) -> int:
        """统计备份元信息总数"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM backup_meta")
            return cur.fetchone()[0]

    def sum_backup_size(self) -> int:
        """统计所有备份文件的总大小"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT IFNULL(SUM(file_size), 0) FROM backup_meta")
            return cur.fetchone()[0]

    def count_recycle_meta(self) -> int:
        """统计回收站元信息总数"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM recycle_meta")
            return cur.fetchone()[0]

    def sum_recycle_size(self) -> int:
        """统计回收站文件总大小"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT IFNULL(SUM(file_size), 0) FROM recycle_meta")
            return cur.fetchone()[0]

    # ── 回收站 meta 操作 ────────────────────────────────────

    def insert_recycle_meta(self, recycle_path: str, original_path: str,
                            relative_path: str, watch_root: str,
                            is_directory: bool, deletion_time: float,
                            deletion_time_str: str, file_size: int,
                            file_hash: str, original_mtime: float):
        """插入回收站元信息"""
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
        """删除回收站元信息"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recycle_meta WHERE recycle_path=%s",
                (recycle_path,)
            )
        conn.commit()

    def delete_expired_recycle_meta(self, cutoff: float) -> List[Dict]:
        """删除过期的回收站元信息，返回被删除的记录列表"""
        conn = self._get_conn()
        # 先查出要删除的记录
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                "SELECT * FROM recycle_meta WHERE deletion_time < %s",
                (cutoff,)
            )
            rows = cur.fetchall()
        if rows:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM recycle_meta WHERE deletion_time < %s",
                    (cutoff,)
                )
            conn.commit()
        return rows
