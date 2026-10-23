> 归档副本来源：`acceptance/2026-10-22-0fd6391` @ `89f1fc8bf3ac6ae7cdda88fdc70db69293a2bafc`。原始报告的 SHA-256 记录在 `evidence/raw-sha256.csv`；本副本增加了本次归档说明。
> **公开范围：**报告和文本证据已进行路径 / 凭据模式复核；本次验收使用合成测试数据。19 张验收截图已按原始证据归档到主干对应目录。截图中的本地数据目录属于验收环境路径，不是测试业务数据；如需对外发布演示材料，可另制作隐藏环境路径的展示副本。

# BiliFavOrganizer 修复后独立复测报告（SQLite 锁定竞态专项） · v2

> 修订时间：2026-10-22 22:43（初版 22:40）。v2 增补：更新按钮的 Edge 实测核查、两个残留缺陷的修复建议摘要、修订记录。结论未变。

- 角色：独立本地测试/验收代理（非功能开发代理）
- 执行时间：2026-10-22 21:44 – 22:40 (GMT+8)
- 验收目录：`%TEMP%\bfo-retest-20261022-0fd6391`
- 证据目录：`%TEMP%\bfo-retest-20261022-0fd6391\acceptance-evidence`

## 0. 结论速览

| 问题 | 结论 |
| --- | --- |
| 1. 412 限流中断 + 保险库锁定 连跑 12 次 | **12/12 PASS** |
| 2. 检查点保存 + 解锁恢复 连跑 12 次 | **12/12 PASS** |
| 3. 完整回归是否通过、只读异常是否消失 | **未通过**（356 项 / 3 失败 / exit 1）；`attempt to write a readonly database` **仍存在**（扫描工作线程写库路径） |
| 4. 新增启动恢复顺序测试 | 3/3 PASS，断言实质覆盖“先取所有权→再恢复→后分发” |
| 5. 安装包构建 / 打包冒烟 | 构建 PASS；冒烟 **BLOCKED**（本机 GPU 进程崩溃） |
| 6. GPU 限制、更新按钮无法触达 Release | **仍然存在**（均按 BLOCKED 记录） |

**最终结论：FAIL**

## 1. 验收对象

| 项目 | 值 |
| --- | --- |
| 仓库 | https://github.com/<user>-soaring-dev/bili-fav-organizer |
| 验收提交 | `0fd6391fe45f030e990722f9cbafe5ae23ae6ec4` |
| 对应 PR | #30 `fix: recover remote operations before scheduler dispatch` |
| 检出 | `git clone` → `git fetch origin main` → `git checkout --detach <sha>` |
| 实际 HEAD | `0fd6391fe45f030e990722f9cbafe5ae23ae6ec4`（与指定一致；同时为 `origin/main` 顶端，`merge-base --is-ancestor` 通过） |
| 工作区 | detached HEAD，`git status --porcelain` 为空 |
| 提交信息 | <user> / 2026-10-22 21:29:10 +0800；父提交 `05095b9`；4 文件 +125/−9 |

修复内容（只读复核）：`TaskScheduler.start()` 新增 `before_dispatch` 钩子，在取得单进程所有权、恢复调度器记录之后、启动分发泵之前执行；`server._ensure_task_scheduler()` 通过该钩子执行 `recover_stranded_dispatches()`，并在恢复成功后才发布全局调度器单例。

## 2. 环境

| 组件 | 版本 |
| --- | --- |
| Windows | 10.0.26300（26H2，Build 26300，AMD64） |
| Git | 2.55.0.windows.3（PortableGit） |
| Python | 3.12.15（独立 `.venv`；本机 `py -3.12` 未注册，直接使用 3.12.15 解释器路径） |
| SQLite（venv 内） | 3.53.1 |
| Node / npm | v22.22.2 / 10.9.7 |
| Playwright / Chromium | 1.63.0 / Chromium 1243（Chrome for Testing 153.0.8010.12） |
| PyInstaller | 6.22.3 |
| Electron / electron-builder | 44.7.0 / 26.15.3 |

环境治理（仅作用于本次验收进程及其子进程，未改系统设置）：

