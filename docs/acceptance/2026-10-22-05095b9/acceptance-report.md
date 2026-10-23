> 归档副本来源：`acceptance/2026-10-22-05095b9` @ `cb5faa3fe9db79040a3230ecac709255ed6d5809`。公开副本中的个人目录已替换为 `<USER_HOME>`，GitHub noreply 地址已脱敏。
> 原始执行目录位于被测机器的 `%TEMP%` 下；本归档仅包含已复核的文本证据，截图暂缓公开。

> **公开范围：**文本报告和日志已进行路径 / 凭据模式复核；截图 PNG 未复制到主干，因为 About 面板显示本机数据目录，需先视觉脱敏。文中截图文件名仅供追溯，不代表主干已有对应图片。

# BiliFavOrganizer 本地独立验收报告

- 验收执行者：独立本地测试/验收代理（非功能开发代理）
- 执行时间：2026-10-22 19:00 – 20:25 (GMT+8)
- 验收目录（隔离克隆）：`<USER_HOME>\AppData\Local\Temp\bfo-acceptance-20261022-190221`
- 证据目录：`<USER_HOME>\AppData\Local\Temp\bfo-acceptance-20261022-190221\acceptance-evidence`

## 1. 验收对象

| 项目 | 值 |
| --- | --- |
| 仓库 | https://github.com/Martin-soaring-dev/bili-fav-organizer |
| 指定提交 | `05095b9779ec8eb208a26d3043f824ffa591a694` |
| 检出方式 | `git clone` → `git fetch origin main` → `git checkout --detach <sha>` |
| 实际 HEAD | `05095b9779ec8eb208a26d3043f824ffa591a694`（与指定 SHA 完全一致） |
| 分支状态 | detached HEAD；`origin/main` 也指向同一 SHA（提交为 main 顶端，且通过 `merge-base --is-ancestor` 校验） |
| 工作区状态 | `git status --porcelain` 为空（干净） |
| 提交作者 / 时间 | Martin (GitHub noreply address redacted) / Thu Oct 22 18:52:51 2026 +0800 |
| 提交说明 | `test: stabilize lock-pause regression suite`（正文：对齐取消相关测试替身与任务控制回调，封库前校验暂停态，Windows 打包校验中加入 Electron 外链单测） |

## 2. 环境信息

| 组件 | 版本 |
| --- | --- |
| Windows | Microsoft Windows NT 10.0.26300.0（26H2，Build 26300，AMD64） |
| Git | 2.55.0.windows.3（PortableGit） |
| Python | 3.12.15（独立 venv：`<验收目录>\.venv`） |
| Node.js / npm | v22.22.2 / 10.9.7 |
| Playwright | 1.63.0；Chromium 1243（Chrome for Testing 153.0.8010.12） |
| PyInstaller | 6.22.3 |
| Electron / electron-builder | 44.7.0 / 26.15.3 |
| SQLite（venv 内） | 3.53.1 |

环境偏差与处置（均为验收侧环境，不涉及代码改动）：

1. 本机 `py -3.12` 未注册 3.12 解释器，改用同小版本解释器路径 `...\uv\python\cpython-3.12.15-...\python.exe` 创建 venv。
2. 本机出口经本地 MITM 代理（SteamTools 根证书），git 需 `http.sslBackend=openssl` + 合并 CA 包；pip 用 `PIP_CERT`，Node 用 `NODE_EXTRA_CA_CERTS`（仅验收进程内环境变量）。
3. Chromium 官方 CDN 下载 20 分钟无进度，改用 `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright` 完成安装（1m23s）。
4. electron-builder 首次运行在下载 winCodeSign/nsis 资源时 600s 超时，改用 `ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/` 后成功。
5. 执行环境注入的三个变量会破坏测试真实性，验收侧已清除：`PYTHONPATH`（指向执行环境的 Python 站点垫片 sitecustomize）、`ELECTRON_RUN_AS_NODE=1`（导致打包壳把自身当 Node 进程运行）、安全删除钩子（`Path.unlink` 被重定向到回收站并在批量删除时 `SystemExit(1)`，直接阻断 SQLite 生命周期中的候选库删除）。

## 3. 关键回归（阶段 2）

