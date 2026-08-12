# USN Journal 生产级架构升级

## 现状分析

当前架构：
- `core/usn.py` - 已有基础 USN Journal 读取能力（UsnJournalReader、UsnJournalMonitor），但**未接入主流程**
- `core/watcher.py` - watchdog 作为主通道，批量轮询备份
- `core/sync.py` - 定期增量扫描（目录 mtime + SQLite 缓存 + LRU）
- `main.py` - 仅启动 watcher + sync，未集成 USN

目标架构：
```
USN Journal (主通道/事实来源)
    + watchdog (低延迟加速器)
    + Full Scan (最终一致性 reconciliation)
        -> Event Normalizer -> Event Coalescer -> SQLite Event Log -> Protection Worker
```

## 文件结构变更

```
core/
├── usn/                    # 新：USN 子包（替代原 usn.py）
│   ├── __init__.py         # 导出 UsnJournalMonitor, create_usn_watcher 等
│   ├── journal.py          # Journal 查询/创建/状态管理
│   ├── record.py           # USN_RECORD 结构体 + 解析
│   ├── reader.py           # UsnJournalReader（卷级读取器）
│   ├── checkpoint.py       # UsnCheckpoint（持久化 + journal_id/gap 校验）
│   └── path_resolver.py    # FrnPathCache + DirectoryIdentityCache
│
├── detector/               # 新：变更检测层
│   ├── __init__.py
│   ├── usn_detector.py     # USN 事件检测器（封装 UsnJournalMonitor）
│   └── reconciler.py       # 一致性修复器（替代原 sync.py 的日常扫描职责）
│
├── engine/                 # 新：事件处理引擎
│   ├── __init__.py
│   ├── event_normalizer.py # USN Reason -> 标准事件 (CREATE/MODIFY/DELETE/RENAME)
│   ├── event_coalescer.py  # 事件合并（同文件多次 MODIFY -> 单次）
│   └── protection_engine.py# 保护引擎（消费事件队列 -> 调度备份/删除/重命名）
│
├── usn.py                  # 保留（deprecated），从 usn/ 导入以兼容
├── watcher.py              # 改：降级为低延迟加速器，事件送入 EventNormalizer
├── sync.py                 # 改：大幅瘦身为 reconciler 的辅助
├── backup.py               # 小改：接入 Protection Engine 的回调
├── config.py               # 改：添加 USN 配置段
├── database.py             # 改：添加 fs_event / usn_checkpoint / file_identity 表
└── ...
```

---

## PR1: USN 基础设施强化

### 1.1 创建 `core/usn/` 子包

**`core/usn/record.py`** - 从 `core/usn.py` 提取：
- 所有 Windows API 常量（USN_REASON_* 等）
- USN_RECORD_V3、USN_JOURNAL_DATA_V2、READ_USN_JOURNAL_DATA_V1 等结构体
- Windows API 函数声明（kernel32.*）
- 辅助函数：`_open_volume_handle`、`_nt_to_dos_path` 等

**`core/usn/journal.py`** - 从 `core/usn.py` 提取并增强：
- `UsnJournalState` -> 重命名为 `UsnCheckpoint`
- 增加 `journal_id` 校验逻辑（方案第四节）
- 增加 `FirstUsn` gap 检测（方案第六节）
- 增加 Journal 状态枚举：HEALTHY / JOURNAL_GAP / RESCAN_REQUIRED / JOURNAL_RESET / ERROR

```python
class JournalStatus(Enum):
    HEALTHY = "HEALTHY"
    JOURNAL_GAP = "JOURNAL_GAP"
    RESCAN_REQUIRED = "RESCAN_REQUIRED"
    JOURNAL_RESET = "JOURNAL_RESET"
    ERROR = "ERROR"

@dataclass
class JournalHealthInfo:
    volume: str
    status: JournalStatus
    journal_id: int
    first_usn: int
    current_usn: int
    checkpoint_usn: int
    lag: int  # current_usn - checkpoint_usn
```

**`core/usn/reader.py`** - 从 `core/usn.py` 提取 `UsnJournalReader` 并增强：
- `read_records()` 增加 journal_id 变化检测 -> 返回 JOURNAL_RESET 信号
- `read_records()` 增加 checkpoint.next_usn < journal.first_usn 检测 -> 返回 JOURNAL_GAP 信号
- 增加 `query_journal_info()` 方法返回完整的 Journal 状态（用于 Health Check）
- 增大输出缓冲区从 1MB -> 4MB（高并发场景减少系统调用次数）
- 记录解析增加 64-bit 对齐（方案第十八节）

**`core/usn/checkpoint.py`** - 新的持久化 checkpoint：
- 从 JSON 文件迁移到 SQLite（与项目其他状态一致）
- 保存 volume / journal_id / next_usn / updated_at
- 关键：**checkpoint 必须与 Durable Event Commit 绑定**（方案第二十节）

