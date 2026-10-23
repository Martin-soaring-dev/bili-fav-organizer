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

- `05095b9` 的报告和若干日志包含 `C:\Users\Martin\AppData\Local\Temp\...`；截图 About 面板还显示 `C:\Users\Martin\AppData\Local\BiliFavOrganizer`。
- `0fd6391` 的报告称文本副本已脱敏，但截图 About 面板同样显示用户数据目录，不能把该目录整体视为已脱敏。
- 归档策略：保留可公开的报告 / 日志脱敏副本；未完成视觉脱敏的 PNG 暂不复制到主干，单独列为待处理证据。原始来源分支在可用的分支管理权限完成清理前仍存在于远端。

## 本次治理分支

`governance/branch-docs-cleanup-20261023` 从上述 `main` 基线创建，用于文档导航、治理规则、历史状态标记和安全证据归档。合并与分支清理应在 PR 验收后完成。
