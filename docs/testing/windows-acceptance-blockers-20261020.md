# Windows 验收阻断与复测（2026-10-20）

## 当前判定

旧候选 `65ba02e2ff0ea32c3fc27f450567f49bb56560a8` 的用户转交 Windows 报告：306 项，261 PASS、6 FAIL、39 ERROR、0 SKIP，70.8 秒、非零退出。T6/T8 对该候选为 **FAIL / BLOCKED**，不能继续进入安装/UAT/发布放行。本记录依据转交摘要，尚无完整原始逐项日志，不把所有失败都归为同一根因。

本提交修复已证实的两个问题并增加回归测试，**新候选仍为 Windows 复验待完成**。准确修复 SHA 以交付消息及目标分支实际 HEAD 为准，须绑定测试证据。

## 根因与修复

1. `_acquire_os_lock` 的 Windows 分支直接调用 `_windows_acquire` → `_windows_open`，绕过 `_open_os_lock_file` 中的父目录创建。空 profile 下 `CreateFileW(OPEN_ALWAYS)` 不能创建父目录。现由 `_windows_open` 自行确保父目录存在；目录位置被普通文件占用时仍失败，保留该文件，不绕过错误。
2. 原代码将 Windows 原生错误码直接传给 `OSError(errno, ...)`，因此 Windows 错误 3 被误展示为 POSIX `ProcessLookupError`。打开/加锁/解锁失败现在使用 `ctypes.WinError`，保留 `.winerror` 和 Windows 错误含义，失败仍关闭已打开句柄。
3. `tests/database/fixtures/v0209_schema7.sqlite3` 在之前工作树存在，但被 `*.sqlite3` 忽略，未进入 `65ba02e` 的 Git tree。本次将 README 已记录的同一合成夹具正式纳入 Git，只为此准确路径增加 ignore 例外；SHA-256 为 `511f66afc63562f8a102a49150a55a22a839c0340fa70a9c64a0690692e1b273`。未加入用户数据库、WAL、密钥或凭据。历史迁移测试也核验该固定 hash。
4. MIG-029 没有到达明文删除检查点可能是初始化失败的连带症状，不能仅凭摘要认定独立缺陷或已解决，必须在 Windows 新候选上重跑。

## 已完成验证及限制

- 新增 Windows 后端边界回归测试在修复前出现缺失目录、原生错误丢失及目录被文件占用三类失败；修复后 `tests.database.test_profile_lock_windows` **5/5 PASS**。内核调用采用 mock，不能替代真实 Windows 锁验收。
- `tests.database.test_schema_migration` **16/16 PASS**，包含此前缺失夹具的实际迁移和数据保留。
- `tests.database.test_lifecycle` **22/22 PASS**；`tests.test_server_instance_lock` **2/2 PASS**。
- `--migration-only` **30/30 PASS**，其中 MIG-029/MIG-030 实际执行。
- 上述命令使用隔离 runner、临时合成 profile，在非目标 Linux 开发环境执行；它们不构成 Windows 产品通过结论。各套件有覆盖关系，不能相加当作全量数。

## Windows 本地 AI 下一步（仅远端获取）

保持旧候选的失败日志，不在旧 checkout 上手动改代码。只从 `origin/feat/reliability-scheduler-implementation` 获取交付消息指定的修复 SHA，在新 disposable Windows checkout/profile 上复测。不要重用故障测试生成的 profile，不用旧离线包覆盖远端代码。

先确认 `git rev-parse HEAD`、`git rev-parse HEAD^{tree}`，并用 `git ls-files --error-unmatch tests/database/fixtures/v0209_schema7.sqlite3` 确认夹具已被版本管理；核对上述 SHA-256。读取本提交的本地 AI 提示词和维护任务清单，沿用完整隔离边界。

用 Python 3.12 与候选 requirements-test.txt 的依赖，在隔离环境设置 `$env:BFO_REQUIRE_PLAYWRIGHT='1'`；确保候选固定版本的 Chromium 可启动。按顺序运行，逐条记录命令和退出码：

```powershell
python tools/run_isolated_tests.py --test tests.database.test_profile_lock_windows
python tools/run_isolated_tests.py --test tests.database.test_schema_migration
python tools/run_isolated_tests.py --test tests.database.test_lifecycle
python tools/run_isolated_tests.py --test tests.test_server_instance_lock
python tools/run_isolated_tests.py --migration-only
python tools/run_isolated_tests.py
```

新增 5 个测试，因此相对先前 306 项，全量预期增加到 311 项；以实际 discovery 为准，记录差异及逐项失败列表。七项 UI 场景必须实际执行、零 skip。真实 Windows 下生命周期/实例锁/迁移跨进程测试通过，才能接受 Windows 锁修复；mock 测试通过不够。

定向或全量仍出现失败时，标记对应任务 FAIL/BLOCKED，并回传完整 traceback、测试名、准确 SHA/tree、实际 profile 路径及运行日志。MIG-029 失败时尤其保留初始化/封存子进程的原始错误，不能只报告 marker 超时。测试通过后再按原清单推进 GUI/安装和逐张截图 UAT；PR、merge、tag、Release 仍须对应阶段明确授权。
