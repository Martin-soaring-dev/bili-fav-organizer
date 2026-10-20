# 第三轮 Windows owner 锁修复与远端复验

## 当前证据及阻断

用户转交旧候选 `f590ae8cbab2a0c58e3bb90c49550b8bdd67134f`：323 项，322 PASS、1 FAIL、0 ERROR、0 SKIP，215.0 秒、非零退出。七项 Chromium UI 全 PASS；migration-only 31/31、Vault API 9/9、atomic IO 3/3 PASS。完整原始逐项日志尚未取得，唯一失败的 traceback 已提供。

失败为 `scheduler.test_server_integration.ServerSchedulerIntegrationTests.test_vault_seals_business_files_without_reading_or_replacing_runtime_locks`：竞争进程在 `FileOwnerLock.acquire()` 中先读已被另一句柄锁住的 byte 0，收到 PermissionError，未到实际锁竞争处理。不是路径超限，也不是 Vault 归档回退。保留原测试断言，不把 PermissionError 当成功。

本提交只修改 Windows owner 锁获取：直接 seek(0) 后执行 msvcrt.LK_NBLCK，不读、不写、不替换锁文件。锁范围可以超出 EOF，因此空文件无需初始化。锁竞争仍经原 OSError→RuntimeError 契约返回，异常时关闭新句柄；release 仍解锁同一字节并关闭原句柄。

依据：[Microsoft CRT _locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking?view=msvc-170)。不新增 Linux 适配，不改 profile 锁、Vault 加密或业务归档规则。

## 开发验证

- 新增 `scheduler.test_owner_lock` 三项：拒读环境直接加锁、竞争异常契约与句柄关闭、真实子进程空/已有文件竞争及释放后重取（文件内容不变）。前两项在旧实现复现 PermissionError；修复后三项通过。
- scheduler 核心 20/20 通过。
- 原 scheduler/server 集成 4 项首次 3 PASS、1 FAIL：scan 测试末尾 `assert not store.database_connections()` 瞬时失败；原样重跑 4/4 PASS。报告该不稳定信号，不将重跑抹去首轮失败。本次 Windows 分支修改没有改变本机走的 POSIX 锁实现；Windows 全量仍需回传该项结果。
- 以上是当前开发容器诊断与模拟 Windows 语义，不替代 Windows 原生内核验证。

## 给 Windows 本地 AI 的执行指令

仓库 `https://github.com/Martin-soaring-dev/bili-fav-organizer.git`，唯一远端分支 `feat/reliability-scheduler-implementation`。只从远端获取代码；准确新 SHA 由本次交付消息提供，不能继续测试 f590ae8 或只按浮动分支名验收。用全新 disposable clone，保护已有工作树与用户数据，不复用旧离线包，不推送任何本地修改。

```powershell
# NEW_SHA 必须替换为交付消息中的完整新提交 SHA
$CandidateSha = 'NEW_SHA'
git clone --no-checkout --branch feat/reliability-scheduler-implementation https://github.com/Martin-soaring-dev/bili-fav-organizer.git BFO-Windows-Acceptance-v4
git -C BFO-Windows-Acceptance-v4 config core.autocrlf false
git -C BFO-Windows-Acceptance-v4 switch --detach $CandidateSha
Set-Location BFO-Windows-Acceptance-v4
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
```

先完整阅读本提交的 `docs/testing/local-ai-windows-acceptance-prompt.md` 与维护任务清单，遵守其中 Python、临时目录、profile、网络、Chromium 严格门禁和证据隔离要求，再按顺序实际运行（每步记录退出码，有失败先保留完整 traceback）：

```powershell
python tools/run_isolated_tests.py --test scheduler.test_owner_lock
python tools/run_isolated_tests.py --test scheduler.test_server_integration
python tools/run_isolated_tests.py
```

新增三项，预计从323增至326；以真实 discovery 列表为准，不能删测试/跳过凑数。第三个新测试必须在真实 Windows 上分别验证空文件及已有字节文件：父进程持锁时子进程被拒绝，父进程释放后子进程可取得锁；不允许把任意 PermissionError 视为合格竞争。全量必须零 FAIL/ERROR/SKIP 且七项 Chromium UI 实际 PASS。

回传准确 SHA/tree、Python/Windows 版本、命令、逐项日志、总数/通过/失败/错误/跳过、耗时/退出码；若失败，提供完整 traceback 与 filename/errno/winerror/被拒操作。重点回传原失败的业务封存测试和 scan route 测试。T6/T8 新候选复验前保持 FAIL/BLOCKED；全绿后再按清单梳理其余门槛，不能自行发布、创建 PR、合并或打 tag。
