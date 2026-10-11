# 旧版数据迁移与兼容性测试报告

## 执行摘要

- 日期：2026-10-11（Windows 11，`10.0.26300`）。
- 分支：`test/legacy-data-migration`。测试所用生产源码基线为 `0e3180cff40befc52698ae80cc467e8f4a288f70`（`v0.209-beta.1`）；本分支只增加测试、报告及 CI/冒烟辅助改动，没有修改生产迁移逻辑。
- 历史基线：`v0.208`，commit `b39d7af30a8a365e1e491d848a70474036ec3113`；`store.py` blob `f9380f5421769620dfdac162db712ca94fb9910b`。
- 被测版本：该 HEAD 上的 `main` 代码。v0.208 与当前 `store.py` 完全相同，SQLite schema 都是 v7，因此这次没有 v0.208 到当前版的 SQL schema 变更。
- 测试环境：Python 3.12.10、SQLite 3.49.1、Node 25.2.1、Electron 44.7.0、electron-builder 26.15.3。
- 安全边界：所有数据库、配置和 GUI profile 都落在仓库内创建的临时目录。没有读取或写入宿主 `%LOCALAPPDATA%\BiliFavOrganizer`，没有真实 Cookie/API Key，也没有调用 B 站或付费模型服务。应用测试代理指向关闭的 `127.0.0.1:9`；带 `--allow-build-downloads` 的打包尝试只允许获取打包工具文件。
- 最终完整自动化结果：**201 项，195 PASS、6 FAIL、0 ERROR、0 NOT_RUN、0 BLOCKED**。6 个失败全部来自迁移/保险库测试。
- 发布结论：**不建议继续发布 v0.209-beta.1**。其中恢复空库、损坏密文仍解锁和重复初始化替换保险库元数据都可能让用户误以为旧数据已恢复。

## 命令与结果

完整套件命令（当前机器缺少 `httpx2`，依赖仅安装到仓库内临时目录）：

```powershell
python tools/run_isolated_tests.py --pythonpath .bfo-test-dependencies
```

最终工作树执行完整套件三次。前两次（固定历史时间戳之前）依次为 **192 PASS、8 FAIL、1 ERROR** 和 **195 PASS、6 FAIL、0 ERROR**；固定时间戳后的运行是 **195 PASS、6 FAIL、0 ERROR**。本次提交前复验再次运行完整套件，结果仍为 **201 项：195 PASS、6 FAIL、0 ERROR**（`177.224s`）。各次均复现相同的 6 个迁移/保险库失败。第一次另有 2 个既有扫描诊断失败和 1 个错误；扫描诊断模块单独运行 **18/18 PASS**，其后完整运行均通过，故将它们记录为首次运行的时序/负载异常。

合成夹具证据命令：

```powershell
python tools/legacy_fixture_evidence.py
```

单独迁移套件命令：

```powershell
python tools/run_isolated_tests.py --migration-only --pythonpath .bfo-test-dependencies
```

固定历史时间戳之前的独立运行结果为 `Ran 22 tests in 95.945s; FAILED (failures=6)`，即 **16 PASS、6 FAIL、0 ERROR**。本次提交前复验重新执行这 22 项，结果仍为 **16 PASS、6 FAIL、0 ERROR**（`94.020s`）。

Windows 包构建尝试：

```powershell
python tools/build_isolated_windows_package.py --keep-workspace
python tools/build_isolated_windows_package.py --keep-workspace --allow-build-downloads
```

PyInstaller 后端成功构建。NSIS 安装包没有完成：Electron Builder 下载依赖长时间停在下载阶段，未生成可安装文件。随后用本机缓存的 Electron 发行目录成功构建 unpacked Electron app，并在仓库内的隔离 profile 中启动。冒烟命令：

```powershell
python tools/run_isolated_packaged_smoke.py .bfo-package-smoke-jmn3_162 --profile-name launch-profile-final3
```

