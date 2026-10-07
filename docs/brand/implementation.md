# 应用与开发指南

[返回品牌入口](README.md) · v1.0.0

## 1. 交付边界与接入路径

本次变更在 `docs/brand/` 提供规范、矢量总览与预览，在 `static/brand/` 提供可服务的资产，并在 README 展示品牌、链接规范。正式 Web 界面和 Windows 打包资源继续沿用当前实现。以下代码是可采用的接入示例，未宣称这些界面改动已经完成。

`server.py` 已挂载 `/static`，新增资产以后可直接通过 `/static/brand/...` 访问。无需额外路由，无需增加 npm 或 Python 应用依赖。

| 场景 | 推荐资产 | 说明 |
| --- | --- | --- |
| GitHub README | 双语横式标志 | 留下独立 Markdown 主标题与产品介绍 |
| Web 顶栏 32 px | 平涂标志 + HTML 产品名 | 用文字保证窄屏可读 |
| Web 顶栏 24 px | 单色 sprite 标志 | 继承主题强调色 |
| 启动展示 | 竖式标志 | 深色使用 inverse 组合 |
| 浏览器标签 | `favicon.svg` | 固定浅底与单色轮廓 |
| Windows 快捷方式 | 应用图标衍生的多帧 ICO | 小帧需用 compact；另行接入 `.spec` |
| 发布封面 | `overview.svg` 中的品牌语言 | 用版本号与功能文案组成正式封面 |

## 2. Web 标志与 favicon

```html
<link rel="icon" type="image/svg+xml" href="/static/brand/favicon.svg">
<div class="brand">
  <img src="/static/brand/mark-flat.svg" alt="" width="32" height="32">
  <span>B站收藏夹智能整理</span>
</div>
```

图标与产品名相邻，因此空 alt 避免重复朗读。若整幅组合标志是页面唯一名称，用 `alt="BiliFav Organizer · B站收藏夹智能整理"`。深色背景搭配 HTML 产品名更稳妥；完整英文与中文组合要选择 inverse 版。

## 3. 功能图标与 CSS

```html
<link rel="stylesheet" href="/static/brand/tokens.css">
<button type="button">
  <svg class="bfo-icon" aria-hidden="true">
    <use href="/static/brand/sprite.svg#bfo-review"></use>
  </svg>
  人工复核
</button>
<button type="button" aria-label="配置模型">
  <svg class="bfo-icon" aria-hidden="true">
    <use href="/static/brand/sprite.svg#bfo-settings"></use>
  </svg>
</button>
```

SVG sprite 引用需要通过同源 HTTP 服务访问。`file://` 场景的跨文件 `<use>` 受浏览器限制，本地预览用独立图标内联处理。独立 `<img src="icons/scan.svg">` 的 `currentColor` 属于图像内部，不能继承外部页面颜色；需要跟随主题时使用 sprite 或内联。

所有新增 CSS 变量使用 `--bfo-` 前缀，类使用 `bfo-` 前缀。`tokens.css` 不直接覆盖现有 `--accent`、`--bg` 和 `--text`，可逐步接入。当前应用的主题仍由 `static/style.css` 管理。

```css
/* 未来接入主题时，逐项替换现有语义变量 */
:root[data-theme="light"] {
  --accent: var(--bfo-ui-action);
  --text: var(--bfo-ui-text);
}
.brand-title { font-family: var(--bfo-font-sans); }
.brand-symbol { color: var(--accent); width: 24px; height: 24px; }
```

令牌的 `data-theme` 规则只匹配显式 light / dark。若应用使用跟随系统模式，应沿用当前解析方式将有效主题写入根元素，或补充相应的媒体查询。不能只加载令牌就假定跟随系统已生效。

## 4. 界面结构与状态语义

沿用已有整理流程，用明确的动作与数据状态呈现。品牌色用作当前操作和选择态；success 只表示已知成功，warning 表示需要注意，danger 表示失败或危险操作。

| 阶段 | 图标 | 主要文字 | 交互原则 |
| --- | --- | --- | --- |
| 连接配置 | settings | 配置账户与模型 | 凭据与连接状态分开显示 |
| 获取收藏明细 | scan | 开始扫描 / 继续扫描 | 提供范围、进度、停止入口 |
| 收藏夹画像 | profile | 生成画像 | 缺失、过期、完整分别标记 |
| 内容归类 | classify | 生成归类建议 | 模型输出仍是建议 |
| 预归类方案 | review | 待人工复核 | 查看、修改、跳过；不标「已整理」 |
| 确认执行 | apply | 执行已确认方案 | 独立确认入口，展示计划条数与写回结果 |
| 收藏夹整理 | merge | 合并建议 | 与内容归类分开呈现，但共用复核规则 |

