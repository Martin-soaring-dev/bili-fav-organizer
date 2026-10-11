---
feature: embedded-terminal
status: in-progress
updated: 2026-10-10
branch: feat/desktop-shell
commits: # 留空
---

# 嵌入式终端（xterm.js + PTY）

## Report

## [S1] Problem

1. 现在的「终端」面板只是个回显框：`/api/terminal/exec` 把命令跑完一次性吐出文本，**没有 stdin、没有 ANSI 颜色、没有光标、不能跑 vim/htop 这类全屏程序**，60 秒硬超时还会杀掉长命令。
2. 明暗主题不统一：终端面板的配色不跟随应用主题。
3. 目标是「像常见 agent 一样」的嵌入式真终端（Cursor / Claude Code 一类）。

## [S2] Design

### 2.1 组件与进程边界

- **渲染进程**：`@xterm/xterm` + `@xterm/addon-fit`，容器挂在现有终端面板里。
- **主进程**：`node-pty`（Windows 走 ConPTY）持有 PTY；一个面板对应一个 PTY。
- 两者经 IPC 通信：`terminal:spawn` / `terminal:write` / `terminal:resize` / `terminal:kill`；数据下行 `terminal:data`、退出 `terminal:exit`。

前端不直接碰 PTY，`preload` 只暴露 `shell.terminal` 一组窄接口。

### 2.2 锁定门（必须保持）

现有的 `/api/terminal/exec` 在应用密码/加密库锁定时会被 `/api/*` 门禁挡掉。**PTY 走的是主进程，天然绕过 HTTP 门禁，因此必须显式复刻同一条规则**：

- 锁定状态（`_APP_LOCK` 未解锁，或加密库未解锁）下拒绝 `terminal:spawn`，返回与 API 一致的 `app_locked` 语义；
- 已 spawn 的 PTY 在锁定时 **kill 掉**，不留后台 shell；
- 解锁后用户重新打开终端才 spawn。

这条是安全边界，不是体验细节。

### 2.3 壳与主题

| 项 | 契约 |
|---|---|
| Shell | `PowerShell 7`（`pwsh`）优先，回退 `powershell.exe`；`%COMSPEC%` 仅作最后回退 |
| 工作目录 | 应用数据目录（与现有 `/api/terminal/exec` 一致） |
| 明暗主题 | 跟随应用主题切换即时换色：暗 `#1f1f1f` 底 / 亮 `#f3f5f7` 底；前景、光标、选区、ANSI 16 色各取自 `static/style.css` 的 token |
| 字体 | 与现有一致：`Cascadia Code, Consolas, monospace`，12px |
| 缩放 | 窗口 resize 时 fit-addon 自动换行列数，并同步给 PTY |
| 回滚 | 5000 行 |
| 关闭 | 面板隐藏不 kill；窗口收进托盘不 kill；应用退出时 kill |

### 2.4 打包影响

`node-pty` 是原生模块：需要 `electron-rebuild`（或 electron-builder 自带的 rebuild），Windows 上要有 MSVC 构建工具。**这是本项目首次引入原生编译链**，`package.json` 的 `postinstall` 里跑 rebuild，CI 的 Windows runner 需要装 C++ 构建工具（`windows-latest` 自带）。

体积代价：约 2–3 MB（比 xterm 本体小）。

### 2.5 测试边界

- 主进程 PTY 管理：spawn/write/resize/kill 的生命周期；锁定时拒绝 spawn 并 kill 已有 PTY（`node:test` 或现有 unittest 不合适，用 Node 侧断言）。
- 渲染层不做单测，用 CDP 冒烟：打开终端面板 → 输入命令 → 输出包含预期字符串；切换主题后 xterm 背景色跟随。
- 现有 `/api/terminal/exec` **保留**（后端脚本仍要用），不因嵌入终端而删除。

## [S3] Out of Scope

- 多终端标签页 / 分屏（本期单个终端面板）。
- 终端内容的持久化或会话恢复。
- 远程 SSH。
- 把 `node-pty` 换成纯 JS PTY（Windows 上不可行）。

## Tasks

- [x] T1: 依赖与打包链打通（`node-pty` + `@xterm/xterm` + fit addon，electron-rebuild 可用） — acceptance: `node -e "require('node-pty')"` 在 Electron 环境下可加载；`npm run dev` 正常起窗口 (covers: S2.4)
- [x] T2: 主进程 PTY 管理器 + IPC（spawn/write/resize/kill/data/exit） — acceptance: 生命周期用例全过；重复 spawn 旧 PTY 被正确回收 (covers: S2.1, S2.2)
- [x] T3: 锁定门（锁定拒绝 spawn、锁定时 kill 已有 PTY） — acceptance: 锁定后 spawn 返回 app_locked；锁定动作把已有 PTY 杀掉 (covers: S2.2)
- [x] T4: 渲染层 xterm 接入面板 + 主题跟随 + resize 联动 — acceptance: 输入 `echo hello` 有回显与颜色；切换深浅色背景即时变；窗口缩放后行列数同步 (covers: S2.3, S2.5)
- [ ] T5: CDP 冒烟 + 打包验证 — acceptance: 冒烟脚本全绿；electron-builder 打出的包内 xterm 可用 (covers: S2.4, S2.5)
