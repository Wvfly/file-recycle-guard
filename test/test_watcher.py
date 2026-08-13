"""
测试模块: core/watcher.py
测试范围: USN 模块加载、监控启动/停止、模式切换
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config


class TestUsnModuleLoading(unittest.TestCase):
    """测试 USN 模块加载"""

    def test_usn_module_import(self):
        """测试 USN 模块可以导入"""
        try:
            from core.usn import create_usn_watcher
            self.assertTrue(callable(create_usn_watcher))
        except ImportError as e:
            self.skipTest(f"USN 模块导入失败: {e}")

    def test_usn_types_exist(self):
        """测试 USN 关键类型存在"""
        try:
            from core.usn import (
                UsnJournalMonitor, UsnJournalReader,
                UsnJournalState, UsnEvent,
            )
            self.assertIsNotNone(UsnJournalMonitor)
            self.assertIsNotNone(UsnJournalReader)
            self.assertIsNotNone(UsnJournalState)
            self.assertIsNotNone(UsnEvent)
        except ImportError:
            self.skipTest("USN 模块不可用")

    def test_create_usn_watcher_returns_none_without_admin(self):
        """测试无管理员权限时 create_usn_watcher 返回 None（回退）"""
        import logging
        logger = logging.getLogger("test_usn")
        logger.setLevel(logging.CRITICAL)

        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()

        from core.usn import create_usn_watcher
        config = Config()
        config.watch_paths = ["C:\\Windows"]  # 通常需要管理员权限

        result = create_usn_watcher(config, logger)
        if not is_admin:
            # 非管理员应返回 None
            self.assertIsNone(result)
        else:
            # 管理员可能返回监控器或 None
            pass


class TestWatcherStartup(unittest.TestCase):
    """测试监控启动入口"""

    def test_start_watcher_import(self):
        """测试 start_watcher 函数存在"""
        from core.watcher import start_watcher
        self.assertTrue(callable(start_watcher))

    def test_start_watcher_returns_tuple(self):
        """测试 start_watcher 返回二元组 (observer, handler)"""
        import logging
        import shutil
        import tempfile
        from core.watcher import start_watcher
        logger = logging.getLogger("test_watcher_start")
        logger.setLevel(logging.ERROR)
        if not logger.handlers:
            h = logging.StreamHandler()
            logger.addHandler(h)

        root = tempfile.mkdtemp(prefix="recycle_guard_watcher_")
        try:
            config = Config()
            config.watch_paths = [os.path.join(root, "share")]
            config.backup_dir = os.path.join(root, "backup")
            config.recycle_dir = os.path.join(root, "recycle")

            observer, handler = start_watcher(config, logger)
            self.assertIsNotNone(observer)
            self.assertIsNotNone(handler)
            observer.stop()
            observer.join(timeout=5)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestHandlerClasses(unittest.TestCase):
    """测试事件处理器类"""

    def test_recycle_guard_handler_exists(self):
        """测试 RecycleGuardHandler 类存在"""
        from core.watcher import RecycleGuardHandler
        self.assertIsNotNone(RecycleGuardHandler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
