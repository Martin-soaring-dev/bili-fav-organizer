---
feature: desktop-shell
status: in-progress
updated: 2026-10-10
branch: feat/desktop-shell
commits: # 留空
---

# 桌面化迁移（Electron 壳 + 后端重写为 Node/TS）

## Report

## [S1] Problem

1. **现在不是桌面应用**。`BiliFavOrganizer.spec:105` 是 `console=True`，`.bat` 直接跑 exe，启动后由 `server.py:5507` 调 `webbrowser.open` 打开系统浏览器。用户看到的是「黑色控制台窗口 + 浏览器标签页」，没有应用窗口、托盘、单实例，也不会记住窗口位置。
2. **界面停留在旧设计语言**。目标观感已由原型（仓库根 `index.html` / `shell.css` / `shell.js`）对齐：无边框标题栏、Mica/亚克力材质、Snap Layouts、亚克力飞出菜单、深浅双主题。
3. **两条技术栈切断窗口级集成**。Python 后端 + 系统浏览器前端，导致托盘、关闭到托盘、开机自启、原生通知、以及「解锁与窗口生命周期对齐」都做不了。
4. 判定：换成 Electron 做壳，并把后端重写为 Node/TS，最终收敛为**单一 Electron 应用**。

## [S2] Design

### 2.1 终态拓扑

单一 Windows 应用：Electron 主进程负责窗口、托盘、单实例与原生能力；渲染进程加载现有 `static/` 页面；Node/TS 后端作为同一进程树内的 `utilityProcess` 运行。

**HTTP 契约保持不变**：Node 后端复刻同一组 `/api/*` 路由、同样的路径与 JSON 形状。因此前端 12k 行 JS **一行不改**，且双跑对比（Python ↔ Node）成为可行的验收手段。

### 2.2 迁移策略：绞杀者 + 双跑对比

- 开关 `BFO_BACKEND=node|python`，默认 `python`，直到 Node 侧通过验收。
- 替换顺序与理由：`store`（纯数据、无网络，风险最低）→ `vault`（本地加密，有格式兼容硬要求）→ `bili_api`（联网只读为主）→ `llm_analyzer`（联网写，提示词与预算）→ `server`（路由与长任务编排，最后收口）。
- 每步验收 = 双跑对比：同一份 `library.sqlite3`、同一组请求分别打到两个后端，响应归一化后必须一致。写操作路径只用本地假数据验证，不打真实账号。
- 任一阶段都可停：Python 侧始终可用，随时回退。

### 2.3 窗口与壳层契约

| 项 | 契约 |
|---|---|
| 窗口 | `titleBarStyle: 'hidden'`；`backgroundMaterial` 见 2.4 材质映射；`minWidth 940` / `minHeight 640` |
| 标题栏 | 高 32px；`titleBarOverlay` 用**系统原生**按钮（46×32，hover 与 Snap Layouts 由系统提供，不自绘）；拖动区由 preload 注入的 32px 条承担 |
| 托盘 | `Tray`；菜单「显示/隐藏、开机自启、退出」；关闭按钮默认关闭到托盘（首次弹提示） |
| 单实例 | `app.requestSingleInstanceLock()`，二次启动唤起已有窗口 |
| 持久化 | 窗口尺寸与位置写入现有 `config.json`（键 `shell_window`），不新建配置文件 |

壳层注入（`electron/preload.js`）只做两件事：注入 32px 拖动条，以及「页面底色透明 + 内容高度减 32」——**T1 阶段不修改 `static/` 下任何文件**，视觉整合归 T8。

### 2.4 视觉契约（token 取自原型）

