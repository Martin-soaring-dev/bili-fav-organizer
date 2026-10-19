# 可靠性维护执行检查点（2026-10-19）

本记录汇总本轮按维护任务清单执行的可复现检查。它不是 Beta 放行结论。所有本机迁移测试均由 `tools/run_isolated_tests.py` 在临时副本和合成数据上运行；没有使用真实 profile、真实 B 站凭据或仓库数据库。交互进度已合并到既有项目状态页 `index(20261018-203653).html`。

## 候选与远端状态

- 远端 `feat/reliability-scheduler-implementation` 仍为 `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63`，tree `939a0607988524b7afec8097fde3e9acda4ec655`。
- 当前维护工作树以该 EA 提交为 detached HEAD，但包含一批未提交迁移、保险库、发布工作流和测试改动；另一个本地功能分支指向 `f22965f6477e90335d3cfad12c2e351d1941c69e`，GitHub API 返回该 SHA 不存在。本轮又修正了 Chromium 必测失败门禁。因此维护候选**尚无单一、固定且已推送的 SHA/tree**，这些结果不得归为远端 EA 已验证结果。
- 维护计划和任务清单仍未推送。Draft PR #21 是 EA 基线的 CI 基础设施验证，不是当前未提交迁移候选。
- 本机为 Linux，没有可丢弃 Windows VM 或完整 Windows 用户状态目录审计；T0 因缺少目标 Windows 隔离环境仍是 `PARTIAL / BLOCKED`。Linux 本身不是产品适配或验收目标；未运行 GUI、安装包或正式 profile 迁移。

## 本机隔离测试

测试解释器使用 `/workspace/scratch/48e90a428342/bfo-test-venv/bin/python`，其中包含项目测试依赖。

| 命令 | 结果 | 证据边界 |
|---|---:|---|
| `../bfo-test-venv/bin/python tools/run_isolated_tests.py --migration-only` | **30/30 PASS** | 覆盖旧库迁移、WAL、vault 错误密码/密文损坏、封存与跨进程封存 |
| `../bfo-test-venv/bin/python tools/run_isolated_tests.py --test tests.database.test_schema_migration` | **16/16 PASS** | 覆盖合成 v0.209/schema 7、逻辑数据保留、原子安装、失败保源、WAL 和拒绝路径 |
| `../bfo-test-venv/bin/python tools/run_isolated_tests.py --test tests.database.test_lifecycle` | **22/22 PASS** | 覆盖数据库生命周期、候选回滚、进程间读写租约 |
| `../bfo-test-venv/bin/python tools/run_isolated_tests.py --test tests.test_server_instance_lock` | **2/2 PASS** | 覆盖第二实例拒绝共享 profile 和服务进程租约释放 |
| `BFO_REQUIRE_PLAYWRIGHT=1 PLAYWRIGHT_BROWSERS_PATH=/workspace/scratch/48e90a428342/playwright-browsers ../bfo-test-venv/bin/python tools/run_isolated_tests.py`（隔离过滤修正后） | **306 项：4 FAIL、2 ERROR、0 SKIP** | 5 项 `test_update_flow` 失败/错误是 Linux 对 Windows 更新路径的环境差异，且在 EA 基线重现；它们不作为 Windows 产品验收门槛。另 1 个 ERROR 是严格门禁按预期拒绝缺失 Chromium。此 Linux 运行不能替代目标 Windows 全量回归，因此 T8 尚未完成 |

第一次以运行时默认 Python 执行 migration-only 时，因为该解释器没有安装 `requests`，导致探针导入失败。改用项目测试虚拟环境后，migration-only 30 项全部通过；前一次结果不作为产品失败记录。全量套件曾在门禁修正前得到 305 项、4 FAIL/1 ERROR/1 SKIP；已由过滤与严格门禁修正后的 306 项最终结果取代。

EA 基线对照命令 `../bfo-test-venv/bin/python tools/run_isolated_tests.py --test tests.test_update_flow` 得到 **19 项：4 FAIL、1 ERROR**，失败/错误与当前全量结果相同：Linux 缺少 `winreg`，以及 4 项 Windows 安装更新路径断言不匹配。这些结果只说明 Linux 不能验证 Windows 更新路径，不判定 Windows 产品失败；目标 Windows 测试仍未运行。

对应测试：`test_missing_registry_marker_means_portable`（ERROR）、`test_installed_build_downloads_and_runs_the_installer`、`test_installer_helper_gets_every_parameter_it_uses`、`test_portable_build_keeps_the_zip_worker`、`test_dev_build_refuses_in_app_update`（4 FAIL）。

## Chromium 必测门禁修正

发现 `tests/test_ui_regressions.py` 的说明和 CI 环境变量要求 Chromium 缺失时失败，但实际启动异常仍无条件转为 `SkipTest`。当前维护工作树已修正：本地可选跳过；`BFO_REQUIRE_PLAYWRIGHT=1` 时 Chromium 启动异常抛出 `RuntimeError` 并使测试作业失败。

