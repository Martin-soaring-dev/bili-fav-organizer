# Windows 验收回报：scheduler owner 锁修复（候选 4baa66e）

- 验收轮次：第 4 轮（owner 锁，Windows 目标平台）
- 验收日期：2026-10-20
- 本轮授权边界：只做验收。未创建 PR、未合并、未打 tag、未发布。

## 机器可读摘要

```yaml
repo: Martin-soaring-dev/bili-fav-organizer
target_branch: feat/reliability-scheduler-implementation
checkout_mode: detached HEAD（未在 clone 内创建验收分支）
candidate_head: 4baa66e628538cfb2762b63bed82facd1b47f28a
candidate_tree: 2b4a7263705ff345fa1fbab6c6173f0510b55673
candidate_parent: f590ae8cbab2a0c58e3bb90c49550b8bdd67134f
commit_subject: "fix(scheduler): acquire Windows owner lock without reading locked bytes"
platform: Windows 11 Pro 10.0.26300 x64
powershell: 5.1.26100.9711
python: 3.12.13
playwright: 1.63.0
chromium: Chrome for Testing 153.0.8010.12 (playwright build v1243)
playwright_gate: BFO_REQUIRE_PLAYWRIGHT=1
full_suite: {discovered: 326, pass: 326, fail: 0, error: 0, skip: 0, seconds: 227.0, exit_code: 0}
targeted_owner_lock: {discovered: 3, pass: 3, fail: 0, error: 0, skip: 0, seconds: 1.458, exit_code: 0}
targeted_server_integration: {discovered: 4, pass: 4, fail: 0, error: 0, skip: 0, seconds: 9.669, exit_code: 0}
ui_tests_executed: 7
ui_tests_passed: 7
ui_tests_skipped: 0
previously_failing_test: PASS
repo_source_modified: false
repo_pushed_during_acceptance: false
pr_created: false
tag_created: false
```

## 1. 候选身份

目标分支唯一为 `feat/reliability-scheduler-implementation`。取件方式：

```powershell
git clone --no-checkout --branch feat/reliability-scheduler-implementation `
  https://github.com/Martin-soaring-dev/bili-fav-organizer.git <disposable-dir>
