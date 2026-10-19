# BiliFavOrganizer Windows 验收执行提示词

把下面完整内容交给能够访问 Windows 本地仓库、PowerShell、Git、Python 和 GUI 的本地 AI。目标是把当前维护候选推进到**可供用户实机验收**的状态，并在安全边界内完成尽可能多的 Windows 验收；这不是发布授权。

---

## 提示词正文

```text
# BiliFavOrganizer 项目负责人模式｜Windows 候选验收与交付准备

你负责对当前 BiliFavOrganizer 维护候选进行真实、可复核的 Windows 验收。不要只给建议或测试计划：先检查仓库和隔离条件，然后实际执行所有当前授权、环境可支持的步骤；遇到清单规定的停止门槛时，保留证据并明确标 BLOCKED，不得绕过。

## 最新前置状态（2026-10-20）

旧候选65ba02e的Windows全量测试失败（306项，261 PASS、6 FAIL、39 ERROR、零SKIP）。本提交修复Windows锁父目录创建/WinError和未跟踪的历史SQLite合成夹具。先完整读取 `docs/testing/windows-acceptance-blockers-20261020.md`，从远端准确修复SHA重建干净Windows测试环境，按其顺序先定向复验，再全量。T6/T8在真实Windows复验通过前保持FAIL/BLOCKED；历史本机PASS记录不构成新候选Windows验收。只从GitHub获取本提交，不使用旧离线包。

## 一、目标与平台

- 仓库：Martin-soaring-dev/bili-fav-organizer
- 克隆地址：`https://github.com/Martin-soaring-dev/bili-fav-organizer.git`
- 目标维护分支：`feat/reliability-scheduler-implementation`。本轮业务是该分支的可靠性维护，不能选择 `main`、`master`、`new_arch` 或其他功能分支替代。
- 本地验收工作分支：仅在全新 disposable clone 中创建 `acceptance/reliability-windows-20261019`，用于保存下面交接包重建的快照；不得覆盖现有同名分支或向远端推送该分支。
- 产品目标平台：Windows。不要新增 Linux 适配，不要把 Linux 测试结果当成 Windows 验收。
- 本次目标：完成最终候选的 Windows 隔离、测试和可执行验收准备，形成可供用户进行实机/截图验收的固定候选及证据包。
- 本提示词和清单都不授予创建 PR、合并、创建 tag、发布 Release 或触发公开发布 workflow 的权限。

## 二、先读的材料

本轮改为通过 GitHub 直接交接。先从目标维护分支获取包含本文档和维护代码的同一个提交。用户交付消息会提供准确候选 SHA；检出该 SHA 后核对 `git rev-parse HEAD` 和 `git rev-parse HEAD^{tree}`，记录全部证据。不要仅凭分支名或旧 EA 基线开始验收。

PowerShell 获取步骤（在新建 disposable Windows 环境中执行，目录必须全新；不要切换已有用户工作树）：

1. `git clone --no-checkout --branch feat/reliability-scheduler-implementation https://github.com/Martin-soaring-dev/bili-fav-organizer.git BFO-Windows-Acceptance`
2. `git -C BFO-Windows-Acceptance config core.autocrlf false`
3. 从用户的交付消息取得准确候选 SHA，执行 `git -C BFO-Windows-Acceptance switch --detach <准确候选SHA>`；尖括号内容必须替换成实际 SHA，不能照字面运行。
4. 记录 HEAD/tree，读取本提交的文档，然后在该新 clone 创建 `acceptance/reliability-windows-20261019` 本地验收分支。代码已提交，不需要再应用旧 zip 或重新提交源码。证据放在 clone 外独立目录。
5. 若指定 SHA 在当前对象库不存在，可以 fetch 目标分支后再核实；仍不存在则报告准确远端 head 与缺失 SHA。不得转测 EA/main/PR #21 或其他提交。

分支/检查点的含义：

| 名称 | 用途 | 能否替代本轮候选 |
|---|---|---|
| `feat/reliability-scheduler-implementation` | 唯一目标维护分支；本交接提交包含候选代码与任务文档 | 从该分支取得交付消息指定的准确 SHA |
| `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63` | 本次提交的父基线 | 不能替代维护候选 |
| `f22965f6477e90335d3cfad12c2e351d1941c69e` | 另一工作树的历史本地提交，与远端 EA 分叉 | 不能 |
| `recovery/codex-20261017` / G0 `ef06338c337ddf7ea060b4cc6e4548fe09d6d24b` | 早期恢复检查点 | 不能 |
| `ci/ea-playwright-full-regression-20261019` / PR #21 | EA/Playwright 的 CI 验证支线 | 不能 |
| `main` | 后续经审批合并的交付分支 | 本次不切换、不合并、不推送 |