```python
CREATE TABLE usn_checkpoint (
    volume       TEXT PRIMARY KEY,
    journal_id   INTEGER NOT NULL,
    next_usn     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'HEALTHY',
    updated_at   REAL NOT NULL
);
```

**`core/usn/__init__.py`** - 导出公共接口

### 1.2 兼容性处理

**`core/usn.py`** - 保留文件，改为从 `core/usn/` 导入：
```python
# Deprecated: 保持向后兼容，新代码请使用 core.usn 子包
from core.usn.reader import UsnJournalReader
from core.usn.journal import UsnJournalMonitor, create_usn_watcher
# ... 其他导出
```

### 1.3 配置增强

**`config.py`** - 添加 USN 配置段：
```python
@dataclass
class UsnConfig:
    enabled: bool = True
    poll_interval: float = 1.0    # 轮询间隔（秒）
    state_dir: str = ".usn_state"
    max_records_per_read: int = 10000  # 单次最大读取记录数
    buffer_size_mb: int = 4       # USN 读取缓冲区大小（MB）
```

**`config.yaml`** - 添加 USN 配置：
```yaml
usn:
  enabled: true
  poll_interval: 1.0
  max_records_per_read: 10000
  buffer_size_mb: 4
```

---

## PR2: 事件标准化 + 合并

### 2.1 `core/engine/event_normalizer.py`

将 USN Reason 映射为标准事件（方案第八节）：

```python
class EventType(Enum):
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    RENAME_OLD = "RENAME_OLD"
    RENAME_NEW = "RENAME_NEW"

@dataclass
class NormalizedEvent:
    event_id: int           # 自增 ID（SQLite rowid）
    event_type: EventType
    full_path: str
    file_reference_number: int
    parent_frn: int
    volume_id: str
    usn: int
    timestamp: float
    state: str = "PENDING"  # PENDING / PROCESSING / DONE / FAILED
```

统一入口：USN 事件和 watchdog 事件都通过 `normalize()` 转为 `NormalizedEvent`。

### 2.2 `core/engine/event_coalescer.py`

事件合并器（方案第九节）：
- 同文件（同 FRN）的多次 MODIFY 合并为一次
- USN_CLOSE 作为写入阶段结束的辅助信号
- 500ms debounce 窗口（可配置）
- 实现：内存 dict `{frn: (event, first_seen_time)}`，定时 flush 过期条目

```python
class EventCoalescer:
    DEBOUNCE_MS = 500
    
    def submit(self, event: NormalizedEvent) -> List[NormalizedEvent]:
        """提交事件，返回可立即处理的事件列表（可能为空）"""
    
    def flush_expired(self) -> List[NormalizedEvent]:
        """flush 超过 debounce 窗口的事件"""
```

### 2.3 `core/engine/protection_engine.py`

保护引擎 - 消费合并后的事件并调度操作：
- 从 SQLite fs_event 表读取 PENDING 事件
- 根据事件类型调度：CREATE/MODIFY -> backup_file, DELETE -> move_to_recycle, RENAME -> 更新路径
- 操作完成后更新事件状态为 DONE/FAILED
- **原子提交**：事件状态更新 + checkpoint 推进必须在同一事务中（方案第二十节）

### 2.4 `core/usn/path_resolver.py`

从 `core/usn.py` 提取 `FrnPathCache` 并增强：
- 增加 `DirectoryIdentityCache`：FRN -> (parent_frn, name) 映射
- 支持通过 parent chain 解析路径（方案第十四节）
- 目录 rename 时只需更新目录 FRN 的 name，子节点自动继承新路径（方案第十五节）

```python
class DirectoryIdentityCache:
    """FRN -> (parent_frn, name, volume_id) 的内存缓存"""
    
    def resolve_path(self, frn: int, volume_id: str) -> Optional[str]:
        """通过 parent chain 递归解析完整路径"""
    
    def update_rename(self, frn: int, new_name: str, new_parent_frn: int):
        """处理目录 rename（方案第十五节）"""
    
    def build_from_walk(self, watch_paths: List[str]):
        """首次构建：通过 os.walk + OpenFileById 建立初始缓存"""
```

---

## PR3: 持久化事件队列 + 文件身份

### 3.1 `core/database.py` - 添加新表