- 清除 `PYTHONPATH`、`ELECTRON_RUN_AS_NODE`，补入 git / node 可执行目录，设 `BFO_REQUIRE_PLAYWRIGHT=1`。
- 子进程洁净性实测（`acceptance-evidence/env-probe.log`）：
  - `PYTHONPATH: None`；`shim_on_path: []`；`sitecustomize: not importable`；`pathlib.Path.unlink` 为 CPython 原生实现；`sqlite: 3.53.1`。
  - 即测试进程未加载执行环境提供的站点垫片或删除钩子。
- 出口经本地 MITM 代理，git 使用 `sslBackend=openssl` + 合并 CA 包；Chromium 与 electron-builder 二进制走镜像源。以上仅为取包/构建所需，不影响测试语义。

## 3. 新增启动恢复顺序测试（问题 4）

| ID | 测试 | 退出码 | 耗时 | 日志 |
| --- | --- | --- | --- | --- |
| new1 | `scheduler.test_scheduler.SchedulerTests.test_start_runs_recovery_hook_before_dispatch_with_ownership_held` | 0 | 4.2s | `new1.log` |
| new2 | `scheduler.test_server_integration.ServerSchedulerIntegrationTests.test_remote_operation_recovery_runs_before_scheduler_dispatch` | 0 | 8.7s | `new2.log` |
| new3 | `scheduler.test_server_integration.ServerSchedulerIntegrationTests.test_scan_route_persists_and_runs_through_the_scheduler` | 0 | 8.6s | `new3.log` |

断言实质（已阅读测试源码核对）：

- new1 在 `before_dispatch` 内断言：`owner_lock.held == True`、`scheduler._pump_thread is None`、`scheduler._started == False`、`worker.calls == 0`；钩子被刻意阻塞 2s，随后才放行，最终排队的任务完成且 worker 只被调用 1 次 → **分发线程确实不会在恢复回调结束前运行**。
- new2 用事件序列断言生产代码的接线顺序恒为 `[pre-dispatch-enter, remote-recovery, pre-dispatch-exit, start-return]`，且 `start()` 必须收到 `before_dispatch` → **服务端确实把远端操作恢复接入启动屏障**。
- new3 在终态后先 `wait_for_idle(timeout=5)` 再断言 `not store.database_connections()` → **先等 worker 退出，再检查连接释放**。
- “恢复失败不留未启动单例”：由 `server.py` 中 `_TASK_SCHEDULER = scheduler` 位于 `scheduler.start(...)` 之后保证（代码复核）；本次未构造失败注入用例，属未覆盖项。

## 4. 两项关键测试 ×12（问题 1、2）

命令固定：`python tools/run_isolated_tests.py --test <用例全名>`，每次独立隔离环境，无重试、无断言放宽。

### key3：锁定中断 B 站 412 限流等待

| 次 | 结果 | 耗时 | 只读异常 | 日志 |
| --- | --- | --- | --- | --- |
| 1–12 | 全部 PASS | 10.3 / 10.5 / 10.2 / 10.8 / 10.6 / 10.7 / 10.8 / 10.7 / 10.8 / 11.0 / 10.7 / 10.7 s | 0 次 | `key3-run01..12.log` |

**结果：12/12 PASS**（`key3-results.csv`）

### key4：锁定检查点保存 + 解锁恢复

| 次 | 结果 | 耗时 | 只读异常 | 日志 |
| --- | --- | --- | --- | --- |
| 1–12 | 全部 PASS | 10.8 / 11.1 / 10.9 / 11.4 / 10.9 / 11.1 / 11.1 / 11.0 / 11.2 / 11.3 / 11.2 / 11.2 s | 0 次 | `key4-run01..12.log` |

**结果：12/12 PASS**（`key4-results.csv`）

对比上一轮同一机器：key3 6/12、key4 7/12。启动路径上的 412 等待中断与封库恢复已稳定。

## 5. 完整回归（问题 3）

`python tools/run_isolated_tests.py` → **exit 1**

| 指标 | 值 |
| --- | --- |
| 用例总数 | **356**（与预期一致） |
| 结果 | `FAILED (failures=3)`，0 ERROR，无 skipped 报告 |
| 耗时 | 521.2s（含 harness 530.1s） |
| 日志 | `full-regression.log` |

