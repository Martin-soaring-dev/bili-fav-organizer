# T11b Windows 安装包安装与升级验收（候选 1981a94）

- 候选：`1981a94ed750d7be4e3404d6622ad776708f2091`（tree `e943cc30ebf0b6dffcf69866c01761fba9928b32`，parent `aa9dfb35033186fcb2e04db19cfbc250ecf37873`）
- 日期：2026-10-21（Windows 11 build 26300，PowerShell 7.6.7，Node v24.20.0 / npm 11.19.0）
- 结论：**安装 + schema 7 升级 + 重启验证 PASS**（本地同 SHA 构建的安装包；非 CI 二进制）

## 1. 安装包

| 项 | 值 |
|---|---|
| 名称 | `BiliFavOrganizer-neo-Setup-0.209.0-beta.2.exe` |
| 字节数 | 140,995,964 |
| SHA-256 | `B2853AC38DF8CF6244367724C0B66F8BB31AE89EB71DA5F742650C917684D532` |
| 构建方式 | 在锁定 checkout `C:\BFOU5`（HEAD 未变、`git status` 空）执行与 CI 相同命令：先 `PyInstaller --noconfirm --clean BiliFavOrganizer.spec`，再 `electron-builder --config electron-builder.yml --win nsis --x64 --publish never -c.extraMetadata.version=0.209.0-beta.2` |
| 与 CI 产物关系 | **本地构建，不是 CI 二进制**。CI 同名产物 141,117,170 字节（构建环境差异导致体积不同）；CI 未上传 artifact（`artifact_count=0`），故无法取得 CI 产物做验收 |

构建途中修正的两个环境问题（均未改产品代码）：MiMo 打包 Node 的 `argv[1]` 不是脚本路径；electron-builder 通过 PATH 解析 `npm` 时需前置 `C:\Program Files\nodejs`，否则 `npm list --json` 被解析失败（`No JSON content found in output`）。

## 2. 隔离安装与升级

- 安装目录：`C:\BFO-UAT\install-t11b`（NSIS 静默安装 `/S /D=…`，退出码 0，16 秒）
- 一次性 profile：`C:\BFO-UAT\prof-t11b`，通过 `LOCALAPPDATA`/`USERPROFILE`/`HOME`/`BILI_FAV_ORGANIZER_DATA_DIR` 注入
- 夹具：`tests/database/fixtures/v0209_schema7.sqlite3`，SHA-256 `511f66afc63562f8a102a49150a55a22a839c0340fa70a9c64a0690692e1b273`（迁移前后未变，见第 4 节）

### 升级前后数据摘要（只读 SQLite）

| 项 | 升级前 | 升级后 | 重启后 |
|---|---|---|---|
| schema 版本 | **7** | **8** | **8** |
| folders / videos / folder_items | 1 / 1 / 1 | 1 / 1 / 1 | 1 / 1 / 1 |
| analyses / plans | 0 / 0 | 0 / 0 | 0 / 0 |
| 关键 ID | `FIXTURE-FOLDER`, `BVFIXTURE0001` | 同左 | 同左 |
| `PRAGMA integrity_check` | — | **ok** | **ok** |
| `PRAGMA foreign_key_check` | — | **0 违规** | **0 违规** |
| analyses 列 | record_json, resource_key | + status, dependent_ids（v6 契约列已补齐） | 同左 |
| 库文件大小 | 126,976 | **184,320** | 184,320 |

### 未用空库冒充升级成功

- 升级后库 184,320 字节，远大于空库；内容与夹具行/ID 一致。
- 原库以 `library.sqlite3.recovery-20261021-033710-1d7cb41c` 保留在同目录（**recovery 副本存在**），未出现"空库替换"。
- `library.sqlite3.enc` 不存在（本次未启用保险库，符合合成 profile 预期）。

### 启动与重启

- 首次启动：`BiliFav Organizer.exe` 拉起随包冻结后端 `resources\backend\BiliFavOrganizer.exe --port 0 --no-browser --handshake`，`Uvicorn running on http://127.0.0.1:58107`，窗口就绪。
- 重启：关闭后重新启动（端口 56554），数据库保持 schema 8 与全部业务行/ID，`integrity_check=ok`，说明升级幂等、重启不破坏数据。
- 截图：`screenshots/T11b-installed-app-after-upgrade.png`、`screenshots/T11b-installed-app-after-restart.png`（安装版真实 UI）。
  说明：界面上业务计数显示 `-`，因为该 profile 的保险库尚未完成 onboarding（业务 API 在此之前被拒），属预期首启状态；升级数据本身已用只读 SQLite 全量核验。

### 隔离证明

`C:\BFO-UAT\prof-t11b` 内可见：`library.sqlite3`、`library.sqlite3.recovery-*`、`database-manifest.json`、`.bfo-profile.lock`、`.scheduler-owner.lock`、`config.json`、`secrets.json`、`token.blob`、`server.log`、`shell.log` 与 Electron `shell/` 缓存 —— **全部位于一次性 profile 内**。真实用户 profile `C:\Users\Martin-Dev\AppData\Local\BiliFavOrganizer` 最新修改时间仍为 **2026-09-26 00:02:08**，未被触碰。未使用真实 B 站账号/Cookie/恢复码/API Key。

## 3. 与 CI 的差异说明

CI 的 `Beta packaging validation`（run 39458669881）在合并树（== 候选 tree `e943cc30`）上构建了同名安装包（141,117,170 字节）并通过 packaged smoke，但**不上传 artifact**，故本轮安装验收使用本地同 SHA 构建件。两者目标版本号、配置、源码内容一致；体积差异来自构建机路径/时间戳等环境因素。

## 4. 门禁状态

| 门禁 | 状态 |
|---|---|
| T8 | PASS（330/330，0/0/0 @ 1981a94；CI 同 tree 330 OK） |
| T10 | PASS（14/14，39 图 @ 1981a94） |
| T11a | PASS（Draft PR #22，head 绑定候选，两 workflow success） |
| **T11b** | **PASS（本地同 SHA 安装包；CI 产物未上传的限制见第 3 节）** |
| T3 | PARTIAL（支持范围 schema 5–8 已批准；覆盖缺口如实记录，不单独阻断本候选） |
| T12 | NOT_RUN（等待维护者明确 GO） |

历史信息（不参与当前结论）：T9“发布保护缺失”曾记为 FAIL。维护者已将该检查从当前发布门禁中移除，因此不再作为本候选的阻断项，也不在门禁表内。原始只读核验记录保留在 `../20261020-windows-t10-uat/t9-github-protection-readonly.md`。


## 5. 证据清单

- `logs/electron-build.log.txt`、`logs/pyinstaller.log.txt` — 构建日志
- `snapshots.json` — 升级前/后/重启后只读快照
- `screenshots/T11b-installed-app-after-upgrade.png`、`…-after-restart.png`
- `sha256-manifest.txt` — 本目录文件哈希

安装包本体（141 MB）未入库，其 SHA-256 与字节数见第 1 节，可由同 SHA 复现构建得到同一配置产物。