- **深色**：底 `#202020`；卡片 `rgba(255,255,255,.045)`；描边 `rgba(255,255,255,.09)`；正文 `#ffffff`；次要 `#d6d6d6`；弱化 `#9c9c9c`。
- **浅色**：底 `#f3f3f3`；卡片 `rgba(255,255,255,.62)`；描边 `rgba(0,0,0,.07)`；正文 `#1b1b1b`；次要 `#3b3b3b`；弱化 `#616161`。
- **字体**：`Segoe UI Variable Text` → `Segoe UI` → `Microsoft YaHei UI`；标题 24/600，正文 14/400，说明 12/400。
- **度量**：窗口圆角 8、卡片 8、控件 4、飞出 8；标题栏 32；图标栏 48，导航列 240（可折叠）；内容内边距 24；控件高 32；列表行 ≥34；开关 40×20；复选框 18。
- **强调色**：默认 `#4cc2ff`，按亮度自动选前景色（原型 `shell.js` 的 `setAccent` 已实现，直接移植）。
- **材质映射**：原型 Mica / Mica Alt / 亚克力 / 纯色 ↔ Electron `backgroundMaterial: 'mica' / 'tabbed' / 'acrylic' / 'none'`。
- 仓库根的原型三文件是**视觉基准**，落地后由正式前端样式取代（原型本身不入库）。

### 2.5 原生能力契约（已由 spike 实测）

| 能力 | 方案 | 实测结果 |
|---|---|---|
| SQLite | `node:sqlite`（Node 内置，无原生编译） | 10683 行单事务写入 24ms；`journal_mode=wal` 生效；`json_extract` 可用 |
| 加密 | `node:crypto` | md5、hmac-sha256、aes-256-gcm 往返通过 |
| DPAPI | 主进程 spawn `powershell -NoProfile -STA -Command`，`windowsHide: true`，调 `crypt32.CryptProtectData/CryptUnprotectData` | 返回 True，236 字节密文 |
| Windows Hello | 同一通道调 WinRT `UserConsentVerifier.RequestVerificationAsync` | `CheckAvailabilityAsync` → `Available` |
| Hello 设备 secret | 独立文件 `device_secret.blob`（DPAPI），与应用密码的 `app_unlock.blob` **分开存放**；旧布局里挤在 `app_unlock.blob` 的随机 secret 迁移过去 | 已修（2026-10-10）：两套材料曾共用一个文件，应用密码的自动解锁会覆盖/删除它，表现为「Hello 验证通过却仍要输密码」。T4 的 Node 侧必须保持这个分离 |
| 本地安全门 | 沿用现有 `/api/*` 的 token 校验与 Host / Origin / Sec-Fetch 门禁 | 契约不变 |
| 就绪探针 | 只能用放行端点 `/api/vault/status`、`/api/version`（名单见 `server.py:409`） | 实测：用 `/api/status` 当探针永远拿到 403（vault 门闩），换放行端点后 200 |

三项原生能力都不需要 node-gyp / VS 构建工具链，electron-builder 链路保持干净。

顺带修掉现有缺陷：`server.py:837` 无 `CREATE_NO_WINDOW`（无控制台环境下 Hello 会闪黑窗）、以及 Python 侧 `timeout=35` 与脚本内 `Wait(45000)` 的超时误杀。Node 侧统一为 `windowsHide: true` + 45s 单一时限。

### 2.6 数据兼容契约

- `%LOCALAPPDATA%\BiliFavOrganizer\data\` 下的 `library.sqlite3`（schema v7）、`vault.json`、`token.blob`、`config.json`、`secrets.json` 必须被 Node 侧直接读写；**不新建平行目录、不改文件名**。
- 老用户首次启动必须能直接解锁并看到既有 10683 条数据，无需重扫。
- **关键交叉验证**：同一份 `vault.json` 在 Python 与 Node 两侧都能解开；DEK 派生、KEK 包装、恢复码格式逐字节一致。

### 2.7 运行与打包

- 开发：`npm run dev` 起 Electron + Node 后端；`BFO_BACKEND=python` 切回旧后端对比。
- 首次装依赖：Electron 二进制默认走 GitHub Releases，本机实测 `fetch failed`；需 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/` 后再跑 `node node_modules/electron/install.js`。
- 后端进程由壳自己拉起：`BFO_PYTHON` 可指定解释器，未指定时依次找应用自带运行时（`%LOCALAPPDATA%\BiliFavOrganizer\runtime\py312`）与 PATH 上的 `python`。
- 打包：electron-builder，目标 `nsis`（安装版）+ `portable`（便携版）；图标复用 `packaging/BiliFavOrganizer.ico`。旧 PyInstaller + Inno 链**保留到 Node 侧验收通过**。
- 体积基线：现 `dist/BiliFavOrganizer` 解包 100.3MB；Electron 便携版目标 < 260MB，实测值记录进 Report。

