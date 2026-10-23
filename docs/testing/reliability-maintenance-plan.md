> **历史计划快照（核对日期：2026-10-16；治理复核：2026-10-23）**：以下分支、SHA、工作区和阻塞状态属于当时的执行阶段，不是当前状态。可靠性候选后续已合并，当前稳定版为 `v0.210.5`，远端长期分支仅保留 `main`。本文件保留旧计划及其当时的 FAIL / BLOCKED / NOT_RUN 结论，不代表这些状态在当前提交仍然成立。当前主干、版本和维护规则请查阅 [文档目录](../INDEX.md)、[维护治理规范](../maintenance/governance.md) 与 [版本台账](../maintenance/version-ledger.md)。不要依据本文件单独决定当前是否可发布。

# 可靠性改造后续维护计划（草案）

**状态：**计划已进入执行；本文件不代表所有任务已完成、PR 已创建或版本已发布。
**核对日期：**2026-10-16
**目标：**完成可靠性分支的数据库兼容性核查、回归验收和 Beta 发布准备。

## 当前状态与证据边界

### 本轮执行进度

远端功能分支核实的基线为 `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63` / tree `939a0607988524b7afec8097fde3e9acda4ec655`。本轮在该基线上用独立工作树重建候选；当前改动仍未提交，不能把基线 SHA 当成本轮最终 SHA。原始 checkout 与 `tests/Test-BFO-Beta2/` 未修改。以下测试均指该隔离候选，不能替代 Windows CI、实际安装验收或 Mimo 对话式 UAT。

| 工作包 | 状态 | 当前结论 |
|---|---|---|
| M0 | **PARTIAL / BLOCKED** | 已核实远端基线并建立隔离候选；没有 Windows VM/完整 Windows 用户状态审计；schema 1 来源和支持范围仍待维护者决定。 |
| M1 | **PARTIAL** | schema 7→8 使用 SQLite backup 候选副本、结构契约、回滚及重试测试；schema 7/lifecycle 定向测试通过。低于 schema 7 的版本契约、schema 1 决策和 Windows/beta.2 安装路径仍未通过验收。 |
| M2 | **PARTIAL / BLOCKED** | 保险库封存、解锁、损坏清单和失败保源有隔离测试；profile 级共享/独占锁已接入数据库、迁移和 vault 路径，Linux 跨进程测试通过。Windows `LockFileEx` 未实测，旧版不遵循新锁的进程必须关闭，归档尚未接入 UI 恢复。 |
| M3a | **PARTIAL / BLOCKED** | 发布流程静态合同审查通过；全量本机套件与浏览器回归状态见下方“本轮候选测试”。Linux `test_update_flow` 有平台专属失败/错误；Chromium 页面测试因浏览器不可用而跳过；Windows CI 尚未运行。 |
| M3b | **NOT_RUN** | 未创建 PR，未取得 PR checks；发布环境保护设置仍需仓库管理员验证。 |
| M4 | **NOT_RUN** | 尚未按最终 SHA 进行 Mimo 对话式截图验收。 |
| M5 | **NOT_RUN** | 未合并、构建或安装验收；两阶段 build-only/publish 工作流已有候选实现，但 Windows 构建和仓库侧保护设置未验证。当前没有 tag/Release 授权。 |
| M6 | **NOT_RUN** | 未发布 Beta/stable。 |
| M7 | **PARTIAL** | 已忽略并验证 `tests/.test-data/`，修正过期测试名与注释；维护计划、任务清单和历史迁移报告正在记录本轮实测，旧 release checklist 保持原样。 |

独立审查确认候选已加入 profile 级跨进程租约，并在 Linux 上验证数据库维护、保险库和真实 `server.py` 入口的竞争/释放；Windows `LockFileEx` 仍待实际 CI。发布流程仍不能判定可发布：GitHub `release-approval` 的 required reviewers、自审限制及管理员绕过策略必须由仓库侧核实；tag 复查与 Release 创建非原子，须配置不可变或受保护 tag 规则。

