# 可靠性维护任务清单（供 Mimo Desktop 执行）

**用途：**按本清单维护 `bili-fav-organizer`，逐项修复、测试、截图验收并汇报。它把 [维护计划](reliability-maintenance-plan.md) 转成可执行任务卡。
**更新日期：**2026-10-19
**状态：**代码维护与复审进行中；未完成 Windows CI、Mimo 验收或发布审批，不代表 Beta 可发布。

**交互看板：**既有项目状态页 `index(20261018-203653).html`（已合并本轮进度） · [最新执行记录](reliability-maintenance-execution-20261019.md)


**Windows 本地交接：**目标分支 `feat/reliability-scheduler-implementation`；[本地 AI 验收提示词](local-ai-windows-acceptance-prompt.md)、代码、测试与本清单在同一交接提交中。准确候选 SHA 以交付消息与远端实际 HEAD 核验为准。旧离线包保留用于溯源，不能向新提交重复应用补丁。

## 最新 Windows 阻断（2026-10-20）

用户转交的 Windows 报告：旧候选 `65ba02e` 共306项，261 PASS、6 FAIL、39 ERROR、0 SKIP。**T6/T8 对旧候选为 FAIL/BLOCKED**。空profile父目录未创建及历史SQLite夹具未入Git已定位并在本提交修复，真实Windows复验仍待完成；下文2026-10-19记录保留为历史，不是最新放行结论。详见 [阻断根因与逐条复测指令](windows-acceptance-blockers-20261020.md)。本地AI只从目标远端分支取得交付消息指定的新SHA，不应用旧离线包。

## 本轮执行状态（2026-10-19）

候选基线为 `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63` / tree `939a0607988524b7afec8097fde3e9acda4ec655`。本次交接将维护工作树的候选代码、测试与文档整理为该基线的正常后继提交，目标远端分支为 `feat/reliability-scheduler-implementation`。读取当前交接提交的 `git rev-parse HEAD`、`git rev-parse HEAD^{tree}` 并与交付消息匹配，固定候选身份。原本地 `f22965f` 工作树与未知未跟踪文件保持保留。此前的离线包源码 tree 为 `0155b38d8d7d16456a69a29981b04686343070b2`；本次仅更新交接文档，生产代码与测试内容不变。下表不代表候选 Windows CI、安装验收或 Mimo 截图验收。

