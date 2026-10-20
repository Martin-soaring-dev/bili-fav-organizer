# BiliFavOrganizer Windows 验收报告｜候选 1981a94（T8 + T10）

本目录是**本轮**证据，候选为 `1981a94`。上一候选 `c75a06b` 的 T8/T10 结果见同级目录
`../20261020-windows-t10-uat/`，那是**历史候选**结果，不能当作本候选的验收结论。

## 1. 候选身份（T8/T10 开始与结束时各复核一次，一致）

| 项 | 值 |
|---|---|
| 仓库 | Martin-soaring-dev/bili-fav-organizer |
| 候选分支 | `feat/reliability-scheduler-implementation` |
| 候选 HEAD | `1981a94ed750d7be4e3404d6622ad776708f2091` |
| 候选 tree | `e943cc30ebf0b6dffcf69866c01761fba9928b32` |
| parent | `aa9dfb35033186fcb2e04db19cfbc250ecf37873`（T3 代码提交） |
| 测试对象 | 远端分支**最新 HEAD**；`aa9dfb3` 是其祖先，两者差异仅一个文档提交 `1981a94` |
| checkout | `C:\BFOU5`，detached，`git status --short` 为空；T8 前后 HEAD 均为 `1981a94` |
| 平台 | Windows 11 专业版 build 26300（64-bit） |
| Python / Playwright / Chromium | 3.12.13 / 1.63.0 / Chrome for Testing 153.0.8010.12（build v1243） |

`c75a06b..1981a94` 变更：`store.py`（+217/-57，T3 schema 列/约束校验）、
`tests/database/test_schema_migration.py`（+65）、任务清单文档。本轮未修改任何产品源码或测试。

## 2. T8 Windows 全量回归 — PASS

| 项 | 结果 |
|---|---|
| 命令 | `python tools/run_isolated_tests.py`（`BFO_REQUIRE_PLAYWRIGHT=1`） |
| 开始 / 结束 | 2026-10-20 22:37:27 → 22:41:06 |
| 耗时 | 219.6 秒（unittest 计 216.994 秒） |
| 发现 / PASS / FAIL / ERROR / SKIP | **330 / 330 / 0 / 0 / 0** |
| 退出码 | 0 |
| 原始日志 | `logs/t8-full-regression.raw.log.txt`（157040 字节，SHA-256 `3c792601…f67c7`） |
| 机器可读汇总 | `t8-summary.json` |

unittest 自报 `Ran 330 tests in 216.994s` + `OK`（无 `skipped=` 标注），日志中无任何
`FAIL:` / `ERROR:` 标题行。七项 Chromium UI 与 T3 新增的 schema 契约校验用例均在本轮实际执行。

## 3. T10 实机 UAT — 14/14 PASS（同一候选 `1981a94`）

真实 `uvicorn` + 真实 Chromium（1440×900）驱动仓库自带 `static/index.html` + `app.js`，
每场景独立 disposable profile（`C:\BFO-UAT\profiles\<场景>`），外网指向 `127.0.0.1:9`
关闭端口，B 站调用由记录型假 session 承担。

| 编号 | 场景 | 状态 | 截图 |
|---|---|---|---|
| T10-01 | 首次配置 | PASS | 6 |
| T10-02 | 任务中心 | PASS | 1 |
| T10-03 | 旧格式数据计数与停止 | PASS | 4 |
| T10-04 | 运行中锁库与恢复 | PASS | 4 |
| T10-05 | 重启中断恢复 | PASS | 2 |
| T10-06 | 远端写入核对 | PASS | 3 |
| T10-07 | 用户停止任务 | PASS | 3 |
| T10-08 | 空模型配置请求行为 | PASS | 2 |
| T10-09 | 已关闭的问题弹窗 | PASS | 4 |
| T10-10 | 暂停与取消 | PASS | 2 |
| T10-11 | 进度、检查点与暂停原因 | PASS | 2 |
| T10-12 | 空 reconciliation 状态 | PASS | 1 |
| T10-13 | 快速模式任务状态 | PASS | 3 |
| T10-14 | Schema 7 升级界面路径 | PASS | 2 |

**合计：PASS 14 · FAIL 0 · BLOCKED 0 · NOT_RUN 0，截图 39 张**，总耗时 255.9 秒。
逐项核验项、备注与状态快照见 `results/T10-*.json`。

T10-14 迁移核验（夹具 `tests/database/fixtures/v0209_schema7.sqlite3`，SHA-256
`511f66af…b273`，迁移前后不变）：schema 7→8、`database_state=READY`、
folders/videos/folder_items 行数与关键 ID 一致、`integrity_check=ok`、
`foreign_key_check` 无违规，界面截图可见 `v0.209 synthetic folder` 与条目
`synthetic video / BVFIXTURE0001`。

已知限制（如实保留）：T10-13 的扫描终态由 `/api/scan/status` 受控注入；T10-06 仅 mock
远端写入；浏览器为无头 Chromium 而非 Electron 外壳。

## 4. 其他门禁状态

- **T9 = FAIL（风险已接受）**。2026-10-20 06:36 以 admin 权限只读核实：`release-approval`
  环境不存在（404）、`main` 无分支保护（404）、rulesets 为空、旧式 tag protection 404。
  维护者明确接受该风险，不配置发布保护。**不得判 PASS**，发布保持 NO-GO。
  证据见 `../20261020-windows-t10-uat/t9-github-protection-readonly.md`。
- **T3 = PARTIAL**：schema 5–8 支持下限已决策并落地（`_MIN_SUPPORTED_SCHEMA = 5`，
  历史夹具 `v0200_schema5.sqlite3` / `v0203_schema6.sqlite3`），本轮 `aa9dfb3` 又补上
  schema 列类型与 CHECK 契约校验；T8 在本候选上覆盖其用例。
- **未运行**：T11a（Draft PR checks，需 PR 授权）、T11b（merge 后安装包验收）、T12（发布）。
  需要维护者另行授权；本轮未创建 PR、未合并、未打 tag、未发布。

## 5. 证据索引

| 路径 | 内容 |
|---|---|
| `t8-summary.json` | T8 机器可读结果 |
| `logs/t8-full-regression.raw.log.txt` | T8 完整原始日志 |
| `results/T10-*.json` | 14 个场景的核验项/备注/耗时 |
| `screenshots/` | 39 张真实 UI 截图 |
| `logs/*.server.log.txt` | 每场景服务端日志 |
| `logs/*.mock-calls.json` | 假 session 记录的远端调用 |
| `logs/startup-and-isolation.log.txt` | 隔离路径与真实 profile 未触碰审计 |
| `logs/mock-remote-write.log.txt` | mock 写调用汇总 |
| `logs/read-only-state-checks.log.txt` | 只读 API 观察 |
| `environment.txt` | 环境快照 |
| `sha256-manifest.txt` | 本目录全部文件的 SHA-256 |

## 6. 边界确认

未修改候选源码、测试断言、workflow、分支规则或 GitHub 设置；未触碰真实 profile、正式
数据库、真实 B 站账号/Cookie/恢复码/API Key；所有远端写为 mock，外网被指向关闭端口；
未创建 PR、未合并、未创建或移动 tag、未 dispatch 发布 workflow、未发布版本。
