# 分支治理初始盘点（2026-10-23）

> 类型：历史盘点 / 执行记录。该列表是核查时的快照，不是实时分支查询。

基线 `main`：`971cde4df684101a7100ffffb3bab430ae4f29dd`。

| 核查时分支 | 核查时 HEAD | 处置建议 | 当前执行说明 |
| --- | --- | --- | --- |
| `main` | `971cde4df684101a7100ffffb3bab430ae4f29dd` | 保留 | 唯一长期分支 |
| `fix/lock-release-ci-20261022` | `7b8baf357cdeffd7d99b16305843a94fd779c045` | 合并后清理 | 对应 PR 已合并；清理操作仍待有权限的 GitHub 操作完成 |
| `fix/scheduler-recovery-before-dispatch-20261022` | `fe606dd8e0cafa65f2107523b4eda919dbbe7275` | 合并后清理 | 对应 PR 已合并；清理操作仍待有权限的 GitHub 操作完成 |
| `fix/sqlite-wal-lifecycle-20261023` | `fd171fd8463ab69e87d4291e61bbc7e76ceda6fb` | 合并后清理 | 对应 PR 已合并；清理操作仍待有权限的 GitHub 操作完成 |
| `acceptance/2026-10-22-05095b9` | `cb5faa3fe9db79040a3230ecac709255ed6d5809` | 证据脱敏、归档后清理 | 发现验收报告、日志和截图含本地用户目录路径 |
| `acceptance/2026-10-22-0fd6391` | `89f1fc8bf3ac6ae7cdda88fdc70db69293a2bafc` | 证据复核、归档后清理 | 文本材料路径检查未发现真实用户名路径；截图仍显示用户数据目录，须脱敏 |

## 已确认事实

- 默认分支为 `main`。
- 核查时无开放 PR；三条修复分支对应的 PR 已合并。
- 核查时 GitHub 分支列表未标记任何分支受保护。
- 仓库设置显示 `delete_branch_on_merge=false`。
- 当前连接可读取公开分支与仓库内容，但不能读取所需的分支保护管理端点，也没有直接删除分支的可用操作。因此本轮不能声称已经配置保护或删除分支。

## 验收证据风险

- `05095b9` 的报告和若干日志包含 `<USER_HOME>\AppData\Local\Temp\...`；截图 About 面板还显示 `<USER_HOME>\AppData\Local\BiliFavOrganizer`。
- `0fd6391` 的报告称文本副本已脱敏，但截图 About 面板同样显示用户数据目录，不能把该目录整体视为已脱敏。
- 归档策略：保留可公开的报告 / 日志脱敏副本；未完成视觉脱敏的 PNG 暂不复制到主干，单独列为待处理证据。原始来源分支在可用的分支管理权限完成清理前仍存在于远端。

## 本次治理分支

`governance/branch-docs-cleanup-20261023` 从上述 `main` 基线创建，用于文档导航、治理规则、历史状态标记和安全证据归档。合并与分支清理应在 PR 验收后完成。


## 执行跟进（2026-10-23，PR #34 合并后）

> 本节是补充执行记录；下表以本次远端核查结果为准。远端删除操作尚未执行。

- 当前 `main` HEAD：`f2a9d13941fdbe6621466385209cc32da4128dd6`（PR [#34](https://github.com/Martin-soaring-dev/bili-fav-organizer/pull/34) 已合并）。合并前 PR CI [#39877437950](https://github.com/Martin-soaring-dev/bili-fav-organizer/actions/runs/39877437950) 为 success；合并后主干 CI [#39883952784](https://github.com/Martin-soaring-dev/bili-fav-organizer/actions/runs/39883952784) 在本次核查时仍为 in_progress。Pages 部署 [#39883952047](https://github.com/Martin-soaring-dev/bili-fav-organizer/actions/runs/39883952047) 已 success。
- 仓库是 **public**，`delete_branch_on_merge=false`。当前 rulesets API 返回空列表；读取 `main` branch protection endpoint 返回 403（Resource not accessible by integration），因此**无法确认传统 branch protection 的实际配置**，不得把“不可读取”解释为“没有保护”。

| 远端分支 | 本次核查 | 处置 |
| --- | --- | --- |
| `main` | HEAD `f2a9d13941fdbe6621466385209cc32da4128dd6` | 保留，唯一长期分支 |
| `docs/mark-v0210-governance-historical-20261023` | PR #34 已合并；分支 HEAD 仍为合并前提交 | 可清理；当前连接没有删除分支操作，尚未删除 |
| `fix/lock-release-ci-20261022` | 无 main 缺失的独有文件路径；分支中的核心文件是较早版本。历史记录显示对应修复 PR 已合并 | 作为过期修复分支的清理候选；尚未删除 |
| `fix/scheduler-recovery-before-dispatch-20261022` | 无 main 缺失的独有文件路径；分支中的核心文件是较早版本。历史记录显示对应修复 PR 已合并 | 作为过期修复分支的清理候选；尚未删除 |
| `fix/sqlite-wal-lifecycle-20261023` | 无 main 缺失的独有文件路径；分支中的核心文件是较早版本。历史记录显示对应修复 PR 已合并 | 作为过期修复分支的清理候选；尚未删除 |
| `governance/branch-docs-cleanup-20261023` | 当前路径集合与 main 相同；治理文档状态落后于 PR #34 | 作为已完成治理分支的清理候选；尚未删除 |
| `acceptance/2026-10-22-0fd6391` | 有 9 个 PNG 路径尚未进入 main；清单均标记 `WITHHELD_VISUAL_REDACTION_REQUIRED` | 证据仍未完成视觉脱敏；不应当作已安全归档 |
| `acceptance/2026-10-22-05095b9` | 有 10 个 PNG 路径尚未进入 main；清单均标记 `WITHHELD_VISUAL_REDACTION_REQUIRED` | 证据仍未完成视觉脱敏；不应当作已安全归档 |

### 公开截图风险说明

两条验收分支仍然存在于公开仓库，且各自 HEAD 的树中仍包含上述 PNG。因此“未复制到 main / withheld”**不等于图片私密或已从公开访问中移除**。目前报告已记录 About 面板显示本机数据目录；在视觉复核前，不应把这些截图视为已通过公开审核。当前 GitHub 连接没有删除分支或管理保护规则的可用操作，本次未尝试 force-push、改写分支历史或改变仓库可见性。

### 管理权限待办

1. 使用具备仓库管理权限的 GitHub 操作删除五条已合并 / 已过期的非验收短分支，以及在完成证据风险处置后删除两条验收分支。
2. 在 GitHub 仓库设置中启用合并后自动删除分支，并确认 `main` 保护规则：要求 PR、要求 CI、禁止 force-push、禁止删除。
3. 如果要求从历史 commit URL 中也彻底移除未脱敏图片，需要单独评估历史对象清除方案；仅删除分支或从当前树移除文件不能保证旧 commit 中的二进制对象立即不可访问。不得在未获授权时重写共享历史。