| ID | 当前状态 | 已核验证据 / 未完成项 |
|---|---|---|
| T0 | **PARTIAL** | 已核实 EA 基线并整理正常候选提交供目标分支交接。没有 Windows VM、完整 Windows profile 路径审计或文件访问审计。 |
| T1 | **BLOCKED** | schema 1 样本来源及支持范围仍未确定。 |
| T2 | **PARTIAL** | Schema 7→8 候选副本迁移有真实 v0.209 DDL 合成夹具、WAL 保留和失败保源测试；schema migration 16/16、lifecycle 22/22 通过。实际 beta.2 profile、Windows 安装升级仍未验收。 |
| T3 | **PARTIAL / BLOCKED** | Schema 7/8 表列、主键、外键、唯一键、ledger 连续性及拒绝路径有测试；schema 1 来源与支持范围未定，列类型/多数 NOT NULL/CHECK 约束尚未全面校验。 |
| T4 | **PARTIAL** | vault migration 模块定向运行 12/12 通过，覆盖锁定/解锁、错误密码、损坏密文、归档保全、重复周期及封存窗口双进程锁；早期全量运行中的 MIG-012/MIG-028 间歇失败未在最新全量运行复现。Windows 锁实现和实际安装 profile 尚未验收。 |
| T5 | **PARTIAL** | 错误密码、密文损坏、缺失/篡改清单均保持失败关闭；没有生成空活动库的回归证据。Windows profile/安装验收未完成。 |
| T6 | **PARTIAL / RELEASE BLOCKED** | profile 共享/独占锁已接入数据库生命周期、迁移、vault 封存/恢复和手动合并恢复工具；Linux 跨进程与替换后回滚用例作为补充诊断通过，不代表 Windows 验收。新增真实 `server.py` 双进程测试覆盖拒绝第二实例、退出释放和再次启动。Windows `LockFileEx` 尚未运行；升级前仍须关闭不遵循新锁协议的旧版本进程。 |
| T7 | **PARTIAL** | vault migration 模块定向运行 12/12 通过，新增 MIG-029 验证封存窗口内第二进程无法打开 profile；早期全量运行中的 MIG-012/MIG-028 间歇失败未在最新全量运行复现。仍需逐项更新历史迁移报告，并在 Windows 候选环境复验。 |
| T8 | **PARTIAL / BLOCKED** | Linux 全量隔离运行的 4 个 `test_update_flow` 断言差异和 1 个 `winreg` 错误在 EA 基线同样复现，属于非目标平台诊断结果，不作为 Windows 产品验收门槛。最新 306 项运行没有复现先前全量运行中的 MIG-012/MIG-028 间歇错误；定向 vault migration 为 12/12。仍未在最终候选 SHA 的 Windows 环境执行全量测试；本机缺 Chromium，7 项浏览器回归未执行。 |
| T9 | **PARTIAL** | Beta/Stable build-only 与独立 publish workflow（实际 Environment 保护待仓库侧核实）有候选 manifest、六项 hash 和源身份复验；当前发布流程复审未发现新的代码级权限绕过。Environment reviewers、自审/绕过限制与不可变 tag 规则仍需仓库侧实查，tag 复查与创建间仍有竞态窗口。 |
| T10 | **NOT_RUN** | 最终 SHA 的 Mimo 对话式截图验收尚未执行。 |
| T11a | **NOT_RUN** | PR #21 的成功 CI 只验证 EA 基线与 Playwright 配置；没有当前候选 SHA 的 GitHub CI 记录。候选 Draft PR checks 未运行。 |
| T11b | **NOT_RUN** | 未合并 main，也未运行 build-only 与安装验收。 |
| T12 | **NOT_RUN** | 未创建 tag、Release 或发布。 |
| T13 | **PARTIAL** | `.gitignore` 已忽略 `tests/.test-data/` 和根目录 `.bfo-isolated-tests-*` runner 临时目录；README、维护计划、任务清单和历史迁移报告已更新。旧 `reliability-release-checklist.md` 保持未跟踪、未改动。 |

当前定向结果（2026-10-19 本轮复核）：schema migration `16/16`、lifecycle `22/22`、server instance lock `2/2`、migration-only `30/30`、release candidate `5/5`、release workflow contract `10/10`、apply safety contract `12/12` 通过。既有 vault migration `12/12`、release/runner/attribution `26/26`、vault device secret `13/13` 及 legacy/failure recovery `17/17` 记录不变。线上只读复核确认 PR #21 仍为 Draft 且说明限定 CI-only/DO NOT MERGE，不能作为当前候选验收。Linux 全量运行记录用于开发诊断；其平台相关失败不判定 Windows 产品通过或失败。T8 仍因最终候选 SHA 尚未在 Windows 上完成全量回归和 Chromium 浏览器测试而阻断后续候选验收。Mimo UAT 和安装验收也未完成。

## 给执行代理的总指令

先核实本地仓库和数据隔离条件，再按任务依赖推进。不要只给方案；在授权范围内实际检查、修改和测试。独立任务可以并行，任何代码变化后都要记录新的 SHA/tree，并重跑受影响的验证。

### 已授权范围与停止边界

- 可以在现有 `feat/reliability-scheduler-implementation` 分支上检查、修改、测试、创建正常提交，并按既有授权向同一分支做**快进推送**。禁止 force push、改写历史或推送到 `main`。
- 不创建 PR、不合并、不创建 tag、不发布 Beta/稳定版，也不触发可能公开发布的 workflow；这些都需要维护者后续明确批准。
- 不运行 `git clean` 或批量删除命令。保留 `tests/Test-BFO-Beta2/`、未跟踪文档、测试目录及其他无法确认归属的文件。
- 不使用真实 B 站账号、Cookie、恢复码、API Key 或正式用户数据；远端写操作必须被 mock 或在网络层阻断。
- 不以放宽断言、跳过测试、弱化 schema/密文校验来消除失败。
- 发现真实 profile 写入、源数据变化、记录/关联缺失、空库被当作恢复成功、密文损坏仍进入 READY、候选身份不明时，立即停止相关操作，保留原件与日志，标为 `FAIL` 或 `BLOCKED`；不要继续重试初始化、迁移或解锁。

