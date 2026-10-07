# SVG 设计语言

[返回品牌入口](README.md) · v1.0.0

本规范将「书签 B、播放负空间、青绿折叠带」转成可编辑、可生成的几何体系。所有发布 SVG 是纯矢量，不包含位图、外部图片、嵌入字体、脚本、`foreignObject`、滤镜或远程资源。正式标志直接引用交付资产；下列规则用于维护和扩展。

## 1. 两种几何尺度

| 类型 | viewBox | 主要图元 | 描边 |
| --- | --- | --- | --- |
| 标志 | `0 0 512 512` | 三次与二次 Bézier 填充路径 | 无 |
| 功能图标 | `0 0 24 24` | 线条、圆、矩形 | 1.8，圆端、圆连接 |

标志强调连续轮廓和负空间；功能图标强调动作识别。折叠带的曲线可用于启动页等品牌展示，不用作图表刻度或状态含义。

## 2. 标志母版路径

生成源为 [`tools/brand/build.py`](../../tools/brand/build.py)，其中 `OUTLINE`、`PLAY`、`RIBBON`、`UPPER`、`FOLD` 是正式路径，不应手工修改生成的 SVG。

### 外轮廓 OUTLINE

```svg
<path d="M96 48H276C362 48 412 92 412 156C412 190 396 212 374 230C424 252 452 286 452 336C452 414 386 464 290 464H214Q199 464 188 452L128 382L84 428Q64 448 64 420V80Q64 48 96 48Z"/>
```

| 节点 | 坐标 | 含义 |
| --- | --- | --- |
| 左上起点 | `(96,48)` | 从书签带进入上层 B 形 |
| 上部水平终点 | `(276,48)` | 上半叶的顶边 |
| 上半叶右节点 | `(412,156)` | 上半叶曲面过渡 |
| 腰部连接 | `(374,230)` | 上下叶交接与折叠汇合 |
| 下半叶最右节点 | `(452,336)` | 墨迹最右界 |
| 底部节点 | `(290,464)` | 下半叶落点 |
| 折叠竖线下端 | `(188,452)` | 左书签与主体连接 |
| 书签缺口 | `(128,382)` | 收藏语义的关键凹口 |
| 左下过渡 | `(84,428)` | 缺口左侧转向竖边 |
| 左边 | `x=64` | 书签竖直边 |

上、下叶不是两段标准圆弧；下叶更饱满，形成稳定底座。控制点已在路径中给定，不应为了「标准化」替换为对称半圆。

### PLAY 播放孔

```svg
<path d="M244 200Q220 186 220 214V294Q220 322 244 308L318 266Q342 254 318 242Z"/>
```

圆角三角孔中心位于 y=254。其二次曲线控制点是构造参数，实际曲线不会经过所有控制点。不要把 `(342,254)` 当成孔的实际最右边，也不要使用字符 `▶` 替代。该孔在透明标志中是真正的镂空，背景从孔中透出。

### RIBBON 左书签带

```svg
<path d="M96 48C122 76 152 105 188 128V452L128 382L84 428Q64 448 64 420V80Q64 48 96 48Z"/>
```

### UPPER 上层带

```svg
<path d="M96 48H276C362 48 412 92 412 156C412 190 396 212 374 230C298 200 186 146 96 48Z"/>
```

### FOLD 中央折叠面

```svg
<path d="M188 128C247 169 309 204 374 230C410 251 412 274 326 310C278 330 225 353 188 388Z"/>
```

## 3. 图层和镂空

依次绘制：主体 OUTLINE → 左带 RIBBON → 上带 UPPER → 中央 FOLD。渐变、平涂两版共用这四层和同一外轮廓。裁切路径由 `OUTLINE + PLAY` 组成，并同时设置 `fill-rule="evenodd"` 与 `clip-rule="evenodd"`。

```svg
<defs>
  <clipPath id="instance-cut">
    <path fill-rule="evenodd" clip-rule="evenodd"
          d="[OUTLINE] [PLAY]"/>
  </clipPath>
</defs>
<g clip-path="url(#instance-cut)">
  <!-- OUTLINE / RIBBON / UPPER / FOLD -->
</g>
```

上例的方括号是说明占位符，不可直接作为发布 SVG。可运行的完整文件见 [`mark-gradient.svg`](../../static/brand/mark-gradient.svg)。

单色版仅有一条复合路径，`fill-rule="evenodd"` 直接产生播放孔，无须蒙版或白色补丁。Compact 去掉全部层次并复用这条路径，不修改轮廓，因此不会引入第二套图形母版。

应用图标有实色容器。深色应用图标额外以 `#5EEAD4` 绘制 RIBBON、以 `#F0FDFA` 绘制 PLAY，这是固定深色容器的对比度适配，不改变透明标志的镂空规则。

## 4. 渐变参数

所有渐变均使用 `gradientUnits="userSpaceOnUse"`，避免路径包围盒变化导致颜色方向漂移。

| 图层 | 起点 → 终点 | 色标 |
| --- | --- | --- |
| 主体 | `(112,64)` → `(440,448)` | 0% `#0F766E`，52% `#14B8A6`，100% `#2DD4BF` |
| 左带 | `(70,72)` → `(188,436)` | 0% `#14B8A6`，100% `#0F766E` |
| 上带 | `(96,48)` → `(400,224)` | 0% `#0F766E`，100% `#2DD4BF` |
| 中央折面 | `(188,150)` → `(360,308)` | 0% `#5EEAD4`，62% `#14B8A6`，100% `#0F766E` |