| 测试 | 首次结果 | 耗时 |
| --- | --- | --- |
| `test_scan_empty_page_continuation.EmptyPageContinuationTests` | PASS（exit 0，3 tests） | 7.7s |
| `test_store_and_llm_budget.LLMContextBudgetSplitTests` | PASS（exit 0，5 tests） | 7.0s |
| `...test_vault_lock_interrupts_bilibili_rate_limit_backoff_and_pauses_task` | **FAIL（首次）/ 间歇** | ~5-12s |
| `...test_vault_lock_waits_for_checkpoint_then_resumes_same_process_runner` | **FAIL（首次）/ 间歇** | ~7-13s |

两项锁定关键测试的重复执行（同一提交、同一机器、不同环境组合）：

| 组合 | key3 结果 | key4 结果 |
| --- | --- | --- |
| 初始（宿主默认 env） | FAIL | FAIL |
| 重试 1/2 | PASS / FAIL | FAIL / PASS |
| 关闭删除垫片（空闲） | FAIL,PASS,PASS | FAIL,PASS,PASS |
| CI 等价 env（无 PYTHONPATH、git+node 在 PATH） | PASS,FAIL,FAIL | FAIL,PASS,PASS |
| 禁用文件系统钩子后 | PASS,FAIL,PASS | PASS,FAIL,PASS |
| **合计** | **12 次中 6 次通过** | **12 次中 7 次通过** |

失败形态（两种，同一根因族）：

- `TypeError: cannot unpack non-iterable JSONResponse object`（测试第 29 行），其真实错误体为
  `{"ok":false,"error":"任务无法排队：attempt to write a readonly database"}`（HTTP 503）。
- `AssertionError: ... status=<TaskStatus.FAILED ...> error={'type': 'OperationalError', 'message': 'attempt to write a readonly database'}`。

根因定位（只读排查，未改任何代码）：

1. 堆栈固定落在 `server._ensure_task_scheduler()` → `SqliteRemoteOperationRepository.recover_stranded_dispatches()` → `_Transaction.__enter__` → `conn.execute("BEGIN IMMEDIATE")` → `sqlite3.OperationalError: attempt to write a readonly database`（`server.py:2256-2258`、`core/remote_operations.py:781`）。
2. 失败瞬间的数据目录快照：`library.sqlite3` 存在（184320 B）、`library.sqlite3-wal` 大小 0、**没有 `-shm` 文件**；紧接着用全新的 `mode=rw` 连接对同一文件 `BEGIN IMMEDIATE` 成功 → 文件与权限正常，问题出在该连接（WAL 共享内存建立失败时 SQLite 会把连接降级为只读）。
3. 对照实验：同机纯 sqlite（`mode=rwc` 建库 + WAL + 提交，再 `mode=rw` + `BEGIN IMMEDIATE`）循环 40 轮共 80 次写操作 **0 失败**，说明不是磁盘/权限的普遍性问题，而是本应用连接层与 WAL 生命周期相关的时序问题。
4. `_ensure_task_scheduler()` 里 `_TASK_SCHEDULER.start()`（2254 行）先于启动期恢复连接（2256 行）执行；此时调度器自身连接与恢复连接对同一个 WAL/-shm 存在交叉，疑似形成竞态窗口。

最小修复建议（仅供参考，未实施）：把启动期 `recover_stranded_dispatches()` 放到 `_TASK_SCHEDULER.start()` **之前**（或复用同一连接），并保证 WAL/-shm 的拆除不与新建 `mode=rw` 连接交叠；若无法消除，则对 `SQLITE_READONLY` 做一次带退避的重开连接重试（仅限启动期恢复路径），以免把可恢复的瞬态错误升级为任务 503。

## 4. 完整 Python 回归（阶段 3）

入口固定为 `python tools/run_isolated_tests.py`（临时副本 + 隔离 LOCALAPPDATA/TEMP + 网络出口封死 127.0.0.1:9）。

| 运行 | 用例数 | FAIL | ERROR | 耗时 | 退出码 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| A 首次（宿主默认 env） | 345 | 9 | 81 | 188.1s | 1 | 81 个 ERROR 主要由执行环境站点垫片劫持 `Path.unlink`（`SystemExit: 1`）与 `git`/`node` 不在 PATH 造成 |
| B 关闭删除垫片 | 345 | 9 | 33 | 149.9s | 1 | 迁移类因 `git` 缺失仍全挂 |
| C **CI 等价 env（采信）** | **354** | **21** | **2** | **461.2s** | **1** | 与 CI 一致：`BFO_REQUIRE_PLAYWRIGHT=1`、`git`+`node` 在 PATH、无 PYTHONPATH |