| 项目 | 当前认知 |
|---|---|
| 正式版基线 | `v0.209`，`main` 为 `1044943cd08a49b5c943b5227e69ae64f862eb43` |
| 候选分支 | `feat/reliability-scheduler-implementation` |
| 本轮重建基线 | `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63` / tree `939a0607988524b7afec8097fde3e9acda4ec655`；当前候选改动尚未提交，最终 SHA/tree 需在提交后补记 |
| 数据库版本 | `v0.209` 为 schema 7；候选分支为 schema 8。`v0.209 → schema 8` 是本次 Beta 必须覆盖的升级路径 |
| 先前候选测试 | Mimo 报告：EA 之前的 `253` 项隔离全量套件全通过，其中 7 项浏览器回归在该环境执行；这是历史证据，不替代本轮验证。 |
| 本轮隔离候选 | 既有定向结果：schema/lifecycle `36/36`、vault migration `12/12`、release/runner/attribution `26/26` 通过。最近两次全量运行均为 300 项，含 4 个在 EA 基线复现的 Linux `test_update_flow` FAIL、1 个 `winreg` ERROR、1 个 Chromium SKIP；另在不同运行中出现 MIG-012、MIG-028 各一次 vault 解锁 500，单项/模块复跑通过但原因未明。Windows CI 尚未运行。 |
| 对话式 UAT | 七场景报告针对较早的 `5c2e7fa` 内容；`9e8fdbc` / `ea4983d` 上的修复尚需按最终 SHA 复验 |
| 旧 Beta UAT | `v0.209-beta.2` 安装验收与远程 R4 报告属于旧版本证据（报告中有 FAIL/MANUAL 项）；不得与 `ea4983d` 候选验收合并或互相抵销 |
| PR / CI | 截止本计划核对时没有候选 PR，也没有该 SHA 的 CI 或 Beta 打包运行记录 |
| 本工作区 | 原始 checkout 未切换；候选工作树位于 `/workspace/scratch/48e90a428342/bfo-maint-recovery`，从 EA 基线建立，当前有未提交改动。 |

**发布判断：暂不具备 Beta 发布条件。** schema 7→8 和 profile 级跨进程锁已有 Linux 隔离测试，但 Windows 文件锁、目标 Windows CI、Chromium 页面回归、Mimo UAT、实际安装包升级、schema 1 支持决策及仓库侧发布保护设置均未完成。即使本机测试通过，也不代表 PR/CI、安装包或 Release 获批。

## 维护原则

1. 任何迁移都在隔离副本上执行，保留源文件和备份；失败时不得创建空库冒充恢复成功。
2. 以提交 SHA 和 tree 绑定每项测试、截图、构建及人工验收，不将旧提交的 PASS 转记到新提交。
3. 不降低完整性校验、数据库生命周期门闩或现有安全断言来换取测试全绿。
4. 所有数据迁移和 GUI 验收只使用合成数据及隔离 profile；优先使用可丢弃 Windows VM/用户账户，并确认 repo-local DB、SQLite、`APPDATA`/`LOCALAPPDATA` 下的 config/secrets/vault/device 状态、`TEMP`、日志、便携包目录、Electron userData 均在沙箱。`BILI_FAV_ORGANIZER_DATA_DIR` 只重定向 SQLite，不足以隔离整个用户状态。仓库已有 `data/library.sqlite3` 及其 WAL/SHM 不能作为测试目标。不使用真实账号、Cookie、恢复码或正式用户数据。
5. 明确分开本机测试、GitHub CI、Beta 包构建和真实安装验收，分别留存结果。

## 工作包与依赖

| ID | 优先级 | 工作包 | 依赖 |
|---|---|---|---|
| M0 | P0 | 锁定候选源、核实 schema 支持范围 | 无；所有实施工作的起点 |
| M1 | P0 | 修复并验证旧数据库安全迁移 | M0 |
| M2 | P0 | 对照最新代码核销历史保险库迁移审计 | M0，可与 M1 并行审查 |
| M3a | P1 | 全量本机回归与 CI 工作流准备 | M1、M2 的修复落盘后 |
| M3b | P1 | Draft PR 的 GitHub 检查与分支保护核实 | M3a；与 M4 并行 |
| M4 | P1 | Mimo 对话式 UI / 隔离升级验收 | M1、M2、M3a 后固定最终候选 SHA；与 M3b 并行 |
| M5 | P1 | 合并、私下构建、安装验收与 Beta 发布 | M3b、M4 |
| M6 | 发布门 | Beta 反馈、稳定版决策与发布 | M5 的 Beta 安装验收通过 |
| M7 | P2 | 测试目录卫生和文档小修 | 可与 M3a 并行，不阻塞 M1 |

### M0 — 锁定候选源与数据支持范围（P0）