失败用例：

| 用例 | 断言 | 判定 |
| --- | --- | --- |
| `migration.test_vault_migration.VaultMigrationTests.test_MIG_030_probe_snapshot_does_not_create_profile_sidecars` | `[] != ['…library.sqlite3-wal', '…library.sqlite3-shm']`（解锁探针结束后仍有 WAL/SHM） | **单独复跑 5/5 全失败**，确定性 |
| `test_scan_troubleshooting.ScanTroubleshootingTests.test_rate_limit_keeps_issue_and_records_failure` | `recover("clean", True)` 返回 503 `{"ok":false,"error":"该收藏夹没有待处理的扫描异常"}`；同测试内 worker 抛 `RuntimeError: attempt to write a readonly database`（`store.save_scan_stage` → `_transaction()`） | 单独复跑 5/5 通过；仅套件内失败 |
| `test_scan_troubleshooting.ScanTroubleshootingTests.test_uncertain_cleanup_is_never_replayed` | `scan_issues_get()["blocked"]` 为 True，期望 False；同测试内出现同一只读异常 | 单独复跑 5/5 通过；仅套件内失败 |

`attempt to write a readonly database` 出现情况：全量日志中 4 行（2 处堆栈），分别落在上述 2 个扫描类用例，堆栈位置为 `store.save_scan_stage` → `_transaction()` → `BEGIN IMMEDIATE`，**已不在** `_ensure_task_scheduler()` 启动路径。

套件上下文复现（定位用，不替代全量结论）：

| 运行 | 结果 | 只读异常 | 失败用例 |
| --- | --- | --- | --- |
| `test_scan_troubleshooting.ScanTroubleshootingTests` 第 1 次 | FAILED（2） | 有 | `test_confirmed_clean_refreshes_and_validates`、`test_normal_scan_does_not_create_issue` |
| 第 2 次 | FAILED（2） | 有 | `test_clean_requires_explicit_confirmation`、`test_rate_limit_keeps_issue_and_records_failure` |
| 第 3 次 | FAILED（1） | 有 | `test_resolving_one_folder_does_not_unblock_other_issue` |

三次运行的失败用例各不相同，但每次都伴随同一只读异常 → 判定为**并发写库竞态**，而非个别用例的断言问题。日志：`class-scan-1..3.log`、`class-scan-results.csv`。

对比上一轮（`05095b9`，同机、同环境）：21 失败 + 2 错误 → 本轮 3 失败。启动路径的只读异常已消除，写库路径仍存在。

## 6. Electron 外链（问题 6 相关）

- `node --test tests/electron_external_links.test.js` → exit 0，1 subtest / 1 pass。
- 逐条矩阵（`link-matrix.js`）**7/7 PASS**：本项目 `releases/latest` 与具体 tag 允许；`http://`、其他仓库、`github.com.evil.example`、`issues/12`、非 URL 全部拒绝。

## 7. 构建产物

| 产物 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist\BiliFavOrganizer\BiliFavOrganizer.exe` | 8,835,821 B | `F3406D992B8140E9D67F44C938C2E7E448426D4A912098C5A889BE9077B43264` |
| `dist\electron\BiliFavOrganizer-neo-Setup-0.209.0-beta.2.exe` | 140,323,267 B | `89E3D23754C84D938D25C2D0CF72BCBAB6891F4460A13C7B38BA09BE73D74E45` |

- `npm ci` exit 0；PyInstaller exit 0（91.9s）；electron-builder exit 0（88.4s）。
- 未发布、未上传、未创建 Tag/Release。

## 8. 打包冒烟与真实界面（问题 5、6）

### 8.1 仓库冒烟脚本

`python tools/run_isolated_packaged_smoke.py .bfo-package-smoke-local --profile-name launch-profile-local` → **exit 1**

```
PACKAGED_SMOKE_ERROR=RuntimeError: Electron exited before readiness (code 2147483651).
electron_exit_code=2147483651 (0x80000003)  profile_file_count=10  sqlite_created=false
```

启动日志：GPU 进程连续崩溃 `exit_code=-1073741819`（0xC0000005）→ `FATAL ... GPU process isn't usable. Goodbye.`。与上一轮同机表现一致，属本机图形环境限制。