采信运行（C）的失败分类（日志：`evidence/full-python-regression-ci.log`）：

- 13 个失败块直接命中 `attempt to write a readonly database` 或其下游症状（`scan` 返回 503 JSONResponse，被测用例按 dict 断言）。
- 另有 11 个非只读断言失败，集中在：
  - `migration`: `test_MIG_018_second_instance_with_active_writer_fails_safely_and_recovers`（期望 200，实得 500）、`MIG_025`、`MIG_028`、`MIG_030`
  - 批次执行类：`test_business_failure_marks_batch_failed_and_continues`、`test_move_updates_membership_before_reporting_done`、`test_rate_limit_stops_run_and_keeps_batch_pending`、`test_unexpected_exception_does_not_write_next_group`
  - 扫描/异常类：`test_interrupted_paged_folder_restarts_from_the_first_page`、`test_resolving_one_folder_does_not_unblock_other_issue`、`test_scan_resume_skips_complete_folders_and_never_replays_writes`
- 跳过项：汇总行未报告 skipped；`BFO_REQUIRE_PLAYWRIGHT=1` 使 UI 回归（`test_ui_regressions`）真实执行而非跳过，本次运行中已实际启动 uvicorn + Chromium。
- 结论：完整回归 **未通过**。关键 4 项中两项（锁定相关）也不稳定，不能以“关键测试通过”替代。

## 5. Node 外部链接白名单（阶段 4）

`node --test tests/electron_external_links.test.js` → exit 0，1 subtest / 1 pass / 0 fail。

逐条矩阵（`evidence/link-matrix.log`，`MATRIX_FAILURES=0`）：

| 输入 | 期望 | 实际 |
| --- | --- | --- |
| `https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/latest` | 允许 | 允许 ✅ |
| `https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/tag/v0.210.4` | 允许 | 允许 ✅ |
| `http://github.com/.../releases/latest` | 拒绝 | 拒绝 ✅ |
| `https://github.com/Martin-soaring-dev/another-repo/releases/latest` | 拒绝 | 拒绝 ✅ |
| `https://github.com.evil.example/.../releases/latest` | 拒绝 | 拒绝 ✅ |
| `https://github.com/.../issues/12` | 拒绝 | 拒绝 ✅ |
| `not a URL` | 拒绝 | 拒绝 ✅ |

（单测仅证明白名单逻辑；真实浏览器行为见第 8 节。）

## 6. 构建产物（阶段 5）

未发布、未上传、未创建 Tag/Release。

| 产物 | 路径 | 大小 | SHA-256 |
| --- | --- | --- | --- |
| 冻结后端 | `dist\BiliFavOrganizer\BiliFavOrganizer.exe` | 8,823,767 B | `52AAEBE054D99C07F68347CB960461253AF0C6F76F7689B1004504AA2E97406B` |
| NSIS 安装包 | `dist\electron\BiliFavOrganizer-neo-Setup-0.209.0-beta.2.exe` | 140,312,482 B | `D443EE67F8F82E64864F8E10B850E7898801120F713C8F25B3C7E140463FEB51` |

- PyInstaller：exit 0，100.7s（`evidence/pyinstaller-build.log`）
- electron-builder：首轮 exit 1（下载依赖 600s 超时），改用镜像后 exit 0，135.3s（`electron-builder.log`、`electron-builder-retry.log`）
- 版本参数 `0.209.0-beta.2` 仅用于打包校验，不代表要求发布。

## 7. 打包冒烟（阶段 6）

`tools/run_isolated_packaged_smoke.py .bfo-package-smoke-local --profile-name launch-profile-local` → **exit 1**

| 尝试 | 结果 | 直接原因 |
| --- | --- | --- |
| 1 | exit 1，Electron 退出码 9，profile 文件 0 个 | `bad option: --remote-debugging-port=19222`（执行环境注入 `ELECTRON_RUN_AS_NODE=1`） |
| 2 | exit 1，退出码 `2147483651`（0x80000003），profile 文件 10 个 | GPU 进程连续崩溃后 `FATAL ... GPU process isn't usable. Goodbye.` |
| 3（禁用执行环境文件系统钩子后） | 同上 | 排除宿主文件系统沙箱因素 |