| 状态 | 颜色 / 图形 | 文字示例 |
| --- | --- | --- |
| 待处理 | 次要文字 + 中性标记 | 未开始 |
| 进行中 | action + 进度 | 正在扫描 120 / 500 |
| 待复核 | warning + review | 归类方案待复核 |
| 成功 | success + 结果说明 | 已成功写回 24 条 |
| 明确失败 | danger + 错误说明 | 写回失败，可查看详情 |
| 结果不确定 | warning + warning | 结果待核实，已停止推进 |
| 停止 / 暂停 | 次要文字 + pause | 已停止，可从断点继续 |

颜色旁边始终提供文字；不要仅凭红绿区分状态，不确定结果不能显示绿色完成态。预览中的「待人工复核」是视觉示例，不执行任何真实任务。

## 5. 页面排版建议

保持「连接与模式 → 数据获取 → 画像 / 归类 → 待复核方案 → 执行结果」的顺序。复杂参数按任务区块渐进展示，日志保留等宽字体。品牌图形用于顶栏或空白状态，一屏避免重复大标志。

窄屏布局中标志保留 24–32 px，产品名允许换行；版本号与更新入口可进入次级区域。按钮建议最小高度 36 px；仅图标的按钮建议 40 × 40 px，不能依赖悬停才知道功能。表格中的 ID、计数可用等宽数字，正文用系统无衬线字体。

键盘 focus 使用至少 2 px 清晰轮廓和 2 px 间距，不只改变品牌颜色。加载时显示进度文本与可停止入口；120–240 ms 过渡不用于掩盖任务等待。启用减少动态效果时取消装饰动画。

## 6. 本地预览

直接打开 [`preview.html`](preview.html)，可查看深浅主题、标志和业务图标、16–128 px 尺寸对比、横竖组合及待复核方案示例。页面只读取仓库中的资源，主题切换不修改应用配置，无网络请求、登录或真实业务操作。

若要验证 sprite 或正式 HTTP 访问，可以从仓库根运行：

```bash
python -m http.server 8081 --bind 127.0.0.1
# 打开 http://127.0.0.1:8081/docs/brand/preview.html
```

GitHub 不会把仓库里的 HTML 直接渲染成网页；在线审阅使用 Markdown 规范与 `overview.svg`。

## 7. Windows 导出建议

维护 SVG 作为母版；ICO 不作为设计源。推荐的多帧尺寸：16、24、32、48、64、128、256 px。前三档由 favicon / compact 导出，其余由浅色应用图标导出；如需深色桌面版本则另行提供独立文件。

导出 ICO 后检查每帧实际尺寸与小尺寸清晰度，再配置 PyInstaller `icon` 或 `--icon`；包含所有帧的 ICO 不能只用一张 256 px 位图机械缩小。v1 尚未交付 ICO / PNG 发布包，也未修改 `.spec`，这项不与品牌源资产混淆。

## 8. 维护与验收

维护时先改 `tools/brand/build.py` 或轮廓 JSON，再运行生成器。不要同时修改生成源和若干独立 SVG 而忘记其余衍生版本。

```bash
python tools/brand/build.py
python tools/brand/build.py --check
git diff --check
```

| 验收项 | 方法 |
| --- | --- |
| 源与资产一致 | 生成器 `--check` |
| SVG 有效 | XML 解析；检查 viewBox、ID、clipPath 和内部引用 |
| 完整矢量 | 检查无 image / script / foreignObject / 外部资源 |
| 文档可导航 | 检查 Markdown 的本地文件链接存在 |
| 主标志与组合 | 渲染 SVG，检查曲线交界、中文、字标边界 |
| 小尺寸 | 在 16、24、32 px 检查 compact、favicon；48–128 px 检查渐变版 |
| 深浅背景 | 对比预览；检查反白文字与深色应用图标 |
| 主题语义 | 核对 `tokens.json` / CSS 与本规范的颜色值 |
| 字体声明 | 保留两份字体 notice，不引入字体运行依赖 |

这套资产修改不影响归类、数据库、账户读写和任务调度。后续真正接入 UI 或打包时，应另行验证静态资源路由、键盘访问、主题切换和打包后图标显示。
