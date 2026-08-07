"""
回收站管理器 - 在检测到文件删除时，将备份文件移入回收站
"""

import os
import shutil
import json
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from .config import Config
from .database import get_db


def _get_relative_path(abs_path: str, watch_root: str) -> str:
    return os.path.relpath(abs_path, watch_root)


def _format_timestamp() -> str:
    """生成可读的时间戳字符串"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _make_unique_path(path: str) -> str:
    """若目标路径已存在（如同一秒内删除同名文件），追加序号避免覆盖"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{stem}_{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def move_to_recycle(abs_src_path: str, is_directory: bool,
                    config: Config, logger) -> Optional[str]:
    """
    将备份的文件/目录移到回收站。

    回收站结构：
      recycle_dir / 原始相对路径 / 删除时间戳_文件名

    返回回收站中的路径，失败返回 None。
    """
    watch_root = config.find_watch_root(abs_src_path)
    if watch_root is None:
        logger.warning(f"未找到包含 {abs_src_path} 的监控根目录，跳过回收")
        return None
    relative = _get_relative_path(abs_src_path, watch_root)
    timestamp = _format_timestamp()

    # 回收站目标路径
    parent_rel_dir = os.path.dirname(relative)
    item_name = os.path.basename(relative)

    recycle_parent = os.path.join(config.recycle_dir, parent_rel_dir)
    os.makedirs(recycle_parent, exist_ok=True)

    if is_directory:
        recycle_path = os.path.join(recycle_parent, f"{timestamp}_{item_name}")
    else:
        name, ext = os.path.splitext(item_name)
        recycle_path = os.path.join(recycle_parent, f"{timestamp}_{name}{ext}")

    # 防止同一秒内同名条目互相覆盖
    recycle_path = _make_unique_path(recycle_path)

    try:
        # 从备份镜像查找文件
        backup_path = os.path.join(config.backup_dir, relative)
        meta_path = backup_path + ".meta"

        file_size = 0
        file_hash = "unknown"
        mtime = 0.0

        # 读取元信息（优先从数据库）
        db = get_db()
        if db is not None:
            try:
                meta = db.get_backup_meta(watch_root, relative)
                if meta:
                    file_size = meta.get("file_size", 0)
                    file_hash = meta.get("file_hash", "unknown") or "unknown"
                    mtime = meta.get("mtime", 0.0)
            except Exception:
                pass
        # 回退：从 .meta 文件读取（兼容旧数据）
        if file_size == 0 and file_hash == "unknown" and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("size:"):
                            try:
                                file_size = int(line.split(":", 1)[1])
                            except ValueError:
                                pass
                        elif line.startswith("hash:"):
                            file_hash = line.split(":", 1)[1]
                        elif line.startswith("mtime:"):
                            try:
                                mtime = float(line.split(":", 1)[1])
                            except ValueError:
                                pass
            except IOError:
                pass

        if is_directory:
            # 对于目录，递归移动备份镜像中的整个目录
            if os.path.exists(backup_path) and os.path.isdir(backup_path):
                shutil.move(backup_path, recycle_path)
                logger.info(f"目录已移入回收站: {relative} -> {recycle_path}")
            else:
                # 备份中没有此目录，创建一个空的标记
                os.makedirs(recycle_path, exist_ok=True)
                logger.info(f"目录已记录到回收站（备份中无数据）: {relative}")
        else:
            # 对于文件，移动备份副本
            if os.path.exists(backup_path):
                shutil.move(backup_path, recycle_path)
                # 同时移动元信息文件（兼容旧数据）
                if os.path.exists(meta_path):
                    meta_dest = recycle_path + ".meta"
                    shutil.move(meta_path, meta_dest)
                logger.info(f"文件已移入回收站: {relative} -> {recycle_path}")
            else:
                logger.warning(f"备份中未找到文件 {relative}，可能从未被修改过")
                return None

        # 计算回收站相对路径
        recycle_rel_path = os.path.relpath(recycle_path, config.recycle_dir)

        # 写入回收站元信息（优先写入数据库）
        if db is not None:
            try:
                db.insert_recycle_meta(
                    recycle_path=recycle_rel_path,
                    original_path=abs_src_path,
                    relative_path=relative,
                    watch_root=watch_root,
                    is_directory=is_directory,
                    deletion_time=time.time(),
                    deletion_time_str=timestamp,
                    file_size=file_size,
                    file_hash=file_hash,
                    original_mtime=mtime,
                )
            except Exception as e:
                logger.error(f"写入回收站元信息到数据库失败: {e}")
                # 回退到文件方式
                _write_recycle_meta_file(recycle_path, abs_src_path, relative,
                                        watch_root, is_directory, timestamp,
                                        file_size, file_hash, mtime)
        else:
            # 回退：写入 .recycle.json 文件（兼容旧模式）
            _write_recycle_meta_file(recycle_path, abs_src_path, relative,
                                    watch_root, is_directory, timestamp,
                                    file_size, file_hash, mtime)

        return recycle_path

    except Exception as e:
        logger.error(f"移入回收站失败 {relative}: {e}")
        return None