关键日志（`launch.stderr.log`）：

```
ERROR:content\browser\gpu\gpu_process_host.cc:1035] GPU process exited unexpectedly: exit_code=-1073741819
FATAL:content\browser\gpu\gpu_data_manager_impl_private.cc:417] GPU process isn't usable. Goodbye.
```

同一份 `win-unpacked` 追加 `--disable-gpu` 后可正常启动：后端就绪、渲染进程加载动态端口、门禁探针 200（见第 8 节）。因此判定为**本机图形环境限制（无可用 D3D/视频设备）导致仓库固定启动参数下无法启动**，而非产品缺陷；但按验收规则，该阶段结论为 **BLOCKED**（未取得冒烟脚本自身的 PASS），不写成通过。

未验证到的冒烟项：`shell-smoke.mjs` 的 16 项检查（顶栏/主题/设置面板开合/窗口尺寸持久化与还原）与 `shell-quit.mjs` 的优雅退出、端口释放，均因脚本无法启动而未执行。

## 8. 真实桌面交互（阶段 7）

打包程序以隔离 profile 启动：`--disable-gpu --no-sandbox --remote-debugging-port=19232`（本机无可用 GPU，必须禁用；其余参数保持默认）。数据目录、LOCALAPPDATA、TEMP 全部指向验收临时目录。

### A. 更新按钮

- 顶部栏设置按钮在本机渲染为 0×0（GPU 缺失导致的布局异常），改用左侧图标栏「设置」→「关于」进入，`检查` 按钮通过 DOM 点击触发（已记录该限制）。
- 点击后界面状态：`当前是开发构建（dev），不做版本比较`；`关于-最新版本` 显示 `开发构建不做比较`；`版本 dev`；**未出现「前往下载」链接**（截图 `截图待脱敏、未归档：A2-update-check-result.png`）。
- 原因（代码事实）：本地构建 `server.py` 中 `BUILD_VERSION = "dev"`，`update_check` 对不可比版本直接返回 `version_comparable=false`，前端在该分支不渲染 Release 链接；正式发布由 `release-beta.yml` 在打包前重写 `BUILD_VERSION`。
- 因此“点击更新按钮 → 系统浏览器打开 Release 页”在**本次可复现的本地构建里并不成立**：该分支不可达。按规则记为 **BLOCKED（需发布版本构建才能验证；改 `BUILD_VERSION` 属修改源码，未获授权）**。

为验证按钮背后的真实外链通路，追加同一 DOM 路由（`window.open(<项目 Release URL>)`，即「前往下载」链接所用路径）：

- OS 层证据：`msedge.exe --single-argument https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/latest`（`evidence/browser-samples.txt`），即**由系统默认浏览器打开，且 URL 正确、未在 Electron 内部打开**。
- `shell.log` 中「外部链接打开失败」计数 = 0（无静默失败）。

### A3. 未知外链限制

- `window.open('https://evil.example/bfo-blocked-check')` → 采样窗口内 **无任何浏览器进程携带该 URL**；
- `shell.log` 出现 `拒绝未知窗口请求`（`shell_log_reject_unknown=true`）；
- 结论：未知外链仍受限 ✅

### B. 保险库锁定（真实界面）

真实 B 站账号不可用（引导页 mid 为 `type="hidden"`，只能由登录 Cookie 填充），故按验收规则使用**合成夹具**：以集成测试同款代码路径在隔离 profile 预置保险库（`bound_mid=10000001`、测试口令），全程无网络、无真实账号。

| 步骤 | 观察 | 证据 |
| --- | --- | --- |
| 启动 | 应用以锁定态启动（`app_started_locked=true`），解锁后进入主界面 | `截图待脱敏、未归档：B0-locked-on-launch.png`、`B1-unlocked.png` |
| 点击左侧图标栏「锁定」 | 弹出「🔒 应用已锁定」遮罩；日志显示「正在等待后台任务到达安全暂停点，然后锁定保险库」→「已锁定」 | `截图待脱敏、未归档：B3-after-lock.png` |
| 封库结果 | `library.sqlite3` 消失；目录变为 `library.sqlite3.enc`、`vault-archives`、`database-manifest.json`、`.bfo-profile.lock`、`.scheduler-owner.lock` | `evidence/gui-result.json: data_dir_after_lock` |
| 解锁 | 输入应用密码解锁成功，`library.sqlite3` 恢复，锁定遮罩消失 | `截图待脱敏、未归档：B4-after-unlock.png`、`db_after_unlock=true` |

