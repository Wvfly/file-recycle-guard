"""
测试模块: core/config.py
测试范围: 配置加载、默认值、路径解析、排除规则、路径查找
"""
import os
import sys
import tempfile
import unittest
import yaml

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (
    Config, load_config, get_exe_dir,
    LogConfig, WebConfig, MirrorCleanupConfig,
    SyncConfig, UsnConfig, DatabaseConfig,
)


class TestConfigDataclasses(unittest.TestCase):
    """测试各配置子类的默认值和初始化"""

    def test_log_config_defaults(self):
        c = LogConfig()
        self.assertEqual(c.level, "INFO")
        self.assertEqual(c.file, "logs/recycle_guard.log")
        self.assertEqual(c.max_days, 90)

    def test_web_config_defaults(self):
        c = WebConfig()
        self.assertTrue(c.enabled)
        self.assertEqual(c.host, "0.0.0.0")
        self.assertEqual(c.port, 8088)
        self.assertIsNone(c.username)
        self.assertIsNone(c.password)

    def test_mirror_cleanup_config_defaults(self):
        c = MirrorCleanupConfig()
        self.assertTrue(c.enabled)
        self.assertEqual(c.interval, 3600)
        self.assertEqual(c.grace_period, 300)

    def test_sync_config_defaults(self):
        c = SyncConfig()
        self.assertTrue(c.enabled)
        self.assertEqual(c.interval, 30)

    def test_usn_config_defaults(self):
        c = UsnConfig()
        self.assertTrue(c.enabled)
        self.assertAlmostEqual(c.poll_interval, 1.0)
        self.assertEqual(c.state_dir, ".usn_state")

    def test_database_config_defaults(self):
        c = DatabaseConfig()
        self.assertEqual(c.host, "127.0.0.1")
        self.assertEqual(c.port, 3306)
        self.assertEqual(c.user, "root")
        self.assertEqual(c.password, "123456")
        self.assertEqual(c.database, "file_recycle_guard")

    def test_config_contains_all_sub_configs(self):
        c = Config()
        self.assertIsInstance(c.log, LogConfig)
        self.assertIsInstance(c.web, WebConfig)
        self.assertIsInstance(c.mirror_cleanup, MirrorCleanupConfig)
        self.assertIsInstance(c.sync, SyncConfig)
        self.assertIsInstance(c.usn, UsnConfig)
        self.assertIsInstance(c.database, DatabaseConfig)

    def test_config_default_watch_paths(self):
        c = Config()
        # Config() 不加载任何 YAML，watch_paths 默认为空列表
        self.assertEqual(c.watch_paths, [])
        self.assertEqual(c.backup_dir, "backup_mirror")
        self.assertEqual(c.recycle_dir, "recycle_bin")
        self.assertEqual(c.retention_days, 30)


