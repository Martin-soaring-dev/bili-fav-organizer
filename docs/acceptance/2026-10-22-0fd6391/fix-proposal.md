# 两个残留问题的定位与修复建议（0fd6391 复测后续）

> 本文只给定位证据与改法建议，**未改动任何源码**。所有实验均在隔离 profile（`%TEMP%`）中执行。

## 一、问题 1：扫描工作线程写库时 `attempt to write a readonly database`

### 现象

- 完整回归 356 项中 3 项失败；`test_scan_troubleshooting.ScanTroubleshootingTests` 整类连跑 3 次全失败，且每次失败用例不同。
- 堆栈固定：`store.save_scan_stage` → `store._transaction()` → `conn.execute("BEGIN IMMEDIATE")` → `sqlite3.OperationalError: attempt to write a readonly database`。
- 启动路径（`_ensure_task_scheduler`）已不再出现该异常（PR #30 的修复有效）。

### 已确认的机制（实验支持）

1. 该库处于 WAL 模式，**`-wal` / `-shm` 只在“至少一个连接打开”期间存在**；最后一个连接正常关闭时 SQLite 会把它们删掉。
   实测（SQLite 3.53.1，`walclean` 实验）：写连接关闭后目录只剩 `t.db`。
2. 应用里所有连接都是 **短生命周期**（`store._transaction()` 每次开/关；调度器 `store.list/create/update` 每次开/关），且**多线程并发**（扫描 worker、调度器泵、API 处理线程）。
3. 当“某连接关闭 → 删除 `-shm`”与“另一线程新建连接 → 创建 `-shm`”交叠时，新建连接可能拿不到共享内存文件；SQLite 此时会把该连接降级为只读，随后 `BEGIN IMMEDIATE` 就抛 `readonly database`。
   旁证（上一轮同机抓现场）：失败瞬间 `-wal` 为 0 字节且**没有 `-shm`**，而紧接着新建的连接能正常写。
4. 该异常与“生命周期拒绝”不同：生命周期拒绝会抛 `DatabaseLifecycleError`，这里是连接已建立、但被 SQLite 判定只读。

### 建议改法（按推荐顺序）

**A. 串行化“打开/关闭”临界区（首选，改动最小）**

在 `core/database/lifecycle.py` 的 `DatabaseLifecycle` 上增加一把进程内锁，只包住连接的建立与释放（不要包住整个事务，避免把并发写退化成串行）：

```python
# __init__
self._conn_gate = threading.RLock()

# connect(): 只把「lease 登记 + sqlite3.connect」放进 gate
with self._conn_gate:
    ... 现有 connect 逻辑 ...

# _release(): 把「lease 移除 + 底层 close 触发的 sidecar 删除」放进同一把 gate
with self._conn_gate:
    ... 现有释放逻辑 ...
```

关键点：`sqlite3.Connection.close()` 必须在 gate 内执行（sidecar 删除发生在 close 里），否则竞态窗口仍然存在。

**B. 读已判定为只读时做一次受控重试（兜底，建议与 A 同批上）**

在 `store._transaction()` 的 `BEGIN IMMEDIATE` 处，仅当 `database_state() == READY` 且错误消息含 `readonly database` 时，关闭连接、短暂退避（10–50ms）、重开重试一次；第二次仍失败则原样抛出。这样既不掩盖真实故障（最终仍会失败并留日志），又能吸收瞬态窗口。

```python
for attempt in range(2):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        break
    except sqlite3.OperationalError as exc:
        conn.close()
        if attempt or "readonly database" not in str(exc) or database_state() != "READY":
            raise
        time.sleep(0.02)
```

**C. 不建议 `SQLITE_FCNTL_PERSIST_WAL`**

它能从根上避免 sidecar 反复删除/重建，但会让 `-wal`/`-shm` 常驻磁盘，**与 `MIG_030` 断言的“空闲时不应存在 sidecar”直接冲突**，也会影响封库/备份的既有假设。除非同时调整生命周期不变量，否则不要用。

### 验证方法

```powershell
# 1) 整类连跑 5 次，应 5/5 通过且日志无 readonly
python tools/run_isolated_tests.py --test "test_scan_troubleshooting.ScanTroubleshootingTests"
# 2) 全量
python tools/run_isolated_tests.py
# 3) 锁定专项不回归
python tools/run_isolated_tests.py --test "scheduler.test_server_integration.ServerSchedulerIntegrationTests.test_vault_lock_interrupts_bilibili_rate_limit_backoff_and_pauses_task"
```

