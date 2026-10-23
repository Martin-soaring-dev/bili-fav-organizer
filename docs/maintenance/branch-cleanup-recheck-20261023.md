# 分支治理复核补充（2026-10-23）

> 状态：远端只读复核记录；不替代实时分支列表。本文基于 PR #34 合并后的远端数据重新核对，纠正初始盘点中“修复分支已可清理”的过早判断。

## 1. 基线

- PR [#34](https://github.com/Martin-soaring-dev/bili-fav-organizer/pull/34) 已合并，merge commit：`f2a9d13941fdbe6621466385209cc32da4128dd6`。
- 本次核查时 `main` 比较基准 commit：`b7a688671fcb3d455f5fef2f3f31d72a96d508fd`。
- 只读 compare 显示以下分支与当前 `main` 均为 diverged。**不能仅凭历史 PR 记录或“没有新文件路径”推断分支已完全合并。**

## 2. 修复分支：暂缓删除

| 分支 | 相对 main 的独有提交 | 当前可见差异 | 处置 |
| --- | ---: | --- | --- |
| `fix/lock-release-ci-20261022` | 4 | `.github/workflows/ci-beta-package.yml`、`tests/scheduler/test_server_integration.py`、`tests/test_scan_empty_page_continuation.py`、`tests/test_store_and_llm_budget.py` 均有差异 | **暂缓删除**；先逐项核对差异是否已由后续提交等价吸收，并确认相关 PR / CI 证据 |
| `fix/scheduler-recovery-before-dispatch-20261022` | 5 | `core/scheduler/scheduler.py`、`server.py`、两组 scheduler 测试均有差异 | **暂缓删除**；先确认独有逻辑是否已在 main 中等价实现 |
| `fix/sqlite-wal-lifecycle-20261023` | 4 | 数据库生命周期、scheduler store、`server.py`、`store.py` 和迁移/生命周期测试均有差异 | **暂缓删除**；涉及 SQLite 写入生命周期，不可为收敛分支而丢弃潜在修复 |

这些 compare 结果不能单独证明差异一定是尚未合并的有效功能：分支相对旧 merge-base 可能包含被后续实现替代的代码。但在完成逐段代码与测试对照前，也不能安全地删除。下一步应查看每个分支的 commit 列表及关联 PR，确认每个独有行为在 main 的落点和验证证据。

## 3. 验收分支：证据与公开风险仍未闭环

- `acceptance/2026-10-22-0fd6391`：相对 main 有 5 个独有提交；compare 显示新增验收报告、文本日志和 9 张截图路径。
- `acceptance/2026-10-22-05095b9`：相对 main 有 2 个独有提交；compare 显示新增验收报告、日志和 10 张截图路径。
- 主干归档清单仍将这 19 张 PNG 标记为 `WITHHELD_VISUAL_REDACTION_REQUIRED`。它们尚未作为脱敏副本进入 main。

**重要安全边界：**仓库是公开仓库；原始截图仍存在于公开验收分支的历史树中。未复制到 main 并不代表原图不可访问。删除分支也不保证旧 commit / blob 立即不可访问。当前连接没有可用的分支删除或仓库管理设置操作，本轮没有改写共享历史、强推或声称风险已消除。

处理顺序：
1. 从可信的本地原始截图副本逐张检查 19 张图，遮盖用户目录、个人标识及其他不应公开的信息，同时保留可判读的验收信息。
2. 对脱敏后的 PNG 重新计算 SHA-256；记录来源分支、源 commit、原始 Git blob SHA、脱敏动作、脱敏副本 SHA-256、复核结果和归档路径。
3. 在 main 中归档脱敏副本，更新 manifest 与归档说明；检查公开链接和图像内容。
4. 对已公开原始图像的历史可访问性单独评估。若需清除历史对象，按独立安全事件流程处理并先获得明确授权；不要把删除 ref 当作历史清除证明。
5. 在证据归档和暴露风险处置达到明确的维护者标准后，再决定是否删除验收分支。

## 4. 文档与管理操作状态

- `docs/maintenance/branch-cleanup-20261023.md` 是初始盘点和历史执行记录；其中修复分支“可清理”的建议须以上述复核为准。
- PR #34 已合并；治理短分支本身仍待具备相应权限的操作清理。
- `delete_branch_on_merge=false` 的历史记录仍需由仓库设置权限重新核实；当前连接无法读取传统 branch protection 管理端点（此前返回 403），也没有分支删除操作。因此自动删除分支与 `main` 保护规则均不能在此报告为已完成。
- 当前文档目录、版本台账与发布流程是治理和版本事实入口；`docs/project-overview.md`、可靠性维护计划和任务清单均应继续作为历史快照使用，不应把旧分支、旧 SHA 或旧发布阻塞状态当作实时事实。

## 5. 后续执行顺序

1. 为三条 fix 分支分别核对独有提交、关联 PR、main 等价实现与测试证据；逐条作出“可删 / 保留 / 需恢复修复”的结论。
2. 完成 19 张截图的脱敏、SHA-256 清单和公开暴露风险复核。
3. 更新主干的分支治理记录，只有具备明确依据的分支才列为清理对象。
4. 更新过期架构快照与可靠性维护文档的历史状态提示。
5. 核对 README、发布流程、在线快速上手、PDF 指南与 Pages 看板的一致性。
6. 由有权限的仓库管理员核实 `main` 保护与合并后自动删除设置。
