---
feature: review-hardening
status: delivered
updated: 2026-10-09
branch: feat/review-hardening
commits: 048082b..6512ffb
---

# Review Hardening（critical + major）

## Report

**What was built** — 本地 API 增加 Host 白名单、Origin/Sec-Fetch 校验与进程级 token（Windows 上 DPAPI 写入 `token.blob`，启动后解密校验），前端由 `GET /` 注入 token 并自动附带请求头，阻断 CSRF / DNS rebinding。六个长任务统一经 `_begin_job` 锁占坑互斥，消除 check-then-act 双开。`run_batch` 的 try 只包远程调用：远端成功后本地落库失败保持 `done` 并停止，不再覆盖成 `failed`。补齐跨批契约测试与 DPAPI 回归测试。

**Verification** — `python -m unittest discover -s tests` → **PASS 140/140**；`tests.test_local_api_guard` + `test_apply_safety_contracts` → PASS；独立复审确认 Host 绕过、token 链路、中间件顺序、Origin 端口、DPAPI 测试均已闭合（无剩余 critical/major）。

**Journey log** —
1. TestClient 的 `Host: testserver` 曾被当成生产豁免，审查判 critical；改为仅 `BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL=1` 时允许。
2. 安全测试误用 `data/clear` 的 `scope:"none"`（会落到 `all`）清库；改为无害写接口。
3. 全局 `ALLOW_INSECURE` 被 setUpClass 弹掉会拖垮其它 TestClient 套件；改为用例级保存/恢复。
4. Starlette 后注册的中间件在外层：`local_api_guard` 必须写在 `scan_issue_gate` 之后。
5. `test_default_folder_*` 原先依赖环境库夹具，补 mock 后不再受数据目录状态影响。

## [S1] Problem

2026-10-09 三路审查（本分支 diff / 架构 / 安全）发现以下 critical 与 major，阻碍合并：

1. **安全 C1**：本地 API 无鉴权、无 CSRF/Origin/Host 校验。任意网页可对 `127.0.0.1` 发起无 body POST（`/api/apply/start`、`/api/folder-organize/start`、`/api/update/install`…），或经 `no-cors` + `text/plain` 调 JSON 接口（`/api/data/clear`、`/api/cookie`）；无 Host 白名单时 DNS rebinding 可读 `/api/data/export`。后果：远程网页可触发真实 B 站批量写、清空本地数据、覆盖 Cookie。
2. **架构 C1**：长任务启动为 check-then-act，仅 scan 有锁。`analyze_start` / `apply_start` / `folder_profiles_generate` 双请求可双开 worker，争用同一 `APP["session"]` 与 plan/analyses。
3. **架构 C2**：跨任务互斥不完整。analyze 可与 apply 并行；profile 可与 scan 重叠；`APP` 状态所有权未定义。
4. **分支 major A**：`run_batch` 的 try 覆盖远程调用与落库。`move_batch`/`batch_delete` 成功后若 `store.save_*` 抛 `OSError`，批次被标 `failed` 并重复计数 `state["done"]`，覆盖真实成功。
5. **分支 major B**：`test_business_failure_*` 与 `test_unexpected_exception_*` 将两条计划放在同一批，未覆盖「业务失败后继续下一组」「未预期异常后不再写」的跨批语义。

## [S2] Design

### 2.1 本地 API 防护（安全 C1）

四层，全部在 `server.py` HTTP 边界完成，不改业务语义：

| 层 | 规则 |
|----|------|
| Host | 仅允许 `Host` 为 `127.0.0.1[:port]`、`localhost[:port]`、`[::1][:port]`。`testserver` 仅在 `BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL=1` 时允许。其余 403。 |
| Origin / Sec-Fetch | 非 GET/HEAD/OPTIONS：若带 `Origin` 必须是 `http://127.0.0.1:<port>` 或 `http://localhost:<port>`（端口须等于监听端口）；若带 `Sec-Fetch-Site` 必须为 `same-origin` 或 `none`。 |
| Token | 进程启动时生成 `secrets.token_urlsafe(32)`。非 GET 写接口及敏感 GET（`/api/data/export`、`/api/cookie`）要求 `X-BiliFav-Token` 或 `Authorization: Bearer <token>`。`GET /` HTML 注入 token，`static/app.js` 自动附加请求头。仅 `_insecure_local()` 跳过 token（测试钩子）。 |
| DPAPI | token 用 Windows DPAPI（`CryptProtectData`；非 Windows `PLAIN\x00` 回退）写入 `token.blob`，启动后 `_load_persisted_token` 解密校验。应用密码 UI 不在本轮。 |

错误响应统一 `403` + `{"ok": false, "error": "..."}`。`local_api_guard` 注册在 `scan_issue_gate` 之后（Starlette 后注册在外层，安全最先执行）。

### 2.2 任务启动锁与互斥（架构 C1/C2）

- `_JOB_START_LOCK` + `LONG_JOBS`（六任务）。
- `_begin_job(key, state)`：锁内检查其它 `running` 后占坑写入 `APP[key]`；冲突返回 409。
- 六个长任务两两互斥；读接口与 SSE 不受影响。

### 2.3 写路径 try 收窄（分支 major A）

`run_batch` 的 `try` **仅**包远程调用；成功后 `finish_items`/`save_progress`/`emit` 在 try 外。落库失败：条目保持 `done`，`state["stop"]=True`，不改写成 `failed`。

### 2.4 跨批契约测试（分支 major B）

业务失败后继续下一组；未预期异常后不再发第二枪；远端成功后落库失败仍为 `done`。

### 2.5 测试与回归

全量 unittest 全绿；现有用例不得回归。

## [S3] Out of Scope

- 应用密码解锁 UI、强制每次启动输密码。
- `server.py` / `app.js` 模块拆分。
- 审查 minor：默认夹识别失败静默放行、CI 未锁 pyinstaller/innosetup、`$WorkDir`、overview 笔误、`FakeBiliSession` 去重。
- NTLM/Windows Hello SSO、HTTPS、远程访问、旋转 token。

## Tasks
- [x] T1: `local_api_guard` 中间件 — Host/Origin/Sec-Fetch/token 校验 + `GET /` 注入 token + `app.js` 附加请求头 — acceptance: 无 token 的 POST `/api/data/clear` 返回 403；合法带 token 本地请求 200；跨 Origin POST 403 (covers: S2.1)
- [x] T2: DPAPI token.blob 读写封装（Windows 加密 / 非 Windows 回退）— acceptance: Windows 下写入的 blob 仅当前用户可解；单元测试在非 Windows 跳过加密断言只测回退 (covers: S2.1)
- [x] T3: `_begin_job` 启动锁 + 六任务互斥 — acceptance: 并发双开 apply 仅一个成功；scan 运行时 analyze 返回 409 (covers: S2.2)
- [x] T4: `run_batch`/`create_folder` try 收窄 — acceptance: save_plan 失败不把已成功条目标 failed，run 停止并报落库错误 (covers: S2.3)
- [x] T5: 跨批契约测试补齐 — acceptance: 业务失败继续、未预期停止、落库失败三场景测试通过 (covers: S2.4; depends: T4)
- [x] T6: 安全/互斥回归测试 + 全量测试全绿 — acceptance: `python -m unittest discover -s tests` 0 fail (covers: S2.5; depends: T1, T2, T3, T4, T5)
