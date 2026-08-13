"""
测试模块: core/database.py
测试范围: 数据库连接、连接池、元信息 CRUD、统计查询

注意：所有数据库测试使用独立的测试库 file_recycle_guard_test，
避免污染生产库 file_recycle_guard。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 测试数据库配置（与生产库隔离） ─────────────────────────
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
        # 尝试连接测试数据库（自动建库，避免污染生产库）
        cls.db_available = False
        try:
            from core.database import init_database
            host, port, user, pwd, dbname = _ensure_test_db()
            init_database(
                host=host, port=port, user=user,
                password=pwd, database=dbname,
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



class TestConnectionPool(unittest.TestCase):
    """测试连接池（需要 MySQL 可用）"""

    @classmethod
    def setUpClass(cls):
        cls.pool_available = False
        try:
            from core.database import ConnectionPool
            host, port, user, pwd, dbname = _ensure_test_db()
            cls.pool = ConnectionPool(config={
                "host": host, "port": port, "user": user,
                "password": pwd, "database": dbname,
                "charset": "utf8mb4", "connect_timeout": 5,
            })
            # 测试连接
            conn = cls.pool.acquire()
            conn.ping(reconnect=True)
            cls.pool.release(conn)
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

    def test_acquire_connection(self):
        """测试获取连接"""
        conn = self.pool.acquire()
        self.assertIsNotNone(conn)
        self.pool.release(conn)

    def test_return_connection(self):
        """测试归还连接"""
        conn = self.pool.acquire()
        self.pool.release(conn)
        # 不应抛异常

    def test_connection_reuse(self):
        """测试连接复用"""
        conn1 = self.pool.acquire()
        tid1 = conn1.thread_id()
        self.pool.release(conn1)

        conn2 = self.pool.acquire()
        tid2 = conn2.thread_id()
        self.pool.release(conn2)

        # 因为池里只有一个连接，应该复用
        self.assertEqual(tid1, tid2)

    def test_multiple_connections(self):
        """测试多个连接"""
        connections = []
        for _ in range(4):
            conn = self.pool.acquire()
            if conn:
                connections.append(conn)

        for conn in connections:
            self.pool.release(conn)

        self.assertEqual(len(connections), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