- 在干净的临时工作树获取 `origin/feat/reliability-scheduler-implementation`，核对 HEAD、tree、parent 与上表目标一致；若 SHA 已前进，更新候选记录并从新 SHA 重算后续证据。
- 检查工作区未跟踪内容并保留 `tests/Test-BFO-Beta2/`；不得用清理命令删除用户数据。隔离测试产物与正式数据分开。
- 建立数据库历史来源表：已确认 `v0.208`、`v0.209` 均为 schema 7；schema 1 样本目前没有可核实的发行版本来源。
- 对报告中的 schema 1 数据库，先离线复制数据库、伴随的 `-wal`/`-shm` 和 vault 元数据，再只对副本做只读取证：记录 `schema_migrations`、表/列、`integrity_check`、`foreign_key_check`、关键业务记录数量及样本来源。副本缺 WAL 时记录快照限制；严禁由应用或普通读写 SQLite 连接打开原件，严禁在原件上初始化、升级或覆盖。
- 维护者根据来源确认 schema 1 的支持边界。若它含真实用户数据，或会由应用默认旧路径自动发现，先按潜在受支持数据保护；在完成迁移或形成明确且可执行的恢复方案并由维护者接受前，这仍是 Beta P0 阻断。不得仅凭缺表就拒绝或替换。若确认只是非发行测试残留，记录证据后再明确最低受支持 schema。若最终不支持 schema 1，必须提供明确的人工恢复/导出路径并证明原库可保全；“安全拒绝”本身不能视为解决了潜在数据不可访问风险。

**完成证据：**候选 SHA/tree 记录；schema 7 来源；schema 1 取证摘要和支持范围决定；隔离路径及未跟踪数据保全记录。

### M1 — 安全迁移与旧库兼容（P0，Beta 阻断）

#### 实施要求

- 用按源版本定义的 schema contract 校验旧库：明确每个受支持版本的必需表、列、标识关系和允许的迁移缺项。不能在升级前无条件套用 schema 8 的完整表集，也不能把 `CREATE TABLE IF NOT EXISTS` 当成有效性证明。
- 优先保证 `v0.209/schema 7 → schema 8`。schema 1 仅在 M0 确认支持范围后纳入版本契约和迁移路径；禁止猜测其逐版本结构。
- 通过 SQLite backup API（包含已提交 WAL 内容）建立同卷候选副本；迁移只写候选副本。迁移期间建立独占窗口：停止新任务/API 写入并排空本进程连接，同时阻止或检测第二实例写源库；若源库在候选完成后发生变化，放弃切换并重新取副本。迁移后验证目标 schema、`integrity_check`、`foreign_key_check`、核心业务记录数量/ID/关系及必要的领域不变量。
- 只有候选库全部通过验证后才原子替换活动库，并保留可恢复备份。迁移失败、进程中断、空间不足或候选验证失败时，原库与密文必须保留，状态不得变成 `READY`，不得留下一个可被误认为成功恢复的空库。
- 覆盖普通启动、旧位置数据库搬迁、保险库解锁/恢复三种进入路径；确认 lifecycle gate、vault 恢复与 schema 升级顺序不会绕过校验或先建空库。

#### 必测矩阵

| 类别 | 用例 | 通过标准 |
|---|---|---|
| 正向升级 | 由真实 `v0.208` / `v0.209` 来源生成或验证的 schema 7 空库、常规数据、WAL/中断扫描等边界数据 | 至少 v0.209 必测，v0.208 按已声明兼容范围覆盖；升到 schema 8，逻辑摘要、ID 和关系符合预期，完整性与外键检查通过 |
| schema 1 | 仅在 M0 判定支持时执行 | 从可核实的真实历史来源生成夹具并逐版迁移；不支持时提供可执行人工恢复/导出路径，证明原库可保全且不会自动替换为空库 |
| 拒绝路径 | 损坏库、该版本必需表或列缺失、列约束不符、按该版本契约必需的 migration ledger 缺失/空/非法/负版本、迁移历史缺边、未知/未来 schema | 依据已批准版本契约进入明确恢复/错误状态；旧版若无 ledger，只能凭可核实历史来源或可靠版本标记识别；来源不明时不得猜版本或自动初始化。源库及密文哈希和身份元数据保持不变，不创建或暴露空活动库 |
| 故障注入 | 候选复制、迁移 SQL、校验、备份/manifest、原子替换及替换后最终校验失败；中断后重试 | 源库/密文身份不变；按授权恢复后数据可读；回滚或进入恢复状态，不把半迁移库标为 READY |
| 并发写入 | 迁移窗口内本进程新请求与第二实例写入 | 写入被锁/排空，或检测到源变化后放弃切换；不得用过期候选覆盖较新的源库 |
| 旧位置搬迁 | 目标数据库已存在、源库损坏、复制或校验失败 | 不覆盖已有目标；失败时旧路径来源保留且可按授权恢复 |
| 幂等性 | 同一已升级库再次启动 | 不重复导入、不改坏身份关系，不重复创建业务记录 |
| 入口路径 | 普通启动、旧路径搬迁、vault 解锁/恢复；错误密码和损坏密文 | 三条路径均升级到相同目标 schema；错误密码在 schema 迁移前拒绝并保持 LOCKED；密文完整性失败在迁移前明确报损坏并进入 RECOVERY_REQUIRED；两者 ciphertext 哈希均不变且不生成空库 |