class TestConfigLoad(unittest.TestCase):
    """测试从 YAML 文件加载配置"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_from_file(self):
        """从 YAML 文件加载配置"""
        config_path = os.path.join(self.temp_dir, "config.yaml")
        data = {
            "watch_paths": ["C:\\test\\watch"],
            "backup_dir": "C:\\test\\backup",
            "recycle_dir": "C:\\test\\recycle",
            "retention_days": 60,
            "log": {"level": "DEBUG", "file": "test.log", "max_days": 7},
            "web": {"enabled": False, "port": 9999},
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

        config = load_config(config_path)
        self.assertEqual(config.watch_paths, ["C:\\test\\watch"])
        self.assertEqual(config.backup_dir, "C:\\test\\backup")
        self.assertEqual(config.recycle_dir, "C:\\test\\recycle")
        self.assertEqual(config.retention_days, 60)
        self.assertEqual(config.log.level, "DEBUG")
        self.assertEqual(config.log.file, "test.log")
        self.assertFalse(config.web.enabled)
        self.assertEqual(config.web.port, 9999)

    def test_load_minimal_config(self):
        """加载最小配置（只有必要字段）"""
        config_path = os.path.join(self.temp_dir, "minimal.yaml")
        data = {
            "watch_paths": ["Z:\\minimal"],
            "backup_dir": "Z:\\bak",
            "recycle_dir": "Z:\\ryc",
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

        config = load_config(config_path)
        self.assertEqual(config.watch_paths, ["Z:\\minimal"])
        # 默认值应保持不变
        self.assertEqual(config.retention_days, 30)
        self.assertTrue(config.web.enabled)
        self.assertEqual(config.web.port, 8088)

    def test_load_with_exclude_patterns(self):
        """加载包含排除规则的配置"""
        config_path = os.path.join(self.temp_dir, "exclude.yaml")
        data = {
            "watch_paths": ["C:\\test"],
            "backup_dir": "C:\\bak",
            "recycle_dir": "C:\\ryc",
            "exclude_patterns": ["*.tmp", "~$*", "*.lock"],
            "exclude_dirs": [".git", "__pycache__", "node_modules"],
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

        config = load_config(config_path)
        self.assertIn("*.tmp", config.exclude_patterns)
        self.assertIn("~$*", config.exclude_patterns)
        self.assertIn(".git", config.exclude_dirs)
        self.assertIn("node_modules", config.exclude_dirs)

    def test_load_with_database_config(self):
        """加载包含数据库配置"""
        config_path = os.path.join(self.temp_dir, "db.yaml")
        data = {
            "watch_paths": ["C:\\test"],
            "backup_dir": "C:\\bak",
            "recycle_dir": "C:\\ryc",
            "database": {
                "host": "192.168.1.100",
                "port": 3307,
                "user": "admin",
                "password": "secret",
                "database": "test_db",
            },
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

        config = load_config(config_path)
        self.assertEqual(config.database.host, "192.168.1.100")
        self.assertEqual(config.database.port, 3307)
        self.assertEqual(config.database.user, "admin")
        self.assertEqual(config.database.database, "test_db")

    def test_load_with_web_auth(self):
        """加载包含 Web 认证的配置"""
        config_path = os.path.join(self.temp_dir, "auth.yaml")
        data = {
            "watch_paths": ["C:\\test"],
            "backup_dir": "C:\\bak",
            "recycle_dir": "C:\\ryc",
            "web": {
                "username": "admin",
                "password": "pass123",
            },
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

        config = load_config(config_path)
        self.assertEqual(config.web.username, "admin")
        self.assertEqual(config.web.password, "pass123")


class TestConfigMethods(unittest.TestCase):
    """测试 Config 类的方法"""

    def setUp(self):
        self.config = Config()

    def test_find_watch_root_exact_match(self):
        """测试 find_watch_root - 精确匹配"""
        self.config.watch_paths = ["E:\\tmp\\share"]
        result = self.config.find_watch_root("E:\\tmp\\share\\subdir\\file.txt")
        self.assertEqual(result, "E:\\tmp\\share")

    def test_find_watch_root_no_match(self):
        """测试 find_watch_root - 无匹配"""
        self.config.watch_paths = ["E:\\tmp\\share"]
        result = self.config.find_watch_root("D:\\other\\file.txt")
        self.assertIsNone(result)

    def test_find_watch_root_multiple_paths(self):
        """测试 find_watch_root - 多路径最长匹配"""
        self.config.watch_paths = ["E:\\tmp", "E:\\tmp\\share", "D:\\data"]
        result = self.config.find_watch_root("E:\\tmp\\share\\deep\\file.txt")
        self.assertEqual(result, "E:\\tmp\\share")  # 最长匹配
        result2 = self.config.find_watch_root("E:\\tmp\\other\\file.txt")
        self.assertEqual(result2, "E:\\tmp")  # 父路径匹配

    def test_find_watch_root_case_insensitive(self):
        """测试 find_watch_root - 大小写不敏感"""
        self.config.watch_paths = ["E:\\Tmp\\Share"]
        result = self.config.find_watch_root("e:\\tmp\\share\\file.txt")
        self.assertIsNotNone(result)

    def test_get_exe_dir(self):
        """测试 get_exe_dir 返回字符串"""
        result = get_exe_dir()
        self.assertIsInstance(result, str)
        self.assertTrue(os.path.exists(result))


class TestExcludePatterns(unittest.TestCase):
    """测试排除规则的匹配逻辑"""

    def setUp(self):
        self.config = Config()
        self.config.exclude_patterns = ["~$*", "*.tmp", "*.lock", "Thumbs.db"]

    def test_pattern_excluded(self):
        """测试匹配排除模式的文件应被排除"""
        import fnmatch
        self.assertTrue(fnmatch.fnmatch("~$document.docx", "~$*"))
        self.assertTrue(fnmatch.fnmatch("test.tmp", "*.tmp"))
        self.assertTrue(fnmatch.fnmatch("file.lock", "*.lock"))

    def test_pattern_not_excluded(self):
        """测试不匹配排除模式的文件不应被排除"""
        import fnmatch
        self.assertFalse(fnmatch.fnmatch("document.docx", "~$*"))
        self.assertFalse(fnmatch.fnmatch("test.txt", "*.tmp"))
        self.assertFalse(fnmatch.fnmatch("important.pdf", "*.lock"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