def _write_recycle_meta_file(recycle_path: str, abs_src_path: str,
                             relative: str, watch_root: str,
                             is_directory: bool, timestamp: str,
                             file_size: int, file_hash: str, mtime: float):
    """写入回收站元信息到文件（兼容旧模式）"""
    recycle_meta = {
        "original_path": abs_src_path,
        "relative_path": relative,
        "watch_root": watch_root,
        "is_directory": is_directory,
        "deletion_time": time.time(),
        "deletion_time_str": timestamp,
        "file_size": file_size,
        "file_hash": file_hash,
        "original_mtime": mtime,
    }
    meta_file = recycle_path + ".recycle.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(recycle_meta, f, ensure_ascii=False, indent=2)


def restore_from_recycle(recycle_rel_path: str, config: Config,
                         logger) -> Optional[str]:
    """
    从回收站恢复文件到原始位置。

    recycle_rel_path: 回收站中文件的相对路径（相对于 recycle_dir）
    返回恢复到的目标路径，失败返回 None。
    """
    recycle_full = os.path.join(config.recycle_dir, recycle_rel_path)
    meta_file = recycle_full + ".recycle.json"

    if not os.path.exists(recycle_full):
        logger.error(f"回收站中文件不存在: {recycle_full}")
        return None

    # 读取元信息（优先从数据库）
    original_relative = None
    watch_root = None
    db = get_db()
    if db is not None:
        try:
            meta = db.get_recycle_meta(recycle_rel_path)
            if meta:
                original_relative = meta.get("relative_path")
                watch_root = meta.get("watch_root")
        except Exception:
            pass

    # 回退：从 .recycle.json 文件读取（兼容旧数据）
    if original_relative is None and os.path.exists(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
                original_relative = meta.get("relative_path")
                watch_root = meta.get("watch_root")
        except (json.JSONDecodeError, IOError):
            pass

    if original_relative is None:
        logger.error(f"无法读取回收站元信息: {recycle_full}")
        return None

    if not watch_root:
        # 兼容旧版本元信息（未记录 watch_root）
        watch_root = config.watch_paths[0] if config.watch_paths else None
        if watch_root is None:
            logger.error("未配置任何监控路径，无法恢复")
            return None

    target_path = os.path.join(watch_root, original_relative)

    try:
        # 确保目标目录存在
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)

        # 如果目标已存在，先备份
        if os.path.exists(target_path):
            backup_name = target_path + f".before_restore_{_format_timestamp()}"
            shutil.move(target_path, backup_name)
            logger.info(f"已存在的文件被重命名: {backup_name}")

        # 移动回原始位置
        shutil.move(recycle_full, target_path)

        # 清理元信息（数据库和文件）
        if db is not None:
            try:
                db.delete_recycle_meta(recycle_rel_path)
            except Exception:
                pass
        # 清理旧的 .recycle.json 文件（兼容旧数据）
        if os.path.exists(meta_file):
            os.remove(meta_file)
        meta_old = recycle_full + ".meta"
        if os.path.exists(meta_old):
            os.remove(meta_old)

        logger.info(f"文件已恢复: {recycle_rel_path} -> {target_path}")
        return target_path

    except Exception as e:
        logger.error(f"恢复文件失败: {e}")
        return None


def list_recycled_files(config: Config) -> List[Dict]:
    """列出回收站中所有已删除的文件（全量，兼容旧调用）
    注意：大量数据下应使用 list_recycled_files_paged()
    """
    results = []
    db = get_db()

    # 优先从数据库读取
    if db is not None:
        try:
            db_records = db.list_all_recycle_meta()
            for meta in db_records:
                recycle_path = meta.get("recycle_path", "")
                recycle_full = os.path.join(config.recycle_dir, recycle_path)
                exists = os.path.exists(recycle_full)
                entry = {
                    **meta,
                    "recycle_path": recycle_full.replace(os.sep, "/"),
                    "exists": exists,
                }
                if exists:
                    try:
                        entry["current_size"] = os.stat(recycle_full).st_size
                    except OSError:
                        pass
                results.append(entry)
            return results
        except Exception:
            pass  # 回退到文件方式

    # 回退：从 .recycle.json 文件读取（兼容旧数据）
    if not os.path.exists(config.recycle_dir):
        return results

    for root, dirs, files in os.walk(config.recycle_dir):
        for file_name in files:
            if file_name.endswith(".recycle.json"):
                meta_path = os.path.join(root, file_name)
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, IOError):
                    continue

                recycle_file = meta_path[:-len(".recycle.json")]
                exists = os.path.exists(recycle_file)
                entry = {
                    **meta,
                    "recycle_path": recycle_file.replace(os.sep, "/"),
                    "exists": exists,
                }
                if exists:
                    try:
                        entry["current_size"] = os.stat(recycle_file).st_size
                    except OSError:
                        pass
                results.append(entry)

    # 按删除时间倒序
    results.sort(key=lambda x: x.get("deletion_time", 0), reverse=True)
    return results


