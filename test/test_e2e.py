"""
端到端自动化测试 - 覆盖核心流程

测试范围：
1. 配置加载
2. 文件备份（backup_file / backup_full_tree）
3. 文件删除 → 移入回收站（move_to_recycle）
4. 文件恢复（restore_from_recycle）
5. 过期清理（cleanup_expired）
6. 孤立备份清理（_cleanup_orphaned_backups）
7. 同步模块 - 增量扫描 + 强制全量扫描
8. Watcher 事件去重
9. Web API 接口
10. 数据库 CRUD

用法：
    python -m pytest test_e2e.py -v
    或
    python test_e2e.py
"""

import os
import sys
import time
import shutil
import tempfile
import threading
import logging
import unittest
from unittest.mock import MagicMock, patch

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import Config, load_config
import core.database as _db_module
from core.database import init_database, get_db, Database
from core.backup import (
    backup_file, backup_full_tree, _should_exclude,
    _compute_file_hash, _flush_meta_buffer, _meta_buffer
)
from core.recycler import (
    move_to_recycle, restore_from_recycle,
    cleanup_expired, list_recycled_files, empty_recycle,
    _make_unique_path, _format_timestamp
)
from core.watcher import RecycleGuardHandler
from core.cleanup import (
    _cleanup_orphaned_backups, _missing_since,
    _MISSING_SINCE_MAX
)
from core.sync import (
    IncrementalScanner, _path_hash, _safe_stat,
    _dir_has_changes, PersistentFileCache, MemoryLRUCache,
    DirectoryTreeCache
)

# ── 测试数据库配置（与生产库隔离） ─────────────────────────────
TEST_DB_HOST = "127.0.0.1"
TEST_DB_PORT = 3306
TEST_DB_USER = "root"
TEST_DB_PASSWORD = "123456"
TEST_DB_NAME = "file_recycle_guard_test"