未覆盖（BLOCKED）：「任务运行中点击锁定 → 任务安全暂停并保存进度 → 解锁后恢复」的 GUI 级演示。原因：需要真实 B 站会话驱动的扫描任务，验收纪律禁止使用真实账号执行任何远端读写。该项仅由自动化集成回归覆盖，而该两项当前不稳定（12 次中 6/7 通过），故不能作为 PASS 依据。

## 9. 安全检查

- 未修改、提交、推送任何源码；未创建/推送 Tag；未发布 Release 或上传发行资产；未合并 PR、未改动 `main`。
- 未在已有开发工作区执行 `reset --hard`、`clean -fdx`、stash 等破坏性操作；整个过程只读取该工作区是否存在，未在其中执行任何命令。
- 生产数据目录 `%LOCALAPPDATA%\BiliFavOrganizer` **不存在**（验收结束时复核），全程未创建、未读取、未覆盖任何生产数据库/配置/Cookie/密钥。
- 所有测试与 GUI 运行均使用 `%TEMP%\bfo-acceptance-*` 下的隔离 profile，数据目录、LOCALAPPDATA、APPDATA、TEMP 全部重定向。
- 未使用真实 B 站账号；未执行移动/删除/合并收藏夹等任何远端写操作；保险库绑定使用合成 mid `10000001`。
- 报告与日志中不含凭据、Cookie 或个人数据（测试口令为本次随机设定的本地测试口令，仅用于隔离 profile）。

## 10. 遗留问题清单

| # | 问题 | 级别 | 复现方式 |
| --- | --- | --- | --- |
| 1 | 锁定相关集成测试不稳定：`SQLITE_READONLY`（`attempt to write a readonly database`）间歇出现（key3 6/12、key4 7/12 通过），并扩散到 `scan`/批次/迁移等多处用例 | 阻断 | `python tools/run_isolated_tests.py --test scheduler.test_server_integration.ServerSchedulerIntegrationTests.test_vault_lock_interrupts_bilibili_rate_limit_backoff_and_pauses_task`（重复执行即可复现） |
| 2 | 完整回归 354 项中 21 FAIL + 2 ERROR（多数为 #1 的下游症状，另有 4 个 `MIG_*` 与 4 个批次执行类非只读失败待复核） | 阻断 | `python tools/run_isolated_tests.py` |
| 3 | 本机无可用 GPU 时打包程序直接崩溃（`GPU process isn't usable`），仓库冒烟脚本未提供软件渲染兜底 | 环境/健壮性 | 不带 `--disable-gpu` 启动 `dist\electron\win-unpacked\BiliFav Organizer.exe` |
| 4 | 本地 dev 构建下更新按钮无法触达 Release 链接分支，无法完成“点击即开浏览器”的端到端验证 | 待授权/待定 | 启动本地构建 → 设置 → 关于 → 点击检查 |
| 5 | GUI 级“任务运行中锁定/恢复”无法在不接触真实账号的前提下演示 | 待授权 | 需要真实会话的扫描任务 |

## 11. 最终结论

**FAIL**

判定依据（均为实际执行证据，非推断）：

1. 指定提交上的两项锁定关键回归在本机以约 50% 概率失败，根因为 `attempt to write a readonly database`，并扩散到全量回归（354 项中 21 FAIL、2 ERROR）。
2. 完整 Python 回归退出码 1，未通过。
3. 打包冒烟脚本因本机 GPU 崩溃无法完成（BLOCKED 子项），未取得 PASS。
4. 更新按钮的真实浏览器行为在当前构建不可达（BLOCKED 子项）；虽已用同一 DOM 路由证明外链通路与默认浏览器打开正常，但不能据此把该项记为 PASS。
5. 已通过的部分：Electron 外链白名单单测（7/7）、真实运行中未知外链被拒绝、真实界面保险库锁定/解锁（封库为 `.enc` 并恢复）、后端与安装包构建、更新检查的 UI 真实响应。

未做任何修复、提交、推送、合并或发布。等待项目负责人下一步指令。

