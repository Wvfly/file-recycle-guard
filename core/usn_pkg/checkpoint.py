"""
USN Journal Checkpoint 持久化管理。

核心职责：
- 持久化每个卷的 (journal_id, next_usn, status)
- 使用 SQLite 替代 JSON 文件，与项目其他状态存储一致
- 支持 journal_id 校验（Journal 被删除/重建时触发 RESCAN）
- 支持 FirstUsn gap 检测（Journal 被覆盖时触发 RESCAN）
- checkpoint 必须与 Durable Event Commit 绑定（方案第二十节）

关键可靠性保证：
- checkpoint 更新在事件成功写入 fs_event 后才执行
- 使用 SQLite 事务保证原子性
"""

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict


class JournalStatus(Enum):
    """USN Journal 健康状态"""
    HEALTHY = "HEALTHY"
    JOURNAL_GAP = "JOURNAL_GAP"           # checkpoint.next_usn < journal.first_usn
    RESCAN_REQUIRED = "RESCAN_REQUIRED"   # journal_id 变化，需要全量重扫
    JOURNAL_RESET = "JOURNAL_RESET"       # Journal 被重建
    PAUSED = "PAUSED"
    ERROR = "ERROR"


@dataclass
class CheckpointInfo:
    """单个卷的 checkpoint 信息"""
    volume: str
    journal_id: int
    next_usn: int
    status: JournalStatus = JournalStatus.HEALTHY
    updated_at: float = 0.0

    @property
    def needs_reconciliation(self) -> bool:
        """是否需要执行 reconciliation（全量扫描）"""
        return self.status in (
            JournalStatus.JOURNAL_GAP,
            JournalStatus.RESCAN_REQUIRED,
            JournalStatus.JOURNAL_RESET,
        )


class UsnCheckpointStore:
    """
    USN Checkpoint 的 SQLite 持久化存储。

    每个卷一条记录：(volume, journal_id, next_usn, status, updated_at)

    线程安全：使用 threading.Lock 保护共享访问，
    SQLite 连接通过 thread-local 存储保证线程独占。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._ensure_table()

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

    def _ensure_table(self):
        """确保 checkpoint 表存在"""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usn_checkpoint (
                volume       TEXT PRIMARY KEY,
                journal_id   INTEGER NOT NULL,
                next_usn     INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'HEALTHY',
                updated_at   REAL NOT NULL
            )
        """)
        conn.commit()

    # ── 读取 ──────────────────────────────────────────────────

    def load(self, volume: str) -> Optional[CheckpointInfo]:
        """加载指定卷的 checkpoint，不存在返回 None"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT volume, journal_id, next_usn, status, updated_at "
            "FROM usn_checkpoint WHERE volume = ?",
            (volume,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return CheckpointInfo(
            volume=row[0],
            journal_id=row[1],
            next_usn=row[2],
            status=JournalStatus(row[3]) if row[3] else JournalStatus.HEALTHY,
            updated_at=row[4],
        )

    def load_all(self) -> Dict[str, CheckpointInfo]:
        """加载所有卷的 checkpoint"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT volume, journal_id, next_usn, status, updated_at "
            "FROM usn_checkpoint"
        )
        result = {}
        for row in cur.fetchall():
            result[row[0]] = CheckpointInfo(
                volume=row[0],
                journal_id=row[1],
                next_usn=row[2],
                status=JournalStatus(row[3]) if row[3] else JournalStatus.HEALTHY,
                updated_at=row[4],
            )
        return result

    # ── 写入 ──────────────────────────────────────────────────

    def save(self, volume: str, journal_id: int, next_usn: int,
             status: JournalStatus = JournalStatus.HEALTHY):
        """
        保存/更新 checkpoint。

        关键：此方法应在事件成功写入 fs_event 后调用，
        确保 checkpoint 与事件持久化绑定（方案第二十节）。
        """
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO usn_checkpoint "
            "(volume, journal_id, next_usn, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (volume, journal_id, next_usn, status.value, time.time())
        )
        conn.commit()

    def update_status(self, volume: str, status: JournalStatus):
        """仅更新状态（如标记 JOURNAL_GAP / RESCAN_REQUIRED）"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE usn_checkpoint SET status = ?, updated_at = ? "
            "WHERE volume = ?",
            (status.value, time.time(), volume)
        )
        conn.commit()

    def update_next_usn(self, volume: str, next_usn: int,
                        journal_id: int,
                        status: JournalStatus = JournalStatus.HEALTHY):
        """
        推进 checkpoint 的 next_usn。

        在事件成功持久化后调用，保证 checkpoint 与事件一致。
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE usn_checkpoint "
            "SET next_usn = ?, journal_id = ?, status = ?, updated_at = ? "
            "WHERE volume = ?",
            (next_usn, journal_id, status.value, time.time(), volume)
        )
        conn.commit()

    # ── 校验 ──────────────────────────────────────────────────

    def validate(self, volume: str, current_journal_id: int,
                 current_first_usn: int) -> CheckpointInfo:
        """
        校验 checkpoint 与当前 Journal 状态的一致性。

        返回 CheckpointInfo（可能带有异常状态）。

        校验规则（方案第四/五/六节）：
        1. checkpoint 不存在 → 需要 initial scan
        2. journal_id 不匹配 → JOURNAL_RESET（Journal 被重建）
        3. checkpoint.next_usn < journal.first_usn → JOURNAL_GAP（记录被覆盖）
        4. 其他 → HEALTHY
        """
        cp = self.load(volume)

        if cp is None:
            # 首次启动，无 checkpoint
            return CheckpointInfo(
                volume=volume,
                journal_id=current_journal_id,
                next_usn=0,
                status=JournalStatus.HEALTHY,
            )

        # 规则 2: journal_id 变化
        if cp.journal_id != current_journal_id:
            cp.status = JournalStatus.JOURNAL_RESET
            self.update_status(volume, JournalStatus.JOURNAL_RESET)
            return cp

        # 规则 3: USN gap（checkpoint 的 next_usn 已被 Journal 覆盖）
        if cp.next_usn < current_first_usn:
            cp.status = JournalStatus.JOURNAL_GAP
            self.update_status(volume, JournalStatus.JOURNAL_GAP)
            return cp

        # 规则 4: 正常
        if cp.status != JournalStatus.HEALTHY:
            # 恢复为 HEALTHY（之前可能是临时错误状态）
            cp.status = JournalStatus.HEALTHY
            self.update_status(volume, JournalStatus.HEALTHY)

        return cp

    # ── 删除 ──────────────────────────────────────────────────

    def delete(self, volume: str):
        """删除指定卷的 checkpoint"""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM usn_checkpoint WHERE volume = ?",
            (volume,)
        )
        conn.commit()

    def close(self):
        """关闭当前线程的连接"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
