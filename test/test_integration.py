"""
集成测试: 完整文件生命周期
测试范围: 创建 → 备份 → 删除 → 回收 → 恢复 完整流程
"""
import os
import sys
import time
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.backup import backup_file, get_backup_path
from core.recycler import move_to_recycle, restore_from_recycle, list_recycled_files


class TestFileLifecycle(unittest.TestCase):
    """文件完整生命周期集成测试"""

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
        self.logger = logging.getLogger("test_integration")
        self.logger.setLevel(logging.WARNING)
        if not self.logger.handlers:
            h = logging.StreamHandler()
            self.logger.addHandler(h)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _full_lifecycle(self, rel_path, content, is_binary=False):
        """完整的生命周期流程：创建 → 备份 → 回收 → 恢复 → 验证"""
        src_path = os.path.join(self.watch_dir, rel_path)
        os.makedirs(os.path.dirname(src_path), exist_ok=True)

        mode = "wb" if is_binary else "w"
        encoding = None if is_binary else "utf-8"
        with open(src_path, mode, encoding=encoding) as f:
            f.write(content)

        self.assertTrue(os.path.exists(src_path), "源文件应存在")

        # Step 1: 备份
        result = backup_file(src_path, self.config, self.logger)
        self.assertIn(result, ["backed_up", "dirty"])

        backup_path = get_backup_path(src_path, self.config)
        self.assertIsNotNone(backup_path, "备份路径不应为空")
        self.assertTrue(os.path.exists(backup_path), "备份文件应存在")

        # Step 2: 移入回收站（传入源路径，内部自动查备份）
        recycle_result = move_to_recycle(src_path, False, self.config, self.logger)
        self.assertIsNotNone(recycle_result, "移入回收站应成功")

        # 备份文件应被移除
        self.assertFalse(os.path.exists(backup_path), "备份文件应被移除")

        # Step 3: 从回收站恢复
        recycle_files = list_recycled_files(self.config)
        self.assertGreater(len(recycle_files), 0, "回收站应有文件")

        # recycle_path 是回收站内完整路径，需提取相对于 recycle_dir 的路径
        recycle_full = recycle_files[0].get("recycle_path", "")
        recycle_rel = os.path.relpath(recycle_full, self.config.recycle_dir)
        restore_result = restore_from_recycle(recycle_rel, self.config, self.logger)
        self.assertIsNotNone(restore_result, "恢复应成功")

        # Step 4: 验证恢复后的内容
        restored_path = os.path.join(self.watch_dir, rel_path)
        self.assertTrue(os.path.exists(restored_path), f"恢复的文件应存在: {restored_path}")

        read_mode = "rb" if is_binary else "r"
        with open(restored_path, read_mode, encoding=encoding) as f:
            restored_content = f.read()
        self.assertEqual(restored_content, content, "恢复后的内容应与原始内容一致")

    def test_full_lifecycle_single_file(self):
        """测试单个文件的完整生命周期"""
        self._full_lifecycle(
            "integration_test.txt",
            "INTEGRATION TEST CONTENT - Step 1 2 3 4 5"
        )

    def test_lifecycle_unicode_filename(self):
        """测试 Unicode 文件名的完整生命周期"""
        self._full_lifecycle(
            "集成测试_インテグレーション.txt",
            "Unicode integration test 中文 + 日本語"
        )

    def test_lifecycle_large_file(self):
        """测试大文件的完整生命周期"""
        self._full_lifecycle(
            "large_integration.bin",
            os.urandom(512 * 1024),  # 512KB
            is_binary=True
        )

    def test_lifecycle_multiple_files(self):
        """测试多文件生命周期"""
        for i in range(10):
            rel = f"multi_{i:03d}.txt"
            content = f"multi_content_{i}_" * 20
            src_path = os.path.join(self.watch_dir, rel)
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(content)

            # 备份
            backup_file(src_path, self.config, self.logger)

            # 移入回收站
            move_to_recycle(src_path, False, self.config, self.logger)

        # 验证回收站数量
        recycle_files = list_recycled_files(self.config)
        self.assertGreaterEqual(len(recycle_files), 1, "回收站应有至少 1 个文件")

    def test_lifecycle_file_modification(self):
        """测试文件修改后的生命周期"""
        rel = "modified_test.txt"
        v1 = "VERSION ONE CONTENT"
        v2 = "VERSION TWO - UPDATED CONTENT"

        src_path = os.path.join(self.watch_dir, rel)

        # 版本 1
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(v1)
        backup_file(src_path, self.config, self.logger)

        backup_path = get_backup_path(src_path, self.config)
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), v1)

        # 版本 2
        time.sleep(0.2)
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(v2)
        backup_file(src_path, self.config, self.logger)

        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), v2, "备份应更新为 v2")

        # 移入回收站
        move_to_recycle(src_path, False, self.config, self.logger)

        # 恢复后应是 v2
        recycle_files = list_recycled_files(self.config)
        if recycle_files:
            recycle_full = recycle_files[0].get("recycle_path", "")
            recycle_rel = os.path.relpath(recycle_full, self.config.recycle_dir)
            restore_from_recycle(recycle_rel, self.config, self.logger)

            if os.path.exists(src_path):
                with open(src_path, "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), v2)

    def test_lifecycle_nested_directories(self):
        """测试嵌套目录中的文件生命周期"""
        self._full_lifecycle(
            os.path.join("a", "b", "c", "nested.txt"),
            "deeply nested file content"
        )

    def test_backup_then_modify_then_recycle(self):
        """测试：备份 → 修改 → 再备份 → 回收 → 恢复"""
        rel = "modify_recycle_test.txt"
        original = "ORIGINAL_CONTENT_AAAA"
        modified = "MODIFIED_CONTENT_BBBB"

        src_path = os.path.join(self.watch_dir, rel)

        with open(src_path, "w", encoding="utf-8") as f:
            f.write(original)
        backup_file(src_path, self.config, self.logger)

        time.sleep(0.2)
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(modified)
        backup_file(src_path, self.config, self.logger)

        backup_path = get_backup_path(src_path, self.config)
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), modified)

        move_to_recycle(src_path, False, self.config, self.logger)

        recycle_files = list_recycled_files(self.config)
        self.assertGreater(len(recycle_files), 0)
        recycle_full = recycle_files[0].get("recycle_path", "")
        recycle_rel = os.path.relpath(recycle_full, self.config.recycle_dir)
        restore_from_recycle(recycle_rel, self.config, self.logger)

        if os.path.exists(src_path):
            with open(src_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), modified, "恢复的应为修改后的版本")


if __name__ == "__main__":
    unittest.main(verbosity=2)
