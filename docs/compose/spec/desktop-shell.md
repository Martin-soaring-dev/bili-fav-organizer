---
feature: desktop-shell
status: in-progress
updated: 2026-10-11
branch: main
release_preparation: release/v0.209-beta.1
commits: merged via PR #11
---

# 桌面化迁移（Electron 壳 + 后端重写为 Node/TS）

## Report

### neo 打包线起步（2026-10-11）

在 T9 之前先打通一条「能打能跑」的 Electron 打包线，便于试用与验收；两条线并存：

| 线 | 产物 | 形态 |
|---|---|---|
| 老线（不变） | `BiliFavOrganizer-Windows-x64-<版本>.zip` + `BiliFavOrganizer-Setup-<版本>.exe` | 控制台 + 浏览器（PyInstaller + Inno） |
| neo（新） | `BiliFavOrganizer-neo-Setup-<版本>.exe` | Electron 壳（electron-builder + NSIS） |

- 配置：`electron-builder.yml`（`appId`/NSIS/`artifactName: BiliFavOrganizer-neo-Setup-${version}`）；
  `npmRebuild: false`（node-pty 1.1.0 自带与 Electron 44/ABI 149 匹配的预编译产物，
  不必为它拉 VS 工具链；`asarUnpack` 解开 `node-pty/prebuilds/**`）。
- **后端随包走（过渡形态）**：`extraResources` 把 PyInstaller 冻结出的后端整目录放进
  `resources/backend`，壳启动时优先拉起它（`resolveFrozenBackend()`，dev 下可用 `BFO_BACKEND_EXE`
  显式指向）。因此打包后的 App 不再依赖仓库源码树与系统 Python；壳与后端的契约（`--port 0` +
  `BFO_READY` 握手、stdin 看门狗、优雅退出）完全沿用 T2，未改一行协议。
- 实测（`dist/electron/win-unpacked` 隔离数据目录启动）：`isPackaged: true`、
  `resources/backend/BiliFavOrganizer.exe` 在位并被拉起、页面来自冻结后端、
  包内 `require('node-pty')` 可用（`spawn/fork/createTerminal/open/native`）、preload 桥正常。
- **体积**：安装包 **148.3MB**，解包 **483.9MB** —— 远超 S2.7 里写的「便携版 < 260MB」目标，
  因为同时背着 Electron 本体与冻结的 Python 后端。**T9 需要一次专门的体积治理**
  （Electron locales/无用资源、Node 后端替换 Python 后端后可省掉约 100MB）。
- 顺手修掉一个只在冻结后端才暴露的问题：PyInstaller 产物在中文 Windows 上即使给了
  `PYTHONIOENCODING` 也按 ANSI 代码页写 stderr，而壳按 UTF-8 读 → `shell.log` 中文乱码。
  现在 `server.py` 在**输出被重定向时**（= 被壳托管）把 stdout/stderr 换成 UTF-8
  （跑真实控制台时保持系统代码页，老便携版的控制台中文不受影响）；壳侧同时改用
  `StringDecoder` 拼接，避免多字节字符被 chunk 切断。
- **尚未做（T9 范围）**：更新器重构（现在这套会去杀后端 pid、用旧 exe 名与 `--port` 重启，
  在 Electron 下不成立）、neo 的便携形态、CI 里加 neo 线、体积治理、签名。

### 发布体系审计（2026-10-11）

对构建/打包与运行时自更新链路做了一次只读审计，全文见 `docs/release-audit.md`。要点：

- **三处现成可修**（均已复核）：安装版 helper 的 `param()` 缺 `$WorkDir`、`Popen` 也没传，
  内部两处清理对 `$null` 操作被 `catch {}` 吞掉 → 每次安装版更新在 `%TEMP%\.bfo-setup-*` 残留约 25MB；
  清道夫无条件清理 `{安装目录名}.previous-*` → 若更新在「旧目录改名」与「stage 上位」之间崩溃，
  回滚的唯一旧副本会在 2 小时后被当残留删掉；「应用更新」页的 `#about-latest` 是死元素（永远「尚未检查」）。
