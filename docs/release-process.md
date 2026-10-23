# 发布操作流程

> 本文件是当前发布操作入口。正式版本和 Beta 均由 GitHub Actions 完成测试、Windows 构建、校验文件生成和 Release 资产发布。执行前请先核对 [版本台账](maintenance/version-ledger.md) 和 [维护治理规范](maintenance/governance.md)。

## 自动触发

| 操作 | 工作流 | 构建源码 | 行为 |
| --- | --- | --- | --- |
| 推送稳定版 tag（如 `v0.210.6`） | [Stable Windows tag release](../.github/workflows/release-stable.yml) | 被推送的 tag | 执行测试、构建并发布稳定版 Release |
| 推送 Beta tag（如 `v0.211-beta.1`） | [Beta Windows tag release](../.github/workflows/release-beta.yml) | 被推送的 tag | 执行测试、构建并发布预发布 Release |
| 发布已有 GitHub Release | 对应稳定版 / Beta 工作流 | Release 对应的 tag | 根据发布事件运行工作流并处理发行附件 |

稳定版 tag 格式为 `vX.Y`、`vX.Y.Z`；Beta tag 格式为 `vX.Y-beta.Z`。工作流会拒绝不符合对应格式的版本。**不要为修复发布流程而移动或覆盖已有正式 tag。**

## 发行物与证据

两条发布工作流均构建以下六项发行物：

1. Windows 便携 ZIP 与对应 SHA-256 文件；
2. Inno Setup 安装程序与对应 SHA-256 文件；
3. Electron Neo 安装程序与对应 SHA-256 文件。

便携包还会包含 `packaging/使用说明.txt`、`output/pdf/快速上手.pdf`、许可和声明文件。发布前应确保在线快速上手指南与 PDF 内容一致。可在 GitHub Actions 手动运行 [Rebuild Quickstart PDF](../.github/workflows/rebuild-quickstart-pdf.yml)，从 `main` 的 Markdown 生成 PDF 并创建独立 PR；审阅渲染后的中文字体、截图、分页与链接后再合并。

构建包会作为 GitHub Actions artifact 保留 14 天；Release 工作流还会上传发行附件。工作流的发布步骤允许同名附件被覆盖，因此对已发布版本重跑工作流前，必须明确确认重建的源码和覆盖行为。

## 人工运行 / 修复已有 Release

GitHub → Actions → 对应的稳定版或 Beta 工作流 → Run workflow。手动运行必须选择分支或 ref，并填写版本 tag。

**重要区别：手动运行使用所选分支 / ref 的源码，而不是自动切换到输入的 tag。** 例如选择 `main` 并输入 `v0.210.5`，会用当前 `main` 的代码构建并嵌入该版本号；这不是重建原始 `v0.210.5` tag 的同一源码。只有当明确有意用所选源码重建该版本时，才执行这种操作。自动 tag 触发则使用 tag 指向的源码快照。

命令示例：

```sh
gh workflow run release-stable.yml \
  --repo Martin-soaring-dev/bili-fav-organizer \
  --ref main \
  -f tag=v0.210.5
```

Beta 修复应使用 `release-beta.yml` 并填写符合格式的 Beta tag。不要把稳定版 tag 传给 Beta 工作流，反之亦然。

重新运行旧失败记录会使用旧运行记录绑定的工作流版本；工作流代码修复后，应创建新的手动运行，而不是仅重跑旧记录。

## 发布门禁

- 先核对工作区 / PR 的准确提交 SHA、测试结果和目标源码。
- 确认测试和 Windows 打包均通过；不能用本机 Linux 测试代替 Windows 产品验收。
- 核验三个发行包及其 SHA-256 文件，确保 PDF、README 更新日志和版本台账一致。
- 发布后核对 Git tag、GitHub Release、六个附件、下载链接与实际 SHA-256。
- 在版本台账记录构建运行 ID、源码 SHA、发行物和未解决限制。
- 如果 Release 正文为空，按 [Release notes 补齐工作流](../.github/workflows/backfill-release-notes.yml) 进行补齐；该流程仅在正文为空时写入，不应覆盖已有说明。

## 历史审计

[2026-10-11 发布体系审计](release-audit.md) 是历史快照，不替代本文件。