判据：整类 5/5、全量 356/356（或至少 0 failure）、key 系列仍 12/12。

## 二、问题 2：`MIG_030` 判定“解锁探针结束后仍有 WAL/SHM”

### 现象

- 单跑 5/5 失败：`database_sidecars_at_probe_end` 返回 `library.sqlite3-wal`、`library.sqlite3-shm`。
- 断言本身只有一句：`assertEqual([], unlocked["database_sidecars_at_probe_end"])`。

### 已确认的机制（实验支持）

1. sidecar 的存在**只取决于“此刻是否有连接打开”**：连接全关 → 自动删除（`walclean` 实验）。
2. **只读连接（`mode=ro`）会在关闭后留下 `-wal`/`-shm`**，且不会自行清理；只有随后一个可写连接关闭才会顺手删除（`roleft` 实验：`after ro close` 仍有 sidecar，`after rw close` 才干净）。
3. 因此探针结束时出现 sidecar，只有两种可能：
   - **仍有连接未关闭**（探针返回 JSON 时进程尚在，调度器/线程可能刚好持有一个连接）；
   - **最后关闭的是只读连接**（应用主流程目前不传 `mode="ro"`，但迁移/探针/快照类代码可能直接 `sqlite3.connect(...,?mode=ro)`）。
4. 该用例在本机是**确定性失败**（5/5），说明不是罕见时序，而是本机路径稳定命中上述两种状态之一。

### 先做 10 分钟定位（决定改哪一边）

在复现进程里（可用验收脚本方式，不改仓库代码）于 `vault_unlock` 返回后打印：

```python
from pathlib import Path
print("leases:", store.database_connections())          # 谁还开着
print("state:", store.database_state())
print("sidecars:", [str(p) for p in (Path(str(store.DB_FILE) + s)
                                     for s in ("-wal", "-shm", "-journal")) if p.exists()])
```

- `leases` 非空 → 是“连接未释放”的泄漏，按 owner 名定位（`store` / `task-scheduler` / `startup:...`）。
- `leases` 为空但 sidecar 仍在 → 最后关闭的是只读连接，去查谁在活库上用了 `mode=ro`（真正的快照应该像 `database_snapshot()` 那样**拷副本再读**）。

### 建议改法

**A. 若为连接泄漏**：在泄漏 owner 的 `finally` 里确保 `close()`；对调度器泵线程，只在实际派发任务时开连接，等待条件变量期间不要持有连接。

**B. 若为只读连接遗留（更可能）**：
- 禁止对**活的 profile 库**使用 `mode=ro`；只读需求一律走“拷贝副本 + 只读打开”（`tests/migration/runtime_probe.py::database_snapshot` 已经是这个正确范式）。
- 或在解锁流程返回前，用一个**可写连接**做 `PRAGMA wal_checkpoint(TRUNCATE)` 再 `close()`，确保“最后关闭的是写连接”，sidecar 被确定性清除：

```python
def _seal_sidecars() -> None:
    if not store.DB_FILE.is_file():
        return
    conn = store._connect(owner="unlock:sidecar-cleanup")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
```

放在 `vault_unlock()` 成功返回之前（或在恢复完成、调度器启动前）。

### 验证方法

```powershell
python tools/run_isolated_tests.py --test "migration.test_vault_migration.VaultMigrationTests.test_MIG_030_probe_snapshot_does_not_create_profile_sidecars"   # 期望 10/10
python tools/run_isolated_tests.py --test "migration.test_vault_migration.VaultMigrationTests"                                                              # 整类不回归
```

## 三、两项改动的相互影响（必须一起看）

- 问题 1 的 A（串行化 close）会让 sidecar 的删除时机更确定，**有利于**问题 2；C（PERSIST_WAL）会让问题 2 更严重，故排除。
- 问题 2 的 B（解锁末尾强制 checkpoint + 关闭）不会引入新的并发窗口，但要注意它发生在“调度器已启动”之前或之后需一致：建议放在 `_ensure_task_scheduler()` 之前，避免调度器连接还在时做 TRUNCATE。
- 两处改动都只碰 `core/database/lifecycle.py`、`store.py`、`server.py` 的既有函数，不动表结构、不动协议，可以独立回滚。

## 四、复现基线（改前）

| 项 | 改前结果 |
| --- | --- |
| key3 / key4 | 12/12、12/12 PASS |
| 完整回归 | 356 项 / 3 失败 / exit 1 |
| `ScanTroubleshootingTests` 整类 | 3/3 失败（每次失败用例不同，均含 readonly） |
| `MIG_030` 单跑 | 5/5 失败 |
