"""
测试模块: core/recycler.py
测试范围: 回收站移入、恢复、分页查询、清理过期、清空
"""
import os
import sys
import time
import shutil
import tempfile
import unittest
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.recycler import (
    move_to_recycle, restore_from_recycle,
    list_recycled_files, list_recycled_files_paged,
    cleanup_expired, empty_recycle,
)


class TestRecyclerBasic(unittest.TestCase):
    """测试回收站基本功能"""

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
        self.config.retention_days = 30

        import logging
        self.logger = logging.getLogger("test_recycler")
        self.logger.setLevel(logging.WARNING)
        if not self.logger.handlers:
            h = logging.StreamHandler()
            self.logger.addHandler(h)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _create_source_and_backup(self, rel_path, content="test"):
        """辅助：在 watch_dir 创建源文件 + 在 backup_dir 创建备份。
        move_to_recycle 需要传入源路径（watch_dir 下），
        内部自动拼接 backup_dir/rel_path 查找备份。
        """
        src_path = os.path.join(self.watch_dir, rel_path)
        backup_path = os.path.join(self.backup_dir, rel_path)

        os.makedirs(os.path.dirname(src_path), exist_ok=True)
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)

        with open(src_path, "w", encoding="utf-8") as f:
            f.write(content)
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(content)
        # 创建 .meta 文件
        meta_path = backup_path + ".meta"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "original_path": src_path,
                "size": len(content),
            }, f)
        return src_path, backup_path

    # ---- 移入回收站 ----

    def test_move_to_recycle_file(self):
        """测试将备份文件移入回收站"""
        rel = "test_file.txt"
        src_path, backup_path = self._create_source_and_backup(rel, "recycle test content")

        # move_to_recycle 传入源路径（watch_dir 下），内部自动查找备份
        result = move_to_recycle(src_path, False, self.config, self.logger)
        self.assertIsNotNone(result)

        # 备份文件应被移除
        self.assertFalse(os.path.exists(backup_path))
        # 回收站中应有该文件
        recycle_files = list_recycled_files(self.config)
        self.assertGreaterEqual(len(recycle_files), 1)

    def test_move_to_recycle_in_subdir(self):
        """测试将子目录中的备份文件移入回收站"""
        rel = "sub/dir/deep_file.txt"
        src_path, backup_path = self._create_source_and_backup(rel, "deep content")

        result = move_to_recycle(src_path, False, self.config, self.logger)
        self.assertIsNotNone(result)

        recycle_files = list_recycled_files(self.config)
        found = any(rel.replace("/", os.sep) in f.get("relative_path", "")
                     or rel in f.get("relative_path", "") for f in recycle_files)
        self.assertTrue(found)

    def test_unicode_filename_recycle(self):
        """测试回收站 Unicode 文件名"""
        rel = "中文文件名_テスト.txt"
        src_path, backup_path = self._create_source_and_backup(rel, "Unicode recycle 中文内容")

        result = move_to_recycle(src_path, False, self.config, self.logger)
        self.assertIsNotNone(result)

        recycle_files = list_recycled_files(self.config)
        found = any("中文" in f.get("relative_path", "")
                     or "テスト" in f.get("relative_path", "") for f in recycle_files)
        self.assertTrue(found)

    # ---- 列出回收站 ----

    def test_list_recycled_files_empty(self):
        """测试列出空回收站"""
        files = list_recycled_files(self.config)
        self.assertEqual(len(files), 0)

    def test_list_recycled_files_paged(self):
        """测试分页列出回收站文件"""
        for i in range(5):
            rel = f"page_test_{i}.txt"
            src_path, _ = self._create_source_and_backup(rel, f"content_{i}")
            move_to_recycle(src_path, False, self.config, self.logger)

        # 分页查询
        page1, total = list_recycled_files_paged(self.config, page=1, page_size=2)
        self.assertEqual(len(page1), 2)
        self.assertGreaterEqual(total, 5)

        page2, total2 = list_recycled_files_paged(self.config, page=2, page_size=2)
        self.assertEqual(len(page2), 2)

    def test_list_recycled_files_paged_search(self):
        """测试搜索回收站文件"""
        for i in range(3):
            rel = f"searchable_{i}.txt"
            src_path, _ = self._create_source_and_backup(rel, f"content_{i}")
            move_to_recycle(src_path, False, self.config, self.logger)

        for i in range(2):
            rel = f"other_{i}.txt"
            src_path, _ = self._create_source_and_backup(rel, f"content_{i}")
            move_to_recycle(src_path, False, self.config, self.logger)

        results, total = list_recycled_files_paged(
            self.config, page=1, page_size=10, search="searchable"
        )
        self.assertGreaterEqual(total, 3)
        for f in results:
            self.assertIn("searchable", f.get("relative_path", ""))

    # ---- 恢复 ----

    def test_restore_from_recycle(self):
        """测试从回收站恢复文件"""
        rel = "restore_test.txt"
        content = "RESTORE_ME_12345"
        src_path, _ = self._create_source_and_backup(rel, content)

        # 移入回收站
        recycle_result = move_to_recycle(src_path, False, self.config, self.logger)
        self.assertIsNotNone(recycle_result)

        # 找到回收站中的文件（recycle_path 是回收站内完整路径）
        recycle_files = list_recycled_files(self.config)
        self.assertGreaterEqual(len(recycle_files), 1)
        recycle_full = recycle_files[0].get("recycle_path", "")
        recycle_rel_path = os.path.relpath(recycle_full, self.config.recycle_dir)

        # 恢复
        restore_result = restore_from_recycle(recycle_rel_path, self.config, self.logger)
        self.assertIsNotNone(restore_result)

        # 原位置应该有文件
        original = os.path.join(self.watch_dir, rel)
        self.assertTrue(os.path.exists(original))
        with open(original, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), content)

    def test_restore_file_not_in_recycle(self):
        """测试恢复不存在的文件"""
        result = restore_from_recycle("nonexistent_file.txt", self.config, self.logger)
        self.assertIsNone(result)

    # ---- 清理过期 ----

    def test_cleanup_expired(self):
        """测试清理过期回收站条目

        注意：cleanup_expired 在数据库模式下检查数据库中的 deletion_time，
        无法通过修改文件 mtime 影响。此测试仅在数据库不可用时运行文件模式。
        """
        from core.database import get_db
        if get_db() is not None:
            self.skipTest("cleanup_expired 数据库模式下依赖 deletion_time 字段，跳过")

        rel = "old_file.txt"
        src_path, _ = self._create_source_and_backup(rel, "old content")

        result = move_to_recycle(src_path, False, self.config, self.logger)
        self.assertIsNotNone(result)

        self.config.retention_days = 1
        recycle_files = list_recycled_files(self.config)
        if recycle_files:
            recycle_full = recycle_files[0].get("recycle_path", "")
            # cleanup_expired 文件模式读取的是 .recycle.json 中的 deletion_time，
            # 而非文件 mtime，需要修改 JSON 元信息
            meta_file = recycle_full + ".recycle.json"
            if os.path.exists(meta_file):
                old_time = time.time() - 86400 * 2  # 2 天前
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta["deletion_time"] = old_time
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump(meta, f)

        cleaned = cleanup_expired(self.config, self.logger)
        self.assertGreaterEqual(cleaned, 1)

    # ---- 清空回收站 ----

    def test_empty_recycle(self):
        """测试清空回收站"""
        for i in range(3):
            rel = f"empty_test_{i}.txt"
            src_path, _ = self._create_source_and_backup(rel, f"content_{i}")
            move_to_recycle(src_path, False, self.config, self.logger)

        count = empty_recycle(self.config, self.logger)
        self.assertGreaterEqual(count, 3)

        files = list_recycled_files(self.config)
        self.assertEqual(len(files), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