def list_recycled_files_paged(config: Config, page: int = 1,
                              page_size: int = 20,
                              search: str = "") -> Tuple[List[Dict], int]:
    """
    分页查询回收站文件列表（后端分页，避免全量加载）。

    返回 (files, total_count) 元组。
    files 中每条已包含 exists 和 current_size 信息。
    """
    db = get_db()

    if db is not None:
        try:
            rows, total = db.list_recycle_meta_paged(page, page_size, search)
            results = []
            for meta in rows:
                recycle_path = meta.get("recycle_path", "")
                recycle_full = os.path.join(config.recycle_dir, recycle_path)
                exists = os.path.exists(recycle_full)
                entry = {
                    **meta,
                    "recycle_path": recycle_full.replace(os.sep, "/"),
                    "exists": exists,
                }
                if exists:
                    try:
                        entry["current_size"] = os.stat(recycle_full).st_size
                    except OSError:
                        pass
                results.append(entry)
            return results, total
        except Exception:
            pass  # 回退到全量方式

    # 回退：全量加载后手动分页（兼容无数据库或旧数据）
    all_files = list_recycled_files(config)
    if search:
        search_lower = search.lower()
        all_files = [
            f for f in all_files
            if search_lower in f.get("relative_path", "").lower()
            or search_lower in f.get("original_path", "").lower()
        ]
    total = len(all_files)
    start = (page - 1) * page_size
    end = start + page_size
    return all_files[start:end], total


def cleanup_expired(config: Config, logger) -> int:
    """清理过期的回收站条目，返回清理数量"""
    if config.retention_days <= 0:
        return 0

    cutoff = time.time() - config.retention_days * 86400
    cleaned = 0
    db = get_db()

    # 优先从数据库清理
    if db is not None:
        try:
            expired_records = db.delete_expired_recycle_meta(cutoff)
            for meta in expired_records:
                recycle_path = meta.get("recycle_path", "")
                recycle_full = os.path.join(config.recycle_dir, recycle_path)
                try:
                    if os.path.exists(recycle_full):
                        if os.path.isdir(recycle_full):
                            shutil.rmtree(recycle_full)
                        else:
                            os.remove(recycle_full)
                    # 清理旧的 .meta 文件（兼容旧数据）
                    meta_old = recycle_full + ".meta"
                    if os.path.exists(meta_old):
                        os.remove(meta_old)
                    cleaned += 1
                    logger.debug(f"清理过期回收项: {meta.get('relative_path')}")
                except Exception as e:
                    logger.error(f"清理失败: {e}")
            if cleaned > 0:
                logger.info(f"已清理 {cleaned} 个过期回收项")
            return cleaned
        except Exception:
            pass  # 回退到文件方式

    # 回退：从文件清理（兼容旧数据）
    if not os.path.exists(config.recycle_dir):
        return 0

    for root, dirs, files in os.walk(config.recycle_dir, topdown=False):
        for file_name in files:
            if file_name.endswith(".recycle.json"):
                meta_path = os.path.join(root, file_name)
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, IOError):
                    continue

                if meta.get("deletion_time", 0) < cutoff:
                    recycle_file = meta_path[:-len(".recycle.json")]
                    try:
                        if os.path.exists(recycle_file):
                            if os.path.isdir(recycle_file):
                                shutil.rmtree(recycle_file)
                            else:
                                os.remove(recycle_file)
                        if os.path.exists(meta_path):
                            os.remove(meta_path)
                        meta_old = recycle_file + ".meta"
                        if os.path.exists(meta_old):
                            os.remove(meta_old)
                        cleaned += 1
                        logger.debug(f"清理过期回收项: {meta.get('relative_path')}")
                    except Exception as e:
                        logger.error(f"清理失败: {e}")

        # 删除空目录
        if root != config.recycle_dir:
            try:
                remaining = os.listdir(root)
                if not remaining:
                    os.rmdir(root)
            except OSError:
                pass

    if cleaned > 0:
        logger.info(f"已清理 {cleaned} 个过期回收项")
    return cleaned


def empty_recycle(config: Config, logger) -> int:
    """清空回收站，返回清理数量"""
    if not os.path.exists(config.recycle_dir):
        return 0

    count = 0
    try:
        for item in os.listdir(config.recycle_dir):
            item_path = os.path.join(config.recycle_dir, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
                count += 1
            except Exception as e:
                logger.error(f"清空回收站时出错: {e}")
    except Exception as e:
        logger.error(f"清空回收站失败: {e}")

    # 同时清空数据库中的回收站元信息
    db = get_db()
    if db is not None:
        try:
            db.delete_expired_recycle_meta(time.time() + 86400)  # 删除所有记录
        except Exception:
            pass

    logger.info(f"回收站已清空，共清理 {count} 个条目")
    return count