隔离验证（使用没有 Chromium 可执行文件的浏览器目录）：

- `BFO_REQUIRE_PLAYWRIGHT=1 ... --test tests.test_ui_regressions`：**按预期 ERROR/非零退出**，错误指出 Chromium 不可启动。
- 不设置 `BFO_REQUIRE_PLAYWRIGHT` 的同一测试：**按预期 `OK (skipped=1)`**，保留本地无浏览器时的便利行为。

这项门禁修正尚未提交。最终全量套件在修正后运行：缺失 Chromium 变为 **1 个显式 ERROR、0 个 SKIP**，7 项 UI 场景仍未在本机执行。

## 隔离 runner 目录边界

复核发现 runner 初始忽略表没有覆盖工作树中的 `.codex-recovery/`、`.bfo-diag-temp/` 与 `.candidate-test-tmp/`，因此先前运行会将这些目录复制到临时 checkout；应用测试并未以这些目录作为数据路径，原目录未被修改。修正后，runner 也忽略 `tests/Test-BFO-Beta2/`，避免复制用户验收材料。新增 `tests/test_isolated_test_runner.py` 检查四类目录均被排除，**1/1 PASS**；修改后的完整 306 项套件使用了该过滤规则。

本次在 `/workspace/scratch` 下没有找到 `tests/Test-BFO-Beta2/` 目录，因此只能确认本轮未对它执行清理，不能声称它在当前工作区中存在或完成了文件级保全核验。

## 远端 Windows CI 证据（仅 EA 基线）