**完成证据：**新增针对性测试先在未修复版本失败；修复后定向测试通过；数据库逻辑摘要对照表；源文件哈希/备份可恢复证据；Mimo 隔离 profile 升级截图或录屏。截图只补充，不替代逻辑数据摘要、负向路径和故障注入。`python tools/run_isolated_tests.py` 全量结果归 M3a。

### M2 — 核销历史保险库迁移审计（P0）

`docs/testing/legacy-migration-report.md` 的严重发现来自较早生产基线 `0e3180c` / `v0.209-beta.1`，不能直接宣称在 `ea4983d` 上仍然存在，也不能因之前报告曾有 253 项测试全绿就默认已修复。

- 在锁定的候选 SHA 上逐项重跑并更新 MIG-009–014：锁定后的明文残留、密文独立恢复、恢复码恢复、密文完整性认证、部分加密文件重试、重复 onboarding 是否覆盖 vault 元数据。
- 对错误密码与损坏密文另验：必须在 schema 迁移前拒绝；数据库不存在时不得生成空活动库；源 ciphertext 字节/哈希不变，应用保持 LOCKED 或 RECOVERY_REQUIRED。
- 对每项标记 `仍存在 / 已修复并有回归测试 / 不适用并有代码依据`，附当前提交上的命令、测试名和实际结果。
- 若空库替代已有数据、损坏密文仍可解锁、既有 vault 元数据可被覆盖或活动明文超出明确产品承诺等高影响问题仍复现，则列为 Beta 阻断，另开修复工作包；未经验证不得继续 tag。

**完成证据：**更新后的迁移审计报告绑定候选 SHA；每个高影响发现均有复现或回归测试证据和处理结论。

### M3a — 本机回归与 CI 工作流准备（P1）

- 在 M1/M2 改动后的同一候选树运行 `python tools/run_isolated_tests.py`；记录总数、通过、失败、错误、跳过数和 SHA。目标为零失败、零错误；浏览器回归不得因缺少 Playwright/Chromium 静默跳过。
- 候选有 7 项真实 Chromium 页面回归；当前环境未提供可用 Chromium，因此本机套件会将浏览器类标为跳过，不能记 PASS。CI workflow 已要求安装 Playwright/Chromium 并设置 fail-on-missing；仍须在实际 Windows PR/构建 workflow 日志中证明 7 项均执行且零跳过。
- Beta packaging validation 的路径过滤已补入 `store.py`、`core/**`、根目录 `vault.py`、`static/**`、`tests/**`、`tools/**`，并保留 `server.py`、发布工作流、Electron/打包配置、依赖和 `packaging/**`。仍需目标 PR 上实际运行以确认过滤和检查结果；不能仅凭 YAML 变更记为 CI 通过。
- 候选已实现 Beta/stable 两阶段发布：build-only 只在 `main` 上由 `workflow_dispatch` 触发，输入版本 tag、以该 dispatch 的 `GITHUB_SHA` 作为源码提交，权限 `contents: read`，不创建 tag/Release；独立 publish workflow 也由 `workflow_dispatch` 触发，要求 source run ID，并复核源 workflow/run、SHA/tree、tag 与六个附件 SHA-256 后复用原文件。仍需 Windows 运行和仓库侧 Environment/tag 保护实查。
- build-only 不得执行任意 PR/不受信任 ref，也不得拥有发布 secret；publish workflow 仅 publish job 赋予 `contents: write`，必须使用需 reviewer 批准的受保护 GitHub Environment，并验证发起人权限、manifest 来源、目标 main SHA/tree、tag 和附件 hash。普通 `workflow_dispatch` 参数不作为批准凭据。代码审查仍指出 tag 最终复查与 Release 创建之间存在非原子竞态，必须由 immutable/protected tag ruleset 弥补。
- 打包验证固定 Electron `0.209.0-beta.2`，用途是候选树上的构建/启动冒烟，不等同于实际 `v0.210-beta` tag 包或最终用户安装验收。