旧 `BFO-Windows-Acceptance-Handoff-20261019.zip` 保留为离线历史交接包，其源码 tree 为 `0155b38d8d7d16456a69a29981b04686343070b2`。本次 GitHub 提交相对该包仅更新交接文档，产品源码与测试代码相同。现在优先使用 GitHub 的准确候选提交；不要把旧包补丁叠加到新提交上。

在仓库根目录完整读取并遵循：

1. `docs/testing/reliability-maintenance-task-list.md`
2. `docs/testing/reliability-maintenance-plan.md`
3. `docs/testing/reliability-maintenance-execution-20261019.md`
4. `docs/testing/legacy-migration-report.md`
5. `.codex-recovery/` 中与本任务相关的恢复/维护记录（若存在）

若必需文档或测试文件不存在，先核实是否检出了错误提交；记录缺失，不要猜测其内容。工作树里的任务清单是停止条件和授权范围的准据；本提示词不能扩大其授权。

## 三、已知检查点（运行前必须重新核实）

- 已知 EA 基线：commit `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63`，tree `939a0607988524b7afec8097fde3e9acda4ec655`。
- 维护改动最初存在于 detached EA HEAD 上的一份未提交工作树；本次将其整理为 EA 的普通后继提交。另一个本地 `feat/reliability-scheduler-implementation` checkout 指向 `f22965f6477e90335d3cfad12c2e351d1941c69e`，它与远端 EA 历史分叉。远端分支当时仍为 EA。不要假定这些改动已经进入你当前 Windows checkout，也不要把 PR #21 的结果算到维护候选上。
- 本次通过同一 GitHub 提交交接候选代码、测试、维护计划和本提示词，准确 SHA 以用户交付消息及实际远端核验为准。历史离线包只供来源追溯，不再作为 Windows 执行的必备前提。
- 2026-10-19 的 Linux 隔离诊断包括 schema migration 16/16、lifecycle 22/22、migration-only 30/30、server lock 2/2、发布候选 5/5、workflow contract 10/10、安全合同 12/12。它们仅供本地背景参考，不能继承为 Windows PASS，必须在准确 Windows 候选上重跑适用测试。
- 线上 Draft PR #21 是 CI-only / DO NOT MERGE，head `df88174bd606e1ab21bdc0a2b11e0d553a2cbb9a`。其 Windows CI 和打包结果只验证 EA/CI 基线，不验证当前维护候选。
- schema 1 样本来源与支持边界仍未确定，需要真实来源证据及维护者决策。不得自行认定支持/不支持，也不得为报绿放宽 schema 校验。

## 四、不可突破的数据与操作边界

1. 绝不对日常 Windows 用户账户、正式安装、正式 profile、真实用户数据库或真实凭据执行迁移/初始化/锁定/解锁/恢复/破坏性测试。
2. 仅使用 disposable Windows VM/Windows Sandbox/一次性本地账户，以及新建、可删除的测试目录和合成数据。测试前建立 VM 快照或同等可恢复检查点。
3. 仓库内已有 `data/library.sqlite3` 及其 `-wal` / `-shm` 永远不得成为测试目标。真实迁移来源只可先复制数据库、sidecar 和 vault 元数据，再对副本做只读分析或测试；记录源副本文件清单和 SHA-256。
4. 只设置 `BILI_FAV_ORGANIZER_DATA_DIR` 不足以证明隔离。实际确认 SQLite、repo-local data、`APPDATA`、`LOCALAPPDATA` 下 config/secrets/vault/device 状态、`TEMP`、日志、便携包工作目录和 Electron `userData` 全部解析到 disposable 环境。对运行进程使用文件访问审计/ProcMon 或同等证据；测试前后比对清单与哈希。
5. 禁止 `git clean`、批量删除、覆盖未知文件、reset、rebase、force push、改写历史、推送 main、使用真实 B 站账号/Cookie/恢复码/API Key。
6. 对 B 站等远端写请求必须 mock 或在网络层阻断。除非只读验证需要，否则关闭外网；不得执行真实收藏夹写入。
7. 不得通过跳过/屏蔽测试、降低断言、伪造结果、替换 PASS 记录消除失败。每个 PASS 都要绑定本次实际测试 SHA/tree、命令、退出码和日志。
8. 不运行会创建 tag、公开 Release、上传公开包或触发发布的 GitHub Actions。GitHub Settings 只读检查可以做。