### 全任务数据隔离要求

从准确候选 SHA 建立全新 disposable checkout，并使用可丢弃 Windows VM 或临时 Windows 用户账户。运行前核实应用实际解析的 SQLite、repo-local `data/library.sqlite3`、`APPDATA`、`LOCALAPPDATA` 下的 config/secrets/vault/device 状态、`TEMP`、日志、便携包工作目录和 Electron `userData` 都指向沙箱；**只设置 `BILI_FAV_ORGANIZER_DATA_DIR` 不够**，它只重定向 SQLite。仓库中现有 `data/library.sqlite3` 及其 WAL/SHM 永远不得作为测试目标。测试前后使用 VM 快照或文件访问审计补充检查，不能只依赖时间戳。若无法证明完整隔离，停止 GUI、vault 和安装包测试并报告 `BLOCKED`。

对迁移源的操作先离线复制数据库、伴随的 `-wal` / `-shm` 与 vault 元数据，所有只读分析和破坏性测试都对副本进行。若副本缺少 WAL，记录该快照限制，不能据此宣称源库内容完整。

## 执行任务

每项结束时标记 `PASS / FAIL / BLOCKED / NOT_RUN`，附实际证据。不得把先前提交的 PASS 继承到新 SHA。

| ID | 任务 / 依赖 | 操作与通过标准 | 立即停止条件 |
|---|---|---|---|
| T0 | **锁定仓库基线与数据边界**；无依赖 | 记录 branch、HEAD、tree、parent、工作区状态；核对远端候选 `ea4983d2bb9f6d8c0ea54faeddc790a9e096df63` / tree `939a0607988524b7afec8097fde3e9acda4ec655`。如远端已前进，记录实际 SHA/tree 并重新绑定全套证据，不要 reset。用全新 disposable checkout/profile 核实所有应用路径，并保存 VM 快照或文件访问审计证据；明确排除 repo 中现有 `data/library.sqlite3` 及 WAL/SHM。把较早 `5c2e7fa` UAT 与 `v0.209-beta.2` 安装验收（含 FAIL/MANUAL 项）单独记录，不作为当前候选 PASS。 | 分支或对象不符、未跟踪内容归属不明、实际数据路径无法确认、现有仓库 DB 被打开/写入、隔离范围无法证明时，不切换/清理/运行数据迁移。 |
| T1 | **schema 1 样本只读取证与支持范围报告**；T0 | 离线复制 DB、WAL、SHM、vault 元数据；仅检查副本的版本标记、表/列、完整性、外键和业务行数/ID 摘要；注明样本来源和 WAL 是否齐全。形成 schema 1 来源报告，交维护者决定支持边界。已知 `v0.208`、`v0.209` 均为 schema 7。 | 原件被应用或普通读写连接打开、原件哈希/时间戳变化，或无法判断样本来源时停止。Mimo 不得自行宣布 schema 1 受支持或不受支持。 |
| T2 | **schema 7→8 正向迁移**；T0；与 T4 可并行 | 使用由真实 `v0.209`（优先包括 beta.2 实际安装路径）生成的合成隔离数据库；覆盖空库、常规记录、WAL 已提交记录、扫描中断状态。迁移前后比较收藏夹/视频/关联/配置/关键 ID 的规范化逻辑摘要，运行 `integrity_check` 和 `foreign_key_check`，再重启并验证幂等。v0.208 按项目声明的兼容范围补测。 | 只凭 UI 截图或 `integrity_check=ok` 不能判 PASS；任何业务摘要不一致、空库替代已有数据、READY 状态错误即停止。 |
| T3 | **版本契约与拒绝路径**；T0、T1 的取证， schema 1 范围待维护者决定 | 按已批准的源版本契约校验必需表、列、关系及迁移缺项。只对确实要求 migration ledger 的版本检查 `schema_migrations`；旧版若没有 ledger，必须靠可核实历史来源/可靠版本标记识别。覆盖缺表/缺列/约束错误、损坏库、无效版本、迁移链缺边、未知/未来 schema。来源不明时进入保全/恢复状态，不猜版本、不自动初始化。 | 未批准的 schema 1 策略、历史版本映射无来源依据、或拒绝路径改写原库/暴露空活动库时阻断。 |
| T4 | **隔离保险库审计 MIG-009–014**；T0；与 T2 可并行 | 在准确候选 SHA 上重跑旧审计的明文残留、密文恢复、恢复码、完整性认证、部分文件重试、重复 onboarding。先查明产品承诺中“锁定”的实际含义；不自行假设它等同于静态加密。逻辑数据保留用解密后规范化摘要比较；原密文哈希用于证明负向测试未改写源文件。 | 空库替代数据、密文完整性绕过、vault 元数据覆盖等高影响问题复现即标 P0 并停止 Beta 流程。若锁定后明文状态与产品承诺不一致或承诺不清，交维护者决策，不擅自清理或标 PASS。 |
| T5 | **错误密码与损坏密文状态**；T4 | 错误密码必须维持 `LOCKED`；AEAD/完整性校验失败必须明确报告密文损坏并进入 `RECOVERY_REQUIRED`（或经代码定义的等价损坏状态）。两者均须在 schema 迁移前拒绝、无空活动库、无 READY，源密文逐字节/哈希不变。 | 错误密码或损坏密文使库进入 READY、生成空库、改写源密文或只检查密文头部就宣称成功，立即停止。 |
| T6 | **迁移并发、回滚与入口路径**；T2–T5 | 迁移前记录源文件清单/哈希和逻辑摘要；排空任务/API 写入并获得跨进程排他锁，或检测并发源变化后放弃切换。用 SQLite backup API 从含已提交 WAL 的一致快照建候选；候选通过校验前源不变。覆盖复制、迁移、验证、backup/manifest 写入、切换后校验失败和重试；实际验证备份可恢复。覆盖普通启动、旧位置搬迁、vault 解锁。目标已存在不得覆盖；切换前复核源身份/摘要。 | 第二实例写源且候选仍被切换、失败后无法恢复源库、旧路径来源被删除、或任何半迁移库被标 READY，立即停止。 |
| T7 | **补迁移回归测试并核销旧报告**；T2–T6 | 为已修复缺陷增加会在旧版本失败的回归测试；在旧代码上确认新测试能捕获问题（只在隔离 checkout 操作）。更新 `legacy-migration-report.md`，逐项标记当前 SHA 的 `仍存在 / 已修复并有测试 / 不适用并有代码依据`。 | 不能拿旧报告中的失败当作当前缺陷，也不能拿其他测试全绿推断已修复。证据不足时标 `NOT_RUN`。 |
| T8 | **目标平台全量回归与 CI 配置检查**；T2–T7 的生产改动固定后 | 在 Windows 目标环境运行 `python tools/run_isolated_tests.py`，记录总数、通过/失败/错误/跳过；确认 7 项 Chromium 测试可执行且零跳过。Linux 可用于开发诊断，但 Linux 专属 API 缺失或 Windows 专属更新流程差异不作为产品验收结果。核对 CI 固定的 Playwright 版本、`BFO_REQUIRE_PLAYWRIGHT`、Chromium 安装与隔离 runner 路径；确认 Beta packaging path filter 覆盖 `store.py`、`core/**`、根 `vault.py`、`static/**`、`tests/**`、`tools/**`，并保留已有 `server.py` 和发布/打包路径。配置静态检查与 Windows 实际回归分别记录，不混为一项结果。 | Windows 目标环境出现失败/错误，或 CI 允许必测浏览器回归静默跳过时，不进入候选验收。Linux 诊断失败单独记录并按其是否揭示与目标平台无关的逻辑缺陷评估；不得通过弱化断言或临时绕过来报绿。 |
| T9 | **发布 workflow 安全改造**；T8 | 核实 Beta/stable 均无 tag push 自动公开触发。build-only 只在 `main` 上由 `workflow_dispatch` 触发，输入目标版本/tag；源码 SHA/tree 绑定该次 dispatch 的 `GITHUB_SHA`，不接受任意 SHA 或执行 PR ref，权限 `contents: read` 且无发布 secret。dispatch 前先确认 `main` 当前 SHA/tree 等于已验收目标；运行后再核对 `head_sha/tree`，不匹配就停止。publish 也只由 `workflow_dispatch` 触发；仅 publish job 有 `contents: write`，并使用受保护 GitHub Environment。校验发起人维护权限、环境批准、可信 build-only run ID、批准的 main SHA/tree、tag SHA/tree 与 manifest/六个附件 SHA-256 一致，再发布同一批文件，不重新构建。维护者还需从仓库设置核实 required reviewer、自审限制、管理员绕过和 immutable/protected tag ruleset。T9 修改后在最终 tree 重跑 T8 的 Windows 目标平台全量回归与配置检查。 | workflow 的事件、权限、checkout ref、`GITHUB_SHA` 与目标版本不符，build-only 运行不可信代码、publish 未经 Environment 审批、tag 自动触发发布、或 build-only 意外创建公开 Release，立即停止。 |
| T10 | **Mimo 对话式 UAT**；T2–T9 完成且 T8 已在最终 tree 重跑；与 Draft PR 检查并行 | 通过 screenshot-by-screenshot 对话验收七个历史场景：首次配置、任务中心、旧格式计数/停止、运行中锁库恢复、重启中断恢复、远端写核对、用户停止；另验本轮 config 请求风暴、异常弹窗、暂停取消、进度/检查点/暂停原因、空 reconciliation、快速模式状态。schema 7 升级用全隔离 profile。每场景等用户提供截图，再用只读 API 和 mock 写入日志判定。 | 截图/数据证据缺失则 NOT_RUN；发现真实远端写、正式 profile 被触碰或数据异常，立即停止。旧 `5c2e7fa` UAT 和 `v0.209-beta.2` UAT 不算本候选 PASS。 |
| T11a | **Draft PR checks**；T8、T9 完成；与 T10 并行 | 仅在获得维护者开 PR 授权后创建 Draft PR；记录 PR head SHA、merge SHA/tree、workflow event 和实际 checkout SHA/tree；核实 required checks/人工门禁。Windows Python CI 与 Beta packaging validation 实际执行，7 项 Chromium 回归零 skip（或由单独 required browser job 执行）。若手动 dispatch，必须指定 PR ref/SHA。PR merge tree 与 T10 Mimo tree 不同，则按差异重跑受影响测试/截图。 | 未获 PR 授权、workflow 未跑到目标 ref、必需 check 不通过、浏览器测试跳过或 tree 不一致未复验时停止。 |
| T11b | **Merge 后 CI、build-only 与包验收**；T10、T11a 均 PASS 且维护者批准 merge | 合并后确认 main tree 与已验收 tree 一致，不同则重验；等待 main CI。dispatch 前确认 `main` 当前 SHA/tree 等于准确目标，再在 `main` 上手动触发 build-only（workflow 输入是 tag，不是 SHA）；核对运行 `head_sha/tree` 与批准目标完全一致。三种安装包分别验启动/隔离数据目录，至少一种完整升级；六个文件逐一校验 SHA-256。 | 未获 merge 授权、main CI 未通过、tree 不匹配未复验、或安装包与 manifest 不一致时停止。 |
| T12 | **Beta 与 stable 发布**；T11b 及后续 Beta 反馈 | 不创建 tag、不公开 Release。完成安装验收和报告后，先向维护者提交证据并等待明确 GO。Beta/stable 的 build-only 附件须由通过保护环境校验的 publish workflow 复用；stable 前确认无未关闭 P0/critical 数据问题并记录已知风险接受情况。 | 当前授权不包含 tag、Release 或 main 合并；未收到对应阶段的明确 GO 时保持停止。 |
| T13 | **低优先级清理**；不阻塞 T2–T6 | 忽略 `tests/.test-data/` 并验证 `git check-ignore`；修正测试名称/旧注释；更新过期文档。先盘点旧 `reliability-release-checklist.md` 与现有未跟踪文件，不删不覆盖。 | 清理命令可能影响不明目录时停止；若改动改变行为契约，转成生产代码任务并补测试。 |