**完成证据：**最终候选 SHA 上全量测试报告；7 项 Chromium 测试无跳过；工作流路径与 fail-on-missing 浏览器步骤核验记录。

### M3b — Draft PR 的 GitHub 检查（P1）

- M3a 完成后，以固定候选 SHA 创建 Draft PR，触发 Windows Python 3.12 CI 与 Beta packaging validation；若手动 dispatch，明确指定并核对 PR ref/SHA，禁止默认跑 main。两条 PR workflow 均须运行 7 项 Chromium 测试（或由单独 required browser job 承担），不得静默跳过。该阶段与 M4 对话式验收并行。
- 核实 branch protection / ruleset 是否把这两项设为 required checks。若仓库没有强制规则，维护者仍须在合并前逐项确认成功，不能把“workflow 已触发”当成门禁。
- 记录 PR head SHA、PR merge SHA/tree 和 Mimo 实测 tree。GitHub PR workflow 可能在 merge ref 上运行，不能只比较 SHA 字符串；若 PR merge tree 与 Mimo 实测 tree 不同，按差异重跑受影响测试/截图。
- 检查实际 workflow run 的 event、权限、checkout ref、`GITHUB_SHA` 和产物目标 SHA；验证发布流程不会被 push tag 隐式触发，publish 阶段拒绝未批准的 run ID/manifest。
- 若代码或文案因测试/验收发生变化，更新候选 SHA/tree；旧 tree 上相应的本机测试、CI 和截图证据全部作废并重跑。

**完成证据：**Draft PR 链接；PR head SHA、PR merge SHA/tree；两项 workflow 的实际测试 SHA/tree 且全绿；浏览器测试 7 项实际执行、零跳过；required checks 配置核实结果。

### M4 — Mimo 对话式验收（P1）

只在 M1/M2 修复后的最终候选 SHA 上，以合成数据和隔离 profile 运行。优先用可丢弃 Windows VM/用户账户，证明 SQLite、`LOCALAPPDATA` 下配置、secrets、vault、device 状态及 Electron userData 均被隔离；仅设置 `BILI_FAV_ORGANIZER_DATA_DIR` 不足以满足隔离条件。每个场景由本地智能体逐步给出操作指引、等待用户截图，再依据截图/API 只读证据判定；不要求完全自动化。通过 fake B 站端点/响应桩或禁用外网路由隔离网络，并结合 mock 请求记录与应用写审计证明没有真实远端写入。

1. **首次配置与引导：**登录设置保存、建库顺序、引导完成及 `database_state:READY`。
2. **任务中心：**运行、暂停、恢复、取消全链路；检查点保留；进度双单位稳定；恢复后暂停原因清除；卡片展示检查点摘要。
3. **旧格式计数与停止状态：**旧格式计数正确展示；停止后已保存内容保留，状态为 cancelled 且无错误。
4. **运行中锁库：**在安全点暂停并显示 vault lock 原因；解锁后同一 `task_id` 恢复且执行计数符合预期。
5. **重启恢复：**运行中重启后状态为 interrupted；checkpoint 保持；不自动重放未确认工作。
6. **远端写入核对：**单次派发与 reconciliation 状态/证据正确；不确定结果进入 NEEDS_RECONCILIATION，不发生重复写入。
7. **用户停止扫描：**扫描页与任务中心状态一致；已完成夹保留；error 为 null；不误报为风控或失败。
8. **本轮新增前端回归：**空模型配置不刷 `/api/config`；关闭异常弹窗后 `scan_idle` 不抢回；空 reconciliation 不虚构记录；暂停/取消/失败/限流及未知状态时快速模式不进入分析，只有 `completed` / `completed_with_issues` 才继续。
9. **升级与保险库路径：**隔离 schema 7 `v0.209` profile 升级并核对收藏夹、条目、关联数与关键 ID；重启、锁库/解锁后数据仍在，状态正确。