## 五、执行顺序

### 阶段 A｜身份、工作区和隔离（T0）

1. 记录本地时间、Windows 版本/架构、PowerShell 版本、Python 版本、仓库绝对路径、branch、HEAD、parent、tree、`git status --short`、远端 URL 和各远端分支 SHA/tree。
2. 逐项列出 tracked diff、untracked 文件/目录及来源；保留 `tests/Test-BFO-Beta2/`、`.codex-recovery/`、用户资料和任何归属不明的文件。不要用删除或清理操作让工作树变干净。
3. 确认当前工作树确实包含同一份维护候选。若候选身份无法唯一确定、代码只在另一个不可访问的 Linux 工作区、当前 checkout 缺关键改动或发现来源不明的文件，停止后续产品测试并标记 BLOCKED，附文件/commit 证据。
4. 在 VM/一次性账户建立全新 disposable checkout 和 profile。记录所有实际数据路径；做文件访问审计；证明 repo 数据库和日常用户目录没有被打开/写入。
5. 如果没有 disposable Windows 环境，T0 标 `BLOCKED`，停止 GUI、vault、安装包和真实旧库迁移操作。仍可继续纯静态 workflow/源码检查，但不能把本地当前用户账户充当沙箱。schema 1 决策、环境保护权限等单个任务的阻断，不应阻止彼此独立的已知 schema 7/8 测试与源码检查。

### 阶段 B｜schema 来源与迁移（T1–T3）

1. 先搜索仓库、归档、历史测试报告、版本 tag/源码中 schema 1 的真实来源。所有数据库操作只针对副本。记录来源 URL/版本/commit、SHA-256、WAL/SHM 是否齐全、schema/ledger 结构及逻辑数据计数。
2. 找不到可核实 schema 1 样本时，T1 保持 BLOCKED，并提交需要维护者选择的清晰选项；不要推断老 schema 可迁移。
3. 对有真实来源的已知版本，在干净 disposable profile 上执行迁移正向矩阵、重复启动幂等性、含已提交 WAL 的迁移、复制/验证/安装故障注入、失败保源、重试和可恢复备份验证。
4. 比较迁移前后规范化逻辑数据摘要、ID/关联/唯一性、SQLite integrity/foreign_key_check、schema/ledger；迁移失败时源文件及 sidecar 必须保持原状且不能出现空 READY 数据库。

### 阶段 C｜Vault、拒绝路径、锁和恢复（T4–T7）

在隔离合成 profile 上执行清单中的 Vault 正/负路径，至少覆盖正确密码/恢复码、错误密码、密文或 manifest 篡改/缺失、重复锁定解锁、部分写入中断、备份保全、缺失数据库、第二进程冲突、进程退出释放锁、迁移失败回滚和重试。记录明文/密文/manifest 的文件清单与 SHA-256。

- 错误密码或损坏密文不能进入 READY，不能创建空活动库，不能覆盖源密文。
- 真实 Windows 上验证 `LockFileEx` 的共享/独占冲突、超时和进程退出释放；Linux 的 `flock` 结果不算。
- 验证升级前的旧版进程冲突处理。若旧版仍能绕过新锁，报告风险，不能开始覆盖安装。
- 更新 `docs/testing/legacy-migration-report.md`：逐项注明“当前候选已测试/仍存在/不适用（附代码依据）/未运行”。不从旧 SHA 继承结果。

### 阶段 D｜目标 Windows 全量回归和 CI 配置（T8）

1. 核验测试依赖和 CI 的 pinned Playwright/Chromium 版本。安装失败要报告，不得把 UI 测试 skip。
2. 在 disposable 环境、准确候选 SHA/tree 上运行全量隔离 runner：

   `python tools/run_isolated_tests.py`

   设置严格浏览器门禁 `BFO_REQUIRE_PLAYWRIGHT=1`；确认 Chromium 实际可启动、7 项 UI 回归实际执行、skipped=0。记录总数、PASS/FAIL/ERROR/SKIP、日志路径和退出码。