**BLOCKED**：脚本自身的 16 项渲染/设置/窗口状态检查与 `shell-quit.mjs` 优雅退出、端口释放、隔离目录清理**均未执行**，不记为通过。

补充诊断（不冒充标准冒烟）：同一 `win-unpacked` 追加 `--disable-gpu` 可正常启动并完成后端就绪与渲染加载，见 8.2/8.3。

### 8.2 保险库锁定（真实界面，合成夹具）

隔离 profile 预置合成保险库（`bound_mid=10000001`，测试口令；无真实账号、无网络）：

| 步骤 | 观察 |
| --- | --- |
| 启动 | 以锁定态启动，输入口令解锁成功 |
| 点击图标栏「锁定」 | 出现「🔒 应用已锁定」遮罩；日志「正在等待后台任务到达安全暂停点，然后锁定保险库」→「已锁定」 |
| 封库 | `library.sqlite3` 消失，目录为 `library.sqlite3.enc`、`vault-archives`、`database-manifest.json`、`.bfo-profile.lock`、`.scheduler-owner.lock` |
| 解锁 | 口令解锁成功，`library.sqlite3` 恢复，遮罩消失 |

证据：`gui/B0-locked-on-launch.png`、`gui/B3-after-lock.png`、`gui/B4-after-unlock.png`、`gui/gui-result.json`。

**BLOCKED（GUI 子项）**：任务运行中点击锁定 → 暂停 → 解锁恢复的实机演示需要真实 B 站会话驱动的扫描任务，按纪律禁止；该路径仅由 key3/key4 自动化证据覆盖（本轮 24/24 通过）。

### 8.3 更新按钮

| 检查 | 结果 |
| --- | --- |
| 点击「关于 → 检查」 | `当前是开发构建（dev），不做版本比较`；无「前往下载」链接（`release_link_present=false`） |
| 系统浏览器真实启动 | `msedge.exe --single-argument https://github.com/<user>-soaring-dev/bili-fav-organizer/releases/latest`（真实进程命令行采样） |
| 未知外链 | `window.open('https://evil.example/...')` 无任何浏览器进程携带该 URL；`shell.log` 出现「拒绝未知窗口请求」；「外部链接打开失败」计数 0 |

判定拆分：

- **外链通路：通过**（白名单 + `shell.openExternal` 在真实打包程序中生效，打开的是系统默认浏览器且 URL 正确）。
- **“点击更新按钮 → 打开 Release 页”端到端：BLOCKED（仅限本地 dev 构建）**。本地构建 `BUILD_VERSION = "dev"`，`version_comparable=false` 分支不渲染下载链接；正式发布流程在打包前重写版本号。未修改源码强行让链接出现。
- 本机顶栏按钮渲染为 0×0（缺 GPU 导致的布局异常），设置入口改用左侧图标栏，检查按钮以 DOM 点击触发，已在结果中标注。

补充核查（2026-10-22 22:46，项目负责人反馈“实际是打开 Edge 后要求登录”）：

- 以全新 Edge profile（隔离 `--user-data-dir`）直接打开该 Release URL，窗口标题为 `Release v0.210.4 稳定版：扫描缺失内容明细提示与安全重试 · GitHub` —— **页面正常加载，本机不存在 GitHub 登录墙**；因此“要求登录”属于 Edge 首次运行/Microsoft 账号引导，而非产品拦截或 GitHub 鉴权。
- 但在**本次验收的 dev 构建**中，按钮分支根本不会触发 `window.open`（状态文案与 `release_link_present=false` 双重证据），所以本轮无法据此判 PASS。
- 若项目负责人在**带版本号的构建/已安装版本**上点按并成功打开系统浏览器，则该端到端项应判 **PASS**，本轮记录随之更正为“dev 构建不可达”，而非产品缺陷。构建来源待确认（关于页版本显示 `dev` 还是 `0.210.x`）。

## 9. BLOCKED 清单