```sql
-- 持久化事件队列（方案第二十一节）
CREATE TABLE IF NOT EXISTS fs_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id       TEXT NOT NULL,
    usn             INTEGER NOT NULL,
    file_reference  INTEGER,
    parent_reference INTEGER,
    reason          INTEGER NOT NULL,
    path            TEXT,
    event_type      TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'PENDING',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(volume_id, usn)
);

-- 文件身份表（方案第十二节）
CREATE TABLE IF NOT EXISTS file_identity (
    volume_id               TEXT NOT NULL,
    file_reference_number   INTEGER NOT NULL,
    watch_root              TEXT NOT NULL,
    relative_path           TEXT NOT NULL,
    is_directory            INTEGER NOT NULL DEFAULT 0,
    last_usn                INTEGER NOT NULL,
    file_size               INTEGER,
    mtime_ns                INTEGER,
    state                   TEXT NOT NULL DEFAULT 'ACTIVE',
    PRIMARY KEY (volume_id, file_reference_number)
);
```

### 3.2 事件持久化流程

```
USN Record -> EventNormalizer -> EventCoalescer 
    -> SQLite fs_event INSERT (state=PENDING) 
    -> usn_checkpoint UPDATE (与 event commit 绑定)
```

关键：**checkpoint 只在事件成功写入 fs_event 后才推进**（方案第二十节）。

### 3.3 Protection Worker

后台线程消费 fs_event 表中 PENDING 事件：
1. 批量取出 PENDING 事件（UPDATE state = PROCESSING）
2. 按文件分组执行备份/删除/重命名
3. 完成后 UPDATE state = DONE + 更新 file_identity
4. crash recovery：启动时将 PROCESSING 状态的事件重置为 PENDING

---

## PR4: sync.py 改造为 reconciler

### 4.1 `core/detector/reconciler.py`

原 `sync.py` 的日常增量扫描职责由 USN 接管。`reconciler.py` 仅负责：

1. **首次扫描**（initial scan）：USN checkpoint 不存在时，执行全量扫描建立基线
2. **USN gap recovery**：checkpoint.next_usn < journal.first_usn 时触发
3. **Journal reset recovery**：journal_id 变化时触发
4. **周期性一致性检查**（默认每 6 小时）：对比 USN 状态与文件系统实际状态

reconciler 不再需要：
- 目录 mtime 判断
- LRU 缓存
- 每 30 秒的增量扫描

保留 IncrementalScanner 的核心逻辑供 reconciler 使用，但调用频率大幅降低。

### 4.2 `core/detector/usn_detector.py`

封装 USN 监控的启动和事件分发：
- 管理 UsnJournalReader 生命周期
- 检测 journal gap / reset -> 触发 reconciler
- 将 USN 事件送入 EventNormalizer

### 4.3 `core/watcher.py` 改造

watchdog 降级为低延迟加速器（方案第二十二节）：
- watchdog 事件不再直接触发备份
- 改为送入 EventNormalizer，与 USN 事件统一处理
- watchdog 的价值：100ms 级延迟 vs USN 的 1s 轮询间隔
- USN 作为 source of truth，watchdog 事件标记为 "accelerator" 优先级

### 4.4 `main.py` 改造

启动流程变更：
1. 初始化 USN Journal 读取器
2. 检查 checkpoint 有效性（journal_id + first_usn）
3. 如需 reconciliation -> 启动 reconciler
4. 启动 USN 监控循环
5. 启动 watchdog（作为加速器）
6. 启动 Protection Worker
7. 启动 Event Coalescer

---

## Web Health Check 增强

**`web/__init__.py`** - 添加 USN 状态 API：

```python
@app.get("/api/usn_health")
async def api_usn_health():
    """USN Journal 健康状态"""
    # 返回每个卷的：
    # - status (HEALTHY / JOURNAL_GAP / RESCAN_REQUIRED / ...)
    # - journal_id, first_usn, current_usn, checkpoint_usn
    # - lag (记录数)
    # - coverage 估算
```

---

## 实施顺序与依赖关系

```
PR1 (USN 基础设施)
  |
  v
PR2 (事件标准化 + 合并)
  |
  v
PR3 (持久化队列 + 文件身份)
  |
  v
PR4 (sync.py -> reconciler + main.py 集成)
```

每个 PR 完成后应可独立运行验证：
- PR1: USN 读取 + checkpoint 持久化正常工作
- PR2: 事件标准化输出正确，coalescer 合并有效
- PR3: 事件持久化 + crash recovery 验证
- PR4: 完整流程端到端验证

## 风险与注意事项

1. **USN Journal 需要管理员权限**：当前 `start_admin.bat` / `start_admin.ps1` 已支持
2. **Journal 容量**：默认创建 128MB，生产环境建议根据业务变化量调整（方案第二十四节）
3. **FRN 复用风险**：FRN 不是永久 UUID，删除后可能复用（方案第十二节），需结合 USN 判断
4. **远程路径不支持 USN**：watchdog + sync 作为回退方案必须保留
5. **Checkpoint 可靠性**：必须与事件 commit 绑定，不能先 checkpoint 再处理（方案第二十节）