### 2.8 测试边界

- Node 侧用内置 `node:test`；`tests/` 现有 105 个用例按模块逐个移植，纯逻辑优先（WBI 签名、字段归一化、schema 迁移）。
- 双跑对比脚本 `tools/dual-run.mjs`：只读接口逐条比对；写接口用本地假数据。
- 前端不改，行为覆盖 = Electron 内 Playwright 冒烟 + 人工走查。

## [S3] Out of Scope

- .NET / WinUI 3 路线（已评估：在 Electron 前提下不省成本，且界面仍是 HTML）。
- 把界面从 HTML 重写为 XAML / 原生控件。
- macOS / Linux 分发（本期只保 Windows）。
- 业务行为变更与功能新增——本次是**等价迁移**。
- 收藏数据的重扫或修复（已在本分支基点 `2b678d7` 之前的提交里完成）。

## Tasks

- [x] T1: Electron 壳骨架（无边框 + 系统标题栏 overlay + Mica + 单实例），加载现有前端，后端仍用 Python — acceptance: 起窗口、拖动区带 `-webkit-app-region: drag`、页面底色透明、内容高度 = 视口 − 32、`/api/*` 走通现有 Python 后端、单实例锁生效、窗口尺寸写入 `config.json`；**材质观感归 T8 验收**（页面仍不透明时 Mica 看不见） (covers: S2.1, S2.3)
- [ ] T2: 后端托管化（动态端口回传、就绪握手、优雅退出、日志转发、Hello 黑窗与超时修复） — acceptance: 端口由托管方分配并回传；关闭后进程组干净退出；解锁不出现控制台窗口 (covers: S2.5, S2.7; depends: T1)
- [ ] T3: Node 侧 `store`（SQLite v7 schema、迁移、CRUD） — acceptance: 双跑对比 videos/folders/folder_items/analyses/plans 计数与抽样记录逐条一致 (covers: S2.2, S2.5, S2.6; depends: T2)
- [ ] T4: Node 侧 `vault`（DEK/多 KEK、DPAPI、恢复码） — acceptance: 现有 `vault.json` 在 Node 侧解锁成功，且 Node 产出的 blob 能被 Python 侧解开 (covers: S2.5, S2.6; depends: T2)
- [ ] T5: Node 侧 `bili_api`（WBI 签名、收藏夹读写） — acceptance: 只读接口双跑结果一致；签名实现与 Python 同参同值 (covers: S2.2; depends: T2)
- [ ] T6: Node 侧 `llm_analyzer` 与长任务编排 — acceptance: 同一模型配置下分析/定案/执行的状态机与 Python 一致，两两互斥语义保持 (covers: S2.2; depends: T2)
- [ ] T7: Node 侧 `server` 路由全量对齐 — acceptance: `/api/*` 逐条双跑一致，含 token / origin / vault 门禁 (covers: S2.2, S2.5; depends: T3, T4, T5, T6)
- [ ] T8: 视觉迁移（原型 token 落进前端 + 托盘、开机自启、窗口位置持久化） — acceptance: 深/浅色与四种材质可切换且与原型一致；托盘菜单四项可用 (covers: S2.3, S2.4; depends: T1)
- [ ] T9: 打包切换（electron-builder `nsis` + `portable`） — acceptance: 便携版解包体积记录在案；安装版可启动并复用既有数据目录 (covers: S2.7; depends: T7, T8)
- [ ] T10: 双跑验收与退役 Python — acceptance: 全量只读接口双跑一致、迁移后的 Node 测试全绿，之后移除 Python 依赖与 PyInstaller 链 (covers: S2.2, S2.8; depends: T9)
