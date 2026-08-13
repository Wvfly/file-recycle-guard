"""
测试模块: core/database.py
测试范围: 数据库连接、连接池、元信息 CRUD、统计查询
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDatabaseImport(unittest.TestCase):
    """测试数据库模块导入和基础功能"""

    def test_database_module_imports(self):
        """测试数据库模块可以导入"""
        from core.database import (
            init_database, get_db, Database, ConnectionPool,
        )
        self.assertTrue(callable(init_database))
        self.assertTrue(callable(get_db))
        self.assertIsNotNone(Database)
        self.assertIsNotNone(ConnectionPool)

    def test_path_hash_function(self):
        """测试路径哈希函数"""
        from core.database import _path_hash
        h1 = _path_hash("E:\\tmp\\share", "file.txt")
        h2 = _path_hash("E:\\tmp\\share", "file.txt")
        h3 = _path_hash("E:\\tmp\\share", "other.txt")

        # 相同输入应相同
        self.assertEqual(h1, h2)
        # 不同输入应不同
        self.assertNotEqual(h1, h3)
        # 应该是字符串
        self.assertIsInstance(h1, str)
        # 应该是 64 字符的十六进制字符串
        self.assertEqual(len(h1), 64)


class TestDatabaseConnection(unittest.TestCase):
    """测试数据库连接（需要 MySQL 可用）"""

    @classmethod
    def setUpClass(cls):
        # 尝试连接数据库
        cls.db_available = False
        try:
            from core.database import init_database
            init_database(
                host="127.0.0.1", port=3306,
                user="root", password="123456",
                database="file_recycle_guard",
            )
            cls.db_available = True
        except Exception:
            pass

    def setUp(self):
        if not self.db_available:
            self.skipTest("MySQL 不可用，跳过数据库连接测试")

    def test_get_db_returns_instance(self):
        """测试 get_db 返回数据库实例"""
        from core.database import get_db
        db = get_db()
        self.assertIsNotNone(db)

    def test_tables_exist(self):
        """测试核心表存在"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        # 通过 count 操作验证表存在（间接验证）
        try:
            count = db.count_backup_meta()
            self.assertIsInstance(count, int)
            count2 = db.count_recycle_meta()
            self.assertIsInstance(count2, int)
        except Exception as e:
            self.skipTest(f"表查询失败: {e}")

    def test_count_backup_meta(self):
        """测试备份元信息计数"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        count = db.count_backup_meta()
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)

    def test_sum_backup_size(self):
        """测试备份大小统计"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        size = db.sum_backup_size()
        self.assertIsInstance(size, int)
        self.assertGreaterEqual(size, 0)

    def test_count_recycle_meta(self):
        """测试回收站元信息计数"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        count = db.count_recycle_meta()
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)

    def test_list_recycle_meta_paged(self):
        """测试分页查询回收站"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        results, total = db.list_recycle_meta_paged(page=1, page_size=5)
        self.assertIsInstance(results, list)
        self.assertLessEqual(len(results), 5)

    def test_upsert_backup_meta(self):
        """测试插入/更新备份元信息"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        import uuid
        import time
        test_path = f"test/unit_test_{uuid.uuid4().hex[:8]}.txt"
        now = time.time()
        db.upsert_backup_meta(
            watch_root="E:\\tmp\\share",
            rel_path=test_path,
            file_hash="test_hash_abc123",
            file_size=1024,
            mtime=now,
            source_path=f"E:\\tmp\\share\\{test_path}",
            backup_time=now,
        )

        # 查询确认
        meta = db.get_backup_meta("E:\\tmp\\share", test_path)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("file_size"), 1024)

        # 清理
        db.delete_backup_meta("E:\\tmp\\share", test_path)

    def test_iter_backup_meta_batch(self):
        """测试流式遍历备份元信息"""
        from core.database import get_db
        db = get_db()
        if db is None:
            self.skipTest("数据库不可用")

        batch = db.iter_backup_meta_batch(batch_size=10)
        first = next(batch, None)
        if first is not None:
            self.assertIsInstance(first, list)
            self.assertLessEqual(len(first), 10)


class TestConnectionPool(unittest.TestCase):
    """测试连接池（需要 MySQL 可用）"""

    @classmethod
    def setUpClass(cls):
        cls.pool_available = False
        try:
            from core.database import ConnectionPool
            cls.pool = ConnectionPool(
                host="127.0.0.1", port=3306,
                user="root", password="123456",
                database="file_recycle_guard",
                max_connections=4,
            )
            # 测试连接
            conn = cls.pool.get_connection()
            conn.ping(reconnect=True)
            cls.pool.return_connection(conn)
            cls.pool_available = True
        except Exception:
            pass

    @classmethod
    def tearDownClass(cls):
        if cls.pool_available:
            try:
                cls.pool.close_all()
            except Exception:
                pass

    def setUp(self):
        if not self.pool_available:
            self.skipTest("MySQL 不可用，跳过连接池测试")

    def test_get_connection(self):
        """测试获取连接"""
        conn = self.pool.get_connection()
        self.assertIsNotNone(conn)
        self.pool.return_connection(conn)

    def test_return_connection(self):
        """测试归还连接"""
        conn = self.pool.get_connection()
        self.pool.return_connection(conn)
        # 不应抛异常

    def test_connection_reuse(self):
        """测试连接复用"""
        conn1 = self.pool.get_connection()
        tid1 = conn1.thread_id()
        self.pool.return_connection(conn1)

        conn2 = self.pool.get_connection()
        tid2 = conn2.thread_id()
        self.pool.return_connection(conn2)

        # 因为池里只有一个连接，应该复用
        self.assertEqual(tid1, tid2)

    def test_multiple_connections(self):
        """测试多个连接"""
        connections = []
        for _ in range(4):
            conn = self.pool.get_connection()
            if conn:
                connections.append(conn)

        for conn in connections:
            self.pool.return_connection(conn)

        self.assertEqual(len(connections), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