| # | 项 | 原因 | 已完成的替代验证 | 未覆盖范围 |
| --- | --- | --- | --- | --- |
| 1 | 仓库打包冒烟 | 本机 GPU 进程崩溃（0x80000003） | 同包 `--disable-gpu` 启动成功；真实界面锁定/外链验收通过 | 冒烟脚本 16 项检查、优雅退出、端口释放、目录清理 |
| 2 | 更新按钮端到端打开浏览器 | dev 构建版本比较分支不可达 | 同一 DOM 路由的真实浏览器启动证据（进程命令行） | 按钮→链接→浏览器的完整链路 |
| 3 | GUI 级任务运行中锁定/恢复 | 需真实 B 站会话 | key3/key4 各 12/12 自动化证据 | 实机任务暂停与恢复 |

## 10. SQLite 竞态是否消失

**启动路径：消失。** 24 次关键测试零只读异常，新增测试证明恢复先于分发；上一轮的「任务无法排队：attempt to write a readonly database」类失败（13 处）在 356 项全量中不再出现于 `_ensure_task_scheduler()`。

**写库路径：仍存在。** 扫描类用例整类连跑 3/3 均失败，且每次伴随 `attempt to write a readonly database`（`store.save_scan_stage` → `_transaction()` → `BEGIN IMMEDIATE`），失败用例每次不同。故不能判定“竞态已整体消失”。

根因证据要点：

- 只读异常发生在普通 `mode=rw` 连接的首个写事务上，而非生命周期拒绝（后者会抛 `DatabaseLifecycleError`）。
- 上一轮同机已观测：失败瞬间 `-wal` 为 0 字节且 `-shm` 缺失；SQLite 在无法建立 WAL 共享内存时会把连接降级为只读。
- 本轮 `MIG_030` 确定性失败同样指向 WAL/SHM 清理语义（探针结束后仍残留 `-wal`/`-shm`），与上面同族。

最小修复建议（未实施，需授权）：

1. 在 `store._transaction()` / 调度器写入路径，对 `sqlite3.OperationalError` 且消息含 `readonly database`、同时 `store.database_state() == READY` 的情形，做一次“关闭并重开连接 + 短退避”重试（仅在读已确认可写后继续），而不是让任务直接失败。
2. 排查 WAL/-shm 拆除与新连接建立的交叠：封库/初始化/诊断快照路径在 `wal_checkpoint(TRUNCATE)` 后删除 `-wal`/`-shm` 时，应确保没有并发 `mode=rw` 连接正在建立；`MIG_030` 可作为该问题的确定性回归点。
3. 复核 `MIG_030` 所述“探针结束后不应残留 sidecar”：确认探针连接在结束时确实全部关闭（当前观测为清理未发生）。

复现命令：

```
python tools/run_isolated_tests.py --test "test_scan_troubleshooting.ScanTroubleshootingTests"
python tools/run_isolated_tests.py --test "migration.test_vault_migration.VaultMigrationTests.test_MIG_030_probe_snapshot_does_not_create_profile_sidecars"
```

更完整的定位证据、改法与验证清单见同目录 `fix-proposal.md`（其中 A/B 两处改动建议均未实施）。

## 11. 安全边界核查

- 未修改、提交或推送任何源码；未改 `main`；未创建/合并 PR（本轮未推送任何分支）；未建 Tag、未发 Release、未上传发行资产。
- 未在既有开发工作区执行任何写操作。
- 生产数据目录 `%LOCALAPPDATA%\BiliFavOrganizer` 在本轮验收期间**未被读写**：该目录由其他行为在两轮验收之间（20:29–20:31）创建，目录内无 21:00 之后的任何修改；本轮全部运行仅使用 `%TEMP%\bfo-retest-20261022-0fd6391` 下的隔离 profile。
- 未使用真实 B 站账号与 Cookie；未执行任何远端写操作；保险库使用合成 `bound_mid`。
- 报告与日志不含凭据、Cookie、个人数据。

## 12. 最终结论

**FAIL**

理由（按验收标准逐条对照）：