Draft PR [#21](https://github.com/Martin-soaring-dev/bili-fav-organizer/pull/21) 的 head 是 `df88174bd606e1ab21bdc0a2b11e0d553a2cbb9a`，用于验证 Playwright/Chromium CI 设置：

- [Windows CI run](https://github.com/Martin-soaring-dev/bili-fav-organizer/actions/runs/39167835259)：成功；日志显示 **253 项全通过**，7 项 UI 测试实际执行。
- [Beta packaging validation run](https://github.com/Martin-soaring-dev/bili-fav-organizer/actions/runs/39167835248)：成功；Python 回归、Electron NSIS 构建和 renderer/backend smoke test 完成。

这些运行验证 EA 基线的 CI 环境和打包链路，**不验证当前未提交的迁移、profile-lock 或发布工作流改动**。PR #21 仍为 Draft，任务清单不授权合并。

## 当前任务状态与剩余门槛

| 任务 | 本检查点结论 |
|---|---|
| T0 隔离与候选锁定 | `PARTIAL / BLOCKED`：候选基线已锁定；缺少 Windows disposable profile/VM 和全用户状态路径审计 |
| T1 schema 1 取证/支持范围 | `BLOCKED`：没有可核实的样本来源，支持边界需维护者决定 |
| T2 schema 7→8 迁移 | `PARTIAL`：合成 v0.209/schema 7 定向测试 16/16；尚无 Windows 安装路径验收 |
| T4/T5 vault 与拒绝路径 | `PARTIAL`：隔离 migration-only 30/30；尚未做最终候选 Windows profile 验收 |
| T6 跨进程锁、回滚与入口 | `PARTIAL`：补充 Linux lifecycle/lock 用例 24/24；Windows `LockFileEx` 双进程与正式候选路径未验证 |
| T8 全量回归/浏览器门禁 | `BLOCKED`：非目标 Linux 运行不能替代验收；本机 Chromium 缺失；最终候选 Windows 全量回归/CI 未运行 |
| T9 发布工作流 | `NOT_ACCEPTED`：本地合同测试可另行核验，但未提交、未在最终候选 SHA/受保护 GitHub Environment 上验证 |
| T10 UAT | `NOT_RUN`：候选截图验收未开始 |
| T11a 候选 PR checks | `NOT_RUN`：PR #21 的成功运行只验证 EA 基线，不覆盖当前候选；候选 PR checks 尚未运行 |
| T11b–T12 合并后构建、安装验收与发布 | `NOT_RUN`：未合并、未 build-only、未创建 tag/Release |

本轮没有提交、推送、合并、创建 tag 或发布。原有未跟踪目录未清理；`tests/Test-BFO-Beta2/` 的实际位置待查。

## 继续执行顺序

1. 在 disposable Windows VM/用户账户补齐 T0 路径审计，再对最终固定 SHA 运行 T2/T4–T8 与 Chromium 全套。
2. 获取 schema 1 样本来源证据，由维护者确定兼容支持范围；在决定前保持该路径阻断。
3. 复核 profile-lock 对 Windows 多进程的实现与测试；确认全量测试及候选 CI 后，才整理提交并按任务清单推进后续 PR 门禁。

## 2026-10-19 续行检查

按任务清单再次运行独立副本测试：schema migration **16/16**、lifecycle **22/22**、server instance lock **2/2**、migration-only **30/30**、release candidate **5/5**、release workflow contract **10/10**、apply safety contract **12/12** 均通过。`git diff --check` 通过。工作流合同测试确认 build-only 由 main 上的手动 dispatch 触发、只读权限；发布写权限隔离到受保护 Environment，并在发布前复验运行来源、manifest/附件 hash 和 tag。它们是静态/本地合同验证，不证明 GitHub Environment 的实际 reviewer、自审限制、管理员绕过或 tag ruleset 已正确设置。

GitHub 只读状态复核：仓库默认分支为 `main`；PR #21 当前仍 open、Draft、mergeable，标题及描述仍明确标记 CI-only / DO NOT MERGE，head 为 `df88174bd606e1ab21bdc0a2b11e0d553a2cbb9a`。这只验证 Playwright CI 基础设施，不覆盖当前工作树候选；任务清单也不授权合并。当前维护工作树仍以 `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63` 为 detached HEAD，改动未提交、未推送，没有固定候选 SHA/tree。现有另一本地 `f22965f` 工作树与该远端分支历史分叉，未切换或覆盖。

**续行结论：**没有发现可在本环境内安全补做的 Windows 验收替代项。Linux 是非目标平台；本轮不为 Linux 增加产品适配。T0/T2/T4–T8 仍需 disposable Windows 环境完成路径审计、`LockFileEx` 跨进程验证、最终候选全量回归与七项 Chromium 场景。T1 的 schema 1 来源/支持范围需要维护者证据和决定；T9 需要仓库保护设置核验。T10 Mimo 截图验收、T11a 候选 PR checks、T11b merge 后 build/install、T12 发布均未执行。本轮没有提交、推送、创建 PR、合并、创建 tag 或发布。

## Windows 本地验收交接包

已补充自包含的 Windows 本地 AI 提示词和候选交接包。明确目标维护分支为 `feat/reliability-scheduler-implementation`；包基于准确 EA commit 重建未推送维护改动，并以 `candidate-manifest.json` 的 candidate tree 和逐文件 SHA-256 固定源码。Windows 执行者须在全新隔离 clone 中验证 patch、文件哈希、Git tree 后创建本地验收快照，不能测试远端 EA 后冒充维护候选通过。源码仍未提交/推送；Windows 测试/UAT 状态不变。包不包含用户数据库、凭据、`.bfo-diag-temp/` 或归属不明的恢复目录。

## GitHub 直接交接修正

此前代码和文档未推送，导致 Windows 本地执行者从远端找不到维护候选。此次将离线包 tree `0155b38d8d7d16456a69a29981b04686343070b2` 的全部候选代码与测试，以 EA 为父基线整理为普通提交，更新交接文档为 GitHub 精确 SHA 获取方式，目标分支为 `feat/reliability-scheduler-implementation`。最终 commit/tree 和推送成功与否以交付消息及远端复核为准；较早未提交、未推送记录保留为历史检查点。原工作树、本地历史分支和未知目录不覆盖。仅推送功能分支不触发 main/PR 专用测试；Windows 候选全量测试和浏览器验收仍待运行。没有创建 PR、合并、tag 或 Release。

## 2026-10-20 Windows 失败反馈与修复

转交Windows结果为旧候选65ba02e的306项：261通过、6失败、39错误、零跳过。T6/T8保持FAIL/BLOCKED。已修复Windows锁父目录创建，保留WinError原生错误信息，并将被忽略的已知合成schema7夹具正式纳入Git。新增回归修复前失败、修复后5/5；schema16/16、lifecycle22/22、server实例锁2/2及migration-only30/30通过，均是本机隔离开发诊断，不能代替Windows复验。详见 [Windows阻断与复测](windows-acceptance-blockers-20261020.md)。后续只从远端准确修复SHA执行，保留旧失败证据，不沿用旧候选通过结论。

## 2026-10-20 第二轮 Windows Vault 反馈

用户转交ac9da20 Windows全量318项：298通过、6失败、14错误、0跳过。已修复错误归档运行锁（Windows字节锁拒绝另句柄读取）、同目录临时名称复制完整archive名导致不必要路径增长，以及失败onboarding探针二次读取manifest掩盖原始500。新增回归修复前失败、修复后通过。本机隔离原子I/O3/3、调度/Vault集成4/4、Vault API9/9、迁移31/31、基础Vault11/11、设备密钥13/13、DEK4/4；真实Windows修复验收待完成，T6/T8继续阻断。详见 [第二轮复测记录](windows-vault-acceptance-retest-20261020.md)。未凭摘要断定全部FileNotFoundError均为路径长度问题，需Windows完整路径复核。
