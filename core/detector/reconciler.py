"""
一致性修复器（Reconciler）。

原 sync.py 的日常增量扫描职责由 USN 接管后，reconciler 仅负责：
1. 首次扫描（initial scan）：USN checkpoint 不存在时，执行全量扫描建立基线
2. USN gap recovery：checkpoint.next_usn < journal.first_usn 时触发
3. Journal reset recovery：journal_id 变化时触发
4. 周期性一致性检查（默认每 6 小时）：对比 USN 状态与文件系统实际状态

reconciler 不再需要：
- 目录 mtime 判断
- LRU 缓存
- 每 30 秒的增量扫描
"""

import os
import random
import threading
import time
from typing import List, Optional, Callable

from core.engine.event_normalizer import EventNormalizer, EventType


class Reconciler:
    """
    一致性修复器。

    职责：
    - 首次启动时执行全量扫描，建立 file_identity 基线
    - USN gap / reset 时执行增量或全量修复
    - 周期性一致性检查

    使用方式：
        reconciler = Reconciler(config, logger, event_store)
        reconciler.start()

        # 当检测到 gap/reset 时
        reconciler.trigger_reconcile("E:", "JOURNAL_GAP")
    """

    # 周期性一致性检查间隔（秒）
    DEFAULT_CHECK_INTERVAL = 6 * 3600  # 6 小时

    # 每轮采样文件数（统计意义足够，避免亿级全量 stat）
    _PERIODIC_SAMPLE_SIZE = 5000

    def __init__(self, config, logger, event_store=None):
        """
        Args:
            config: Config 实例
            logger: Logger 实例
            event_store: UsnEventStore 实例
        """
        self.config = config
        self.logger = logger
        self.event_store = event_store

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reconcile_cond = threading.Condition()
        self._pending_reconciles: List[tuple] = []  # [(volume, reason)]

        self.normalizer = EventNormalizer()
        self._check_interval = self.DEFAULT_CHECK_INTERVAL

    def start(self):
        """启动 reconciler 后台线程"""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._reconcile_loop,
            daemon=True,
            name="Reconciler"
        )
        self._thread.start()
        self.logger.info("一致性修复器已启动")

    def stop(self):
        """停止 reconciler"""
        self._stop_event.set()
        with self._reconcile_cond:
            self._reconcile_cond.notify_all()
        if self._thread:
            self._thread.join(timeout=10)
        self.logger.info("一致性修复器已停止")

    def trigger_reconcile(self, volume: str, reason: str):
        """
        触发 reconciliation。

        Args:
            volume: 卷标识（如 'E:'）
            reason: 触发原因（"JOURNAL_GAP" / "JOURNAL_RESET" / "INITIAL_SCAN"）
        """
        with self._reconcile_cond:
            self._pending_reconciles.append((volume, reason))
            self._reconcile_cond.notify()

    def _reconcile_loop(self):
        """reconciler 主循环"""
        last_check_time = 0.0

        while not self._stop_event.is_set():
            # 检查是否有待处理的 reconcile 请求
            pending = []
            with self._reconcile_cond:
                if self._pending_reconciles:
                    pending = list(self._pending_reconciles)
                    self._pending_reconciles.clear()

            # 处理待处理的 reconcile 请求
            for volume, reason in pending:
                try:
                    self._do_reconcile(volume, reason)
                except Exception as e:
                    self.logger.error(
                        f"Reconciliation 失败: 卷 {volume}, "
                        f"原因 {reason}: {e}"
                    )

            # 周期性一致性检查
            now = time.time()
            if now - last_check_time >= self._check_interval:
                try:
                    self._periodic_check()
                except Exception as e:
                    self.logger.error(f"周期性检查失败: {e}")
                last_check_time = now

            # 等待下一轮（最多等 60 秒，或有新请求时唤醒）
            with self._reconcile_cond:
                self._reconcile_cond.wait(timeout=60.0)

    def _do_reconcile(self, volume: str, reason: str):
        """
        执行 reconciliation。

        根据原因选择不同的修复策略：
        - INITIAL_SCAN: 全量扫描建立基线
        - JOURNAL_GAP: 增量扫描（从 journal.first_usn 开始）
        - JOURNAL_RESET: 全量重扫
        """
        self.logger.info(
            f"开始 reconciliation: 卷 {volume}, 原因 {reason}"
        )
        start_time = time.time()

        if reason == "INITIAL_SCAN":
            self._initial_scan(volume)
        elif reason == "JOURNAL_GAP":
            self._gap_recovery(volume)
        elif reason == "JOURNAL_RESET":
            self._full_rescan(volume)
        else:
            self.logger.warning(f"未知的 reconcile 原因: {reason}")

        elapsed = time.time() - start_time
        self.logger.info(
            f"Reconciliation 完成: 卷 {volume}, "
            f"耗时 {elapsed:.1f}s"
        )

    def _initial_scan(self, volume: str):
        """
        首次扫描：遍历监控目录，建立 file_identity 基线。

        扫描结果写入 file_identity 表，供后续 USN 事件参考。
        P0-2b 修复：攒批写入替代逐条 upsert。
        """
        watch_paths = self._get_watch_paths_for_volume(volume)
        if not watch_paths:
            self.logger.warning(f"卷 {volume} 没有对应的监控路径")
            return

        if not self.event_store:
            self.logger.warning("无 event_store，无法执行首次扫描")
            return

        total_files = 0
        batch_items = []
        _BATCH_SIZE = 1000

        for watch_path in watch_paths:
            if not os.path.exists(watch_path):
                continue

            for root, dirs, files in os.walk(watch_path):
                if self._stop_event.is_set():
                    break

                for filename in files:
                    full_path = os.path.join(root, filename)
                    try:
                        stat = os.stat(full_path)
                        rel_path = os.path.relpath(full_path, watch_path)
                        # Windows NTFS 上 os.stat().st_ino 就是 File Reference Number (FRN)
                        frn = stat.st_ino
                        batch_items.append((
                            volume, frn, watch_path, rel_path,
                            0, 0, stat.st_size, stat.st_mtime_ns, "ACTIVE"
                        ))
                        total_files += 1

                        if len(batch_items) >= _BATCH_SIZE:
                            self.event_store.batch_upsert_file_identities(batch_items)
                            batch_items.clear()
                    except OSError:
                        continue

        # 写入剩余缓冲区
        if batch_items:
            self.event_store.batch_upsert_file_identities(batch_items)

        self.logger.info(f"首次扫描完成: 卷 {volume}, {total_files} 个文件")

    def _gap_recovery(self, volume: str):
        """
        USN gap recovery：从 journal.first_usn 开始重新扫描。

        由于 gap 期间的变更已不可追溯，执行一次全量扫描
        重新建立基线，然后重置 checkpoint。
        """
        self.logger.info(f"USN gap recovery: 卷 {volume}")
        # gap recovery 实际上需要全量重扫，因为丢失的记录不可恢复
        self._full_rescan(volume)

    def _full_rescan(self, volume: str):
        """
        全量重扫：重新扫描所有监控路径。

        用于 journal_id 变化或 gap recovery。
        对比 file_identity 表，标记已删除文件，添加新发现文件。
        P1-2 修复：流式分页读取 file_identity，避免亿级 OOM。
        P0-3 修复：批量更新删除状态 + 批量 upsert 新增文件。
        """
        watch_paths = self._get_watch_paths_for_volume(volume)
        if not watch_paths:
            return

        if not self.event_store:
            self.logger.warning("无 event_store，无法执行全量重扫")
            return

        # 1. 流式分页读取 file_identity，构建 known_paths 索引
        known_identities = {}
        known_paths = set()
        try:
            for page in self.event_store.iter_file_identities(volume):
                for frn, identity in page.items():
                    known_identities[frn] = identity
                    known_paths.add(os.path.normcase(
                        os.path.join(identity["watch_root"], identity["relative_path"])
                    ))
        except Exception as e:
            self.logger.error(f"查询 file_identity 失败: {e}")

        # 2. 扫描文件系统，收集当前文件
        current_files = {}
        for watch_path in watch_paths:
            if not os.path.exists(watch_path):
                continue

            for root, dirs, files in os.walk(watch_path):
                if self._stop_event.is_set():
                    break

                for filename in files:
                    full_path = os.path.join(root, filename)
                    try:
                        stat = os.stat(full_path)
                        frn = stat.st_ino  # Windows NTFS 上 st_ino 就是 FRN
                        current_files[os.path.normcase(full_path)] = (
                            frn, stat.st_size, stat.st_mtime_ns
                        )
                    except OSError:
                        continue

        # 3. 批量标记已删除文件（在 file_identity 中但不在文件系统中）
        deleted_frns = []
        for frn, identity in known_identities.items():
            abs_path = os.path.normcase(
                os.path.join(identity["watch_root"], identity["relative_path"])
            )
            if abs_path not in current_files and identity.get("state") == "ACTIVE":
                deleted_frns.append(frn)

        if deleted_frns:
            self.event_store.batch_update_file_identity_states(
                volume, deleted_frns, "DELETED"
            )
        deleted_count = len(deleted_frns)

        # 4. 批量添加新发现文件（在文件系统中但不在 file_identity 中）
        new_items = []
        for full_path, (frn, file_size, mtime_ns) in current_files.items():
            if full_path not in known_paths:
                watch_root = None
                for wp in watch_paths:
                    wp_norm = os.path.normcase(os.path.abspath(wp))
                    if full_path.startswith(wp_norm + os.sep) or full_path == wp_norm:
                        watch_root = wp_norm
                        break
                if watch_root:
                    rel = os.path.relpath(full_path, watch_root)
                    new_items.append((
                        volume, frn, watch_root, rel,
                        0, 0, file_size, mtime_ns, "ACTIVE"
                    ))

        if new_items:
            self.event_store.batch_upsert_file_identities(new_items)
        new_count = len(new_items)

        self.logger.info(
            f"全量重扫完成: 卷 {volume}, "
            f"{len(current_files)} 个文件, "
            f"标记删除 {deleted_count}, 新增 {new_count}"
        )

    def _periodic_check(self):
        """
        周期性一致性检查（R1 修复：流式分页 + 采样）。

        从 file_identity 表中随机采样文件，验证磁盘上是否存在且属性一致。
        不再全量加载 file_identity，避免亿级 OOM。

        检测项：
        - 文件不存在但 file_identity 中为 ACTIVE → 不一致
        - 文件存在但 size/mtime 变化 → 不一致

        采样量：每卷 _PERIODIC_SAMPLE_SIZE 个文件。
        对于 1 亿文件，每 6 小时检查 5000 个，一周覆盖 >1%，
        足以发现系统性偏差（如 USN gap、数据库损坏）。
        """
        self.logger.debug("执行周期性一致性检查")

        if not self.event_store:
            return

        inconsistencies = 0
        total_active = 0
        total_sampled = 0

        for watch_path in self.config.watch_paths:
            if not os.path.exists(watch_path):
                self.logger.warning(
                    f"监控路径不存在: {watch_path}，可能需要重新配置"
                )
                inconsistencies += 1
                continue

            drive = os.path.splitdrive(os.path.abspath(watch_path))[0]
            volume = drive.upper().rstrip('\'')

            # R1 修复：流式分页 + 随机采样，内存占用 O(page_size) 而非 O(全量)
            sampled_inconsistencies = 0
            sampled_count = 0
            page_count = 0

            try:
                for page in self.event_store.iter_file_identities(volume):
                    page_count += 1
                    records = list(page.values())
                    total_active += len(records)

                    #  reservoir-style 随机采样：每页随机取若干条
                    if len(records) <= self._PERIODIC_SAMPLE_SIZE:
                        sample = records
                    else:
                        sample = random.sample(
                            records, self._PERIODIC_SAMPLE_SIZE
                        )

                    for identity in sample:
                        if identity.get("state") != "ACTIVE":
                            continue
                        sampled_count += 1
                        total_sampled += 1

                        full_path = os.path.normcase(
                            os.path.join(
                                identity["watch_root"],
                                identity["relative_path"],
                            )
                        )

                        if not os.path.exists(full_path):
                            sampled_inconsistencies += 1
                            continue

                        try:
                            st = os.stat(full_path)
                        except OSError:
                            continue

                        db_size = identity.get("file_size")
                        db_mtime = identity.get("mtime_ns")
                        if (db_size is not None
                                and db_size != st.st_size):
                            self.logger.warning(
                                f"文件被替换检测: {full_path} "
                                f"(size: {db_size} → {st.st_size})"
                            )
                            sampled_inconsistencies += 1
                        elif (db_mtime is not None
                              and db_mtime != st.st_mtime_ns):
                            self.logger.warning(
                                f"文件内容变化: {full_path} "
                                f"(mtime 不一致)"
                            )
                            sampled_inconsistencies += 1

                    # 已达到总采样目标后提前退出
                    if sampled_count >= self._PERIODIC_SAMPLE_SIZE:
                        break

            except Exception as e:
                self.logger.error(f"周期性检查异常: {e}")
                continue

            inconsistencies += sampled_inconsistencies

            if sampled_count > 0:
                self.logger.info(
                    f"周期性检查（采样）: 卷 {volume}, "
                    f"采样 {sampled_count}/{len(records) if page_count == 1 else total_active} 个文件, "
                    f"发现 {sampled_inconsistencies} 处不一致"
                )

        if inconsistencies > 0:
            self.logger.warning(
                f"周期性检查共发现 {inconsistencies} 处不一致"
            )

    def _get_watch_paths_for_volume(self, volume: str) -> List[str]:
        """获取指定卷对应的监控路径"""
        result = []
        volume_prefix = volume.upper().rstrip('\\')
        for wp in self.config.watch_paths:
            abs_wp = os.path.abspath(wp)
            drive = os.path.splitdrive(abs_wp)[0]
            if drive.upper() == volume_prefix:
                result.append(abs_wp)
        return result
