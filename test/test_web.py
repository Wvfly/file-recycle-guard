"""
测试模块: web/__init__.py (FastAPI 应用)
测试范围: 路由、API 响应、认证、分页、统计缓存
"""
import os
import sys
import unittest
import urllib.request
import urllib.error
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config


class TestWebAppCreation(unittest.TestCase):
    """测试 FastAPI 应用创建"""

    @classmethod
    def setUpClass(cls):
        import logging
        cls.logger = logging.getLogger("test_web")
        cls.logger.setLevel(logging.ERROR)
        if not cls.logger.handlers:
            h = logging.StreamHandler()
            cls.logger.addHandler(h)
        cls.config = Config()

    def test_create_app_returns_fastapi(self):
        from web import create_app
        app = create_app(self.config, self.logger)
        from fastapi import FastAPI
        self.assertIsInstance(app, FastAPI)

    def test_app_has_routes(self):
        from web import create_app
        app = create_app(self.config, self.logger)
        routes = [r.path for r in app.routes]
        self.assertIn("/", routes)
        self.assertIn("/api/files", routes)
        self.assertIn("/api/restore", routes)
        self.assertIn("/api/empty", routes)
        self.assertIn("/api/stats", routes)

    def test_app_title(self):
        from web import create_app
        app = create_app(self.config, self.logger)
        self.assertEqual(app.title, "文件回收站管理")


class TestWebAPI(unittest.TestCase):
    """测试 Web API 端点（需要服务运行）"""

    @classmethod
    def setUpClass(cls):
        cls.server_available = False
        try:
            r = urllib.request.urlopen("http://127.0.0.1:8088/api/stats", timeout=3)
            r.read()
            cls.server_available = True
        except Exception:
            pass

    def setUp(self):
        if not self.server_available:
            self.skipTest("Web 服务未运行，跳过 API 测试")

    def test_stats_endpoint(self):
        r = urllib.request.urlopen("http://127.0.0.1:8088/api/stats", timeout=5)
        self.assertEqual(r.status, 200)
        data = json.loads(r.read())
        self.assertIn("recycled_count", data)
        self.assertIsInstance(data["recycled_count"], int)

    def test_files_endpoint_default(self):
        r = urllib.request.urlopen("http://127.0.0.1:8088/api/files", timeout=5)
        self.assertEqual(r.status, 200)
        data = json.loads(r.read())
        self.assertIn("total", data)
        self.assertIn("files", data)

    def test_files_endpoint_pagination(self):
        r = urllib.request.urlopen(
            "http://127.0.0.1:8088/api/files?page=1&page_size=5", timeout=5)
        self.assertEqual(r.status, 200)
        data = json.loads(r.read())
        self.assertLessEqual(len(data["files"]), 5)

    def test_files_endpoint_search(self):
        r = urllib.request.urlopen(
            "http://127.0.0.1:8088/api/files?search=test", timeout=5)
        self.assertEqual(r.status, 200)

    def test_index_page(self):
        r = urllib.request.urlopen("http://127.0.0.1:8088/", timeout=5)
        self.assertEqual(r.status, 200)
        content = r.read().decode("utf-8")
        self.assertIn("<!DOCTYPE html>", content)

    def test_stats_cached(self):
        r1 = urllib.request.urlopen("http://127.0.0.1:8088/api/stats", timeout=5)
        d1 = json.loads(r1.read())
        r2 = urllib.request.urlopen("http://127.0.0.1:8088/api/stats", timeout=5)
        d2 = json.loads(r2.read())
        self.assertEqual(d1["recycled_count"], d2["recycled_count"])

    def test_restore_without_path(self):
        data = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:8088/api/restore",
            data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            self.assertEqual(r.status, 400)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_restore_invalid_path(self):
        data = json.dumps({"path": "../etc/passwd"}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:8088/api/restore",
            data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            self.assertIn(r.status, [400, 404])
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, [400, 404])


if __name__ == "__main__":
    unittest.main(verbosity=2)