- **Electron 共存三处必须重构**（已写进 T9 验收）：更新助手重启的是后端 pid/旧 exe 而非壳自身；
  助手传 `--port` 而 Electron 只认 `BFO_PORT`，于是壳会起一个随机新端口、页面停在旧 URL；
  便携分支「单进程 + 固定端口 + 覆盖后重启同一 exe」的模型与壳的托管模型不兼容。
- **卫生问题**：CI 不跑测试且无 `pull_request` 触发（已知 `test_vault_api.py` 有 2 个红灯，加门禁前要先处理）；
  发布物完全不签名；`.sha256` 与载荷同源、无独立信任根；`install_mode` 只看注册表，
  导致「装过安装版的机器上再跑便携 exe」会去更新注册表里记录的那个目录。
- 版本链本身是一致且受门禁保护的（tag 格式 + README 更新日志段落必须存在）。

### T2 后端托管化（2026-10-11）

壳不再假设端口，也不再只杀直接子进程。

| 项 | 落地方式 | 实测 |
|---|---|---|
| 动态端口 | 壳传 `--port 0` 让系统分配；后端起监听后往 stdout 打一行 `BFO_READY {"port":…,"pid":…}`，壳解析这一行拿真实端口（不存在「先猜端口再启动」的竞态）。设 `BFO_PORT` 时退回固定端口，供 dev 与旧后端双跑对比 | 连续三次启动分别拿到 43234 / 45509 / 26833，不再是 8080 |
| 就绪握手 | 那一行本身就是就绪信号：socket 已在收连接，而 token/vault 初始化发生在 `uvicorn.run()` 之前，必然已完成。壳随后再探放行端点 `/api/vault/status` 兜底 | 探针 200；冒烟断言「页面来自动态端口」通过 |
| 优雅退出 | 关 stdin → 后端的父进程看门狗收到 EOF → 置 `_SHUTDOWN`（先断 SSE 长连接）→ 请 uvicorn 退出；等 7s 不退才 `taskkill /T /F` 整树强杀 | 499ms、exit code 0、走优雅分支、端口已释放 |
| 不留孤儿 | 同一个看门狗：壳被强杀（`taskkill /F` 不带 `/T`）后后端自行退出，`server.log` 留「托管方已退出，后端自行关闭」 | 实测 1.1s 自退，端口释放 |
| 日志转发 | 后端 stdout/stderr 按行转发，与壳日志一起写 `%LOCALAPPDATA%\BiliFavOrganizer\shell.log`（同步追加，退出不丢最后几行） | shell.log 可见 `[py]`/`[py!]` 全量启动与关闭日志 |
| Hello 黑窗 | `subprocess.run(powershell …)` 补 `CREATE_NO_WINDOW`（Hello 与「打开数据目录」两处） | pythonw（无控制台父进程）差分实验：默认参数弹出**可见**的 `CASCADIA_HOSTING_WINDOW_CLASS` 窗口（连续 26 次采样可见），加 flag 后新窗口数为 0 |
| 超时口径 | Python 侧 35s → 50s，让脚本自己的 `Wait(45000)` 成为唯一时限（原 35s 会先杀掉脚本，把「用户还在看 Hello 对话框」误判成取消） | 45s < 50s，脚本内时限胜出 |

顺带修掉 / 发现的问题：