## 对话式验收操作方式

Mimo 不要一次性自动模拟用户截图。每个 UAT 场景依次：说明当前操作及预期 → 等待用户操作并提供截图 → 检查截图中可见状态 → 必要时给出只读 API 查询方法 → 根据 UI、API 与 mock 记录共同判定 → 记录 PASS/FAIL/NOT_RUN 和证据。不要要求用户提供账号、Cookie、密码或恢复码。

## 每项完成后的回报模板

```text
任务 ID / 状态：PASS | FAIL | BLOCKED | NOT_RUN
候选 branch / SHA / tree：
profile / 夹具 / 全隔离路径确认：
执行步骤与命令：
测试总数 / pass / fail / error / skipped：
迁移前后逻辑摘要：
负向测试源文件与密文哈希：
截图 / 日志 / 只读 API / mock 写入审计位置：
当前未解决的问题和阻断级别：
是否触发停止条件：
下一项可执行任务：
```

## 执行顺序

**T0 →（T1 + T2/T4 可并行）→ T3/T5/T6 → T7 → T8 → T9 → 在最终 tree 重跑 T8 →（T10 与获批后的 T11a 并行）→ 维护者批准 merge → T11b → 提交维护者审阅 → 获得明确 GO 后才进入 T12。** schema 1 支持决定、生产代码大改、main 合并、tag 与公开 Release 都不能由 Mimo 单方面推断或执行。T13 可在不影响安全边界时并行。

