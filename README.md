# 文件回收站守护程序 (File Recycle Guard)

保护共享文件夹中的文件不被远程误删除或恶意删除。当用户通过 SMB 网络共享删除文件时，程序会自动将文件备份并移入回收站，支持一键恢复。

## 功能特性

- **实时监控** - 基于 watchdog 监控共享文件夹的文件变更（创建/修改/删除/移动）
- **定期同步** - 主动扫描监控目录，解决 SMB 网络共享变更检测不可靠的问题
- **备份镜像** - 实时同步文件到备份目录，保留完整历史版本
- **回收站** - 被删除的文件自动移入回收站，支持按保留天数自动清理
- **一键恢复** - 通过 Web 管理界面浏览和恢复已删除的文件
- **Web 管理界面** - 基于 FastAPI 的 Web UI，支持文件浏览、恢复、清空等操作
- **MySQL 元数据存储** - 使用 MySQL 存储备份和回收站元信息，高效可靠
- **Windows 服务** - 支持以 Windows 服务模式运行（可选）

## 快速开始

### 环境要求

- Python 3.10+
- MySQL 5.7+ / 8.0+
- Windows 10/11 或 Windows Server

### 安装

```bash
# 1. 克隆项目
git clone <repo-url>
cd file-recycle-guard

# 2. 安装依赖
pip install -r requirements.txt

# 3. 创建 MySQL 数据库
mysql -u root -p -e "CREATE DATABASE file_recycle_guard CHARACTER SET utf8mb4;"
```

### 配置

编辑 `config.yaml`：

```yaml
# 要监控的共享文件夹路径
watch_paths:
  - "E:\\share"

# 备份镜像目录
backup_dir: "E:\\share_bak"

# 回收站目录
recycle_dir: "E:\\share_ryc"

# 回收站保留天数
retention_days: 30

# Web 管理界面
web:
  enabled: true
  host: "0.0.0.0"
  port: 8088
  # 建议设置认证
  username: "admin"
  password: "your_password"

# MySQL 数据库
database:
  host: "127.0.0.1"
  port: 3306
  user: "root"
  password: "123456"
  database: "file_recycle_guard"
```

### 启动

```bash
# 方式一：使用启动脚本（自动检查依赖）
start.bat

# 方式二：直接运行
python main.py start

# 停止
python main.py stop

# 查看状态
python main.py status
```

### 打包为单文件 exe

```bash
# 安装 Nuitka
pip install nuitka

# 执行构建
python _build_nuitka.py

# 构建产物在 dist/ 目录
#   dist/main.exe      - 主程序
#   dist/config.yaml   - 配置文件（需与 exe 放在同一目录）
```

> **注意：** 运行 exe 时，`config.yaml` 必须与 `main.exe` 放在同一目录下。

## 项目结构

```
file-recycle-guard/
├── core/                   # 核心模块
│   ├── config.py           # 配置加载
│   ├── watcher.py          # 文件监控（watchdog）
│   ├── backup.py           # 备份逻辑
│   ├── recycler.py         # 回收站管理
│   ├── cleanup.py          # 定期清理
│   ├── sync.py             # 定期同步扫描
│   ├── logger.py           # 日志模块
│   └── database.py         # MySQL 数据库
├── web/                    # Web 管理界面
│   ├── __init__.py         # FastAPI 应用
│   └── templates/
│       └── index.html      # 管理页面
├── main.py                 # 程序入口
├── service.py              # Windows 服务入口（可选）
├── config.yaml             # 配置文件
├── requirements.txt        # Python 依赖
├── start.bat               # 快速启动脚本
└── _build_nuitka.py        # Nuitka 打包脚本
```

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `watch_paths` | 监控的共享文件夹路径列表 | `[]` |
| `backup_dir` | 备份镜像目录 | `backup_mirror` |
| `recycle_dir` | 回收站目录 | `recycle_bin` |
| `retention_days` | 回收站保留天数 | `30` |
| `exclude_patterns` | 排除的文件模式 | `~$*, *.tmp, *.lock, Thumbs.db` |
| `exclude_dirs` | 排除的文件夹 | `$RECYCLE.BIN, System Volume Information` |
| `log.level` | 日志级别 | `INFO` |
| `log.file` | 日志文件路径 | `logs/recycle_guard.log` |
| `log.max_days` | 日志保留天数 | `90` |
| `web.enabled` | 是否启用 Web 界面 | `true` |
| `web.host` | Web 监听地址 | `0.0.0.0` |
| `web.port` | Web 端口 | `8088` |
| `web.username` | Web 认证用户名 | `None` |
| `web.password` | Web 认证密码 | `None` |
| `mirror_cleanup.interval` | 备份清理间隔（秒） | `3600` |
| `mirror_cleanup.grace_period` | 清理宽限期（秒） | `300` |
| `sync.enabled` | 是否启用定期同步 | `true` |
| `sync.interval` | 同步扫描间隔（秒） | `30` |
| `database.*` | MySQL 连接配置 | 见 `config.yaml` |

## 工作原理

```
用户删除文件 ──→ watchdog 检测到删除事件
                      │
                      ▼
              备份文件到 backup_dir
                      │
                      ▼
              移动文件到 recycle_dir
                      │
                      ▼
              记录元信息到 MySQL
                      │
                      ▼
              定期清理超过保留天数的文件
```

- **watchdog** 实时监控文件变更，捕获删除事件
- **定期同步** 每隔 30 秒主动扫描，弥补 watchdog 在 SMB 场景下的检测遗漏
- **备份镜像** 保留文件的所有历史版本，支持恢复到任意时间点
- **回收站** 被删除的文件不会真正消失，可在 Web 界面一键恢复
