---
feature: review-hardening
status: in-progress
updated: 2026-10-09
branch: feat/review-hardening
commits: # leave empty while in progress; fill at delivery
---

# Review Hardening（critical + major）

## Report

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
| Host | 仅允许 `Host` 为 `127.0.0.1[:port]`、`localhost[:port]`、`[::1][:port]`。其余 403。 |
| Origin / Sec-Fetch | 非 GET/HEAD/OPTIONS：若带 `Origin` 必须是 `http://127.0.0.1:<port>` 或 `http://localhost:<port>`；若带 `Sec-Fetch-Site` 必须为 `same-origin` 或 `none`。缺省（无 Origin 且无 Sec-Fetch）视为非浏览器客户端，继续做 token 校验，不因缺省放行 CSRF。 |
| Token | 进程启动时生成 `secrets.token_urlsafe(32)`。非 GET 写接口（以及敏感 GET：`/api/data/export`、`/api/cookie`、`/api/status`、`/api/version` 可除外）要求 `X-BiliFav-Token` 或 `Authorization: Bearer <token>` 与启动 token 一致。`GET /` HTML 注入 `<script>window.__BILI_FAV_TOKEN__=...`，`static/app.js` 自动附加请求头。 |
| DPAPI | token 明文仅存进程内存；另用 Windows DPAPI（`ctypes` 调 `CryptProtectData`，无第三方依赖；非 Windows 回退 `secrets.json` 权限收紧）把「自动解锁密钥」加密写入 `%LOCALAPPDATA%\BiliFavOrganizer\token.blob`。同 Windows 用户下次启动可恢复 UI 会话密钥；拷贝文件到其他账号无法解密。应用密码 UI 不在本轮（见 Out of Scope）。 |

错误响应统一 `403` + `{"ok": false, "error": "..."}`。`scan_issue_gate` 中间件之后再挂 `local_api_guard`。

开发/测试：`BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL=1` 时跳过 token（仅 token 层；Host/Origin 仍生效），供 unittest 使用；默认关闭。

### 2.2 任务启动锁与互斥（架构 C1/C2）

- 新增模块级 `_JOB_START_LOCK = threading.Lock()` 与常量 `LONG_JOBS = ("scan_run", "analyze_run", "apply_run", "folder_merge_run", "folder_merge_ai_run", "folder_profile_run")`。
- 抽出 `def _begin_job(key: str, state: dict, *, conflicts: tuple[str, ...] = LONG_JOBS) -> str | None`：在锁内检查 `conflicts` 中是否已有 `running`，否则写入 `APP[key] = state` 并返回 `None`；冲突时返回错误文案。所有长任务入口改为「先 `_begin_job`，失败则 409」，去掉分散的 check-then-act。
- **互斥矩阵（本轮）**：六个长任务两两互斥（同一时刻至多一个 `running`）。读接口与 SSE 不受影响。`folder_merge_ai_run`（纯 LLM 建议）也纳入互斥，避免与 apply 抢 `APP`/store 语义未定义；与设计文档冲突时以本节为准。
- `apply` / `analyze` / `profile` / `merge` 已有的「入口再查一次 readiness」保留，但只在 `_begin_job` 成功之后执行。
- 测试：并发双开 `apply_start`/`analyze_start` 仅一个 `started: true`；scan 运行中 `analyze_start` 返回 409。

### 2.3 写路径 try 收窄（分支 major A）

`server.py` `run_batch`：

- `try` **仅**包住 `session.batch_delete` / `session.move_batch` 远程调用。
- 成功后在 try 外调用 `finish_items` → `save_progress` → `emit`。若 `save_progress` 失败：记日志 + `emit("err")`，**不得**把已成功批次标 `failed`，**不得**回滚远端（先加后删已发生）；将 `state["error"]` 置为「远端已成功但本地落库失败…」并 `state["stop"] = True`，条目保持 `done`。
- `create_folder` 的 try 同样只包远程调用。

### 2.4 跨批契约测试（分支 major B）

`tests/test_apply_safety_contracts.py` 增强（或新增用例）：

- **业务失败后继续**：两组不同 `target`/批次（参考 `test_uncertain_batch_stops_and_requires_review` 的双组写法），第一组 `BiliApiError` → 整组 `failed`，第二组必须仍被调用且 `done`。
- **未预期异常后停止**：第一组抛 `RuntimeError` → 整单停止；第二组 `fake.calls` 无记录。
- **落库失败不覆盖远端成功**：`store.save_plan` 抛 `OSError`，远端 `move_batch` 成功后计划仍为 `done`，`apply_run` 停止且带落库错误。

### 2.5 测试与回归

- 新增/改编单测覆盖 2.1（Host 拒绝、缺 token 拒绝、跨 Origin 拒绝、合法本地请求通过）、2.2（双开互斥、跨任务 409）。
- 全量 `python -m unittest discover -s tests -v` 必须全绿。
- 现有 121 用例不得回归。

## [S3] Out of Scope

- 应用密码解锁 UI、强制每次启动输密码（第二阶段；本轮只做 token + DPAPI 文件）。
- `server.py` / `app.js` 模块拆分（P1 原暂缓项中的拆分部分）。
- 审查中的 minor：默认夹识别失败静默放行、`执行初始化失败` 文案、CI 未锁 pyinstaller/innosetup、`$WorkDir`、overview 计数笔误、`FakeBiliSession` 去重。
- NTLM/Windows Hello SSO、HTTPS、远程访问。
- 旋转 token、双 token。

## Tasks

- [ ] T1: `local_api_guard` 中间件 — Host/Origin/Sec-Fetch/token 校验 + `GET /` 注入 token + `app.js` 附加请求头 — acceptance: 无 token 的 POST `/api/data/clear` 返回 403；合法带 token 本地请求 200；跨 Origin POST 403 (covers: S2.1)
- [ ] T2: DPAPI token.blob 读写封装（Windows 加密 / 非 Windows 回退）— acceptance: Windows 下写入的 blob 仅当前用户可解；单元测试在非 Windows 跳过加密断言只测回退 (covers: S2.1)
- [ ] T3: `_begin_job` 启动锁 + 六任务互斥 — acceptance: 并发双开 apply 仅一个成功；scan 运行时 analyze 返回 409 (covers: S2.2)
- [ ] T4: `run_batch`/`create_folder` try 收窄 — acceptance: save_plan 失败不把已成功条目标 failed，run 停止并报落库错误 (covers: S2.3)
- [ ] T5: 跨批契约测试补齐 — acceptance: 业务失败继续、未预期停止、落库失败三场景测试通过 (covers: S2.4; depends: T4)
- [ ] T6: 安全/互斥回归测试 + 全量测试全绿 — acceptance: `python -m unittest discover -s tests` 0 fail (covers: S2.5; depends: T1, T2, T3, T4, T5)