## 本轮独立复审记录

- **Schema / lifecycle：**候选副本、SQLite backup、安装前校验、失败回滚路径整体通过只读审查；Schema 7→8 定向测试通过。未解决项：schema 7 以下版本缺少逐版本源结构契约，旧 ledger 缺边可能被接受；schema 1 来源仍未知。
- **保险库：**旧路径主库、活动库、plain backup 和 recovery 源在锁库时先封存验证、最后删源；公开失败路由保留 READY/unlocked 状态和原始源。未解决项：源枚举/摘要检查到最终删除仍有跨进程 TOCTOU；加密归档未接入 UI 恢复。
- **T6 写入锁范围：**独立审查盘点 store、scheduler、vault、备份及恢复工具，确认现有 lifecycle leases 和 scheduler owner lock 均非全局维护锁。仅在迁移入口加锁会遗漏写入路径；需集中实现 profile 级进程间协议并做 Windows 双进程测试，当前仍为 Beta P1 阻断。
- **发布流程：**build-only 只在 `main` 上手动 dispatch，输入版本 tag，源码绑定该次事件的 `GITHUB_SHA`，仅 `contents: read`；publish 复核 build run、source SHA/tree、manifest、六项资产哈希和 tag。静态审查未发现新的代码级权限绕过；tag 复查到 Release 创建之间仍有竞态，GitHub `release-approval` reviewers、自审/管理员绕过策略与 immutable tag ruleset 仍需仓库侧核验。此前发现并修正两项：GitHub Actions 不支持 `administration` token permission key；collaborator API 的 maintain 角色应读 `role_name=maintain`。release/runner/attribution 合同测试为 26 项通过。
- **锁与保险库复审：**新增真实 `server.py` 子进程入口测试，验证同一 profile 的第二进程退出码为 2、首个进程退出释放租约、之后进程可启动；补测重新绑定写入 metadata 后失败并恢复旧 secret/wrap。独立复审未发现新的 P1/P2。Windows `LockFileEx` 仍待 CI 验证。
- **非目标平台诊断：**Linux 全量隔离运行中，4 个 Windows 更新流程断言差异和 1 个 `winreg` 错误也在 EA 基线出现，不作为 Windows 产品验收门槛。早期全量运行中的 MIG-012/MIG-028 间歇失败未在最新 306 项运行复现，且 vault migration 定向套件 12/12 通过。最终候选仍须在 Windows 上跑全量回归；本机 Chromium 缺失使 7 项 UI 测试尚未执行，线上 CI 目前只验证 EA 基线。