平涂版对应填色为主体 Teal、左带 Deep、上带 Teal、折面 Mint。渐变方向随整个标志等比变换；不单独旋转颜色轴。

## 5. 字标与组合

路径字标源：

- [`wordmark-paths.json`](../../tools/brand/wordmark-paths.json)：DejaVu Sans Bold，64 单位 em，保存两个英文单词的路径；两词之间增加 20 单位空隙。总 advance 为 617.90625。
- [`descriptor-paths.json`](../../tools/brand/descriptor-paths.json)：Noto Sans CJK SC Regular，22 单位 em，中文说明逐字 3 单位间距；末尾不增加间距。总 advance 为 214.454。

轮廓已从字体坐标转为 SVG 坐标，基线为 y=0，字面位于基线上方。组合时用 `<g transform="translate(...)"/>` 设定基线；不能在现有路径上再做一次 y 轴翻转。英文和中文都没有 `<text>` 元素，所以应用端不需要安装这些字体。

若更换名称或字形，重新制作轮廓源并记录字体名称与 SHA256，保留字体许可；不能直接往导出 SVG 填一个依赖系统字体的 `<text>` 冒充固定字标。文档示意图的说明文字允许使用 `<text>`，但不属于正式标志组成。

## 6. 功能图标系统

24 单位画布中，常规内容落在 x/y=2–22 的区域；内建的 1.8 单位描边向外扩展约 0.9 单位。默认显示 24 px，紧凑处允许 20 px，16 px 只用于附属信息且必须检查描边清晰度。

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <g fill="none" stroke="currentColor" stroke-width="1.8"
     stroke-linecap="round" stroke-linejoin="round">
    <rect x="5" y="3" width="14" height="18" rx="2"/>
    <path d="m8 12 3 3 5-6"/>
  </g>
</svg>
```

| 文件 / symbol | 动作语义 | 使用边界 |
| --- | --- | --- |
| `scan` / `bfo-scan` | 获取与扫描 | 不表示联网已成功 |
| `folder` / `bfo-folder` | 收藏夹对象 | 不表示某夹已被选择 |
| `profile` / `bfo-profile` | 收藏夹画像 | 文本与主题画像 |
| `classify` / `bfo-classify` | 内容分类、归类建议 | 不表示已经执行 |
| `review` / `bfo-review` | 人工复核 | 搭配「待复核」「确认方案」文字 |
| `apply` / `bfo-apply` | 确认执行 | 不用于预览阶段 |
| `merge` / `bfo-merge` | 收藏夹合并建议 | 写回前仍须复核 |
| `archive` / `bfo-archive` | 本地保存与归档 | 不替代删除图标 |
| `pause` / `bfo-pause` | 暂停或停止推进 | 不表示完成 |
| `warning` / `bfo-warning` | 需人工关注 | 状态还需有文字 |
| `settings` / `bfo-settings` | 参数与模型配置 | 三滑杆形式 |
| `video` / `bfo-video` | 视频内容对象 | 不当成开始任务按钮 |

滑杆图标的圆形手柄使用 `fill="var(--bfo-icon-bg, #fff)"` 遮住后面的线条。深色背景使用 sprite 并设 `--bfo-icon-bg` 为实际表面色；独立 SVG 作为 `<img>` 时不能继承父页面变量，若需深色适配优先内联或 sprite。

添加新图标时保持线宽、圆端、网格、填充方式，不复用渐变，不引入拟物阴影。不要将打勾图标同时用作「已确认方案」与「已成功写回」而不加状态文字。

## 7. 文件和引用约定

- 小写短横线命名：`mark-*`、`lockup-*`、`app-icon-*`。图标文件为动作或对象名。
- 每个独立 SVG 包含 `xmlns`、`viewBox`、`role="img"`、`title`、`desc` 及相应 `aria-labelledby`。
- 同一文档内 `id` 必须唯一。生成文件用各自前缀；将同一个 SVG 重复内联时，为每次实例重新前缀化所有 `id`、`url(#...)`、`aria-labelledby`。
- `<img>` 外链使用原文件即可，不存在跨 SVG 文档 ID 冲突。SVG 内部描述不能替代 HTML `<img alt="...">`。
- 图标与品牌图片相邻已有名称时设装饰性 `alt=""` 或 `aria-hidden="true"`；无文字图标按钮需要可读的 `aria-label`。
- Sprite 是定义容器，本身不提供可读名称；名称由每个 `<svg>` 使用实例负责。仅包含同源引用，不依赖 CDN。

## 8. 输出与兼容

源资产保留可编辑路径、defs 和图层顺序，使用 XML 与 SVG 常见图元；不要为节省几百字节删除必要的描述或负空间规则。字标已经转为轮廓，不再需要文本转曲。

```bash
python tools/brand/build.py --check
# 可选：Inkscape 导出一张 512 px PNG
inkscape static/brand/app-icon-light.svg --export-type=png --export-width=512 --export-filename=app-icon.png
```

PNG / ICO 由 SVG 按目标尺寸导出，不通过位图放大。Windows `.ico` 推荐 16、24、32、48、64、128、256 px 多帧：前三档从 favicon 或 compact 衍生，48 px 及以上从应用图标衍生。v1 仓库交付 SVG 母版，尚未将 ICO 接入打包。

导出后检查播放孔、缺口、曲线交界和中文完整性；对 16、24、32 px 在 100% 显示比例检查。小尺寸可辨认不等于保留全部折叠细节，compact 正是明确移除细节的版本。
