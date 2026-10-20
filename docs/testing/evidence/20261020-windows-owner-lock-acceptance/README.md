# Windows 验收证据包：scheduler owner 锁修复（2026-10-20）

候选 `4baa66e628538cfb2762b63bed82facd1b47f28a` / tree `2b4a7263705ff345fa1fbab6c6173f0510b55673`，目标分支 `feat/reliability-scheduler-implementation`。

先读 [`acceptance-report.md`](acceptance-report.md)，那里是完整结论；本文件只做索引与阅读说明。

## 目录内容

| 文件 | 内容 |
|---|---|
| `acceptance-report.md` | 本轮完整回报：身份、环境、命令、计数、七项 UI、失败根因、边界 |
| `environment.txt` | 机器快照：OS/PS/Python/Playwright/Chromium、clone 的 git 身份、路径长度占位符 |
| `logs/01-owner-lock.longpath.log.txt` | 首次定向 `scheduler.test_owner_lock`（长路径 clone） |
| `logs/02-server-integration.longpath.log.txt` | 首次定向 `scheduler.test_server_integration`（长路径 clone） |
| `logs/03-full-regression.longpath.log.txt` | 首次全量：326 项、2 FAIL + 29 ERROR，含全部原始 traceback |
| `logs/r1-owner-lock.shortpath.log.txt` | 短路径 clone 定向重跑：3/3 ok |
| `logs/r2-server-integration.shortpath.log.txt` | 短路径 clone 定向重跑：4/4 ok |
| `logs/r3-full-regression.shortpath.log.txt` | 短路径 clone 全量：326/326 ok |
| `logs/r4-full-regression-clean.shortpath.log.txt` | 逐行无折行复核全量：326/326 ok |
| `logs/playwright-install.log.txt` | 按锁定版本安装 chromium build 1243 的下载记录 |
| `tools/pathlen_probe.py` | 路径长度对照实验（267 字符 FAIL / 227 字符 OK） |

## 阅读顺序建议

1. `acceptance-report.md` 第 3–5 节看最终结论与七项 UI。
2. `logs/03-full-regression.longpath.log.txt` 看首次失败的完整 traceback（搜索 `unable to open database file`）。
3. `acceptance-report.md` 第 6 节看该失败的根因判定，`tools/pathlen_probe.py` 是对照实验。
4. `logs/r4-full-regression-clean.shortpath.log.txt` 尾部看 `Ran 326 tests` + `OK`。

## 两点必须注意

**一、第 3 次全量的失败不是候选缺陷。** 验收方最初把 clone 放在 41 字符的带时间戳目录名下，使 SQLite 首次初始化候选库路径达到 266 字符，超过本机 `LongPathsEnabled=0` 下的 MAX_PATH 260，`sqlite3.connect(..., "rwc")` 抛 `SQLITE_CANTOPEN`。31 个问题用例全部由此引起。改用 5 字符短目录后全部通过。未修改源码、未放宽断言。

**二、本目录不含 UI 截图。** 七项 Chromium UI 由 Playwright 驱动真实浏览器做断言，测试脚本本身不产出截图文件。断言通过不等于截图验收，T10 的截图式 UAT 仍为 NOT_RUN。

## 复现方式

```powershell
git clone --no-checkout --branch feat/reliability-scheduler-implementation `
  https://github.com/Martin-soaring-dev/bili-fav-organizer.git C:\BFOA4
git -C C:\BFOA4 config core.autocrlf false
git -C C:\BFOA4 switch --detach 4baa66e628538cfb2762b63bed82facd1b47f28a
cd C:\BFOA4
python -m venv ..\bfo-venv
..\bfo-venv\Scripts\python.exe -m pip install -r requirements-test.txt
..\bfo-venv\Scripts\python.exe -m playwright install chromium
$env:BFO_REQUIRE_PLAYWRIGHT = '1'
..\bfo-venv\Scripts\python.exe tools/run_isolated_tests.py --test scheduler.test_owner_lock
..\bfo-venv\Scripts\python.exe tools/run_isolated_tests.py --test scheduler.test_server_integration
..\bfo-venv\Scripts\python.exe tools/run_isolated_tests.py
```

隔离 clone 路径必须保持短：`tools/run_isolated_tests.py` 会在 clone 内再嵌套 `.bfo-isolated-tests-<8hex>\checkout\tests\.test-data\...`，路径越短越安全。

## 脱敏

日志中的个人路径已替换为定长占位符（`<USERPROFILE>`=19、`<LONGPATH-CLONE>`=41、`<RUN-TEMP>`=28、`<SHORTPATH-CLONE>`=5），长度与原文一致，因此第 6 节的路径长度结论可复算。除替换外日志内容无删改，已确认不含 Cookie、token、API Key 或恢复码。
