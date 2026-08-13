"""
USN 事件持久化存储（SQLite）。

提供 fs_event（持久化事件队列）和 file_identity（文件身份追踪）两张表的
SQLite 存储层，与 UsnCheckpointStore 共享同一个 SQLite 数据库文件。

架构决策：
- USN 相关状态全部使用 SQLite（本地 NTFS 功能，不需要 MySQL 分布式特性）
- SQLite WAL 模式适合高频小事务写入
- 与主库 MySQL 解耦，USN 子系统独立运行
"""

import os
import sqlite3
import threading
import time
from typing import List, Dict, Optional, Tuple


class UsnEventStore:
    """
    USN 事件 + 文件身份的 SQLite 持久化存储。

    线程安全：使用 threading.Lock 保护写操作，
    SQLite 连接通过 thread-local 存储保证线程独占。

    表结构：
    - fs_event: 持久化事件队列（PENDING → PROCESSING → DONE/FAILED）
    - file_identity: 文件身份追踪（基于 volume_id + FRN）
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接（延迟创建）"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _ensure_tables(self):
        """确保 fs_event 和 file_identity 表存在"""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fs_event (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id       TEXT NOT NULL,
                usn             INTEGER NOT NULL,
                file_reference  INTEGER,
                parent_reference INTEGER,
                reason          INTEGER NOT NULL,
                path            TEXT,
                event_type      TEXT NOT NULL,
                state           TEXT NOT NULL DEFAULT 'PENDING',
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL,
                UNIQUE(volume_id, usn)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fs_event_state
            ON fs_event(state)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_identity (
                volume_id               TEXT NOT NULL,
                file_reference_number   INTEGER NOT NULL,
                watch_root              TEXT NOT NULL,
                relative_path           TEXT NOT NULL,
                is_directory            INTEGER NOT NULL DEFAULT 0,
                last_usn                INTEGER NOT NULL,
                file_size               INTEGER,
                mtime_ns                INTEGER,
                state                   TEXT NOT NULL DEFAULT 'ACTIVE',
                PRIMARY KEY (volume_id, file_reference_number)
            )
        """)
        conn.commit()

    # ── fs_event 操作 ─────────────────────────────────────────

    def batch_insert_fs_events(self, events: List[Dict]):
        """
        批量插入事件到 fs_event 表。

        Args:
            events: [{"volume_id", "usn", "file_reference", "parent_reference",
                      "reason", "path", "event_type", "state", "created_at"}, ...]
        """
        if not events:
            return

        conn = self._get_conn()
        now = time.time()

        with self._lock:
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO fs_event "
                    "(volume_id, usn, file_reference, parent_reference, "
                    "reason, path, event_type, state, created_at, updated_at) "
                    "VALUES (:volume_id, :usn, :file_reference, :parent_reference, "
                    ":reason, :path, :event_type, :state, :created_at, :updated_at)",
                    [
                        {
                            "volume_id": e.get("volume_id", ""),
                            "usn": e.get("usn", 0),
                            "file_reference": e.get("file_reference", 0),
                            "parent_reference": e.get("parent_reference", 0),
                            "reason": e.get("reason", 0),
                            "path": e.get("path", ""),
                            "event_type": e.get("event_type", ""),
                            "state": e.get("state", "PENDING"),
                            "created_at": e.get("created_at", now),
                            "updated_at": now,
                        }
                        for e in events
                    ]
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def update_fs_event_state(self, event_id: int, state: str):
        """更新单个事件的状态"""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "UPDATE fs_event SET state = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), event_id)
            )
            conn.commit()

    def reset_processing_events(self) -> int:
        """
        crash recovery：将 PROCESSING 状态的事件重置为 PENDING。

        Returns:
            被重置的事件数量
        """
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                "UPDATE fs_event SET state = 'PENDING', updated_at = ? "
                "WHERE state = 'PROCESSING'",
                (time.time(),)
            )
            conn.commit()
            return cur.rowcount

    def fetch_pending_events(self, batch_size: int = 100) -> List[Dict]:
        """
        取出一批 PENDING 事件并标记为 PROCESSING。

        Returns:
            事件列表 [{"id", "volume_id", "usn", "path", "event_type", ...}, ...]
        """
        conn = self._get_conn()
        now = time.time()

        with self._lock:
            # 先查出要处理的 event ids
            cur = conn.execute(
                "SELECT id FROM fs_event WHERE state = 'PENDING' "
                "ORDER BY id LIMIT ?",
                (batch_size,)
            )
            ids = [row[0] for row in cur.fetchall()]

            if not ids:
                return []

            # 批量更新为 PROCESSING
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE fs_event SET state = 'PROCESSING', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                [now] + ids
            )
            conn.commit()

        # 查询完整数据（在锁外，使用只读查询）
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"SELECT id, volume_id, usn, file_reference, parent_reference, "
            f"reason, path, event_type, state, created_at "
            f"FROM fs_event WHERE id IN ({placeholders})",
            ids
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def batch_update_event_states(self, event_ids: List[int], state: str):
        """批量更新事件状态"""
        if not event_ids:
            return
        conn = self._get_conn()
        now = time.time()
        placeholders = ",".join("?" for _ in event_ids)
        with self._lock:
            conn.execute(
                f"UPDATE fs_event SET state = ?, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                [state, now] + event_ids
            )
            conn.commit()

    def count_pending_events(self) -> int:
        """统计 PENDING 事件数量"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT COUNT(*) FROM fs_event WHERE state = 'PENDING'"
        )
        return cur.fetchone()[0]

    # ── file_identity 操作 ────────────────────────────────────

    def upsert_file_identity(self, volume_id: str, frn: int,
                              watch_root: str, relative_path: str,
                              is_directory: bool = False,
                              last_usn: int = 0,
                              file_size: Optional[int] = None,
                              mtime_ns: Optional[int] = None,
                              state: str = "ACTIVE"):
        """插入或更新文件身份信息"""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO file_identity "
                "(volume_id, file_reference_number, watch_root, relative_path, "
                "is_directory, last_usn, file_size, mtime_ns, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(volume_id, file_reference_number) DO UPDATE SET "
                "watch_root = excluded.watch_root, "
                "relative_path = excluded.relative_path, "
                "is_directory = excluded.is_directory, "
                "last_usn = excluded.last_usn, "
                "file_size = excluded.file_size, "
                "mtime_ns = excluded.mtime_ns, "
                "state = excluded.state",
                (volume_id, frn, watch_root, relative_path,
                 int(is_directory), last_usn, file_size, mtime_ns, state)
            )
            conn.commit()

    def get_file_identity(self, volume_id: str, frn: int) -> Optional[Dict]:
        """查询文件身份信息"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT volume_id, file_reference_number, watch_root, "
            "relative_path, is_directory, last_usn, file_size, mtime_ns, state "
            "FROM file_identity WHERE volume_id = ? AND file_reference_number = ?",
            (volume_id, frn)
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))

    def list_file_identities(self, volume_id: str) -> Dict[int, Dict]:
        """
        列出指定卷的所有文件身份记录。

        Returns:
            {frn: {volume_id, file_reference_number, watch_root,
                   relative_path, is_directory, last_usn,
                   file_size, mtime_ns, state}, ...}
        """
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT volume_id, file_reference_number, watch_root, "
            "relative_path, is_directory, last_usn, file_size, mtime_ns, state "
            "FROM file_identity WHERE volume_id = ?",
            (volume_id,)
        )
        columns = [desc[0] for desc in cur.description]
        result = {}
        for row in cur.fetchall():
            record = dict(zip(columns, row))
            frn = record["file_reference_number"]
            result[frn] = record
        return result

    def update_file_identity_state(self, volume_id: str, frn: int,
                                    state: str):
        """更新单个文件身份状态（如标记为 DELETED）"""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "UPDATE file_identity SET state = ? "
                "WHERE volume_id = ? AND file_reference_number = ?",
                (state, volume_id, frn)
            )
            conn.commit()

    def batch_upsert_file_identities(self, items: List[Tuple]):
        """
        批量 upsert file_identity（P0-2b）。
        items: [(volume_id, frn, watch_root, rel_path, is_dir, last_usn,
                 file_size, mtime_ns, state), ...]
        """
        if not items:
            return
        conn = self._get_conn()
        with self._lock:
            conn.executemany(
                "INSERT INTO file_identity "
                "(volume_id, file_reference_number, watch_root, relative_path, "
                "is_directory, last_usn, file_size, mtime_ns, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(volume_id, file_reference_number) DO UPDATE SET "
                "watch_root = excluded.watch_root, "
                "relative_path = excluded.relative_path, "
                "is_directory = excluded.is_directory, "
                "last_usn = excluded.last_usn, "
                "file_size = excluded.file_size, "
                "mtime_ns = excluded.mtime_ns, "
                "state = excluded.state",
                items
            )
            conn.commit()

    def batch_update_file_identity_states(self, volume_id: str,
                                           frns: List[int], state: str):
        """
        批量更新文件身份状态（P0-3）。
        分批执行，每批 500 条，避免 IN 子句过长。
        """
        if not frns:
            return
        conn = self._get_conn()
        with self._lock:
            batch_size = 500
            for i in range(0, len(frns), batch_size):
                batch = frns[i:i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                conn.execute(
                    f"UPDATE file_identity SET state = ? "
                    f"WHERE volume_id = ? AND file_reference_number IN ({placeholders})",
                    [state, volume_id] + batch
                )
            conn.commit()

    def iter_file_identities(self, volume_id: str,
                              page_size: int = 10000):
        """
        分页迭代 file_identity 记录（P1-2），避免亿级数据 OOM。
        每次 yield 一个 {frn: record} 字典。
        """
        conn = self._get_conn()
        offset = 0
        while True:
            cur = conn.execute(
                "SELECT volume_id, file_reference_number, watch_root, "
                "relative_path, is_directory, last_usn, file_size, mtime_ns, state "
                "FROM file_identity WHERE volume_id = ? "
                "ORDER BY file_reference_number LIMIT ? OFFSET ?",
                (volume_id, page_size, offset)
            )
            rows = cur.fetchall()
            if not rows:
                break
            columns = [desc[0] for desc in cur.description]
            page = {}
            for row in rows:
                record = dict(zip(columns, row))
                page[record["file_reference_number"]] = record
            yield page
            offset += page_size

    # ── 生命周期 ──────────────────────────────────────────────

    def close(self):
        """关闭当前线程的连接"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