Electron/后端行为证据：主窗口导航到动态本地端口，preload 注入成功，SQLite 和 config 在隔离目录内生成；执行退出后 Python 后端 **365 ms** 内优雅退出，服务端口释放，Electron 进程退出码为 0。窗口/前端 smoke 共 17 项，**12 PASS、5 FAIL**：标题栏 drag 区域、系统按钮留白、背景透明度、内容区高度、版本号显示断言未通过。该 UI smoke 不改变迁移逻辑，但打包验收仍为失败；`tools/shell-smoke.mjs` 已加上等待实际页面导航的逻辑，避免把导航中的 `about:blank` 当成应用页。

## 合成旧版夹具

`tests/migration/fixtures/seed_v0208.py` 从 Git 中读取真实 `v0.208:store.py`，在临时工作目录导入并运行其初始化逻辑生成数据库。生成前会核对 tag commit 和源码 blob；没有手工仿造表结构。Cookie 和 API Key 都是 `SYNTHETIC_*_DO_NOT_USE` 明显无效占位值。

| 数据集 | v0.208 记录数 | 说明 |
| --- | --- | --- |
| `legacy-empty` | `schema_migrations=7`，`app_state=8`；业务表为空；旧版 `providers/models=0/0` | 当前服务首次启动会按默认配置新增 1 个 provider 和 1 个 model。 |
| `legacy-normal` | folders 10，videos 500，folder_items 1,000，scan_state 10，profiles 10，analyses 4，plans 3，app_state 8，providers/models 1/1，schema_migrations 7 | 每条视频跨两个收藏夹关联；包含画像、完成/未完成方案、任务状态和无效测试凭据。 |
| `legacy-edge` | folders 12，videos 48，folder_items 96，scan_stage 1，scan_state 10，profiles 10，analyses 4，plans 3，app_state 9，providers/models 1/1，schema_migrations 7 | 包含空收藏夹、重复/跨夹视频引用、中断扫描、失效项、缺省配置；夹具复制时保留了 8,272 字节 WAL。当前启动迁移配置后 providers/models 为 2/2。 |

三组旧版和迁移后数据库的 `PRAGMA integrity_check` 均为 `ok`，`PRAGMA foreign_key_check` 均无返回行。MIG-004 也对迁移后的正常与 edge 数据重复检查。

规范化核心业务数据的 SHA-256（不比较 SQLite 文件哈希）。历史夹具子进程固定时间后，核心摘要跨两次独立证据生成保持稳定：

| 数据集 | 核心业务摘要 | 结果 |
| --- | --- | --- |
| empty | `39caac493cb26e311675bbd3315ca3b14f67d8188680622c133d0ff041d873df` | 核心业务表旧版与当前一致；启动时新增的默认 provider/model 不计入核心摘要。 |
| normal | `67595540e7fe246436f9295901c0f6f7762fd0c4e513d3bba2a52099e251a1cf` | 旧版与当前一致；同一次生成中的全部表也一致。 |
| edge | `c00352ed206752f93784e92d4a1276ab91aae179a561a2885f88091cc0894424` | 旧版与当前一致；全部表因配置/provider/model 迁移而不同。 |

全表逻辑摘要包含服务引导时写入的时间字段，因而会在不同运行间变化；报告不把这些全表摘要固定为跨运行基准。测试逐次比较同一生成批次中的旧版与当前全表数据，固定并跨运行核对的是核心业务表摘要。

配置/API Key 保留断言通过。测试证实旧版凭据会被迁移到当前配置/SQLite provider 记录，但当前活动 SQLite 文件仍是明文，这与 vault 加密快照并存。

## 用例结果

迁移套件共 22 个 unittest 方法（MIG-014 含 4 个故障子场景，另有 data-dir override 断言）。表内 `MIG-014` 的总结果按其 4 个方法汇总。

