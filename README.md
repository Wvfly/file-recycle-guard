<div align="center">

# 🗑️ 文件回收站守护程序

### File Recycle Guard

**保护共享文件夹，防止文件被远程误删或恶意删除**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010/11/Server-0078D4?logo=windows)]()
[![FastAPI](https://img.shields.io/badge/Web-FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![MySQL](https://img.shields.io/badge/DB-MySQL%205.7+-4479A1?logo=mysql&logoColor=white)]()

</div>

---

## 📖 简介

在企业环境中，共享文件夹常被多人通过 SMB 网络共享访问。一旦有人误按 Delete 键，文件将直接从共享中消失，难以恢复。

**File Recycle Guard** 在后台默默守护你的共享文件夹：

- 文件被删除时，自动拦截并备份
- 文件不会真正消失，而是进入「回收站」
- 通过 Web 界面一键恢复，告别数据丢失焦虑

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 💽 **USN Journal 检测** | 基于 NTFS USN Journal 内核级零遗漏检测，SQLite checkpoint 断线追补，gap/reset 自动一致性修复 |
| 📡 **实时监控** | watchdog 低延迟加速器，USN 不可用时自动回退为主通道 |
| 🔄 **增量分片同步** | IncrementalScanner 主动扫描兜底，SQLite 持久化缓存 + 三层缓存架构，支持亿级文件 |
| 💾 **备份镜像** | 实时同步文件到备份目录，per-file 锁池并行备份，SHA256 去重 |
| 🗑️ **回收站** | 被删除文件自动移入回收站，支持按保留天数自动清理 |
| 🔁 **一键恢复** | Web 界面浏览、搜索、恢复已删除文件，后端分页查询 |
| 🌐 **Web 管理** | 暗色主题 Web UI，后端分页 + 统计聚合 + 缓存，支持文件浏览、恢复、清空 |
| 🔒 **访问认证** | 支持 HTTP Basic 认证，防止未授权操作 |
| 🗄️ **MySQL 存储** | 元信息存入 MySQL，路径哈希索引 + 批量 upsert + 统计聚合，高效可靠 |
| 🪟 **Windows 服务** | 支持注册为 Windows 服务，开机自启、后台运行 |
| 📦 **单文件打包** | Nuitka 编译为单个 exe，无需 Python 环境即可运行 |

## 🏗️ 架构设计

### 系统总览

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                          👥  用 户 层 (Clients)                            ║
║                                                                            ║
║      👤 用户 A          👤 用户 B          👤 用户 C                       ║
║      ┌────────┐         ┌────────┐         ┌────────┐                      ║
║      │ Win10  │         │ Win11  │         │ macOS  │                      ║
║      └───┬────┘         └───┬────┘         └───┬────┘                      ║
╚══════════╪══════════════════╪══════════════════╪═════════════════════════════╝
           │    SMB/CIFS 协议  │                  │
           └──────────────────┼──────────────────┘
                              ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                    📁 共享文件夹 (SMB Server)                                ║
║                    E:\share  ─  被监控目录                                   ║
║                                                                            ║
║   📄 report.docx    📊 data.xlsx    📁 projects    🖼️ design.psd           ║
╚══════════════════════════╦═══════════════════════════╦═══════════════════════╝
                           ║                           ║
              ┌────────────╨────────────┐  ┌───────────╨───────────┐
              │  🅰 通道 A ─ 实时检测    │  │  🅱 通道 B ─ 兜底扫描  │
              │                         │  │                       │
              │  💽 USN Journal 主通道   │  │  🔄 IncrementalScanner│
              │  ─────────────────────  │  │  ────────────────────  │
              │  ✦ NTFS 内核级记录      │  │  ✦ SQLite 持久化缓存   │
              │  ✦ 零事件遗漏           │  │  ✦ 目录树 mtime 分层   │
              │  ✦ checkpoint 断线追补  │  │  ✦ 内存 LRU 热缓存     │
              │  ✦ gap/reset 自动修复   │  │  ✦ 异步备份线程池      │
              │  📡 watchdog 低延迟加速 │  │  ✦ 时间预算分片扫描    │
              │  on_created   → 备份    │  │                       │
              │  on_modified  → 备份    │  │                       │
              │  on_deleted   → 延迟确认 │  │                       │
              │  on_moved     → 备份+回收│  │                       │
              │                         │  │                       │
              └────────────┬────────────┘  └───────────┬───────────┘
                           │                           │
                           └─────────┬─────────────────┘
                                     ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                🧠  RecycleGuard 核心引擎  (main.py)                         ║
║                                                                            ║
║  ╭──────────────────────────────────────────────────────────────────────╮  ║
║  │  💾 backup 模块 ─── 备份处理引擎                                     │  ║
║  │  ┌──────────────────────────────────────────────────────────────┐   │  ║
║  │  │  ✦ SHA256 哈希去重     ✦ mtime+size 快速路径                 │   │  ║
║  │  │  ✦ per-file 锁池(4096) ✦ 活跃备份跟踪机制                   │   │  ║
║  │  │  ✦ 竞态校验(dirty检测) ✦ 元数据攒批写入(MySQL)              │   │  ║
║  │  │  ✦ 优雅降级 (MySQL → 文件)  ✦ 流式初始备份(8线程并行)       │   │  ║
║  │  └──────────────────────────────────────────────────────────────┘   │  ║
║  ╰──────────────────────────────┬───────────────────────────────────────╯  ║
║                                 ▼                                          ║
║  ╭──────────────────────────────────────────────────────────────────────╮  ║
║  │  🗑️ recycler 模块 ─── 回收站管理器                                   │  ║
║  │  ┌──────────────────────────────────────────────────────────────┐   │  ║
║  │  │  ✦ 延迟 2s 删除确认    ✦ PendingDeleteQueue                 │   │  ║
║  │  │  ✦ 备份 → 移入回收站    ✦ 一键恢复 / 批量清空               │   │  ║
║  │  └──────────────────────────────────────────────────────────────┘   │  ║
║  ╰──────────────────────────────┬───────────────────────────────────────╯  ║
║                                 ▼                                          ║
║  ╭──────────────────────╮  ╭───────────────────────────────────────────╮  ║
║  │ 🧹 cleanup 模块     │  │ 🗄️ database 模块 ─── MySQL 元数据存储    │  ║
║  │ ─────────────────── │  │ ─────────────────────────────────────────  │  ║
║  │ ✦ 过期回收站清理    │  │ ✦ 连接池+断线重连    ✦ 优雅降级策略      │  ║
║  │ ✦ 孤立备份回收      │  │ ✦ backup_meta 表     ✦ recycle_meta 表    │  ║
║  │ ✦ 宽限期保护        │  │ ✦ 流式分批遍历       ✦ 批量upsert+统计聚合│  ║
║  ╰──────────────────────╯  ╰───────────────────────────────────────────╯  ║
║                                                                            ║
║  ╭──────────────────────────────────────────────────────────────────────╮  ║
║  │  🌐 Web 管理界面 ─── FastAPI + Uvicorn + Jinja2                     │  ║
║  │  ┌──────────────────────────────────────────────────────────────┐   │  ║
║  │  │  ✦ HTTP Basic Auth     ✦ 后端分页查询                        │   │  ║
║  │  │  ✦ 统计缓存 60s        ✦ 文件浏览 / 恢复 / 清空             │   │  ║
║  │  └──────────────────────────────────────────────────────────────┘   │  ║
║  ╰───────────────────────────────────────────────────────────────────────╯  ║
╚══════════════════╦═════════════════════════════╦════════════════════════════╝
                   │                             │
    ┌──────────────╨──────────┐   ┌──────────────╨──────────┐   ┌──────────╨──────────┐
    │  📦 backup_dir          │   │  🗑️ recycle_dir         │   │  🐬 MySQL            │
    │  E:\share_bak           │   │  E:\share_ryc           │   │  127.0.0.1:3306     │
    │  ─────────────────────  │   │  ─────────────────────  │   │  ──────────────────  │
    │  备份镜像（完整副本）    │   │  时间戳_文件名 结构     │   │  file_recycle_guard  │
    │  ✦ 实时同步             │   │  ✦ 按保留天数清理       │   │  ✦ backup_meta       │
    │  ✦ 哈希去重             │   │  ✦ 支持一键恢复         │   │  ✦ recycle_meta      │
    └─────────────────────────┘   └─────────────────────────┘   └────────────────────┘
```

### 文件生命周期

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                        📝 场景一：新建 / 修改文件                            ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

  👤 用户                📡 检测层                💾 处理层                📦 存储层
  ─────────            ──────────              ──────────              ──────────

  ┌──────┐
  │ 新建  │──→ on_created ──────→ backup_file() ──→ 📦 backup_dir/文件
  │ 文件  │      (watchdog)       ┌────────────┐      + 🗄️ MySQL 记录
  └──────┘                       │ ✦ mtime+size│
                                 │   快速检查   │
  ┌──────┐                       │ ✦ SHA256    │
  │ 修改  │──→ on_modified ─────→│   哈希比对   │──→ 📦 backup_dir/文件 (覆盖)
  │ 文件  │      (watchdog)       └────────────┘      + 🗄️ MySQL 更新
  └──────┘


┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                   🗑️ 场景二：删除文件（延迟确认机制）                         ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

  👤 用户                📡 检测层                💾 处理层                📦 存储层
  ─────────            ──────────              ──────────              ──────────

  ┌──────┐              ┌──────────────────┐
  │ 删除  │──→ on_deleted│  ⏱️ 待确认队列    │
  │ 文件  │              │  等待 2 秒...     │
  └──────┘              └────────┬─────────┘
                                 │
                    ┌────────────┴────────────┐
                    │  文件是否被重建？         │
                    └────┬───────────────┬────┘
                    是 ✅│               │否 ❌
                         ▼               ▼
              ┌──────────────┐  ┌──────────────────┐
              │ 🔄 取消删除   │  │ 🗑️ 确认删除       │
              │ 触发备份      │  │ move_to_recycle() │
              │ (Office保存)  │  │                  │
              └──────────────┘  │ 📦 backup_dir 中  │
                                │   文件移入         │
                                │ 🗑️ recycle_dir/   │
                                │   时间戳_文件名    │
                                │                  │
                                │ + 🗄️ MySQL 记录   │
                                └──────────────────┘


┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                🔄 场景三：增量分片同步（弥补 SMB 遗漏）                       ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

  🔄 IncrementalScanner    💾 缓存层                 💾 处理层                📦 存储层
  ──────────────────      ──────────              ──────────              ──────────

  ┌──────────────────┐
  │ 游标分页获取目录  │──→ 目录 mtime 比对 ──→ 未变化 ✅ ──→ ⏭️ 跳过整棵子树
  │ (SQLite dir_tree) │    (层次化扫描)
  └──────────────────┘      变化 ❌
                             │
                             ▼
                      ┌──────────────────┐
                      │ os.scandir() 扫描 │
                      └────────┬─────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌──────────┐   ┌──────────┐   ┌──────────────┐
        │ 内存 LRU  │──→│ SQLite   │──→│ 提交备份线程池 │
        │ 热缓存命中│   │ 持久缓存  │   │ (8 workers)   │
        │ → 跳过    │   │ 命中→跳过│   │ → backup_file │
        └──────────┘   └──────────┘   └───────┬───────┘
                                              │
                                              ▼
                                       📦 backup_dir + 🗄️ MySQL
                                       (攒批写入元数据)


┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                        🧹 场景四：定期清理                                    ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

  🧹 CleanupThread       💾 处理层                📦 存储层
  ────────────          ──────────              ──────────

  ┌──────────────┐
  │ 每 3600 秒   │──→ 过期回收站文件 ──────────→ 🗑️ 直接删除 + 🗄️ 清除记录
  │ 执行清理      │
  │              │──→ 孤立备份文件 ────────────→ 📦 移入回收站（安全优先）
  │ ✦ 宽限期保护  │     (源文件已不存在)
  └──────────────┘
```

### USN 变更检测流水线

USN Journal 模式下，事件经过「读取 → 标准化 → 合并 → 执行」四层处理，并通过 SQLite 持久化队列保证不丢事件：

```
USN Journal (NTFS 内核)          watchdog (低延迟加速器)
        │                                │
        ▼                                ▼
   UsnJournalReader               on_created / on_modified
   (轮询 + checkpoint)            on_deleted / on_moved
        │                                │
        └──────────────┬─────────────────┘
                       ▼
   ┌───────────────────────────────────────────────┐
   │  EventNormalizer ── 标准化为统一事件格式        │
   │  EventCoalescer  ── 合并同文件重复事件          │
   │  ProtectionEngine ── 消费事件并执行操作         │
   │    ├── CREATE / MODIFY ──→ backup_file()       │
   │    ├── DELETE          ──→ move_to_recycle()   │
   │    └── RENAME          ──→ 更新备份路径         │
   └───────────────────────────────────────────────┘
                       │
                       ▼
        SQLite fs_event（durable queue）
        checkpoint 仅在事件持久化后推进
        （崩溃恢复：PROCESSING → PENDING 重放）

   Reconciler ── 一致性修复器
     ├── 首次启动全量扫描（build full snapshot）
     ├── journal gap / reset ──→ 触发增量重扫
     └── 周期性一致性检查（修正遗漏事件）
```

### 线程模型

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                     🧠  RecycleGuard 进程  (main.exe)                       ║
║                                                                            ║
║  ┌─────────────────────────┐   ┌──────────────────────────────────────┐   ║
║  │ 🧵 MainThread           │   │ 🧵 PendingDeleteWorker               │   ║
║  │ ─────────────────────── │   │ ──────────────────────────────────── │   ║
║  │ ✦ 加载 config.yaml      │   │ ✦ 待确认删除队列 (deque)             │   ║
║  │ ✦ 初始化数据库连接       │   │ ✦ 2s 延迟等待                        │   ║
║  │ ✦ 启动所有子线程         │   │ ✦ 文件重建 → 取消，触发备份           │   ║
║  │ ✦ 信号处理 SIGINT/TERM  │   │ ✦ 文件消失 → 确认，move_to_recycle() │   ║
║  │ ✦ PID 文件管理          │   │                                      │   ║
║  └─────────────────────────┘   └──────────────────────────────────────┘   ║
║                                                                            ║
║  ┌─────────────────────────┐   ┌──────────────────────────────────────┐   ║
║  │ 🧵 UsnMonitor           │   │ 🧵 ProtectionEngine Workers          │   ║
║  │ ─────────────────────── │   │ ──────────────────────────────────── │   ║
║  │ ✦ 轮询 NTFS USN Journal │   │ ✦ 消费合并后的标准化事件 (4 workers) │   ║
║  │ ✦ 事件标准化 + 去重合并 │   │ ✦ CREATE / MODIFY → 备份            │   ║
║  │ ✦ checkpoint 断线追补   │   │ ✦ DELETE → 移入回收站               │   ║
║  │ ✦ gap/reset → 触发修复  │   │ ✦ RENAME → 更新备份路径             │   ║
║  │ ✦ fs_event 持久化队列   │   │ ✦ 操作完成 → 推进 checkpoint         │   ║
║  └─────────────────────────┘   └──────────────────────────────────────┘   ║
║                                                                            ║
║  ┌─────────────────────────┐   ┌──────────────────────────────────────┐   ║
║  │ 🧵 Reconciler           │   │ 🧵 watchdog Observer (加速器)         │   ║
║  │ ─────────────────────── │   │ ──────────────────────────────────── │   ║
║  │ ✦ 首次启动全量扫描      │   │ ✦ USN 不可用时回退为主通道           │   ║
║  │ ✦ journal gap 恢复      │   │ ✦ 事件去重 (deque 500)               │   ║
║  │ ✦ 周期性一致性检查      │   │ ✦ created/modified → backup_file()   │   ║
║  │                         │   │ ✦ deleted → 延迟队列 (2s 确认)        │   ║
║  └─────────────────────────┘   └──────────────────────────────────────┘   ║
║                                                                            ║
║  ┌─────────────────────────┐   ┌──────────────────────────────────────┐   ║
║  │ 🧵 SyncThread (增量扫描)│   │ 🧵 CleanupThread                     │   ║
║  │ ─────────────────────── │   │ ──────────────────────────────────── │   ║
║  │ ✦ 游标分页遍历目录树    │   │ ✦ 每 3600s 执行                      │   ║
║  │ ✦ 三层缓存: LRU→SQLite  │   │ ✦ 过期回收站清理                     │   ║
║  │ ✦ 目录 mtime 层次化跳过 │   │ ✦ 孤立备份文件回收                   │   ║
║  │ ✦ 异步备份线程池 (8)    │   │ ✦ 宽限期保护机制                     │   ║
║  │ ✦ 时间预算分片扫描      │   │                                       │   ║
║  └─────────────────────────┘   └──────────────────────────────────────┘   ║
║                                                                            ║
║  📡 Web (Uvicorn) 线程 ── FastAPI + Jinja2 暗色主题 · HTTP Basic Auth     ║
║     ✦ REST API (JSON) · 后端分页 · 统计缓存 60s · 浏览/搜索/恢复/清空     ║
║                                                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  🔒 并发安全机制                                                            ║
║  ┌──────────────────────────────────────────────────────────────────────┐   ║
║  │  🔐 _file_locks[4096]  per-file 锁池，路径哈希选槽，多文件并行备份   ║
║  │  🔐 _makedirs_lock     目录创建锁，防止 os.makedirs 并发冲突         ║
║  │  🔐 _active_backups    活跃备份跟踪，供 watcher 删除前等待           ║
║  │  🔐 _pending_lock      删除队列锁，保护待确认删除列表                ║
║  │  🔐 _db_lock           数据库初始化锁，保证单例                      ║
║  │  📦 _meta_buffer       MySQL 元数据攒批写入缓冲(50条/批)             ║
║  │  📦 _pcache_buffer     SQLite 缓存攒批写入缓冲(100条/批)             ║
║  └──────────────────────────────────────────────────────────────────────┘   ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 存储层设计

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                      💾 存储层 (Storage Layer)                              ║
║                                                                            ║
║  ┌────────────────────────────────────────────────────────────────────┐    ║
║  │  🐬 MySQL ─── 主存储 (优先读写)                                    │    ║
║  │                                                                    │    ║
║  │  ┌──────────────────────────┐  ┌──────────────────────────────┐   │    ║
║  │  │ 📋 backup_meta 表        │  │ 📋 recycle_meta 表            │   │    ║
║  │  │ ──────────────────────── │  │ ────────────────────────────  │   │    ║
║  │  │ 🔑 path_hash   (UK)     │  │ 🔑 recycle_path    (UK)      │   │    ║
║  │  │    rel_path              │  │    original_path              │   │    ║
║  │  │    watch_root            │  │    relative_path              │   │    ║
║  │  │    file_hash   (SHA256)  │  │    is_directory   (TINYINT)   │   │    ║
║  │  │    file_size   (BIGINT)  │  │    deletion_time  (DOUBLE)    │   │    ║
║  │  │    mtime       (DOUBLE)  │  │    file_size      (BIGINT)    │   │    ║
║  │  │    source_path           │  │    file_hash      (VARCHAR)   │   │    ║
║  │  │    backup_time           │  │    original_mtime  (DOUBLE)   │   │    ║
║  │  └──────────────────────────┘  └──────────────────────────────┘   │    ║
║  └────────────────────────────────────────────────────────────────────┘    ║
║                                ▲                                           ║
║                                │  优先读写                                  ║
║                                │                                           ║
║  ┌────────────────────────────────────────────────────────────────────┐    ║
║  │  📂 文件系统 ─── 回退存储 / 兼容旧数据                              │    ║
║  │                                                                    │    ║
║  │  📄 backup_dir/文件.meta              ← 备份元信息 (旧格式)         │    ║
║  │  📄 recycle_dir/文件.recycle.json     ← 回收站元信息 (旧格式)       │    ║
║  │                                                                    │    ║
║  │  ⚠️ 数据库不可用时自动降级为文件方式，核心功能不受影响               │    ║
║  └────────────────────────────────────────────────────────────────────┘    ║
║                                                                            ║
║  ┌────────────────────────────────────────────────────────────────────┐    ║
║  │  🔄 优雅降级策略                                                    │    ║
║  │                                                                    │    ║
║  │  ✅ MySQL 可用    →  读写数据库（高性能、事务安全）                  │    ║
║  │  ⚠️ MySQL 不可用  →  自动切换文件方式（.meta / .recycle.json）      │    ║
║  │                                                                    │    ║
║  │  💡 两种模式无缝切换，业务层代码无需感知                             │    ║
║  └────────────────────────────────────────────────────────────────────┘    ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 关键设计决策

| 决策 | 方案 | 原因 |
|------|------|------|
| **删除确认机制** | 延迟 2 秒确认 | Office 等程序保存文件时会先删后建，避免误判为删除 |
| **三通道检测** | USN Journal + watchdog + 增量分片扫描 | USN 内核级零遗漏（主通道），watchdog 低延迟加速，增量扫描兜底一致性 |
| **USN checkpoint** | SQLite 持久化 + 事件先落库再推进 | 崩溃后可断线追补，fs_event 持久化队列保证事件不丢失 |
| **事件流水线** | Normalizer → Coalescer → ProtectionEngine | 多来源事件统一标准化，同文件重复事件合并后再执行，避免重复备份 |
| **per-file 锁池** | 4096 个锁 + 路径哈希选槽 | 替代全局锁，允许多文件并行备份，大幅提升 SMB 吞吐 |
| **三层缓存架构** | 内存 LRU → SQLite → os.stat | SQLite 持久化避免亿级文件 OOM，内存 LRU 加速高频访问 |
| **目录树分层扫描** | 目录 mtime + 游标分页 | 未变化的整个子树跳过，避免无效 stat 调用 |
| **哈希去重备份** | SHA256 比对 | 避免文件未变化时重复复制，减少 I/O 开销 |
| **竞态校验** | 备份后 re-stat 源文件 | 检测备份期间并发修改(dirty)或源文件删除(source_gone) |
| **元数据攒批写入** | MySQL 50条/批，SQLite 100条/批 | 减少数据库交互次数，提升亿级场景吞吐 |
| **优雅降级** | MySQL → 文件回退 | 数据库不可用时自动切换，保证核心功能持续运行 |
| **宽限期清理** | 首次发现缺失起算 | 避免备份尚未完成就被清理线程误删 |
| **路径安全** | 防路径穿越 + 二次校验 | Web API 恢复时严格校验路径，防止逃逸出回收站目录 |
| **单文件打包** | Nuitka onefile | 部署无需 Python 环境，config.yaml 放 exe 旁边可编辑 |

## 🚀 快速开始

### 环境要求

| 组件 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 或 Windows Server |
| Python | 3.10+（仅开发/构建时需要） |
| MySQL | 5.7+ / 8.0+ |
| 权限 | 管理员权限（USN Journal 模式必需，读取 NTFS 变更日志） |

### 安装步骤

```bash
# 1. 克隆项目
git clone <repo-url>
cd file-recycle-guard

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 创建 MySQL 数据库
mysql -u root -p -e "CREATE DATABASE file_recycle_guard CHARACTER SET utf8mb4;"
```

### 配置

编辑 `config.yaml`，按需修改以下关键配置：

```yaml
# 要监控的共享文件夹路径（支持多个）
watch_paths:
  - "E:\\share"
  - "F:\\public"

# 备份镜像目录（请确保有足够磁盘空间）
backup_dir: "E:\\share_bak"

# 回收站目录
recycle_dir: "E:\\share_ryc"

# 回收站保留天数（超过自动清理）
retention_days: 30

# USN Journal 检测（推荐，需管理员权限运行）
usn:
  enabled: true            # 启用后 USN 为主通道，watchdog 为加速器
  poll_interval: 1.0       # USN 轮询间隔（秒）
  state_dir: ".usn_state"  # checkpoint 状态存储目录
  max_records_per_read: 10000  # 单次轮询最大读取记录数
  buffer_size_mb: 4        # USN 读取缓冲区大小（MB），高并发建议 4-8
  protection_workers: 4    # 保护引擎 worker 线程数

# Web 管理界面（强烈建议设置密码）
web:
  enabled: true
  port: 8088
  username: "admin"
  password: "your_secure_password"

# MySQL 数据库
database:
  host: "127.0.0.1"
  port: 3306
  user: "root"
  password: "123456"
  database: "file_recycle_guard"
```

### 启动运行

```bash
# 方式一：使用启动脚本（推荐，自动检查依赖）
#   普通模式：      start.bat
#   USN 模式：      start_admin.bat / start_admin.ps1（自动提权管理员运行）
# 方式二：直接运行
python main.py start       # 启动守护程序（USN 模式需在管理员终端执行）
python main.py stop        # 停止守护程序
python main.py status      # 查看运行状态
python main.py web         # 仅启动 Web 界面（不监控）
```

启动后访问 `http://localhost:8088` 即可打开 Web 管理界面。

> **提示：** 启用 USN Journal 检测（`usn.enabled: true`）时必须**以管理员身份**运行程序，否则 USN 初始化失败会自动回退到 watchdog 模式（可运行 `start_admin.bat` 一键提权启动）。

## 📦 打包部署

将程序打包为单个 exe 文件，部署到目标服务器无需安装 Python。

```bash
# 安装 Nuitka 编译器
pip install nuitka

# 执行构建（也可双击 build.bat）
build.bat

# 构建产物
#   dist/main.exe      - 主程序（约 15 MB）
#   dist/config.yaml   - 配置文件（自动复制）
```

> **部署说明：** 将 `dist/` 目录整体复制到目标服务器，`config.yaml` 与 `main.exe` 放在同一目录下，修改配置后运行 `main.exe start` 即可。

## 🖥️ Web 管理界面

暗色主题的现代 Web 界面，功能包括：

- **统计概览** - 回收站文件数、总大小、备份文件数一目了然（数据库聚合 + 60s 缓存）
- **文件列表** - 后端分页查询，按删除时间倒序展示，显示文件名、原路径、大小、删除时间
- **搜索过滤** - 支持按文件名或路径实时搜索（后端 LIKE 查询）
- **一键恢复** - 点击恢复按钮，文件自动回到原始位置
- **清空回收站** - 支持一键清空（带二次确认）
- **自动刷新** - 页面定时自动刷新，保持数据最新

## 📁 项目结构

```
file-recycle-guard/
├── core/                       # 核心模块
│   ├── config.py               # 配置加载与路径解析
│   ├── watcher.py              # 文件监控（watchdog 加速器）
│   ├── backup.py               # 备份逻辑（per-file锁池 + 竞态校验 + 攒批写入）
│   ├── recycler.py             # 回收站管理（列表/恢复/清空/数据库对账）
│   ├── cleanup.py              # 备份镜像定期清理
│   ├── sync.py                 # 增量分片同步（SQLite缓存 + 目录树分层 + 异步备份）
│   ├── logger.py               # 日志模块（按天轮转）
│   ├── database.py             # MySQL 数据库（连接池 + 路径哈希 + 批量upsert）
│   ├── usn/                    # USN Journal 模块
│   │   ├── record.py           # USN 记录结构体 / 卷句柄 / 路径解析
│   │   ├── reader.py           # UsnJournalReader 读取器 + 事件模型
│   │   ├── journal.py          # Journal 信息查询 / 健康度计算
│   │   ├── checkpoint.py       # SQLite checkpoint 存储（断线追补）
│   │   ├── path_resolver.py    # FRN 路径缓存（快路径映射）
│   │   ├── event_store.py      # fs_event 持久化队列（SQLite）
│   │   └── __init__.py         # UsnJournalMonitor 多卷监控器
│   ├── detector/               # 变更检测层
│   │   ├── usn_detector.py     # UsnDetector（整合监控/标准化/合并/执行）
│   │   └── reconciler.py       # Reconciler 一致性修复器（全量扫描/gap恢复）
│   └── engine/                 # 事件处理引擎
│       ├── event_normalizer.py # 多来源事件统一标准化
│       ├── event_coalescer.py  # 同文件重复事件合并
│       └── protection_engine.py# 消费事件执行备份/删除/重命名
├── web/                        # Web 管理界面
│   ├── __init__.py             # FastAPI 应用（路由/认证/分页API）
│   └── templates/
│       └── index.html          # 暗色主题管理页面
├── main.py                     # 程序入口（start/stop/status/web）
├── service.py                  # Windows 服务入口（可选）
├── config.yaml                 # 配置文件
├── requirements.txt            # Python 依赖
├── start.bat                   # 快速启动脚本
├── start_admin.bat             # 管理员提权启动脚本（USN 模式）
├── build.bat                   # Nuitka 打包脚本
└── _build_nuitka.py            # Nuitka 打包脚本（Python 版）
```

## ⚙️ 配置参考

<details>
<summary><b>点击展开完整配置说明</b></summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `watch_paths` | 监控的共享文件夹路径列表 | `[]` |
| `backup_dir` | 备份镜像目录 | `backup_mirror` |
| `recycle_dir` | 回收站目录 | `recycle_bin` |
| `retention_days` | 回收站保留天数 | `30` |
| `exclude_patterns` | 排除的文件模式（支持通配符） | `~$*, *.tmp, *.lock, Thumbs.db` |
| `exclude_dirs` | 排除的文件夹名称 | `$RECYCLE.BIN, System Volume Information` |
| `log.level` | 日志级别（DEBUG/INFO/WARNING/ERROR） | `INFO` |
| `log.file` | 日志文件路径 | `logs/recycle_guard.log` |
| `log.max_days` | 日志保留天数 | `90` |
| `web.enabled` | 是否启用 Web 界面 | `true` |
| `web.host` | Web 监听地址 | `0.0.0.0` |
| `web.port` | Web 端口 | `8088` |
| `web.username` | Web 认证用户名（留空则无需认证） | `None` |
| `web.password` | Web 认证密码 | `None` |
| `mirror_cleanup.enabled` | 是否启用备份清理 | `true` |
| `mirror_cleanup.interval` | 备份清理间隔（秒） | `3600` |
| `mirror_cleanup.grace_period` | 清理宽限期（秒） | `300` |
| `sync.enabled` | 是否启用定期同步 | `true` |
| `sync.interval` | 同步扫描间隔（秒） | `30` |
| `sync.cache_dir` | 同步缓存目录（留空自动推导为备份目录父目录） | `自动推导` |
| `sync.backup_workers` | 增量同步 + 初始全量备份线程数 | `8` |
| `usn.enabled` | 是否启用 USN Journal 检测（需管理员权限） | `false` |
| `usn.poll_interval` | USN 轮询间隔（秒） | `1.0` |
| `usn.state_dir` | checkpoint 状态存储目录 | `.usn_state` |
| `usn.max_records_per_read` | 单次轮询最大读取记录数 | `10000` |
| `usn.buffer_size_mb` | USN 读取缓冲区大小（MB） | `4` |
| `usn.protection_workers` | 保护引擎 worker 线程数 | `4` |
| `database.host` | MySQL 主机 | `127.0.0.1` |
| `database.port` | MySQL 端口 | `3306` |
| `database.user` | MySQL 用户名 | `root` |
| `database.password` | MySQL 密码 | `123456` |
| `database.database` | MySQL 数据库名 | `file_recycle_guard` |

</details>

## ❓ 常见问题

<details>
<summary><b>Q: USN Journal 检测需要管理员权限吗？</b></summary>

<p>需要。读取 NTFS USN Journal 需要管理员权限，请使用 <code>start_admin.bat</code> 或右键「以管理员身份运行」。若权限不足，程序会自动回退到 watchdog 模式并记录日志，核心功能不受影响。</p>
</details>

<details>
<summary><b>Q: USN Journal、watchdog、增量扫描是什么关系？</b></summary>

<p>三者构成三通道检测：<b>USN Journal</b> 由 NTFS 内核维护变更日志，零事件遗漏，是主通道；<b>watchdog</b> 提供低延迟响应，是加速器；<b>IncrementalScanner</b> 主动扫描目录树，是最终一致性兜底。USN 不可用时自动回退 watchdog 模式。</p>
</details>

<details>
<summary><b>Q: watchdog 能检测到 SMB 网络共享的文件变更吗？</b></summary>

<p>watchdog 在 SMB 场景下存在检测不可靠的问题（某些客户端的删除操作不会触发通知）。因此程序内置了 <b>增量分片同步模块</b>（IncrementalScanner），通过 SQLite 持久化缓存 + 三层缓存架构主动扫描目录，弥补 watchdog 的遗漏。三者配合使用，确保变更不会漏检。</p>
</details>

<details>
<summary><b>Q: 打包后的 exe 找不到 config.yaml？</b></summary>

<p>请确保 <code>config.yaml</code> 与 <code>main.exe</code> 放在<b>同一目录</b>下。程序通过 exe 所在目录定位配置文件。</p>
</details>

<details>
<summary><b>Q: 构建 exe 时报 PermissionError？</b></summary>

<p>旧的 <code>main.exe</code> 可能正在运行，占用了文件。请先终止所有 main.exe 进程后再构建。<code>build.bat</code> 已自动处理此问题。</p>
</details>

<details>
<summary><b>Q: 数据库连接失败会影响使用吗？</b></summary>

<p>不会。程序采用优雅降级策略：数据库不可用时，自动切换为文件方式存储元信息（兼容模式），核心功能不受影响。</p>
</details>

<details>
<summary><b>Q: 如何注册为 Windows 服务？</b></summary>

<p>使用 <code>service.py</code> 可注册为 Windows 服务，实现开机自启和后台运行：</p>

```bash
# 安装服务
python service.py install

# 启动服务
python service.py start

# 停止服务
python service.py stop

# 卸载服务
python service.py remove
```
</details>

## 🔧 技术栈

| 组件 | 技术 |
|------|------|
| 文件监控 | USN Journal（NTFS 内核 API）+ [watchdog](https://github.com/gorakhargosh/watchdog) |
| Web 框架 | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| 模板引擎 | [Jinja2](https://jinja.palletsprojects.com/) |
| 数据库 | [PyMySQL](https://github.com/PyMySQL/PyMySQL) + MySQL |
| 持久化缓存 | SQLite (WAL 模式) + 内存 LRU |
| 配置文件 | [PyYAML](https://github.com/yaml/pyyaml) |
| 打包工具 | [Nuitka](https://nuitka.net/) (onefile 模式) |
| Windows 服务 | [pywin32](https://github.com/mhammond/pywin32) |

## 📄 License

MIT
