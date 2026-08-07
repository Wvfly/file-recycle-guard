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
| 📡 **实时监控** | 基于 watchdog 监控文件变更（创建/修改/删除/移动） |
| 🔄 **定期同步** | 主动扫描监控目录，解决 SMB 网络共享变更检测不可靠的问题 |
| 💾 **备份镜像** | 实时同步文件到备份目录，保留完整历史版本 |
| 🗑️ **回收站** | 被删除文件自动移入回收站，支持按保留天数自动清理 |
| 🔁 **一键恢复** | Web 界面浏览、搜索、恢复已删除文件 |
| 🌐 **Web 管理** | 暗色主题 Web UI，支持文件浏览、恢复、清空、统计 |
| 🔒 **访问认证** | 支持 HTTP Basic 认证，防止未授权操作 |
| 🗄️ **MySQL 存储** | 元信息存入 MySQL，高效可靠，替代文件存储 |
| 🪟 **Windows 服务** | 支持注册为 Windows 服务，开机自启、后台运行 |
| 📦 **单文件打包** | Nuitka 编译为单个 exe，无需 Python 环境即可运行 |

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                        共享文件夹 (SMB)                       │
│                      E:\share  (被监控目录)                   │
└──────────────┬──────────────────────────────┬───────────────┘
               │ 文件删除事件                   │ 定期扫描
               ▼                              ▼
        ┌──────────────┐              ┌──────────────┐
        │   watchdog   │              │  sync 模块   │
        │  实时监控模块  │              │  定期扫描模块  │
        └──────┬───────┘              └──────┬───────┘
               │                             │
               └──────────┬──────────────────┘
                          ▼
                 ┌────────────────┐
                 │   backup 模块  │──── 备份文件到 backup_dir
                 │   备份处理引擎  │
                 └────────┬───────┘
                          ▼
                 ┌────────────────┐
                 │  recycler 模块 │──── 移入 recycle_dir
                 │   回收站管理    │
                 └────────┬───────┘
                          ▼
                 ┌────────────────┐
                 │  database 模块 │──── 记录元信息到 MySQL
                 │  MySQL 元数据  │
                 └────────────────┘
                          │
                          ▼
                 ┌────────────────┐
                 │   Web 管理界面  │──── http://0.0.0.0:8088
                 │   FastAPI 应用  │      浏览 / 搜索 / 恢复
                 └────────────────┘
```

## 🚀 快速开始

### 环境要求

| 组件 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 或 Windows Server |
| Python | 3.10+（仅开发/构建时需要） |
| MySQL | 5.7+ / 8.0+ |

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
start.bat

# 方式二：直接运行
python main.py start       # 启动守护程序
python main.py stop        # 停止守护程序
python main.py status      # 查看运行状态
python main.py web         # 仅启动 Web 界面（不监控）
```

启动后访问 `http://localhost:8088` 即可打开 Web 管理界面。

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

- **统计概览** - 回收站文件数、总大小、备份文件数一目了然
- **文件列表** - 按删除时间倒序展示，显示文件名、原路径、大小、删除时间
- **搜索过滤** - 支持按文件名或路径实时搜索
- **一键恢复** - 点击恢复按钮，文件自动回到原始位置
- **清空回收站** - 支持一键清空（带二次确认）
- **自动刷新** - 页面定时自动刷新，保持数据最新

## 📁 项目结构

```
file-recycle-guard/
├── core/                       # 核心模块
│   ├── config.py               # 配置加载与路径解析
│   ├── watcher.py              # 文件监控（watchdog）
│   ├── backup.py               # 备份逻辑（全量 + 增量）
│   ├── recycler.py             # 回收站管理（列表/恢复/清空）
│   ├── cleanup.py              # 备份镜像定期清理
│   ├── sync.py                 # 定期同步扫描（解决 SMB 问题）
│   ├── logger.py               # 日志模块（按天轮转）
│   └── database.py             # MySQL 数据库（元数据存储）
├── web/                        # Web 管理界面
│   ├── __init__.py             # FastAPI 应用（路由/认证/API）
│   └── templates/
│       └── index.html          # 暗色主题管理页面
├── main.py                     # 程序入口（start/stop/status/web）
├── service.py                  # Windows 服务入口（可选）
├── config.yaml                 # 配置文件
├── requirements.txt            # Python 依赖
├── start.bat                   # 快速启动脚本
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
| `database.host` | MySQL 主机 | `127.0.0.1` |
| `database.port` | MySQL 端口 | `3306` |
| `database.user` | MySQL 用户名 | `root` |
| `database.password` | MySQL 密码 | `123456` |
| `database.database` | MySQL 数据库名 | `file_recycle_guard` |

</details>

## ❓ 常见问题

<details>
<summary><b>Q: watchdog 能检测到 SMB 网络共享的文件变更吗？</b></summary>

<p>watchdog 在 SMB 场景下存在检测不可靠的问题（某些客户端的删除操作不会触发通知）。因此程序内置了 <b>定期同步模块</b>（sync），每隔 30 秒主动扫描目录，弥补 watchdog 的遗漏。两者配合使用，确保变更不会漏检。</p>
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
| 文件监控 | [watchdog](https://github.com/gorakhargosh/watchdog) |
| Web 框架 | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| 模板引擎 | [Jinja2](https://jinja.palletsprojects.com/) |
| 数据库 | [PyMySQL](https://github.com/PyMySQL/PyMySQL) + MySQL |
| 配置文件 | [PyYAML](https://github.com/yaml/pyyaml) |
| 打包工具 | [Nuitka](https://nuitka.net/) (onefile 模式) |
| Windows 服务 | [pywin32](https://github.com/mhammond/pywin32) |

## 📄 License

MIT