| 编号 | 结果 | 实际结果 |
| --- | --- | --- |
| MIG-001 | PASS | v0.208 空库迁移后有效，业务数据保持为空。 |
| MIG-002 | PASS | 10 个收藏夹、500 条视频、1,000 条关联和附属数据保留。 |
| MIG-003 | PASS | 中断状态、edge 记录和捕获的 WAL 数据可迁移。 |
| MIG-004 | PASS | `integrity_check=ok`；无外键违规。 |
| MIG-005 | PASS | 收藏夹、视频和多对多关系的关键 ID/关联保持一致。 |
| MIG-006 | PASS | 画像、归类方案、扫描状态和历史记录保留。 |
| MIG-007 | PASS | 合成供应商配置、无效 Cookie/API Key 可从迁移目标读取。 |
| MIG-008 | PASS | 重复运行位置迁移后逻辑摘要不变，旧来源仍保留。 |
| MIG-009 | FAIL | 密文快照逻辑内容正确；完成锁定后活动明文 DB 和 `.plain-backup` 仍存在。 |
| MIG-010 | FAIL | 删除隔离目录中的明文 DB 后，启动导入逻辑先创建空 DB；解锁返回成功但没有从密文恢复旧数据。 |
| MIG-011 | PASS | 错误密码和错误恢复码均返回拒绝，保险库仍锁定。 |
| MIG-012 | FAIL | 恢复码解锁返回成功，但数据库摘要从 normal 夹具变成空库摘要 `39ca…`。 |
| MIG-013 | FAIL | 对密文末尾翻转 1 bit 后仍通过自检并解锁，生成 0 文件夹/0 视频的活动数据库。 |
| MIG-014 | FAIL（2 PASS、2 FAIL） | SQLite 复制中断后可重试，损坏 SQLite 头不会覆盖来源；部分密文写入无法被后续重试替换，重复 onboarding 会改写 vault 元数据。 |
| MIG-015 | PASS（路径契约）/ NOT_RUN（双壳同基线） | 源码路径检查确认传统版与 Neo 默认根目录相同；未用传统 Inno Setup 安装程序与 Neo 对同一份迁移夹具做端到端读取。unpacked Neo 另有独立启动检查，见上文。 |
| MIG-016 | PASS | v0.208 reader 可读取当前 schema；隔离 plain backup 可恢复相同逻辑数据。 |
| MIG-017 | PASS | 全新 profile 创建独立空数据库，不复用旧夹具。 |
| MIG-018 | PASS | SQLite 写事务占用时第二个隔离进程不能破坏数据；释放锁后重试正常。 |

失败断言摘录（路径已省略，全部来自该次临时 checkout）：

- MIG-009：期望 `working_db=false, plain_backup=false`；实际两者均为 `true`。
- MIG-010：解锁 HTTP 200 且 `vault_unlocked=true`，但 `db_restored=false`，记录数变成 folders/videos `0/0`；逻辑摘要从预期的 normal 数据变为空库摘要 `39caac493cb26e311675bbd3315ca3b14f67d8188680622c133d0ff041d873df`。
- MIG-012：恢复码解锁 HTTP 200，实际逻辑摘要同样为空库 SHA `39ca…`，与解锁前 normal 核心摘要 `67595540e7fe246436f9295901c0f6f7762fd0c4e513d3bba2a52099e251a1cf` 不同。
- MIG-013：翻转密文最后一位后实际为 `http_status=200, vault_unlocked=true, db_exists=true, folders=0, videos=0, selftest.ok=true`；期望拒绝解锁，并报告密文损坏。
- MIG-014 重复初始化：两次 onboarding 后 `vault.json` 字节不同。
- MIG-014 中断写入：模拟 `.enc` 部分写后再次加密，重试结果为 `None`，未替换标记内容 `synthetic-partial-encrypted-file`。

完整 suite 本次复验结果为 `Ran 201 tests in 177.224s; FAILED (failures=6)`；更早的两次运行分别为 `211.236s; failures=8, errors=1` 和 `178.810s; failures=6`。最终 6 个失败全部来自上表的保险库/故障用例。首次运行中两个扫描诊断测试失败并有一个错误，但这些用例单独重跑通过，扫描诊断模块也以 18/18 通过；其后完整运行同样全部通过。该首次差异显示既有扫描测试在全量运行下出现过时序/负载敏感性；没有删减断言。

## 故障与安全分析