git -C <disposable-dir> config core.autocrlf false
git -C <disposable-dir> switch --detach 4baa66e628538cfb2762b63bed82facd1b47f28a
```

`git rev-parse HEAD` / `HEAD^{tree}` / `HEAD^` 分别得到 candidate_head / candidate_tree / candidate_parent，与交付消息一致。检出为 detached HEAD，因此 clone 内不存在本地验收分支；未切换、未清理任何既有工作树。验收期间 `git status --short` 始终为空。

## 2. 环境

| 项目 | 值 |
|---|---|
| OS | Windows 11 专业版 10.0.26300（64-bit），build 26300 |
| PowerShell | 5.1.26100.9711 |
| Python | 3.12.13（独立 venv，仅安装 `requirements-test.txt`） |
| Playwright | 1.63.0 |
| Chromium | Chrome for Testing 153.0.8010.12（playwright build v1243），实测 launch 成功 |
| 严格门禁 | `BFO_REQUIRE_PLAYWRIGHT=1` 全程设置 |

本机原先只有 chromium build 1161，缺少 playwright 1.63.0 所需的 1243。按 CI 相同的锁定版本安装到 `%LOCALAPPDATA%\ms-playwright`（约 310 MB 测试工具链），未安装 skip，未降级版本。

## 3. 执行命令与结果

每条命令都带 `BFO_REQUIRE_PLAYWRIGHT=1`，通过 `tools/run_isolated_tests.py` 在隔离 checkout 中运行。

| # | 命令 | 发现 | PASS | FAIL | ERROR | SKIP | 耗时 | 退出码 | 日志 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `--test scheduler.test_owner_lock` | 3 | 3 | 0 | 0 | 0 | 1.458s | 0 | `logs/r1-owner-lock.shortpath.log.txt` |
| 2 | `--test scheduler.test_server_integration` | 4 | 4 | 0 | 0 | 0 | 9.669s | 0 | `logs/r2-server-integration.shortpath.log.txt` |
| 3 | 全量（首次，长路径 clone） | 326 | 295 | 2 | 29 | 0 | 215.334s | 1 | `logs/03-full-regression.longpath.log.txt` |
| 4 | 全量（短路径 clone） | 326 | 326 | 0 | 0 | 0 | 227.026s | 0 | `logs/r3-full-regression.shortpath.log.txt` |
| 5 | 全量（逐行无折行复核） | 326 | 326 | 0 | 0 | 0 | 227.574s | 0 | `logs/r4-full-regression-clean.shortpath.log.txt` |

第 4、5 次为同一候选的两次独立全量，unittest 自报 `Ran 326 tests in ...` + `OK`（无 `skipped=` 标注），逐行统计为 326 个 `ok` 结果标记。

## 4. 七项 Chromium UI 逐项结果

均实际执行、无跳过（`skipped=0`）。

| # | 测试 | 结果 |
|---|---|---|
| 1 | `test_fast_mode_stops_after_paused_scan_instead_of_analyzing` | ok |
| 2 | `test_fast_mode_stops_after_cancelled_scan_instead_of_analyzing` | ok |
| 3 | `test_fast_mode_still_continues_after_completed_scan` | ok |
| 4 | `test_empty_model_config_does_not_storm_the_config_endpoint` | ok |
| 5 | `test_closed_scan_issue_popup_is_not_reopened_by_scan_idle` | ok |
| 6 | `test_task_center_separates_progress_units_and_shows_checkpoint` | ok |
| 7 | `test_operations_tab_does_not_invent_a_reconciliation_record` | ok |

说明：这七项由 Playwright 驱动真实 Chromium 做断言，测试本身不产出截图文件，因此本目录没有 UI 截图证据。若维护者需要视觉证据，需另行安排一轮带 `page.screenshot()` 的采集，不能把断言通过当成截图验收。

## 5. 重点用例

| 用例 | 结果 |
|---|---|
| `test_vault_seals_business_files_without_reading_or_replacing_runtime_locks`（上一候选唯一失败） | ok |
| `test_real_process_contention_and_reacquisition_for_empty_and_existing_files` | ok |
| `test_windows_acquire_never_reads_or_writes_lock_bytes` | ok |
| `test_windows_contender_reports_ownership_and_closes_handle_without_io` | ok |
| `test_scan_route_persists_and_runs_through_the_scheduler` | ok |

子进程竞争用例同时覆盖空锁文件与已含 1 字节的锁文件：父进程持锁时子进程退出码 23（被拒），父进程释放后子进程退出码 0（取锁成功），且锁文件字节内容与初始值逐字节一致——没有把任意 `PermissionError` 当成合格竞争。

## 6. 首次全量出现的 2 FAIL + 29 ERROR（环境缺陷，非候选缺陷）

第 3 次全量有 31 个问题用例（2 FAIL + 29 ERROR），全部同一根因：验收方最初把 clone 放在带时间戳的长目录名下，导致 SQLite 候选库路径超过 Windows MAX_PATH，而本机 `LongPathsEnabled=0`。

- 异常：`sqlite3.OperationalError: unable to open database file`（SQLite 层 `SQLITE_CANTOPEN`；Python 侧 `errno`/`winerror` 均为 `None`）
- 抛出位置：`core/database/lifecycle.py:416`，`connect_for_initialization()` → `sqlite3.connect(sqlite_uri(target, mode), uri=True, timeout=30)`，`mode="rwc"`
- 被拒绝的操作：创建/打开首次初始化候选库
- 路径：`<隔离checkout>\tests\.test-data\scan-recovery\troubleshooting-<32hex>\data\.library.sqlite3.<32hex>.initializing`，按占位符长度重建为 **266 字符**（占位符长度见下）
- 对照实验（`tools/pathlen_probe.py`）：同一 `sqlite_uri` 构造方式下，267 字符 FAIL、227 字符 OK

改用文档规定的短目录名（5 字符）后，第 4、5 次全量中这 31 个用例全部 ok。逐项核对结果：31/31 ok。

`test_MIG_017` / `test_MIG_018` 表现为 `AssertionError: 200 != 500`，`test_MIG_014` 表现为 `KeyError: 'decrypted_snapshot'`，都是 onboarding 内嵌 SQLite 打开失败的下游症状。

处理原则：未修改任何源码、未放宽任何断言、未忽略 `PermissionError`；首次失败日志原样保留（`logs/03-full-regression.longpath.log.txt`），未被后续重跑结果覆盖。

## 7. 隔离与数据边界

- `tools/run_isolated_tests.py` 把 checkout 复制到 `<clone>/.bfo-isolated-tests-<8>/checkout`，并将 `LOCALAPPDATA`、`USERPROFILE`、`HOME`、`TEMP`、`TMP` 全部指向同一工作区的 `profile`/`home`/`temp`；`BILI_FAV_ORGANIZER_DATA_DIR` 被显式移除。
- 真实用户 profile 目录（含 `data/library.sqlite3`、`config.json`、`secrets.json`）在测试后最新修改时间仍为 2026-09-26，未被打开或写入。
- 两次全量运行后 clone 的 `git status --short` 为空。
- 未使用真实 B 站账号、Cookie、恢复码或 API Key；远端写请求由测试内 mock 承担；runner 将 `HTTP_PROXY`/`HTTPS_PROXY` 指向 `127.0.0.1:9` 以阻断外网。
- 未创建 PR、未合并、未打 tag、未发布。

## 8. 脱敏说明

为使证据可入库，日志中的个人路径已替换为占位符，长度保持一致以便重建路径长度结论：

| 占位符 | 原文字符数 |
|---|---|
| `<USERPROFILE>` | 19 |
| `<LONGPATH-CLONE>` | 41 |
| `<RUN-TEMP>`（`.bfo-isolated-tests-<8hex>`） | 28 |
| `<SHORTPATH-CLONE>` | 5 |

除上述替换外，日志内容未做任何删改。已确认无 Cookie、token、API Key、恢复码等敏感值。

## 9. 结论

在候选 `4baa66e` / tree `2b4a726` 上，Windows 目标平台全量回归为 **326 项、326 PASS、0 FAIL、0 ERROR、0 SKIP**，七项 Chromium UI 实际执行并通过，上一候选唯一失败用例转为通过，owner 锁在真实 Windows 字节锁语义下的拒读/竞争/释放后重取行为符合契约。

清单中仍有未执行项：T10（截图式 UAT）仍为 NOT_RUN，T11b（安装包/升级验收）与 T12（发布）未动。本报告不构成发布授权。
