# 第二轮 Windows Vault 阻断与复测（2026-10-20）

## 最新证据与状态

用户转交 `ac9da2057f4d7adf7640e16b499e50bd5d5fcbd2` 的 Windows 全量摘要：318 项，298 PASS、6 FAIL、14 ERROR、0 SKIP，171.8 秒、非零退出。此前锁父目录和历史夹具问题已获 Windows 反馈确认修复；Vault 归档/锁定路径仍失败，T6/T8 保持 FAIL/BLOCKED。本记录未取得完整原始逐项日志，不能把所有剩余失败都归因于一个问题。

本次修复是该候选的正常后继提交；准确 SHA 由交付消息和远端 HEAD 核实。生产代码只从目标远端分支获取，不使用旧离线包。下面的本机回归通过不构成新的 Windows 全量 PASS。

## 已证实缺陷及修复

### 运行锁被错误地当作明文业务来源

调度器把 `.scheduler-owner.lock` 放在 DATA_DIR，并通过 `FileOwnerLock` 长期持有内核字节锁。旧 `_vault_managed_plaintext_sources` 只排除了当前目录的 `.bfo-profile.lock`，会把调度器锁及 legacy 目录内的控制锁加入归档来源。Windows 对锁区的另一个句柄读取会失败；POSIX advisory lock 允许读取，使本机诊断遗漏了这个平台差异。

修复只排除准确的活动/legacy `.bfo-profile.lock`、`.scheduler-owner.lock` 和用户根 `.bfo-server-instance.lock` 路径，保持其文件身份与持锁状态。任意其他 `.lock` 业务文件仍归档、验证和清理，不用广泛后缀跳过数据。新生产集成回归在旧代码下通过模拟 Windows 锁区拒读复现 PermissionError，修复后确认锁定/解锁期间同一 owner 文件仍存在、第二调度器仍无法取得所有权，业务文件仍被完整封存。

Windows 行为参考：https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files

### 原子写入临时路径不必要地变长

`Vault._atomic_bytes` 本来已经创建 `dest.parent`；`_vault_archive_sources` 也创建归档目录，因此不能仅凭 FileNotFoundError 认定漏建目录。旧临时名称复制完整归档文件名（含64位hash）后再加UUID，使临时路径比最终目标更长。已在模拟 Windows 路径预算的回归中复现“目标可写、临时文件创建失败”。现用不含目标名称的短随机临时文件名，仍在目标同目录独占创建、fsync 后原子 replace；替换拒绝时旧密文不变、临时文件被清理。

这消除了临时名称额外膨胀的确定缺陷；用户本次 FileNotFoundError 是否正由该长度触发，仍需 Windows 完整路径和原始 traceback 核实。目标路径本身超限或真实目录权限错误不应被吞掉。下一次失败必须保留 filename、winerror、路径长度和完整 traceback。

Windows 路径限制参考：https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation

### 原始错误被探针二次异常掩盖

onboarding 可能先成功写入加密快照，后在归档阶段失败。旧探针只看密文存在便强行读取必需 manifest，产生“清单缺失”并掩盖原始500。现在只有完成接口成功后才收集成功清单/解密摘要；接口失败仍原样返回失败，不伪造 PASS。增加归档 PermissionError 故障注入测试，验证原始 completion 500 和 traceback 可保留、源库仍存在。

锁定/onboarding 的服务日志增加 traceback；MIG-029 的 marker 断言与调度器直接调用断言增加原始响应诊断，断言仍要求成功。真实 PermissionError 继续失败关闭。

## 本次本机验证

| 隔离 runner 参数 | 结果 |
|---|---:|
| `--test tests.test_vault_atomic_io` | 3/3 |
| `--test scheduler.test_server_integration` | 4/4 |
| `--test tests.test_vault_api` | 9/9 |
| `--migration-only` | 31/31，含 MIG-027/029 和新增错误保留用例 |
| `--test tests.test_vault` | 11/11 |
| `--test tests.test_vault_device_secret` | 13/13 |
| `--test tests.test_dek_selftest` | 4/4 |

均为 Linux 隔离合成 profile 的开发验证；路径预算和 Windows 拒读语义通过故障模拟覆盖。生产调度器集成真实持有当前平台内核锁，但不能当作 Win32 原生验证。先前的 `test_profile_lock_windows` 5项也使用mock，即使在Windows执行也不能单凭它声称内核锁路径实测；需要lifecycle、实例锁及生产集成的Windows证据。修复前新路径预算回归 ERROR、锁文件集成回归 FAIL；修复后通过。套件有覆盖关系，不相加为全量数。

## Windows 本地 AI 复测顺序

仅 fetch `feat/reliability-scheduler-implementation` 并检出用户交付消息的准确新 SHA，在全新 disposable Windows checkout/profile 上执行。保留 `ac9da20` 的失败证据。先完整读取本提交的维护任务清单与本地 AI 提示词并核实隔离路径，设置严格 Chromium 门禁，再执行：

```powershell
$env:BFO_REQUIRE_PLAYWRIGHT = '1'
python tools/run_isolated_tests.py --test tests.test_vault_atomic_io
python tools/run_isolated_tests.py --test scheduler.test_server_integration
python tools/run_isolated_tests.py --test tests.test_vault_api
python tools/run_isolated_tests.py --migration-only
python tools/run_isolated_tests.py
```

本次新增5个测试，相对报告中的318项预计增加到323项；实际总数受 setUpClass 是否成功、七项 UI 是否执行影响，必须记录实际测试列表。311并非固定验收阈值，不能通过删测试或跳过来凑数。零skip也不能替代“七项UI实际执行”的证据。

所有错误必须附准确 SHA/tree、完整测试名、traceback、临时目录与归档路径长度、filename/winerror。若还有 PermissionError，明确被拒读/拒写/拒删的是哪个文件；若 FileNotFoundError，核对父目录是否实际存在及路径长度。不得只依据摘要修改系统权限或将异常忽略。真实 Windows 全量零 FAIL/ERROR、七项 UI 通过后，才按原清单继续安装/UAT；发布审批边界保持原约定。