1. 两项关键测试各 12/12 PASS —— 达标。
2. 完整回归 356 项中 3 项失败、退出码 1，且日志中仍存在 `attempt to write a readonly database` —— **不达标**。
3. 关键缺陷未整体消除（写库路径竞态可稳定复现于扫描类用例）。
4. 打包冒烟因本机 GPU 环境 BLOCKED，未取得 PASS。
5. 更新按钮：外链通路通过，端到端仅在 **dev 构建**下不可达；若业主确认观测来自版本化构建，该项应单独改判 PASS（不影响整体 FAIL）。

按“PASS 需同时满足 12/12 + 完整回归 + 构建 + 必需验收”的标准，本轮为 FAIL。未做任何源码修复、提交或发布，等待项目负责人下一步指令。

---

## 附录 A：两个残留缺陷的修复建议摘要

> 只给定位与改法，**未改动源码**；完整版见同目录 `fix-proposal.md`。

### A1. 写库路径 `attempt to write a readonly database`

机制：`-wal`/`-shm` 只在“至少一个连接打开”时存在，最后一个连接关闭即被删除。应用全部使用短生命周期连接（每次操作开/关）且多线程并发，当“关闭删除 `-shm`”与“新建连接创建 `-shm`”交叠时，新连接可能拿不到共享内存，SQLite 将其降级为只读，随后 `BEGIN IMMEDIATE` 抛出该异常。（旁证：上轮抓现场时 `-wal` 为 0 字节、`-shm` 缺失，紧接着的新连接可正常写。）

建议：

1. `core/database/lifecycle.py`：在 `DatabaseLifecycle` 上加进程内锁，仅包住 connect 与 close/释放两个临界区（close 必须在锁内，因为 sidecar 删除发生在 close 中）；不要包整个事务，避免写并发退化为串行。
2. `store._transaction()`：仅当 `database_state()=="READY"` 且错误含 `readonly database` 时，做一次有界重试（关闭、10–50ms 退避、重开）；第二次失败原样抛出，禁止掩盖。
3. **不要用 `SQLITE_FCNTL_PERSIST_WAL`**：它使 sidecar 常驻，与 `MIG_030` 的“空闲无 sidecar”不变量直接冲突。

### A2. `MIG_030` 探针结束残留 WAL/SHM

实测（SQLite 3.53.1，隔离目录）：

- 写连接关闭 → `-wal`/`-shm` 自动删除；
- **`mode=ro` 连接关闭后不会清理**，需其后一个可写连接关闭才清。

因此残留只有两种可能：探针结束时仍有连接未关，或最后关闭的是只读连接。定位方法：在 `vault_unlock` 返回后打印 `store.database_connections()`、`store.database_state()` 与 sidecar 列表。

建议：

1. 禁止对活的 profile 库使用 `mode="ro"`；只读需求一律“拷副本再只读”（`runtime_probe.database_snapshot()` 已是正确范式）。
2. 若是连接泄漏，按 owner 名补 `finally: close()`；调度器在等待条件变量期间不得持有连接。
3. 兜底：`vault_unlock()` 成功返回前，用可写连接 `PRAGMA wal_checkpoint(TRUNCATE)` 后 `close()`，确保最后关闭的是写连接。

### A3. 改完的验收判据

| 项 | 判据 |
| --- | --- |
| `ScanTroubleshootingTests` 整类 | 连跑 5 次全绿，日志无 `readonly` |
| 全量回归 | 356/356，exit 0 |
| `MIG_030` | 连跑 10 次全绿 |
| 锁定专项 | key3/key4 仍各 12/12（防回归） |

---

## 附录 B：修订记录

| 版本 | 时间 | 变更 |
| --- | --- | --- |
| v1 | 2026-10-22 22:40 | 初版：12 次连跑、全量回归、构建、冒烟、GUI 结果与 FAIL 结论 |
| v2 | 2026-10-22 22:43 | ① 增补 8.3 的 Edge 实测（全新 profile 可正常加载 Release 页，无登录墙，故“要求登录”属浏览器首次运行引导；dev 构建按钮仍不可达）；② 新增附录 A 修复建议摘要；③ 新增本修订记录；④ 结论条文中把“更新按钮 BLOCKED”细化为“仅 dev 构建不可达” |