- `tools/shell-smoke.mjs` 的断言停留在 T1 的 32px 空拖动带，而标题栏合并（`10e8cb7`）按用户要求去掉了空带、让顶栏本身充当标题栏，因此这些断言必然失败（5 条）。已改为按当前设计断言：无空拖动带、顶栏即 drag 区域、给系统按钮让位 138px、页面半透明、内容满高；并新增「页面来自动态端口」。现 12/12 通过。
- 新增 `tools/shell-quit.mjs`：从主进程 inspector 触发 `app.quit()`，断言「优雅退出而非强杀 + 端口释放 + 无孤儿 + 壳记录退出」。
- 退出顺序有个坑：收摊时先 `win.destroy()` 会触发 `window-all-closed → app.quit()` 再入，若这一轮没被拦下就会打断收摊并留下孤儿后端（实测复现：壳提前死、日志没落盘、后端卡住）。现改为收摊期间 `before-quit` 一律 `preventDefault`，真正的退出只走收摊末尾的 `app.exit(0)`；`window-all-closed` 在收摊期间也不再触发退出。
- 退出慢的根因不在壳：SSE（`/api/events/stream`）会一直挂着，客户端断开它也不返回，uvicorn 只能等满 `timeout_graceful_shutdown`（5s）再强拆并抛 `CancelledError`。补上自更新路径早就有的 `_SHUTDOWN.set()` 后，7412ms → 499ms，traceback 消失。
- **既有失败，与本次改动无关**：`tests/test_vault_api.py` 两个解锁用例在改动前的 `HEAD`（`10e8cb7`）上同样失败（用干净 worktree 复现）。原因是解锁自检（`8ab48d5` 新增）要求解密结果是 SQLite 库头，而 fixture 只换了临时 `VAULT_DB_ENC` 路径、没先生成真实的加密库，于是返回 500「解密自检未通过：解密结果不是 SQLite 库头」。修法是在 fixture 里走一次真实加密落盘。

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
- [x] T2: 后端托管化（动态端口回传、就绪握手、优雅退出、日志转发、Hello 黑窗与超时修复） — acceptance: 端口由托管方分配并回传（实测 43234/45509/26833）；关闭后进程组干净退出（499ms、code 0、端口释放、无孤儿）；解锁不出现控制台窗口（pythonw 差分实验：默认弹可见控制台窗口，加 `CREATE_NO_WINDOW` 后为 0） (covers: S2.5, S2.7; depends: T1)
- [ ] T3: Node 侧 `store`（SQLite v7 schema、迁移、CRUD） — acceptance: 双跑对比 videos/folders/folder_items/analyses/plans 计数与抽样记录逐条一致 (covers: S2.2, S2.5, S2.6; depends: T2)
- [ ] T4: Node 侧 `vault`（DEK/多 KEK、DPAPI、恢复码） — acceptance: 现有 `vault.json` 在 Node 侧解锁成功，且 Node 产出的 blob 能被 Python 侧解开 (covers: S2.5, S2.6; depends: T2)
- [ ] T5: Node 侧 `bili_api`（WBI 签名、收藏夹读写） — acceptance: 只读接口双跑结果一致；签名实现与 Python 同参同值 (covers: S2.2; depends: T2)
- [ ] T6: Node 侧 `llm_analyzer` 与长任务编排 — acceptance: 同一模型配置下分析/定案/执行的状态机与 Python 一致，两两互斥语义保持 (covers: S2.2; depends: T2)
- [ ] T7: Node 侧 `server` 路由全量对齐 — acceptance: `/api/*` 逐条双跑一致，含 token / origin / vault 门禁 (covers: S2.2, S2.5; depends: T3, T4, T5, T6)
- [ ] T8: 视觉迁移（原型 token 落进前端 + 托盘、开机自启、窗口位置持久化） — acceptance: 深/浅色与四种材质可切换且与原型一致；托盘菜单四项可用 (covers: S2.3, S2.4; depends: T1)
- [ ] T9: 打包切换（electron-builder `nsis` + `portable`） — acceptance: 便携版解包体积记录在案；安装版可启动并复用既有数据目录；**且按 `docs/release-audit.md` 的 2.2 重做更新链路**：① 更新助手必须重启**壳自身**（现在是杀后端 pid、重启旧 exe，壳不在其列；`BiliFavOrganizer.exe` 这个名字与「覆盖后重启同一 exe」的假设在 Electron 下作废）；② 端口只能来自壳的分配+握手（`--port 0` + `BFO_READY`）或 `BFO_PORT`，**不得把运行期随机端口写进重启命令**（Electron 不读 `--port`）；③ 产物改名后同步更新 asset 名模板（`_latest_release_version` / `_download_release_asset`）与三处文档（README 快速开始、`packaging/使用说明.txt`、`docs/快速上手.md`） (covers: S2.7; depends: T7, T8)
- [ ] T10: 双跑验收与退役 Python — acceptance: 全量只读接口双跑一致、迁移后的 Node 测试全绿，之后移除 Python 依赖与 PyInstaller 链 (covers: S2.2, S2.8; depends: T9)