**完成证据：**旧七场景和新增回归逐项列为 PASS/FAIL/NOT_RUN，并附最终候选 SHA/tree、操作截图和只读 API 摘要；任何未重测的旧场景都保持 NOT_RUN，不能继承 `5c2e7fa` 的 PASS。mock 收到的请求和应用写审计共同证明没有真实写入。不得清理或覆盖 `tests/Test-BFO-Beta2/` 等现有用户目录。

### M5 — 合并、隔离构建与 Beta 发布（P1）

- M3b 的 Draft PR 检查与 M4 的截图验收均绑定同一候选 tree；必需检查未全绿、Mimo 未验收前不合并。
- PR 包含所有生产代码、回归测试及 Beta changelog，目标 `main`；描述列出 schema 迁移风险、回滚证据和已知限制。
- 合并后记录真实 `main` merge SHA，等待该 SHA 的主分支 CI 通过；比较 `main` merge tree 与 Mimo/PR 已验收 tree。只有 tree 完全相同才沿用截图及 PR 检查；若 tree 不同，按差异重跑受影响测试和人工场景，并确认 main CI 后才继续。最终 Beta 构建必须从该 SHA 生成。
- 候选已将 Beta 发布工作流改为两阶段 build-only/publish。build-only 仅在 `main` dispatch，输入目标 tag/版本并从 `GITHUB_SHA` 绑定源 commit，不创建 Release；publish workflow 在 protected Environment 审批后校验 source run、SHA/tree、tag 和六项附件哈希，并复用原工件。该代码配置尚未在 GitHub 实际执行，仓库的 Environment 审批设置与 tag 不可变 ruleset 也未核实；tag 复查与创建仍有竞态窗口。
- 用 disposable Windows VM/profile 验收 build-only 的实际目标版本包：便携 ZIP、Inno Setup、Electron Neo 三种实际产物各自至少验证启动与隔离数据目录行为；至少一种完整执行 v0.209-beta.2 实际安装路径对应的 schema 7 升级、重启、锁定/解锁及数据核对。六个文件（3 包+3 校验文件）逐一核对 SHA-256，校验文件不计为安装对象。卸载/重装用可丢弃合成 profile，分别核对默认保留与显式清除行为，清除测试前先保留副本。
- 全部技术门槛、实际包安装验收和附件校验通过后，再由维护者明确 GO；之后才创建唯一 Beta tag，并手动触发 publish workflow 发布已验收文件。不得把创建 tag 与公开发布混成一个自动动作。分支推送授权不等同于发布授权。

**完成证据：**PR 检查链接、合并 SHA、main CI、build-only 运行链接、六个附件及 SHA-256、隔离安装验收报告、维护者发布 GO、最终发布与已验收附件哈希对照记录。

### M6 — Beta 后的稳定版决策

- 收集 Beta 安装、数据库迁移和真实使用反馈；没有未关闭的 P0/critical 数据完整性或恢复缺陷，其余已知风险有负责人、影响说明和书面接受记录。
- 稳定版前明确锁库竞态下 `/api/config` 偶发 500 是否修复；若延期，维护者在发布记录中书面接受影响范围与恢复办法。
- 更新 README 中稳定版 changelog；核对版本号、产物、LICENSE、NOTICE 和文档；对通过 `main` CI 的最终 SHA 执行 stable build-only，生成目标 `v0.210` 的三种安装包和 6 个附件并完成隔离安装验收。
- 候选已将 stable workflow 改为仅 `main` 上手动 build-only；版本 tag 作为输入，源 commit 绑定 `GITHUB_SHA`。publish workflow 显式发布先前验证过的同一批附件。仍需 Windows 构建实跑、仓库保护设置核验以及稳定版维护者 GO。
- 发布后核对正式工作流、tag 指向的 main SHA 和 6 个附件哈希，记录安装/升级回归结果。

### M7 — 维护性清理（P2，不阻塞迁移实现）

