# BiliFav Organizer 版本台账

> 最近核对：2026-10-23（UTC+8）
> 当前稳定版：**v0.210.5**
> 本文记录 Git 标签、GitHub Release 对象、构建证据与文档一致性；它不替代用户可见的 [README 更新日志](../../README.md#更新日志)。

## 1. 版本事实与证据

| 版本 | Git tag | GitHub Release 当前可见性 | 源码 / 构建证据 | 变更摘要 |
| --- | --- | --- | --- | --- |
| `v0.210.5` | 存在 | **正式稳定版；Release 对象存在** | tag SHA `41d10d05084cb305b49d0abb5b45ef67774b5dbb`；[稳定版构建 #39841350498](https://github.com/Martin-soaring-dev/bili-fav-organizer/actions/runs/39841350498) 成功 | 锁定前挂起任务；修复获取更新时外链无法打开；调度分发前恢复未完成的远端操作；修复 SQLite WAL sidecar 生命周期导致的只读写入问题 |
| `v0.210.4` | 存在 | 当前 Releases 列表未返回对应记录 | tag SHA `d2eb46facd75ec865460e171bf7d8dfdedf398d2` | 扫描缺失详情提示、安全重试、阻止缺失明细状态下的自动清理 |
| `v0.210.3` | 存在 | 当前 Releases 列表未返回对应记录 | tag SHA `44fc5c5135d60c8215e216e40fb69cad115c62b9` | 修复设置保存/重载的 UI 回归测试夹具；应用功能与 v0.210.2 相同 |
| `v0.210.2` | 存在 | 当前 Releases 列表未返回对应记录 | tag SHA `70a3064a61bb24c8f0ba8817058a3456bda83403` | 恢复 B 站收藏夹批量资源读取；改善高级任务参数校验、载入与并发保存 |
| `v0.210.1` | 存在 | 当前 Releases 列表未返回对应记录 | tag SHA `83e48d193ccb19ce6dcb0d7654fcfb2a6fcec2de` | 扫描异常只读诊断、设置参数、Electron 外链与分页空页恢复修复 |
| `v0.210.0` | 存在 | **正式稳定版；Release 对象存在** | Release 对象含 6 个 Windows 发行资产 | 持久化任务调度、恢复与取消可靠性、数据库迁移和生命周期校验加固 |
| `v0.209` | 存在 | **正式稳定版；Release 对象存在** | Release 对象含 6 个 Windows 发行资产 | 保险库可靠性、旧版数据迁移验证、传统 Windows 与 Electron Neo 交付 |

**可见性说明：** 这里的“当前 Releases 列表未返回”只描述本次查询到的 GitHub 状态，不推断这些 Release 对象为何缺失，也不代表对应 Git tag 被删除。历史 tag 均保留；在确认维护者意图前，不重建历史 Release、不重写 tag、不重新上传或覆盖历史附件。

## 2. v0.210.5 发布验收

- Release：[v0.210.5](https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/tag/v0.210.5)
- 发布状态：`draft=false`、`prerelease=false`，属于正式稳定版。
- 稳定版工作流：运行 #39841350498，结论 `success`。
- 六个附件已存在：
  - `BiliFavOrganizer-Windows-x64-v0.210.5.zip` 与 `.sha256`
  - `BiliFavOrganizer-Setup-v0.210.5.exe` 与 `.sha256`
  - `BiliFavOrganizer-neo-Setup-0.210.5.exe` 与 `.sha256`
- **待修复：Release 正文为空。** README 已有本版变更说明，但发布页面没有显示这些说明；需要安全地补齐正文，同时保留已发布资产与历史记录。
- 本次构建在标签对应的源码上执行了 Windows 全量回归测试，测试步骤通过；不把这项 CI 结果等同于用户真实 Windows 环境的安装/升级实机验收。

## 3. 已识别的版本信息不一致

| 项目 | 核查结果 | 本轮处理 / 后续动作 |
| --- | --- | --- |
| README 版本历史 | 原来缺少 `v0.210.2`–`v0.210.4`；“开发中”段落包含已发布内容 | 已补齐版本条目，删除重复的旧“开发中”段落；历史 tag 链接到对应提交 |
| README 当前版本介绍 | 仍把 v0.209 的界面当作最新 | 已更新为 v0.210.5 |
| 在线快速上手 | 顶部仍称 v0.209 为当前正式版 | 已更新为 v0.210.5；旧截图明确标为历史参考 |
| Electron package metadata | `package.json` 为 `0.209.0`，`package-lock.json` 为 `0.0.0` | 已对齐到 `0.210.5`；发行工作流仍以 tag 注入最终包版本 |
| 快速上手 PDF | `output/pdf/快速上手.pdf` 是静态文件，可能落后于 Markdown | **待处理：** 重新生成 PDF，并在发布前核对 PDF 与在线指南一致 |
| Release 正文 | v0.210.5 Release body 为空 | **待处理：** 通过受控流程只填充空正文，不覆盖已有人工说明 |
| 历史 Release 对象 | v0.210.1–v0.210.4 标签存在，但当前列表未返回相应 Release | **待维护者确认：** 决定是否恢复这些历史 Release 页面；不改历史 tag、不替换资产 |

## 4. 后续版本治理规则

1. **Tag 是不可变的源码锚点。** 已发布版本的 tag 不移动、不重打；任何修复都进入新版本。
2. **长期分支只保留 `main`。** 如需短期分支，合并验收后清理；不把分支名称当作版本号。
3. **README 更新日志是版本说明的源文本。** 每个准备发布的 tag 必须有唯一标题、紧随其后的加粗摘要，以及完整的变更列表。
4. **版本号必须可追溯。** 发布 tag、构建注入的后端版本、Electron 包版本、资产文件名与 Release 页面标题应一致。
5. **稳定版与预发布版明确区分。** 稳定版 `vX.Y.Z`；预发布版使用仓库既有的 `vX.Y-beta.N` 形式，并正确设置 GitHub Release 的 prerelease 标志。
6. **发布流水线必须先验证、后打包、再发布。** Windows 全量测试失败时禁止生成可发布资产；所有三个安装/分发形态均应生成 SHA-256 sidecar。
7. **不覆盖人工说明。** 发布自动化只能在 Release 正文为空时补齐 README 对应版本段；若正文已有内容则原样保留。
8. **发布后做资产对账。** 核对 Release 状态、源 tag SHA、构建运行、六个资产名称/大小/hash，以及 README / 在线指南 / PDF 的版本标注。
9. **历史清理必须有证据。** 不因文件老旧、版本记录不完整或测试夹具数量多就直接删除；先查引用、tag、release、workflow 与恢复用途。

## 5. 建议执行顺序

1. 完成本台账与 README/在线指南版本信息同步（本轮已开始）。
2. 补齐 v0.210.5 Release 正文；修复发布自动化，使后续版本只在正文为空时补充说明。
3. 查清 v0.210.1–v0.210.4 Release 对象缺失的原因，再决定是否恢复历史页面。
4. 重新生成并验收 `output/pdf/快速上手.pdf`。
5. 审核 `docs/release-audit.md` 等历史审计文件，明确“历史快照 / 当前有效结论”，不把过期描述当作现状。
6. 从下一个版本起按同一清单执行发布验收。
