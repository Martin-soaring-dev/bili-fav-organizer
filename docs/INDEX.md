# 文档目录与权威来源

> 最近核对：2026-10-23（UTC+8）。默认分支：`main`。本文是导航，不代替各专题文档。

## 首选入口

| 主题 | 权威来源 | 说明 |
| --- | --- | --- |
| 用户安装与使用 | [快速上手](快速上手.md)、[根 README](../README.md) | 面向用户；发行包内还包含 `packaging/使用说明.txt` 和 PDF 指南；PDF 可通过 Actions 的 Rebuild Quickstart PDF 工作流重建 |
| 当前版本事实 | [版本台账](maintenance/version-ledger.md) | Git tag、Release 对象、构建运行和发行物核验 |
| 发布操作 | [发布操作流程](release-process.md) | 稳定版 / Beta 触发、手动重建、发行物和发布后核验 |
| 分支与文档治理 | [维护治理规范](maintenance/governance.md) | 长期分支规则、PR 门禁、证据归档、文档生命周期与隐私 |
| 历史交付快照 | [Delivery Console 历史快照](index.html) | 数据截至 2026-10-21，含 v0.210.0 发布前状态；非实时看板，不用于当前版本 / 发布决策 |
| 技术架构 | [项目全景历史快照](project-overview.md) | 原始整理日期 2026-10-09；仅作历史参考，不是当前代码事实 |
| 发布审计 | [发布体系审计](release-audit.md) | 2026-10-11 历史审计；当前操作以发布流程为准 |

## 设计与接口

- [品牌与视觉规范](brand/README.md)
- [扫描策略与 SQLite 迁移方案](design/scan-sqlite-migration-plan.md)
- [数据可靠性与任务调度重构方案](design/data-reliability-task-scheduler-refactor-plan.md)
- [持久索引与文件夹画像](design/persistent-index-folder-profiles.md)
- [供应商 API 兼容性](design/provider-api-compatibility.md)
- [B 站接口规范与项目验证记录](README.md)：此文件名是既有接口规范入口，不是全站文档目录。

## 测试、验收与维护记录

- [可靠性维护计划（历史快照）](testing/reliability-maintenance-plan.md)
- [可靠性维护任务清单（历史执行档案）](testing/reliability-maintenance-task-list.md)
- [验收证据库](testing/evidence/)
- [2026-10-22 验收分支归档说明](testing/evidence/20261022-acceptance-branch-archive.md)
- [2026-10-23 分支治理复核补充](maintenance/branch-cleanup-recheck-20261023.md)：修复分支清理前的差异核对、验收截图公开风险与管理权限待办
- [验收分支最终处置记录](maintenance/acceptance-branch-final-disposition-20261023.md)：记录 19 张截图归档及两条验收分支删除结果
- [文档治理复核记录](maintenance/document-governance-review-20261023.md)：记录 README / 发布流程一致性修复、历史文档状态和 PDF / Pages 等剩余工作

### 如何判断文档是否有效

1. **当前规则**：以维护治理规范和发布操作流程为准。
2. **当前版本事实**：以版本台账、Git tag、GitHub Release 和相应 Actions 运行互相核验。
3. **历史计划 / 审计**：只说明某一时间点的认知和结果，不自动代表当前状态。
4. **测试结论**：必须绑定被测提交 SHA。旧提交的 PASS、FAIL 或 BLOCKED 均不得转记到新提交。
5. **公开验收证据**：先检查绝对用户路径、凭据、Cookie、个人数据和截图隐私；只有完成脱敏复核后才进入主干公开归档。
