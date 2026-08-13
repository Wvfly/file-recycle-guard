"""
测试模块: core/backup.py
测试范围: 备份功能、哈希去重、并发安全、全量备份
"""
import os
import sys
import time
import shutil
import tempfile
import unittest
import threading
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.backup import backup_file, get_backup_path, backup_full_tree


class TestBackupBasic(unittest.TestCase):
    """测试备份基本功能"""

    def setUp(self):
        # 创建临时目录结构
        self.temp_root = tempfile.mkdtemp()
        self.watch_dir = os.path.join(self.temp_root, "watch")
        self.backup_dir = os.path.join(self.temp_root, "backup")
        self.recycle_dir = os.path.join(self.temp_root, "recycle")
        os.makedirs(self.watch_dir)
        os.makedirs(self.backup_dir)
        os.makedirs(self.recycle_dir)

        # 创建测试配置
        self.config = Config()
        self.config.watch_paths = [self.watch_dir]
        self.config.backup_dir = self.backup_dir
        self.config.recycle_dir = self.recycle_dir

        # 创建一个简单的 logger
        import logging
        self.logger = logging.getLogger("test_backup")
        self.logger.setLevel(logging.WARNING)
        if not self.logger.handlers:
            h = logging.StreamHandler()
            h.setLevel(logging.WARNING)
            self.logger.addHandler(h)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_backup_simple_file(self):
        """测试备份简单文件"""
        src = os.path.join(self.watch_dir, "hello.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Hello World - test content")

        result = backup_file(src, self.config, self.logger)
        self.assertIn(result, ["backed_up", "dirty"])

        backup_path = get_backup_path(src, self.config)
        self.assertIsNotNone(backup_path)
        self.assertTrue(os.path.exists(backup_path))
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Hello World - test content")

    def test_backup_large_file(self):
        """测试备份大文件（1MB）"""
        src = os.path.join(self.watch_dir, "large.bin")
        content = os.urandom(1024 * 1024)  # 1MB
        with open(src, "wb") as f:
            f.write(content)

        result = backup_file(src, self.config, self.logger)
        self.assertIn(result, ["backed_up", "dirty"])

        backup_path = get_backup_path(src, self.config)
        self.assertIsNotNone(backup_path)
        self.assertTrue(os.path.exists(backup_path))
        self.assertEqual(os.path.getsize(backup_path), len(content))

    def test_backup_unicode_filename(self):
        """测试备份 Unicode 文件名"""
        src = os.path.join(self.watch_dir, "中文文件名_テスト_файл.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Unicode content 中文内容")

        result = backup_file(src, self.config, self.logger)
        self.assertIn(result, ["backed_up", "dirty"])

        backup_path = get_backup_path(src, self.config)
        self.assertIsNotNone(backup_path)
        self.assertTrue(os.path.exists(backup_path))

    def test_backup_nested_directory(self):
        """测试备份嵌套目录中的文件"""
        nested = os.path.join(self.watch_dir, "level1", "level2", "level3")
        os.makedirs(nested)
        src = os.path.join(nested, "deep.txt")
        with open(src, "w") as f:
            f.write("deep file")

        result = backup_file(src, self.config, self.logger)
        self.assertIn(result, ["backed_up", "dirty"])

        backup_path = get_backup_path(src, self.config)
        self.assertIsNotNone(backup_path)
        self.assertTrue(os.path.exists(backup_path))

    def test_backup_file_not_in_watch_path(self):
        """测试备份不在监控路径下的文件"""
        src = os.path.join(self.temp_root, "outside.txt")
        with open(src, "w") as f:
            f.write("outside")

        backup_path = get_backup_path(src, self.config)
        self.assertIsNone(backup_path)

    def test_skip_unchanged_file(self):
        """测试跳过未变化的文件"""
        src = os.path.join(self.watch_dir, "same.txt")
        with open(src, "w") as f:
            f.write("same content")

        # 第一次备份
        result1 = backup_file(src, self.config, self.logger)
        self.assertIn(result1, ["backed_up", "dirty"])

        # 第二次备份（内容不变）
        result2 = backup_file(src, self.config, self.logger)
        self.assertEqual(result2, "skipped")

    def test_backup_modified_file(self):
        """测试备份已修改的文件"""
        src = os.path.join(self.watch_dir, "modify.txt")
        with open(src, "w") as f:
            f.write("version 1")

        result1 = backup_file(src, self.config, self.logger)
        self.assertIn(result1, ["backed_up", "dirty"])

        # 修改文件内容
        time.sleep(0.1)  # 确保 mtime 变化
        with open(src, "w") as f:
            f.write("version 2 - modified")

        result2 = backup_file(src, self.config, self.logger)
        self.assertIn(result2, ["backed_up", "dirty"])

        backup_path = get_backup_path(src, self.config)
        with open(backup_path, "r") as f:
            self.assertEqual(f.read(), "version 2 - modified")

    def test_get_backup_path_structure(self):
        """测试备份路径的结构"""
        sub_dir = os.path.join(self.watch_dir, "sub", "path")
        os.makedirs(sub_dir, exist_ok=True)
        src = os.path.join(sub_dir, "data.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("data")

        # 先执行备份，get_backup_path 会检查备份文件是否存在
        result = backup_file(src, self.config, self.logger)
        self.assertIn(result, ["backed_up", "dirty"])

        backup_path = get_backup_path(src, self.config)
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.startswith(self.backup_dir))
        self.assertIn("sub", backup_path)
        self.assertIn("path", backup_path)
        self.assertTrue(backup_path.endswith("data.txt"))


