"""
MySQL 元数据存储模块

将备份元信息（hash/size/mtime/source/backup_time）和回收站元信息
从文件系统迁移到 MySQL，消除 .meta 和 .recycle.json 文件。
"""

import time
import threading
import pymysql
from typing import Optional, Dict, List
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
        """获取当前线程的数据库连接（惰性创建）"""
        conn = getattr(self._local, "conn", None)
        if conn is None or not conn.open:
            conn = pymysql.connect(**self._config)
            self._local.conn = conn
        return conn

    def close(self):
        """关闭当前线程的连接"""
        conn = getattr(self._local, "conn", None)
        if conn and conn.open:
            conn.close()
            self._local.conn = None

    # ── 初始化 ──────────────────────────────────────────────

    def init_tables(self):
        """创建所需的表结构（幂等操作）"""
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
                    created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_path (watch_root(255), rel_path(255))
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
                    UNIQUE KEY uk_recycle_path (recycle_path(512))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COMMENT='回收站文件元信息'
            """)

    # ── 备份 meta 操作 ──────────────────────────────────────

    def get_backup_meta(self, watch_root: str, rel_path: str) -> Optional[Dict]:
        """查询备份元信息，返回 dict 或 None"""
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                "SELECT file_hash, file_size, mtime, source_path, backup_time "
                "FROM backup_meta WHERE watch_root=%s AND rel_path=%s",
                (watch_root, rel_path)
            )
            return cur.fetchone()

    def upsert_backup_meta(self, watch_root: str, rel_path: str,
                           file_hash: str, file_size: int, mtime: float,
                           source_path: str, backup_time: float):
        """插入或更新备份元信息"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backup_meta
                    (watch_root, rel_path, file_hash, file_size, mtime,
                     source_path, backup_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    file_hash   = VALUES(file_hash),
                    file_size   = VALUES(file_size),
                    mtime       = VALUES(mtime),
                    source_path = VALUES(source_path),
                    backup_time = VALUES(backup_time)
            """, (watch_root, rel_path, file_hash, file_size, mtime,
                  source_path, backup_time))

    def delete_backup_meta(self, watch_root: str, rel_path: str):
        """删除备份元信息"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM backup_meta WHERE watch_root=%s AND rel_path=%s",
                (watch_root, rel_path)
            )

    def list_all_backup_meta(self) -> List[Dict]:
        """列出所有备份元信息（用于清理模块）"""
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                "SELECT rel_path, watch_root, file_hash, file_size, mtime, "
                "source_path, backup_time FROM backup_meta"
            )
            return cur.fetchall()

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
        """列出所有回收站元信息"""
        conn = self._get_conn()
        with conn.cursor(DictCursor) as cur:
            cur.execute("SELECT * FROM recycle_meta ORDER BY deletion_time DESC")
            return cur.fetchall()

    def delete_recycle_meta(self, recycle_path: str):
        """删除回收站元信息"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recycle_meta WHERE recycle_path=%s",
                (recycle_path,)
            )

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
        return rows
