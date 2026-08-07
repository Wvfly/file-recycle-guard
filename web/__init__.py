"""
Web 管理界面 - FastAPI 应用
提供浏览、搜索、恢复已删除文件的功能

性能优化（亿级场景）：
- 文件列表使用后端分页查询，不再全量加载
- 统计接口使用数据库聚合（COUNT/SUM），不再 os.walk 遍历
- 统计结果缓存 60 秒，避免频繁查询
"""

import os
import time
import datetime
import threading
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from core.config import Config
from core.database import get_db
from core.recycler import (
    list_recycled_files, list_recycled_files_paged,
    restore_from_recycle, empty_recycle
)


def create_app(config: Config, logger) -> FastAPI:
    app = FastAPI(title="文件回收站管理")

    # 模板目录
    templates = Jinja2Templates(
        directory=os.path.join(os.path.dirname(__file__), "templates")
    )

    # ── 统计缓存 ────────────────────────────────────────────
    _stats_cache = {"value": None, "timestamp": 0}
    _stats_lock = threading.Lock()
    _STATS_CACHE_TTL = 60  # 缓存 60 秒

    # ── 认证中间件 ──────────────────────────────────────────

    def _check_auth(request: Request) -> bool:
        """HTTP Basic 认证检查"""
        if config.web.username and config.web.password:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                import base64
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if username != config.web.username or password != config.web.password:
                    return False
            except Exception:
                return False
        return True

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        """对所有路由强制认证（配置了用户名密码时）"""
        if not _check_auth(request):
            return Response(
                content="需要认证",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="FileRecycleGuard"'},
            )
        return await call_next(request)

    # ── 工具函数 ────────────────────────────────────────────

    def _format_size(size_bytes: int) -> str:
        """格式化文件大小"""
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    def _format_time(ts: float) -> str:
        """格式化时间戳"""
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _seconds_ago(ts: float) -> str:
        """计算相对时间"""
        diff = time.time() - ts
        if diff < 60:
            return f"{int(diff)} 秒前"
        elif diff < 3600:
            return f"{int(diff / 60)} 分钟前"
        elif diff < 86400:
            return f"{int(diff / 3600)} 小时前"
        else:
            return f"{int(diff / 86400)} 天前"

    def _invalidate_stats_cache():
        """使统计缓存失效"""
        with _stats_lock:
            _stats_cache["timestamp"] = 0

    # ── 页面路由 ────────────────────────────────────────────

    @app.get("/")
    async def index(request: Request):
        """主页 - 文件列表通过 AJAX 分页加载"""
        return templates.TemplateResponse(
            request=request, name="index.html", context={}
        )

    # ── API 路由 ────────────────────────────────────────────

    @app.get("/api/files")
    async def api_files(search: str = "", page: int = 1, page_size: int = 20):
        """获取回收站文件列表（JSON API，后端分页）"""
        # 使用后端分页查询，不再全量加载
        page_files, total = list_recycled_files_paged(
            config, page=page, page_size=page_size, search=search
        )

        total_pages = max(1, (total + page_size - 1) // page_size)
        actual_page = max(1, min(page, total_pages))

        return {
            "total": total,
            "page": actual_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "files": [
                {
                    "relative_path": f.get("relative_path", ""),
                    "recycle_path": f.get("recycle_path", ""),
                    "deletion_time": f.get("deletion_time_str", ""),
                    "deletion_ts": f.get("deletion_time", 0),
                    "file_size": f.get("file_size", 0),
                    "file_size_fmt": _format_size(f.get("file_size", 0)),
                    "deletion_time_fmt": _format_time(f.get("deletion_time", 0)),
                    "ago_fmt": _seconds_ago(f.get("deletion_time", 0)),
                    "exists": f.get("exists", False),
                    "is_directory": f.get("is_directory", False),
                }
                for f in page_files
            ],
        }

    @app.post("/api/restore")
    async def api_restore(request: Request):
        """恢复文件"""
        try:
            data = await request.json()
        except Exception:
            data = {}
        recycle_path = data.get("path", "")

        if not recycle_path:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "请指定要恢复的文件"},
            )

        # recycle_path 可能是正斜杠（前端传来），统一转为系统路径分隔符
        recycle_path = recycle_path.replace("/", os.sep).replace("\\", os.sep)

        # recycle_path 是绝对路径，转为相对路径（跨盘符时 relpath 会报错）
        try:
            rel_path = os.path.relpath(recycle_path, config.recycle_dir)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "非法路径"},
            )

        # 防止路径穿越：相对路径不允许逃逸出回收站目录
        if rel_path.startswith("..") or os.path.isabs(rel_path):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "非法路径"},
            )

        # 二次校验：解析后的完整路径必须仍在回收站内
        recycle_root = os.path.normcase(os.path.normpath(config.recycle_dir))
        full_path = os.path.normcase(
            os.path.normpath(os.path.join(config.recycle_dir, rel_path))
        )
        if not full_path.startswith(recycle_root + os.sep):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "非法路径"},
            )

        result = restore_from_recycle(rel_path, config, logger)

        if result:
            _invalidate_stats_cache()
            return {"success": True, "restored_to": result}
        else:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "恢复失败"},
            )

    @app.post("/api/empty")
    async def api_empty():
        """清空回收站"""
        count = empty_recycle(config, logger)
        _invalidate_stats_cache()
        return {"success": True, "cleaned": count}

    @app.get("/api/stats")
    async def api_stats():
        """
        获取统计信息。
        优化：使用数据库聚合替代 os.walk，结果缓存 60 秒。
        """
        now = time.time()

        # 检查缓存
        with _stats_lock:
            if (_stats_cache["value"] is not None
                    and now - _stats_cache["timestamp"] < _STATS_CACHE_TTL):
                return _stats_cache["value"]

        # 从数据库获取统计（毫秒级），不再 os.walk
        db = get_db()
        if db is not None:
            try:
                recycled_count = db.count_recycle_meta()
                recycled_total_size = db.sum_recycle_size()
                backup_size = db.sum_backup_size()

                # 回收站目录实际大小仍需遍历（但频率低，可接受）
                # 优先使用数据库记录的 file_size 总和
                recycle_size = recycled_total_size

                result = {
                    "recycled_count": recycled_count,
                    "recycled_total_size": recycled_total_size,
                    "recycled_formatted": _format_size(recycled_total_size),
                    "backup_size": backup_size,
                    "backup_formatted": _format_size(backup_size),
                    "recycle_dir_size": recycle_size,
                    "recycle_formatted": _format_size(recycle_size),
                }

                with _stats_lock:
                    _stats_cache["value"] = result
                    _stats_cache["timestamp"] = now

                return result
            except Exception:
                pass  # 回退到文件遍历方式

        # 回退：文件遍历方式（无数据库时）
        files = list_recycled_files(config)
        total_size = sum(f.get("file_size", 0) for f in files)
        backup_size = 0
        recycle_size = 0

        if os.path.exists(config.backup_dir):
            for root, dirs, files_in_dir in os.walk(config.backup_dir):
                for fn in files_in_dir:
                    try:
                        backup_size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass

        if os.path.exists(config.recycle_dir):
            for root, dirs, files_in_dir in os.walk(config.recycle_dir):
                for fn in files_in_dir:
                    try:
                        recycle_size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass

        result = {
            "recycled_count": len(files),
            "recycled_total_size": total_size,
            "recycled_formatted": _format_size(total_size),
            "backup_size": backup_size,
            "backup_formatted": _format_size(backup_size),
            "recycle_dir_size": recycle_size,
            "recycle_formatted": _format_size(recycle_size),
        }

        with _stats_lock:
            _stats_cache["value"] = result
            _stats_cache["timestamp"] = now

        return result

    return app


def start_web(config: Config, logger):
    """启动 Web 管理界面"""
    if not config.web.enabled:
        logger.info("Web 界面已禁用")
        return None

    app = create_app(config, logger)
    logger.info(f"Web 管理界面启动: http://{config.web.host}:{config.web.port}")

    import uvicorn
    from threading import Thread

    thread = Thread(
        target=lambda: uvicorn.run(
            app,
            host=config.web.host,
            port=config.web.port,
            log_level="warning",
        ),
        daemon=True,
        name="WebUI",
    )
    thread.start()
    return thread