- `.gitignore` 忽略整个 `tests/.test-data/` 与根目录隔离 runner 的 `.bfo-isolated-tests-*` 临时目录，用 `git check-ignore` 验证；只清理本轮明确创建的临时目录，不删除已有测试数据。
- 重命名 `test_local_persist_failure_keeps_remote_success_as_done` 以匹配 `unknown` / `reconciliation_required` 契约，并修正 `server.py` 中相反的旧注释；不改变断言语义。
- 更新仍引用已移除工作流或声称测试数据已跟踪的文档文字。
- 标记本文件为新的维护计划基准，并核对旧 `reliability-release-checklist.md` 的状态/数据后再决定更新或归档；不得覆盖或清理未跟踪的旧清单。
- 保留并说明已接受限制：分页恢复目前是收藏夹级、未完成收藏夹从第 1 页重新读取；精确页级续扫另行排期。Issue #6 的真实场景反馈、Authenticode 签名也单独跟踪，不应被本轮状态误报为完成。

## 发布门槛汇总

| 门 | 必须满足 |
|---|---|
| Beta 开始候选 PR | M0 完成；schema 7 迁移与 M2 高影响 vault 项无未处理失败；候选 SHA/tree 固定 |
| 合并 PR | M1 定向与完整隔离测试通过；Mimo 对话式验收完成；Windows CI 与 Beta packaging validation 通过；没有跳过的必测浏览器回归；branch protection 或人工门禁已核实 |
| 构建 Beta 候选 | PR 已合并；对应 `main` SHA 的 CI 通过；changelog 已入库；build-only 流程不会创建公开 Release |
| 创建/公开 Beta tag | 三种实际包完成隔离安装/升级验收；六个文件逐一核对 SHA-256；数据库与 vault 保留证据齐全；维护者明确 GO；发布复用已验收的同一批文件 |
| 构建稳定候选 | Beta 反馈完成评估；目标 `main` SHA 的 CI 通过；stable build-only 生成目标版本包但不公开 |
| 创建/公开稳定 tag | 三种实际稳定包隔离安装验收通过；六个文件逐一核对 SHA-256；无未关闭 P0/critical 数据问题；已知限制和锁库竞态决策记录齐全；维护者最终 GO；发布复用已验收附件 |

## 估时与下一步

此前“1–2 个工程日”的估算不再可靠：schema 1 来源判定、Windows CI/文件锁、Chromium 回归、Mimo UAT、真实安装验收和仓库发布保护设置尚未完成。当前不能给出负责任的发布日期；可在这些门禁有负责人和窗口后再估算剩余日历时间。

本草案通过审查后，建议执行顺序为：**M0 →（M1 与 M2 并行）→ M3a →（M3b 与 M4 并行）→ 合并及 main CI → build-only Beta 包与安装验收 → 维护者 GO → 公开 Beta → Beta 反馈 → M6**。若任一代码/文案/工作流修改造成 tree 变化，旧 tree 上相应的测试、CI 和截图证据作废。M7 可与 M3a 并行，但不能替代任何数据安全门槛。

## 审查记录

| 审查 | 结果 |
|---|---|
| schema 迁移独立分析 | schema 7 → 8 为确定支持路径；schema 1 需来源取证和明确支持决策；建议源版本契约、候选副本迁移及失败保全 |
| 测试 / CI 独立分析（历史发现） | 253 项为本机证据而非 CI；浏览器测试存在 CI 跳过风险；当时的 Beta packaging 路径过滤可能漏项。本轮已在候选中补路径并设 fail-on-missing，仍需实际 PR workflow 证明有效。 |
| 发布流程独立分析 | 旧七场景 UAT 早于最终修复提交；Beta 前应重验升级、同 SHA Windows CI、真实 Beta 安装 |
| 最终草案审查 | 已采纳：拆分 M3a/M3b 消除 PR 依赖循环；按 tree 绑定 PR 与 Mimo 证据；补齐旧七场景、branch protection、准确的 workflow 路径过滤；Beta/stable 均改为 build-only、隔离验收、明确 GO 后复用已验收附件发布；加严 schema 1 恢复路径、真实 v0.208/v0.209 夹具、并发写入及替换后失败测试。 |
| 2026-10-14 候选实现审查（历史） | 当时跨进程锁范围尚未解决；该结论已由本轮 T6 候选修正，不能当作当前代码状态。 |
| 2026-10-16 候选复审 | profile 级跨进程锁与正式 `server.py` 入口的单实例保护已审查；补充了入口级竞争/释放测试和旧设备绑定回滚测试。Linux 验证通过，Windows `LockFileEx` 仍待 CI。发布 workflow 静态审查未发现新的代码级权限绕过，但 tag 竞态、GitHub Environment reviewers、自审/管理员绕过和 immutable tag 规则仍需仓库侧核验。 |