3. 单独重跑 schema、lifecycle、migration-only、Vault、server instance lock、发布合同等清单指定测试，以便报告结果；不要把多项 `--test` 参数误当成已执行多个模块，先读取 runner 源码并核对实际测试名称；当前 runner 不实现 `--help`，未知参数可能落入全量运行，不能用 `--help` 探测。
4. 检查 Beta packaging workflow 的 path filter、Playwright 门禁、权限、固定 action SHA；区分静态配置检查与实际 Windows 测试。
5. 任何与 Windows 有关的失败、错误或浏览器 skip 都阻断 T8；不得用 Linux 成功替代。Linux 测试仅用于发现与平台无关的逻辑缺陷。

### 阶段 E｜发布配置只读复核（T9）

检查 beta/stable build-only 与 publish workflow 的触发事件、checkout SHA/tree、权限、tag 绑定、release asset 六项 hash、manifest 和公开发布步骤。通过 GitHub UI/只读 API 核实 Environment required reviewers、自审限制、管理员绕过和 tag protection/immutability；如没有权限或无法读到，记录 BLOCKED，不改仓库设置。不得 dispatch publish workflow。

### 阶段 F｜实机验收准备（T10）

仅当 T0–T9 所需前置条件满足且最终候选身份固定后：

1. 准备 disposable Windows 安装包或可运行包，写清测试版本、SHA/tree、SHA-256、VM/profile 恢复方法和不会触碰正式数据的证据。
2. 先在测试环境列出 7 个历史场景和本轮新增场景的操作步骤、预期 UI/API 状态、只读核验方式、mock 远端写审计方式。
3. 要求用户逐张提供截图后再判定截图场景；没有截图/用户操作证据的一律 NOT_RUN，不能凭代码测试冒充 UAT。
4. 如仍有 T1、T8 等阻断项，明确本包仅供诊断或不得实机验收，不能标成候选可验收。

## 六、权限边界与禁止动作

- 可以执行隔离测试、源码检查、文档更新、构建本地诊断包；必要时可以创建正常本地提交，但必须先核清工作树归属和目标分支历史。
- 不创建 PR、不请求/批准合并、不合并、不创建 tag、不发布、不推送 main、不触发公开发布工作流。不要把当前 PR #21 当作承载当前候选的 PR。
- 如果后续确需推送，只能先证明候选唯一、目标分支关系可 fast-forward、改动归属明确，且清单已授权；否则只保留本地结果并报告原因。
- 任何恢复码、Cookie、API key、token、个人路径或敏感数据不得写入报告、截图、终端输出或提交。

## 七、每一步的记录格式

每个 T0–T13 行都用 `PASS / FAIL / BLOCKED / NOT_RUN`，包含：

- 候选 branch/SHA/tree/parent 与工作树状态；
- Windows 版本、隔离 VM/账户及快照 ID；
- 实际 SQLite、APPDATA、LOCALAPPDATA、TEMP、日志、Electron userData 路径；
- 夹具来源和 SHA-256，测试前后文件清单/哈希；
- 精确命令、退出码、总数/PASS/FAIL/ERROR/SKIP、日志/截图路径；
- 与旧报告差异、失败复现条件、源文件是否保持不变；
- 当前阻断、是否触发停止条件、下一步责任人。

结束时输出：

1. 一页结论：是否达到“可供用户在 disposable Windows 环境实机验收”，不要等同于可发布。
2. T0–T13 更新表和证据链接。
3. 全量测试结果及失败明细。
4. 数据隔离/文件访问审计结论。
5. 候选身份和 Git 操作记录。
6. 未完成的维护者决策/审批清单。
7. 明确说明是否触碰真实 profile、真实 B 站数据、远端写接口、PR、main、tag、Release；预期全部为否。

按以上顺序开始执行。先汇报当前实际仓库身份和数据边界，再开始任何可能访问 profile 的测试。遇到停止条件时只停止受影响的测试分支，继续可独立且安全的只读工作，并更新执行记录和既有 dashboard。
```

---

## 交给本地 AI 前的使用提醒

将本提示词的 GitHub 链接与交付消息指定的候选 SHA 一并交给本地 AI。文档、源码、测试均在同一目标分支提交内；本地 AI 必须检出准确提交后执行，不能把旧 EA 代码测试结果归给当前候选。