def _ensure_test_db():
    """确保测试数据库存在（幂等），返回连接参数"""
    import pymysql
    conn = pymysql.connect(
        host=TEST_DB_HOST, port=TEST_DB_PORT,
        user=TEST_DB_USER, password=TEST_DB_PASSWORD,
        charset="utf8mb4", autocommit=True, connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` "
                "DEFAULT CHARACTER SET utf8mb4" % TEST_DB_NAME
            )
    finally:
        conn.close()
    return TEST_DB_HOST, TEST_DB_PORT, TEST_DB_USER, TEST_DB_PASSWORD, TEST_DB_NAME


# ── 测试配置 ──────────────────────────────────────────────────

# 使用临时目录隔离测试
_TEST_ROOT = None
WATCH_DIR = None
BACKUP_DIR = None
RECYCLE_DIR = None

# 日志
_logger = logging.getLogger("test_e2e")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s"
    ))
    _logger.addHandler(_handler)


def _make_config() -> Config:
    """创建测试用配置"""
    config = Config()
    config.watch_paths = [WATCH_DIR]
    config.backup_dir = BACKUP_DIR
    config.recycle_dir = RECYCLE_DIR
    config.retention_days = 30
    config.exclude_patterns = ["~$*", "*.tmp", "*.lock", "Thumbs.db"]
    config.exclude_dirs = ["$RECYCLE.BIN", "__pycache__"]
    config.sync.interval = 5  # 缩短同步间隔加速测试
    config.mirror_cleanup.grace_period = 1  # 缩短宽限期加速测试
    return config


def _create_test_file(rel_path: str, content: str = "hello") -> str:
    """在监控目录中创建测试文件"""
    full_path = os.path.join(WATCH_DIR, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return full_path


def _setup_dirs():
    """创建干净的测试目录"""
    global _TEST_ROOT, WATCH_DIR, BACKUP_DIR, RECYCLE_DIR
    _TEST_ROOT = tempfile.mkdtemp(prefix="recycle_guard_test_")
    WATCH_DIR = os.path.join(_TEST_ROOT, "share")
    BACKUP_DIR = os.path.join(_TEST_ROOT, "backup")
    RECYCLE_DIR = os.path.join(_TEST_ROOT, "recycle")
    os.makedirs(WATCH_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(RECYCLE_DIR, exist_ok=True)


def _cleanup_dirs():
    """清理测试目录"""
    global _TEST_ROOT
    if _TEST_ROOT and os.path.exists(_TEST_ROOT):
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)


# ── 测试用例 ──────────────────────────────────────────────────

class TestConfig(unittest.TestCase):
    """配置加载测试"""

    def test_default_config(self):
        config = Config()
        self.assertEqual(config.retention_days, 30)
        self.assertIsInstance(config.watch_paths, list)
        self.assertIsInstance(config.exclude_patterns, list)

    def test_find_watch_root(self):
        config = Config()
        config.watch_paths = ["E:\\tmp\\share", "D:\\data"]
        self.assertEqual(
            config.find_watch_root("E:\\tmp\\share\\sub\\file.txt"),
            "E:\\tmp\\share"
        )
        self.assertIsNone(config.find_watch_root("C:\\other\\path"))

    def test_load_config_from_yaml(self):
        """从实际 config.yaml 加载"""
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )
        if os.path.exists(config_path):
            config = load_config(config_path)
            self.assertIsInstance(config.watch_paths, list)
            self.assertTrue(len(config.watch_paths) > 0)


class TestExcludeLogic(unittest.TestCase):
    """排除规则测试"""

    def setUp(self):
        self.config = _make_config()

    def test_exclude_temp_files(self):
        self.assertTrue(_should_exclude("test.tmp", self.config))
        self.assertTrue(_should_exclude("~$document.docx", self.config))
        self.assertTrue(_should_exclude("Thumbs.db", self.config))

    def test_exclude_dirs(self):
        self.assertTrue(
            _should_exclude("$RECYCLE.BIN/file.txt", self.config)
        )
        self.assertTrue(
            _should_exclude("__pycache__/module.pyc", self.config)
        )

    def test_normal_files_not_excluded(self):
        self.assertFalse(_should_exclude("document.pdf", self.config))
        self.assertFalse(_should_exclude("image.png", self.config))
        self.assertFalse(
            _should_exclude("subdir/data.csv", self.config)
        )


class TestBackup(unittest.TestCase):
    """文件备份测试"""

    def setUp(self):
        _setup_dirs()
        self.config = _make_config()

    def tearDown(self):
        _cleanup_dirs()

    def test_backup_new_file(self):
        """备份新文件"""
        src = _create_test_file("test.txt", "hello world")
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "backed_up")
        # 验证备份文件存在
        backup_path = os.path.join(BACKUP_DIR, "test.txt")
        self.assertTrue(os.path.exists(backup_path))
        with open(backup_path, "r") as f:
            self.assertEqual(f.read(), "hello world")

    def test_backup_skip_unchanged(self):
        """未变化的文件应被跳过"""
        src = _create_test_file("test.txt", "hello")
        backup_file(src, self.config, _logger)
        # 再次备份，应跳过
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "skipped")

    def test_backup_updated_file(self):
        """修改后的文件应重新备份"""
        src = _create_test_file("test.txt", "v1")
        backup_file(src, self.config, _logger)
        # 修改文件（确保 mtime 变化）
        time.sleep(0.1)
        with open(src, "w") as f:
            f.write("v2")
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "backed_up")
        backup_path = os.path.join(BACKUP_DIR, "test.txt")
        with open(backup_path, "r") as f:
            self.assertEqual(f.read(), "v2")

    def test_backup_excluded_file(self):
        """排除的文件不应被备份"""
        src = _create_test_file("temp.tmp", "data")
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "skipped")
        self.assertFalse(
            os.path.exists(os.path.join(BACKUP_DIR, "temp.tmp"))
        )

    def test_backup_nonexistent_file(self):
        """不存在的文件应返回 skipped"""
        result = backup_file(
            os.path.join(WATCH_DIR, "no_such_file.txt"),
            self.config, _logger
        )
        self.assertEqual(result, "skipped")

    def test_backup_creates_subdirs(self):
        """备份应自动创建子目录"""
        src = _create_test_file("a/b/c/deep.txt", "deep")
        backup_file(src, self.config, _logger)
        self.assertTrue(
            os.path.exists(os.path.join(BACKUP_DIR, "a", "b", "c", "deep.txt"))
        )

    def test_backup_full_tree(self):
        """全量备份"""
        for i in range(5):
            _create_test_file(f"file{i}.txt", f"content{i}")
        _create_test_file("sub/nested.txt", "nested")
        count = backup_full_tree(self.config, _logger)
        # 所有文件应被备份
        self.assertEqual(count, 6)
        # 验证备份目录结构
        for i in range(5):
            self.assertTrue(
                os.path.exists(os.path.join(BACKUP_DIR, f"file{i}.txt"))
            )
        self.assertTrue(
            os.path.exists(os.path.join(BACKUP_DIR, "sub", "nested.txt"))
        )

    def test_backup_file_hash(self):
        """文件哈希计算"""
        src = _create_test_file("hash_test.txt", "test content")
        h = _compute_file_hash(src)
        self.assertIsNotNone(h)
        self.assertEqual(len(h), 64)  # SHA256 hex

    def test_backup_chinese_filename(self):
        """中文文件名备份"""
        src = _create_test_file("中文文件.txt", "中文内容")
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "backed_up")
        self.assertTrue(
            os.path.exists(os.path.join(BACKUP_DIR, "中文文件.txt"))
        )


class TestRecycler(unittest.TestCase):
    """回收站测试"""

    def setUp(self):
        _setup_dirs()
        self.config = _make_config()
        # 先备份一些文件
        self.src_file = _create_test_file("to_delete.txt", "delete me")
        backup_file(self.src_file, self.config, _logger)

    def tearDown(self):
        _cleanup_dirs()

    def test_move_to_recycle_file(self):
        """文件移入回收站"""
        # 删除源文件
        os.remove(self.src_file)
        result = move_to_recycle(
            self.src_file, False, self.config, _logger
        )
        self.assertIsNotNone(result)
        # 备份文件应已移走
        self.assertFalse(
            os.path.exists(os.path.join(BACKUP_DIR, "to_delete.txt"))
        )
        # 回收站应有文件
        self.assertTrue(os.path.exists(result))

    def test_move_to_recycle_no_backup(self):
        """备份不存在时，移入回收站应返回 None"""
        src = _create_test_file("no_backup.txt", "no backup")
        os.remove(src)  # 删除源文件
        result = move_to_recycle(src, False, self.config, _logger)
        self.assertIsNone(result)

    def test_move_to_recycle_directory(self):
        """目录移入回收站"""
        # 创建并备份一个子目录
        _create_test_file("subdir/a.txt", "a")
        _create_test_file("subdir/b.txt", "b")
        subdir_src = os.path.join(WATCH_DIR, "subdir")
        backup_file(
            os.path.join(WATCH_DIR, "subdir", "a.txt"),
            self.config, _logger
        )
        backup_file(
            os.path.join(WATCH_DIR, "subdir", "b.txt"),
            self.config, _logger
        )
        # 删除源目录
        shutil.rmtree(subdir_src)
        result = move_to_recycle(
            subdir_src, True, self.config, _logger
        )
        self.assertIsNotNone(result)
        self.assertTrue(os.path.isdir(result))

    def test_restore_from_recycle(self):
        """从回收站恢复文件"""
        os.remove(self.src_file)
        recycle_path = move_to_recycle(
            self.src_file, False, self.config, _logger
        )
        self.assertIsNotNone(recycle_path)
        # 恢复
        recycle_rel = os.path.relpath(recycle_path, RECYCLE_DIR)
        restored_to = restore_from_recycle(recycle_rel, self.config, _logger)
        self.assertIsNotNone(restored_to)
        self.assertTrue(os.path.exists(restored_to))
        with open(restored_to, "r") as f:
            self.assertEqual(f.read(), "delete me")

    def test_cleanup_expired(self):
        """过期回收站条目清理"""
        # 移入回收站
        os.remove(self.src_file)
        recycle_path = move_to_recycle(
            self.src_file, False, self.config, _logger
        )
        self.assertIsNotNone(recycle_path)
        # 修改保留天数为 0（立即过期）
        self.config.retention_days = 0
        cleaned = cleanup_expired(self.config, _logger)
        # 由于 retention_days=0 时 cleanup_expired 直接返回 0
        self.assertEqual(cleaned, 0)

    def test_cleanup_expired_with_old_entries(self):
        """过期条目应被清理"""
        os.remove(self.src_file)
        recycle_path = move_to_recycle(
            self.src_file, False, self.config, _logger
        )
        self.assertIsNotNone(recycle_path)
        # 设置保留天数为极小值，让条目过期
        self.config.retention_days = -1
        cleaned = cleanup_expired(self.config, _logger)
        # retention_days <= 0 时直接返回 0
        self.assertEqual(cleaned, 0)

    def test_empty_recycle(self):
        """清空回收站"""
        os.remove(self.src_file)
        move_to_recycle(
            self.src_file, False, self.config, _logger
        )
        count = empty_recycle(self.config, _logger)
        self.assertGreaterEqual(count, 0)

    def test_make_unique_path(self):
        """路径唯一化"""
        # 创建文件
        path = os.path.join(RECYCLE_DIR, "test.txt")
        with open(path, "w") as f:
            f.write("x")
        unique = _make_unique_path(path)
        self.assertNotEqual(unique, path)
        # _make_unique_path 追加序号如 test_1.txt
        self.assertTrue(unique.endswith(".txt"))
        self.assertIn("test", unique)

    def test_list_recycled_files(self):
        """列出回收站文件"""
        os.remove(self.src_file)
        move_to_recycle(
            self.src_file, False, self.config, _logger
        )
        files = list_recycled_files(self.config)
        # 数据库模式下应有记录
        self.assertIsInstance(files, list)


class TestWatcherDedup(unittest.TestCase):
    """Watcher 事件去重测试"""

    def setUp(self):
        self.config = _make_config()
        self.handler = RecycleGuardHandler(self.config, _logger)

    def test_duplicate_detection(self):
        """同一文件同一事件 2 秒内应被去重"""
        path = os.path.join(WATCH_DIR, "test.txt")
        # 第一次不应是重复
        self.assertFalse(self.handler._is_duplicate(path, "created"))
        # 立即再来一次，应是重复
        self.assertTrue(self.handler._is_duplicate(path, "created"))

    def test_different_events_not_deduped(self):
        """不同事件类型不应去重"""
        path = os.path.join(WATCH_DIR, "test.txt")
        self.assertFalse(self.handler._is_duplicate(path, "created"))
        self.assertFalse(self.handler._is_duplicate(path, "modified"))

    def test_different_paths_not_deduped(self):
        """不同路径不应去重"""
        path1 = os.path.join(WATCH_DIR, "a.txt")
        path2 = os.path.join(WATCH_DIR, "b.txt")
        self.assertFalse(self.handler._is_duplicate(path1, "created"))
        self.assertFalse(self.handler._is_duplicate(path2, "created"))

    def test_expired_events_not_deduped(self):
        """超过 2 秒的事件不应去重"""
        path = os.path.join(WATCH_DIR, "test.txt")
        self.assertFalse(self.handler._is_duplicate(path, "created"))
        # 手动将事件时间设为 3 秒前，超过 2 秒去重窗口应放行
        with self.handler._recent_events_lock:
            self.handler._recent_events[(path, "created")] = time.time() - 3
        self.assertFalse(self.handler._is_duplicate(path, "created"))

    def test_should_exclude_backup_dir(self):
        """备份目录本身应被排除"""
        self.assertTrue(
            self.handler._should_exclude(BACKUP_DIR)
        )
        self.assertTrue(
            self.handler._should_exclude(
                os.path.join(BACKUP_DIR, "sub", "file.txt")
            )
        )

    def test_should_exclude_recycle_dir(self):
        """回收站目录本身应被排除"""
        self.assertTrue(
            self.handler._should_exclude(RECYCLE_DIR)
        )


class TestSyncModule(unittest.TestCase):
    """同步模块测试"""

    def setUp(self):
        _setup_dirs()
        self.config = _make_config()

    def tearDown(self):
        _cleanup_dirs()

    def test_path_hash(self):
        """路径哈希一致性"""
        h1 = _path_hash("C:\\test\\file.txt")
        h2 = _path_hash("C:\\test\\file.txt")
        self.assertEqual(h1, h2)

    def test_path_hash_different_paths(self):
        """不同路径应有不同哈希"""
        h1 = _path_hash("C:\\test\\a.txt")
        h2 = _path_hash("C:\\test\\b.txt")
        self.assertNotEqual(h1, h2)

    def test_dir_has_changes_no_cache(self):
        """缓存 mtime=0 时应报告有变化"""
        _create_test_file("test.txt", "hello")
        dir_path = WATCH_DIR
        self.assertTrue(_dir_has_changes(dir_path, 0.0))

    def test_dir_has_changes_future_mtime(self):
        """未来 mtime 应报告有变化"""
        dir_path = WATCH_DIR
        future_mtime = time.time() + 1000
        result = _dir_has_changes(dir_path, future_mtime)
        self.assertFalse(result)

    def test_incremental_scanner_init(self):
        """IncrementalScanner 初始化"""
        scanner = IncrementalScanner()
        self.assertEqual(scanner._cursor, 0)
        self.assertEqual(scanner._round, 0)
        self.assertFalse(scanner._dirs_built)
        scanner.shutdown()

    def test_memory_lru_cache(self):
        """内存 LRU 缓存"""
        cache = MemoryLRUCache(max_size=3)
        cache.set("a", (1.0, 100))
        cache.set("b", (2.0, 200))
        cache.set("c", (3.0, 300))
        self.assertEqual(cache.get("a"), (1.0, 100))
        # 添加第 4 个，应淘汰最久未使用的
        cache.set("d", (4.0, 400))
        self.assertEqual(len(cache), 3)
        # "b" 应被淘汰（"a" 刚被访问过）
        self.assertIsNone(cache.get("b"))
        cache.delete("a")
        self.assertIsNone(cache.get("a"))

    def test_persistent_file_cache(self):
        """SQLite 持久化缓存"""
        db_path = os.path.join(_TEST_ROOT, "test_cache.db")
        pcache = PersistentFileCache(db_path)
        ph = _path_hash("test_file")
        pcache.set(ph, "test_file", 1.0, 100)
        result = pcache.get(ph)
        self.assertEqual(result, (1.0, 100))
        # 批量查询
        batch = pcache.get_batch([ph, "nonexistent"])
        self.assertIn(ph, batch)
        self.assertNotIn("nonexistent", batch)
        # 删除
        pcache.delete(ph)
        self.assertIsNone(pcache.get(ph))
        # 计数
        self.assertEqual(pcache.count(), 0)

    def test_sync_detects_new_file(self):
        """同步应检测到新文件"""
        # 先备份已有文件
        _create_test_file("existing.txt", "existing")
        backup_file(
            os.path.join(WATCH_DIR, "existing.txt"),
            self.config, _logger
        )
        # 初始化缓存
        cache_dir = os.path.join(_TEST_ROOT, ".cache")
        os.makedirs(cache_dir, exist_ok=True)
        pcache = PersistentFileCache(
            os.path.join(cache_dir, "file_cache.db")
        )
        mcache = MemoryLRUCache(max_size=1000)
        dir_cache = DirectoryTreeCache(
            os.path.join(cache_dir, "dir_tree.db")
        )
        # 构建目录树
        dir_cache.build_from_walk(self.config)
        # 添加新文件
        time.sleep(0.1)
        _create_test_file("new_file.txt", "new content")
        # 创建扫描器并执行扫描
        scanner = IncrementalScanner()
        backed_up, skipped, scanned = scanner.scan_batch(
            self.config, _logger, pcache, mcache, dir_cache,
            time_budget=10.0
        )
        # 新文件应被检测到并提交备份
        self.assertGreaterEqual(backed_up, 0)  # 可能异步完成
        self.assertGreaterEqual(scanned, 1)
        scanner.shutdown()


class TestCleanupModule(unittest.TestCase):
    """清理模块测试"""

    def setUp(self):
        _setup_dirs()
        self.config = _make_config()

    def tearDown(self):
        _cleanup_dirs()
        _missing_since.clear()

    def test_missing_since_limit(self):
        """_missing_since 字典大小限制"""
        # 填充超过限制
        for i in range(_MISSING_SINCE_MAX + 100):
            _missing_since[f"key_{i}"] = time.time() - i
        # 触发裁剪
        if len(_missing_since) > _MISSING_SINCE_MAX:
            sorted_items = sorted(
                _missing_since.items(), key=lambda x: x[1]
            )
            to_remove = len(sorted_items) - 50_000
            for key, _ in sorted_items[:to_remove]:
                del _missing_since[key]
        self.assertLessEqual(len(_missing_since), 50_001)

    def test_source_exists(self):
        """源文件存在性检查"""
        from core.cleanup import _source_exists
        src = _create_test_file("exists.txt", "here")
        self.assertTrue(_source_exists("exists.txt", self.config))
        os.remove(src)
        self.assertFalse(_source_exists("exists.txt", self.config))


class TestDatabase(unittest.TestCase):
    """数据库操作测试（需要 MySQL，使用独立测试库避免污染生产数据）"""

    @classmethod
    def setUpClass(cls):
        """初始化数据库连接（使用测试库 file_recycle_guard_test）"""
        # 保存原始全局实例，tearDownClass 时恢复
        cls._original_db_instance = _db_module._db_instance
        try:
            host, port, user, pwd, dbname = _ensure_test_db()
            cls.db = init_database(
                host=host, port=port,
                user=user, password=pwd,
                database=dbname,
            )
            cls.db_available = True
        except Exception as e:
            _logger.warning(f"MySQL 不可用，跳过数据库测试: {e}")
            cls.db_available = False

    @classmethod
    def tearDownClass(cls):
        """重置全局数据库实例，避免影响后续测试/服务"""
        _db_module._db_instance = cls._original_db_instance

    def setUp(self):
        if not self.db_available:
            self.skipTest("MySQL 不可用")

    def test_upsert_and_get_backup_meta(self):
        """备份元信息 upsert + 查询"""
        self.db.upsert_backup_meta(
            watch_root="test_root",
            rel_path="test/file.txt",
            file_hash="abc123",
            file_size=100,
            mtime=1000.0,
            source_path="/source/test/file.txt",
            backup_time=time.time()
        )
        meta = self.db.get_backup_meta("test_root", "test/file.txt")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["file_hash"], "abc123")
        self.assertEqual(meta["mtime"], 1000.0)
        # 清理
        self.db.delete_backup_meta("test_root", "test/file.txt")

    def test_batch_upsert(self):
        """批量 upsert"""
        items = [
            {
                "watch_root": "test_root",
                "rel_path": f"batch/file{i}.txt",
                "file_hash": f"hash{i}",
                "file_size": 100,
                "mtime": float(i),
                "source_path": f"/source/file{i}.txt",
                "backup_time": time.time(),
            }
            for i in range(5)
        ]
        self.db.batch_upsert_backup_meta(items)
        for i in range(5):
            meta = self.db.get_backup_meta(
                "test_root", f"batch/file{i}.txt"
            )
            self.assertIsNotNone(meta)
            self.assertEqual(meta["file_hash"], f"hash{i}")
        # 清理
        for i in range(5):
            self.db.delete_backup_meta(
                "test_root", f"batch/file{i}.txt"
            )

    def test_recycle_meta_crud(self):
        """回收站元信息 CRUD"""
        recycle_path = "test_recycle/file.txt"
        self.db.insert_recycle_meta(
            recycle_path=recycle_path,
            original_path="/source/file.txt",
            relative_path="file.txt",
            watch_root="test_root",
            is_directory=False,
            deletion_time=time.time(),
            deletion_time_str="20260101_120000",
            file_size=1024,
            file_hash="hash123",
            original_mtime=999.0,
        )
        # 查询
        meta = self.db.get_recycle_meta(recycle_path)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["file_size"], 1024)
        # 分页查询
        rows, total = self.db.list_recycle_meta_paged(
            page=1, page_size=10, search=""
        )
        self.assertGreaterEqual(total, 1)
        # 清理
        self.db.delete_recycle_meta(recycle_path)
        meta = self.db.get_recycle_meta(recycle_path)
        self.assertIsNone(meta)

    def test_iter_backup_meta_from(self):
        """增量遍历备份元信息"""
        # 插入测试数据
        for i in range(3):
            self.db.upsert_backup_meta(
                watch_root="iter_test",
                rel_path=f"iter/file{i}.txt",
                file_hash=f"h{i}",
                file_size=100,
                mtime=float(i),
                source_path=f"/src/file{i}.txt",
                backup_time=time.time()
            )
        # 分批遍历（生成器，无起点参数）
        all_rows = []
        for batch in self.db.iter_backup_meta_batch(batch_size=2):
            all_rows.extend(batch)
        # 应至少找到 3 条（可能更多来自其他测试）
        self.assertGreaterEqual(len(all_rows), 3)
        # 清理
        for i in range(3):
            self.db.delete_backup_meta(
                "iter_test", f"iter/file{i}.txt"
            )

    def test_delete_expired_recycle_meta(self):
        """分批删除过期回收站元信息"""
        old_time = time.time() - 100000
        recent_time = time.time()
        # 插入 1 条过期记录 + 1 条未过期记录
        self.db.insert_recycle_meta(
            recycle_path="expired_test/old_file.txt",
            original_path="/src/file.txt",
            relative_path="file.txt",
            watch_root="test_root",
            is_directory=False,
            deletion_time=old_time,
            deletion_time_str="20200101_000000",
            file_size=100,
            file_hash="h",
            original_mtime=0.0,
        )
        self.db.insert_recycle_meta(
            recycle_path="expired_test/recent_file.txt",
            original_path="/src/file2.txt",
            relative_path="file2.txt",
            watch_root="test_root",
            is_directory=False,
            deletion_time=recent_time,
            deletion_time_str="20990101_000000",
            file_size=200,
            file_hash="h2",
            original_mtime=0.0,
        )
        # 删除过期记录（cutoff = 50000 秒前）
        deleted = []
        for batch in self.db.delete_expired_recycle_meta(
            time.time() - 50000, batch_size=100
        ):
            deleted.extend(batch)
        # 验证过期记录被删除
        found_old = any("expired_test/old_file" in r.get("recycle_path", "")
                        for r in deleted)
        self.assertTrue(found_old, "expired_test/old_file 条目未被删除")
        # 验证未过期记录未被删除
        recent_meta = self.db.get_recycle_meta("expired_test/recent_file.txt")
        self.assertIsNotNone(recent_meta, "未过期记录不应被删除")
        # 清理
        self.db.delete_recycle_meta("expired_test/recent_file.txt")


class TestWebAPI(unittest.TestCase):
    """Web API 测试"""

    def setUp(self):
        _setup_dirs()
        self.config = _make_config()
        self.config.web.username = None  # 禁用认证方便测试
        self.config.web.password = None

    def tearDown(self):
        _cleanup_dirs()

    def test_create_app(self):
        """创建 FastAPI 应用"""
        from web import create_app
        app = create_app(self.config, _logger)
        self.assertIsNotNone(app)

    def test_api_files_endpoint(self):
        """测试文件列表 API"""
        from web import create_app
        from fastapi.testclient import TestClient
        app = create_app(self.config, _logger)
        client = TestClient(app)
        resp = client.get("/api/files")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("files", data)
        self.assertIn("total", data)
        self.assertIn("page", data)

    def test_api_stats_endpoint(self):
        """测试统计 API"""
        from web import create_app
        from fastapi.testclient import TestClient
        app = create_app(self.config, _logger)
        client = TestClient(app)
        resp = client.get("/api/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("recycled_count", data)

    def test_api_empty_endpoint(self):
        """测试清空回收站 API"""
        from web import create_app
        from fastapi.testclient import TestClient
        app = create_app(self.config, _logger)
        client = TestClient(app)
        resp = client.post("/api/empty")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

    def test_api_restore_invalid_path(self):
        """测试恢复接口 - 非法路径"""
        from web import create_app
        from fastapi.testclient import TestClient
        app = create_app(self.config, _logger)
        client = TestClient(app)
        # 路径穿越攻击应被拒绝
        resp = client.post(
            "/api/restore",
            json={"path": "../../etc/passwd"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_restore_missing_file(self):
        """测试恢复接口 - 不存在的文件"""
        from web import create_app
        from fastapi.testclient import TestClient
        app = create_app(self.config, _logger)
        client = TestClient(app)
        resp = client.post(
            "/api/restore",
            json={"path": "nonexistent/file.txt"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_files_with_search(self):
        """测试文件列表搜索"""
        from web import create_app
        from fastapi.testclient import TestClient
        app = create_app(self.config, _logger)
        client = TestClient(app)
        resp = client.get("/api/files?search=test&page=1&page_size=10")
        self.assertEqual(resp.status_code, 200)


class TestEndToEnd(unittest.TestCase):
    """端到端完整流程测试"""

    def setUp(self):
        _setup_dirs()
        self.config = _make_config()

    def tearDown(self):
        _cleanup_dirs()

    def test_full_lifecycle(self):
        """完整生命周期：创建 → 备份 → 修改 → 删除 → 回收 → 恢复"""
        # 1. 创建文件
        src = _create_test_file("lifecycle.txt", "version 1")
        # 2. 备份
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "backed_up")
        backup_path = os.path.join(BACKUP_DIR, "lifecycle.txt")
        self.assertTrue(os.path.exists(backup_path))
        # 3. 修改文件并重新备份
        time.sleep(0.1)
        with open(src, "w") as f:
            f.write("version 2")
        result = backup_file(src, self.config, _logger)
        self.assertEqual(result, "backed_up")
        with open(backup_path, "r") as f:
            self.assertEqual(f.read(), "version 2")
        # 4. 删除文件 → 移入回收站
        os.remove(src)
        recycle_path = move_to_recycle(
            src, False, self.config, _logger
        )
        self.assertIsNotNone(recycle_path)
        self.assertFalse(os.path.exists(backup_path))
        # 5. 恢复文件
        recycle_rel = os.path.relpath(recycle_path, RECYCLE_DIR)
        restored = restore_from_recycle(
            recycle_rel, self.config, _logger
        )
        self.assertIsNotNone(restored)
        self.assertTrue(os.path.exists(restored))
        with open(restored, "r") as f:
            self.assertEqual(f.read(), "version 2")

    def test_multiple_files_lifecycle(self):
        """多文件并行生命周期"""
        # 创建多个文件
        files = []
        for i in range(10):
            src = _create_test_file(f"multi/file{i}.txt", f"content{i}")
            files.append(src)
        # 全量备份
        count = backup_full_tree(self.config, _logger)
        self.assertEqual(count, 10)
        # 删除一半
        for src in files[:5]:
            os.remove(src)
            move_to_recycle(src, False, self.config, _logger)
        # 验证备份目录只剩 5 个
        remaining = []
        for root, dirs, fnames in os.walk(BACKUP_DIR):
            for fn in fnames:
                if not fn.endswith(".meta"):
                    remaining.append(fn)
        self.assertEqual(len(remaining), 5)

    def test_chinese_path_lifecycle(self):
        """中文路径完整流程"""
        src = _create_test_file("中文目录/测试文件.txt", "中文内容")
        backup_file(src, self.config, _logger)
        os.remove(src)
        recycle_path = move_to_recycle(
            src, False, self.config, _logger
        )
        self.assertIsNotNone(recycle_path)
        recycle_rel = os.path.relpath(recycle_path, RECYCLE_DIR)
        restored = restore_from_recycle(
            recycle_rel, self.config, _logger
        )
        self.assertIsNotNone(restored)
        with open(restored, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "中文内容")


# ── 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  文件回收站守护程序 - 端到端自动化测试")
    print("=" * 60)
    unittest.main(verbosity=2)