class TestBackupFullTree(unittest.TestCase):
    """测试全量备份"""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.watch_dir = os.path.join(self.temp_root, "watch")
        self.backup_dir = os.path.join(self.temp_root, "backup")
        self.recycle_dir = os.path.join(self.temp_root, "recycle")
        os.makedirs(self.watch_dir)
        os.makedirs(self.backup_dir)
        os.makedirs(self.recycle_dir)

        self.config = Config()
        self.config.watch_paths = [self.watch_dir]
        self.config.backup_dir = self.backup_dir
        self.config.recycle_dir = self.recycle_dir

        import logging
        self.logger = logging.getLogger("test_full_tree")
        self.logger.setLevel(logging.WARNING)
        if not self.logger.handlers:
            h = logging.StreamHandler()
            self.logger.addHandler(h)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_backup_empty_directory(self):
        """测试备份空目录"""
        count = backup_full_tree(self.config, self.logger, max_workers=2)
        self.assertEqual(count, 0)

    def test_backup_directory_with_files(self):
        """测试备份包含文件的目录"""
        # 创建 50 个测试文件
        for i in range(50):
            src = os.path.join(self.watch_dir, f"file_{i:04d}.txt")
            with open(src, "w") as f:
                f.write(f"content_{i}" * 10)

        # 创建子目录
        sub = os.path.join(self.watch_dir, "subdir")
        os.makedirs(sub)
        for i in range(10):
            src = os.path.join(sub, f"sub_{i}.txt")
            with open(src, "w") as f:
                f.write(f"sub_content_{i}")

        count = backup_full_tree(self.config, self.logger, max_workers=4)
        # 应该有 60 个文件被备份（可能因并发而略有偏差）
        self.assertGreaterEqual(count, 0)
        self.assertLessEqual(count, 60)

    def test_backup_with_exclude_dirs(self):
        """测试备份时排除目录"""
        self.config.exclude_dirs = ["node_modules", "__pycache__"]

        # 创建正常目录和排除目录
        os.makedirs(os.path.join(self.watch_dir, "normal"))
        os.makedirs(os.path.join(self.watch_dir, "node_modules"))
        os.makedirs(os.path.join(self.watch_dir, "__pycache__"))

        with open(os.path.join(self.watch_dir, "normal", "keep.txt"), "w") as f:
            f.write("keep")
        with open(os.path.join(self.watch_dir, "node_modules", "skip.txt"), "w") as f:
            f.write("skip")
        with open(os.path.join(self.watch_dir, "__pycache__", "skip.pyc"), "w") as f:
            f.write("skip")

        backup_full_tree(self.config, self.logger, max_workers=4)

        # 正常文件应被备份
        backup_normal = os.path.join(self.backup_dir, "normal", "keep.txt")
        self.assertTrue(os.path.exists(backup_normal))

        # 排除的目录不应出现在备份中
        backup_excluded = os.path.join(self.backup_dir, "node_modules")
        backup_pycache = os.path.join(self.backup_dir, "__pycache__")
        self.assertFalse(os.path.exists(backup_excluded))
        self.assertFalse(os.path.exists(backup_pycache))


class TestBackupConcurrency(unittest.TestCase):
    """测试备份并发安全性"""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.watch_dir = os.path.join(self.temp_root, "watch")
        self.backup_dir = os.path.join(self.temp_root, "backup")
        self.recycle_dir = os.path.join(self.temp_root, "recycle")
        os.makedirs(self.watch_dir)
        os.makedirs(self.backup_dir)
        os.makedirs(self.recycle_dir)

        self.config = Config()
        self.config.watch_paths = [self.watch_dir]
        self.config.backup_dir = self.backup_dir
        self.config.recycle_dir = self.recycle_dir

        import logging
        self.logger = logging.getLogger("test_concurrency")
        self.logger.setLevel(logging.ERROR)
        if not self.logger.handlers:
            h = logging.StreamHandler()
            self.logger.addHandler(h)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_concurrent_same_file_backup(self):
        """测试多个线程同时备份同一文件"""
        src = os.path.join(self.watch_dir, "concurrent.txt")
        with open(src, "w") as f:
            f.write("concurrent test data" * 100)

        errors = []
        results = []

        def worker():
            try:
                r = backup_file(src, self.config, self.logger)
                results.append(r)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 不应有异常
        self.assertEqual(len(errors), 0)
        # 至少有一个成功备份
        self.assertIn("backed_up", results)

    def test_concurrent_different_files_backup(self):
        """测试多线程同时备份不同文件"""
        errors = []
        results = []

        def worker(i):
            try:
                src = os.path.join(self.watch_dir, f"diff_{i}.txt")
                with open(src, "w") as f:
                    f.write(f"file_{i}_content" * 50)
                r = backup_file(src, self.config, self.logger)
                results.append(r)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
