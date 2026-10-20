# T10 Windows 截图式 UAT 证据包

先读 [`acceptance-report.md`](acceptance-report.md)。

- 候选：`c75a06bd008c9f546349f60c7e3a147c266dade2` / tree `be8cef375bc9334e67decc7383d35905ee146eac`
- 结果：PASS 14 · FAIL 0 · BLOCKED 0 · NOT_RUN 0，截图 39 张

| 目录 | 内容 |
|---|---|
| `acceptance-report.md` | 逐场景结论、核验项、限制 |
| `screenshots/` | 39 张真实 UI 截图，文件名含场景编号与状态 |
| `results/` | 每个场景一份结构化结果（核验项、备注、耗时） |
| `logs/*.server.log.txt` | 每个场景的服务端启动/运行日志 |
| `logs/*.mock-calls.json` | 假 session 记录的远端调用序列 |
| `logs/startup-and-isolation.log.txt` | 逐场景隔离路径与真实 profile 未触碰的 mtime 审计 |
| `logs/mock-remote-write.log.txt` | 汇总的 mock 写调用与载荷 |
| `logs/read-only-state-checks.log.txt` | 通过 HTTP API 读取的状态观察 |
| `t9-github-protection-readonly.md` | T9 仓库保护只读核验；风险已接受，T9 仍为 FAIL |
| `logs/t9-github-api-readonly.log.txt` | T9 只读 API 调用记录 |

## 复现

```powershell
git clone --no-checkout --branch feat/reliability-scheduler-implementation `
  https://github.com/Martin-soaring-dev/bili-fav-organizer.git C:\BFOU1
git -C C:\BFOU1 config core.autocrlf false
git -C C:\BFOU1 switch --detach c75a06bd008c9f546349f60c7e3a147c266dade2
# 需要 venv + requirements-test.txt + playwright install chromium，然后运行
# harness/boot.py 驱动的场景（本轮 harness 位于 C:\BFO-UAT，属仓库外临时工具）。
```

隔离 profile 路径必须保持短（Windows MAX_PATH）。T10-14 使用仓库内夹具
`tests/database/fixtures/v0209_schema7.sqlite3`。
