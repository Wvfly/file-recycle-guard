"""
Web 管理界面 - FastAPI 应用
提供浏览、搜索、恢复已删除文件的功能
"""

import os
import time
import datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from core.config import Config
from core.recycler import (
    list_recycled_files, restore_from_recycle, empty_recycle
)


def create_app(config: Config, logger) -> FastAPI:
    app = FastAPI(title="文件回收站管理")

    # 模板目录
    templates = Jinja2Templates(
        directory=os.path.join(os.path.dirname(__file__), "templates")
    )

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

    # ── 页面路由 ────────────────────────────────────────────

    @app.get("/")
    async def index(request: Request):
        files = list_recycled_files(config)
        total_size = sum(f.get("file_size", 0) for f in files)
        # 预处理文件数据，在 Python 端格式化，避免向模板传递 callable
        for f in files:
            f["file_size_fmt"] = _format_size(f.get("file_size", 0))
            f["deletion_time_fmt"] = _format_time(f.get("deletion_time", 0))
            f["ago_fmt"] = _seconds_ago(f.get("deletion_time", 0))
        context = {
            "files": files,
            "total_count": len(files),
            "total_size": _format_size(total_size),
        }
        return templates.TemplateResponse(
            request=request, name="index.html", context=context
        )

    # ── API 路由 ────────────────────────────────────────────

    @app.get("/api/files")
    async def api_files(search: str = ""):
        """获取回收站文件列表（JSON API）"""
        search_lower = search.lower()
        files = list_recycled_files(config)

        if search_lower:
            files = [
                f for f in files
                if search_lower in f.get("relative_path", "").lower()
                or search_lower in f.get("original_path", "").lower()
            ]

        return {
            "total": len(files),
            "files": [
                {
                    "relative_path": f.get("relative_path", ""),
                    "deletion_time": f.get("deletion_time_str", ""),
                    "deletion_ts": f.get("deletion_time", 0),
                    "file_size": f.get("file_size", 0),
                    "exists": f.get("exists", False),
                    "is_directory": f.get("is_directory", False),
                    "ago": _seconds_ago(f.get("deletion_time", 0)),
                }
                for f in files
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
        return {"success": True, "cleaned": count}

    @app.get("/api/stats")
    async def api_stats():
        """获取统计信息"""
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

        return {
            "recycled_count": len(files),
            "recycled_total_size": total_size,
            "recycled_formatted": _format_size(total_size),
            "backup_size": backup_size,
            "backup_formatted": _format_size(backup_size),
            "recycle_dir_size": recycle_size,
            "recycle_formatted": _format_size(recycle_size),
        }

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
