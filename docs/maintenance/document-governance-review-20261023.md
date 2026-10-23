# 文档治理复核记录（2026-10-23）

> 状态：本轮复核记录。本文记录文档之间的一致性修复、保留的历史边界和未完成事项；不代替 CI、Release 或 Windows 实机验收。

## 1. 当前权威来源

| 主题 | 当前权威来源 | 本轮结论 |
| --- | --- | --- |
| 用户入口 / 功能概述 | 根目录 `README.md` | 修正发布工作流描述，使其与当前 YAML 触发条件一致 |
| 在线使用指南 | `docs/快速上手.md` | 顶部已标明 v0.210.5；旧版截图作为历史参考；PDF 尚待重新生成并逐项核对 |
| 发布操作 | `docs/release-process.md` + 实际 workflow YAML | Tag push 会触发 stable / beta 工作流；手动 dispatch 使用所选 ref，不能把输入 tag 当作自动 checkout 的源码 |
| 当前版本事实 | `docs/maintenance/version-ledger.md` + Git tag / Release / Actions | 当前稳定版记为 v0.210.5；历史 Release 页面缺失情况仍需单独核实 |
| 分支治理 | `docs/maintenance/governance.md` | 长期分支目标为仅保留 main；本轮已实时复核远端只剩 main |
| 验收证据 | `docs/testing/evidence/20261022-acceptance-branch-archive.md` + manifest | 19 张合成测试截图已归档；报告保留各自原始 FAIL / BLOCKED 结论 |
| 历史架构 | `docs/project-overview.md` | 保留为 2026-10-09 历史快照，不作为当前架构事实 |
| 可靠性计划 / 任务 | `docs/testing/reliability-maintenance-plan.md`、`reliability-maintenance-task-list.md` | 保留原始执行证据；顶部状态提示已强调历史性，不把旧状态视为当前状态 |
| Pages 看板 | `docs/index.html` | 当前发布后 CI 的 deploy 检查成功；页面中的状态文本仍需单独人工对照权威文档复核 |

## 2. 本轮已修复

1. 根 README 的开发说明此前声称 stable / beta 工作流只手动构建候选、推送 tag 不会自动发布；这与 `.github/workflows/release-stable.yml` 和 `release-beta.yml` 的 tag push 触发器不一致。README 已改为描述当前工作流，并链接当前发布操作流程。
2. 第一批验收报告的归档说明误写为“19 张截图”；已改为该批次的 9 张。第二批是 10 张，合计 19 张。
3. 验收归档说明已将截图状态从“尚未脱敏 / 未归档”改为已原样归档，并记录两条验收分支已删除、远端仅剩 main。没有把归档等同于整体验收 PASS。
4. 分支治理初始盘点与最终处置文件增加了“历史记录 / 最终状态”提示，避免旧表格中“分支尚未删除”的陈述被误读为实时状态。
5. 可靠性维护计划和任务清单保留当时的证据与状态，同时明确它们不是当前分支或版本状态的来源。

## 3. 尚未完成

### P1 — 快速上手 PDF

- 在线指南已标明 v0.210.5，且说明旧截图主要参考 v0.208。
- `output/pdf/快速上手.pdf` 是静态文件，当前文档已明确提示它可能落后。
- 待从最新 Markdown 重新生成 PDF，核对版本、安装步骤、界面截图、链接、许可 / 升级说明，并确认发布工作流打包的是新 PDF。
- 在 PDF 重建和验收完成前，不宣称在线指南与发行包 PDF 一致。

### P1 — Pages Delivery Console

- 最新主干对应的 CI 检查中 `deploy` 成功，但部署成功不等于看板中的每条状态都准确。
- 待人工打开公开 Pages 页面，检查资源加载、链接、移动端布局、当前稳定版、分支数量、PR / CI 状态和验收截图入口是否与权威源一致。
- 看板只做导航 / 汇总，不应覆盖版本台账、发布流程或带 SHA 的验收报告。

### P2 — 历史 Release 页面

- 版本台账记录 v0.210.1–v0.210.4 的 tag 存在，但先前 Releases 列表未返回相应 Release 对象。
- 已分别查询 v0.210.1–v0.210.4 的 Release-by-tag endpoint：均返回 HTTP 404；对应 tag refs 均存在，且解析为台账所列 commit SHA。因此确认这些 tag 存在，但相应 Release 对象当前不存在。
- 未创建历史 Release。是否恢复历史发布页面涉及发行资产、正文与 prerelease 元数据的重建，不应仅凭 tag 存在就自动操作；待维护者决定。

### 仓库保护规则

- 维护者明确说明项目由本人单独维护，不需要额外配置分支保护规则。
- 此项已从当前治理待办中移除；远端仅保留 main 的目标已达成。

## 4. 证据与编辑边界

- 历史报告中的 FAIL、PARTIAL、BLOCKED、NOT_RUN 结果保持原样，不因文档整理而改成 PASS。
- 19 张截图按维护者决定原样归档，因为其业务测试数据是合成数据。截图中的验收环境路径仍可能可见；如用于教程或宣传，应生成独立展示副本，不覆盖原始证据。
- 本轮文档修改不创建新版本 tag、不创建 Release、不更改代码或发布工作流。
- 每项当前状态必须绑定本轮核验来源；旧 SHA 的测试结果不能继承到新提交。

## 5. 收尾验收清单

- [x] 远端仅保留 main
- [x] 19 张验收截图归档，manifest 记录 9 + 10 张
- [x] README 发布触发说明与 workflow YAML 对齐
- [x] 历史维护 / 分支文档标明历史状态
- [ ] 重新生成并验收快速上手 PDF
- [ ] 人工检查公开 Pages 页面内容与移动端布局
- [x] 复核 v0.210.1–v0.210.4 tag refs 存在，四个 Release-by-tag endpoint 均返回 HTTP 404；是否恢复历史 Release 页面待维护者决定
- [x] 仓库保护规则按维护者决定排除在当前治理范围之外