| 严重程度 | 发现 | 证据与影响 |
| --- | --- | --- |
| 高 | 完成 vault 锁定后仍留下明文 DB 和 plain backup | MIG-009；同一目录下的 Cookie/API Key 相关 provider 字段可能继续明文可读，锁定不代表磁盘数据已加密。 |
| 高 | 加密库缺失时普通启动先创建空 SQLite，解锁路径没有恢复密文 | MIG-010/012；接口仍可能报告 unlocked，用户会看到空库并可能继续写入。源密文未被删除，但日常启动路径没有自动回到它。 |
| 高 | 只校验密文头部，未验证完整 AEAD tag | MIG-013；末尾损坏仍被接受，解锁自检返回成功，空活动库成为可见状态。 |
| 高 | 部分 `.enc` 文件存在时迁移重试被跳过 | MIG-014；故障留下的截断/标记内容阻止重建，重试响应不能作为完整快照已写成的证据。 |
| 高 | 对已有 vault 再次 onboarding 可覆盖 vault 元数据 | MIG-014；旧 DEK 与已有快照可能失去匹配关系。初始化应对已有保险库拒绝或验证后幂等返回。 |
| 中 | 传统版与 Neo 没有共享的跨壳实例互斥 | 源码检查；Neo 使用 Electron 单实例锁，传统版没有同一把锁。SQLite 可串行化部分写入，但不等于应用层跨壳互斥。 |
| 中 | `BILI_FAV_ORGANIZER_DATA_DIR` 只重定向 SQLite | 其他 config/secrets/vault/device 状态仍在 `%LOCALAPPDATA%\BiliFavOrganizer`，可能造成目录拆分和用户误判。 |

### 真实代码调用时序

`server.py` 导入 `store.py` 时会触发 `_initialize()`；该阶段会运行 `_migrate_database_location()` 并创建/升级目标 SQLite。服务加载 vault 状态后才进入 API 解锁/恢复路径。若活动 DB 文件不存在，导入初始化已经可能建立空文件，导致 `_vault_restore_db_if_needed()` 的“DB 不存在才解密”条件不成立。

onboarding 会将 DB 加密到 `library.sqlite3.enc`，但当前实现保留明文工作 DB 与 `.plain-backup`。加密写入直接落到最终路径；发现 `.enc` 已存在时可能跳过重试。`decrypt_head` 只解出文件头用于 SQLite 自检，未验证完整密文认证标签。Electron 退出时关闭后端 stdin 并等待进程退出，没有显式触发最后一次 vault 加密保存。

完整路径图、路径覆盖和调用点分析见 [legacy-migration-test-plan.md](legacy-migration-test-plan.md)。

## Windows 与 Electron 验证边界

- PyInstaller 冻结后端构建成功，且作为 Electron unpacked package 的随包后端成功启动。
- unpacked Electron 主程序可启动；托管 Python 后端优雅退出和端口释放通过。
- 真实 NSIS 安装器没有生成；Electron Builder 的安装器工具下载未在本次时限内完成。传统版 Inno Setup 安装行为未运行。
- Windows DPAPI helper 的现有单元测试通过；Windows Hello 的绑定、系统提示和真实解锁需要交互式测试 profile，本次 **NOT_RUN**。
- 没有对传统版和 Neo 版同时启动同一份数据目录。源码只证明默认路径相同，没有证明跨壳写入互斥。
- unpacked Electron UI smoke 5 项失败，故整体 Electron 视觉/版本验收未通过；不能把后端进程退出通过扩大成完整 Neo 安装验收通过。

## 发布判断与下一步

当前不建议继续发布 `v0.209-beta.1`。在发布前至少需要修复并回归：锁定后的明文处理策略、空 DB 创建与密文恢复顺序、完整 AEAD 验证、加密文件原子写入/截断重试，以及已有 vault 的重复初始化保护。随后在隔离数据上重跑 MIG-009–014，并让 v0.208 reader、传统安装形态和 Neo 安装形态读取相同基线；Windows Hello 与 NSIS/Inno Setup 测试仍需真实 Windows 验收。

本分支没有修改生产迁移/保险库逻辑；本报告记录的缺陷仍待独立修复和回归验证。
