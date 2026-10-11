# 发布体系审计（构建 / 打包 / 运行时自更新）

- 审计日期：2026-10-11
- 审计范围：`.github/workflows/release-windows.yml`、`BiliFavOrganizer.spec`、
  `packaging/BiliFavOrganizer.iss`、`BiliFavOrganizer.ps1` / `.bat`、
  `server.py` 的自更新链路、`static/app.js` 的「检查更新」UI
- 方法：只读通读 + 跨文件比对；标 **[已复核]** 的条目由我另外亲手复现过
- 说明：文中的行号对应审计当日的工作区（`server.py` 等当时有未提交改动）

## 结论摘要

链路的**纪律**是好的：tag 格式与 README 更新日志双重门禁、便携包拒绝打包本地隐私文件、
zip 解压有 ZipSlip / 符号链接 / zip bomb 防护、下载支持 Range 续传且每轮全包 SHA256 校验、
dev 构建三重闸封死。问题集中在三处：

1. **三处现成可修的真 bug**（见 2.1）——**[已于 2026-10-11 全部修复并加回归测试]**
2. **Electron 壳上线前必须重构的三处模型冲突**（见 2.2）
3. **门禁缺测试 + 未签名 + 信任根同源等卫生问题**（见 2.3）——其中「CI 不跑测试」
   **[已于 2026-10-11 修复：新增 `.github/workflows/ci.yml`、发布流程内加同一道门禁、
   并修掉当时阻碍加门禁的 4 个红灯测试]**

| 级别 | 数量 | 主题 |
|---|---|---|
| 高 | 3 | 安装版临时目录永不清理、清道夫误删回滚副本、更新页「最新版本」死元素 |
| 高 | 3 | Electron 共存：重启错进程 / `--port` 语义失效 / 单进程同端口模型不兼容 |
| 中 | 4 | CI 不跑测试、未签名、安装标记错位、校验信任根同源 |
| 低 | 4 | 缺可写性预检、PowerShell 无降级、文档滞后、`package.json` 版本无注入 |

## 一、链路现状（事实）

### 1.1 构建与发布

- 触发：仅 `push` 到 tag `v*`；**无 `workflow_dispatch`、无 `pull_request` 触发**
  （`release-windows.yml:3-6`）→ PR 阶段零 CI。
- 8 步：checkout → setup-python 3.12 → `pip install -r requirements.txt pyinstaller` →
  **注入版本**（`:28-50`）→ `pyinstaller --noconfirm --clean BiliFavOrganizer.spec`（`:52-53`）→
  从 README 生成 Release 标题与摘要（`:55-79`）→ 便携 ZIP（`:81-105`）→ Inno 安装包（`:107-128`）→
  `gh release upload/create` 上传 4 个附件（`:130-149`）。
- 两条硬门禁：tag 必须匹配 `^v?\d+(\.\d+)*$`（`:34`）；README 的「更新日志」必须有该 tag 段落、
  且该段首行是加粗摘要，否则直接 `throw`（`:66-73`）。
- 版本号唯一来源是 tag → 替换 `server.py` 的 `BUILD_VERSION` / `BUILD_COMMIT` / `BUILD_DATE`
  （`:36-40`，仓库有测试断言三个标记各出现一次），同一个 tag 作为 `/DAppVersion` 传给 Inno（`:121`）。
- **不跑测试**；**不签名**（无 signtool / Authenticode / 证书 secrets）。
- 便携包**拒绝**打包本地隐私文件 `config.json` / `secrets.json` / `data` / `server.log` /
  `write_operations.jsonl`（`:87-88`）——这条做得对。
- 产物（4 个附件）：
  - `BiliFavOrganizer-Windows-x64-<tag>.zip` + `.sha256`（`:99-105`）
  - `BiliFavOrganizer-Setup-<tag>.exe` + `.sha256`（`:123-128`）
  - `.sha256` 内容格式为 `"<hash>  <文件名>"`，与更新器解析一致。
- PyInstaller 形态：**one-dir**（`EXE(exclude_binaries=True)` + `COLLECT`）、`console=True`、UPX 关闭、
  `static/` 作为 data、图标缺失即构建失败（`BiliFavOrganizer.spec:11,17-21,93-108`）。
- Inno：`DefaultDirName={autopf}\BiliFavOrganizer`、**默认免管理员**
  （`PrivilegesRequired=lowest` + `PrivilegesRequiredOverridesAllowed=dialog`）、
  开始菜单 + 可选桌面快捷方式、**没有开机自启项**、`[Registry]` 只写
  `HKCU\Software\Martin-soaring-dev\BiliFavOrganizer`（`InstallMode` / `InstallDir` / `Version` / `Scope`）、
  整目录递归复制、`OutputBaseFilename=BiliFavOrganizer-Setup-{#AppVersion}`
  （`packaging/BiliFavOrganizer.iss:41,45-46,51,66,69,74-76,79-83`）。
- `.bat` 是分发入口：便携分支直接跑 `BiliFavOrganizer.exe`；没有 exe 时才调 `.ps1`
  （`BiliFavOrganizer.bat:5-8,17`）。`.ps1` 是**源码运行的运行时准备**脚本（找/装 Python、建 venv、装依赖、
  最后 `--port 8080 --no-browser` 启动）。发布 ZIP 里只复制了 `.bat`，不含 `.ps1`。

### 1.2 运行时自更新

- 前端 `checkForUpdates`（`static/app.js:758-822`）：POST `/api/update/check` → 看
  `version_comparable` / `update_available` / `supports_self_update` / `install_mode` →
  `confirm()` → POST `/api/update/install` → 轮询 `/api/update/status` 与 `/api/version`，
  版本号变化后 `location.reload()`。
- 版本发现**不经 GitHub REST API**，走 `/releases/latest` 的 302 拿 tag（`server.py:5078-5097`），
  注释明确「不消耗配额」；全程无 token。
- URL / asset 名模板（`server.py:5100-5117`）：
  `https://github.com/{repo}/releases/download/{tag}/BiliFavOrganizer-Windows-x64-{tag}.zip`
  与 `BiliFavOrganizer-Setup-{tag}.exe`（各带 `.sha256`）——与 CI 产物一致。
- 按 `_installed_info()` 分派两条分支（`server.py:5648-5654`）：
  - **便携版**（`_run_update_worker`，`5354-5432`）：取期望 SHA256 → 下载（Range 续传 + 每轮全包校验）
    → 解压到 `stage/` 并校验 `_internal` → 写 helper 脚本 → `Popen powershell -ExecutionPolicy Bypass`
    → 置 `_SHUTDOWN` 请 uvicorn 退出 → 15s 兜底硬退。
    helper（`5155-5269`）：等父进程退出 → 旧目录 `Move-Item` 成 `{名}.previous-<guid>` → stage 上位
    → 把备份里新版本没有的文件拷回 → 成功则删备份；失败则把失败目录改名 `.failed-<guid>`、
    把 `.previous-*` 移回、并用旧 exe 重启；失败原因写 `%LOCALAPPDATA%\BiliFavOrganizer\update-error.json`。
    重启后用 `http://127.0.0.1:$Port/api/version` 健康检查、比对 `version` 字段（`:5213-5223`）。
  - **安装版**（`_run_installer_update_worker`，`5499-5562`）：从注册表取 `install_dir` / `scope` →
    下载 Setup → helper 用 `/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS` 静默安装
    （`all-users` 时加 `/ALLUSERS` 并以 `RunAs` 提权）→ 重启 exe。**覆盖与回滚完全交给 Inno**，
    自己不做备份；失败只把旧 exe 拉起来；**不做健康检查**。
- 校验：`.sha256` 里的 64 位十六进制（`5281-5288`）→ 下载每轮结束算整包 SHA256，匹配才返回，
  不匹配抛错且**不重试**（`5341-5345`）；期望值与载荷**同源同 TLS**，无独立信任根。
- 解压安全检查（`5125-5152`）：文件数 ≤10000、解压总量 ≤3GiB、拒绝 `..` / 绝对路径 / 盘符 /
  反斜杠 / 符号链接、路径越界检查、必须含 `BiliFavOrganizer.exe`；下载侧另有 2GiB 上限。
- `install_mode` 只看注册表（HKCU 或 HKLM 的 `InstallMode == "installed"`，`155-174`）；
  `supports_self_update = sys.frozen and os.name == "nt"`（`5576`）。两者互不校验，
  所以会出现「`installed` + `false`」这种组合（本机就是：曾装过安装版留下注册表键，
  但现在是从源码/壳里跑，`sys.frozen` 为 False）。真为 true 的只有 Release 里的便携 exe。

## 二、待修问题

### 2.1 现成可修（三条，[已复核]）

> 三条均已于 2026-10-11 修复，并各配了回归测试（`tests/test_update_flow.py`）。

1. **安装版更新后临时目录永不清理** ✅ 已修。helper 的 `param()` 没有 `$WorkDir`
   （`server.py:5435-5442`），worker 的 `Popen` 也没传 `-WorkDir`（`5533-5541`），
   但脚本内部两处在 `Remove-Item -LiteralPath $WorkDir`（`5473`、`5494`）→ 对 `$null` 操作抛错
   被 `catch {}` 吞掉。结果：每次安装版更新在 `%TEMP%\.bfo-setup-*` 留下约 25MB 的安装包，
   只能等下次启动、且超过 2 小时的清道夫扫掉。对照便携版 helper 的 `param()` 是**有** `$WorkDir` 的
   （`5155-5161`），属不对称实现缺口。
   修法：param 加 `[string]$WorkDir`，`Popen` 加 `-WorkDir`。
   回归测试：`test_installer_helper_gets_every_parameter_it_uses`（把调用方传的参数与脚本体
   对不上这类问题钉死——测法是把 Popen 参数抓下来逐个断言）。

2. **清道夫会删掉「回滚的唯一副本」** ✅ 已修。`_cleanup_stale_update_leftovers` 无条件清理
   `{安装目录名}.previous-*`（`server.py:128-141`，只按 2 小时判龄）。而便携更新是先
   `Move-Item 旧目录 → .previous-*`（`5194`）、再 `Move-Item stage → 安装目录`（`5196`）——
   若两步之间崩溃，旧版本只剩 `.previous-*` 一份；用户隔天才开应用，它已被当残留删除，
   回滚不再可能。修法（已实施）：安装目录不存在时跳过该 pattern，并打一条 warning。
   回归测试：`test_sweep_keeps_the_only_rollback_copy_when_install_dir_is_missing`
   （两条测试都验证过「把修复回退后必红」）。

3. **更新页的「最新版本」永远是「尚未检查」** ✅ 已修（前端三处接线：dev 构建 / 已是最新 / 发现新版本
   各写一次 `#about-latest`）。`index.html:1098` 有 `#about-latest`，
   但 `static/app.js` 全仓库无人写入（`checkForUpdates` 只写 `#update-status`）。
   修法：检查更新完成时同时写 `#about-latest`（含「已是最新」/「发现 x.y.z」）。

### 2.2 Electron 壳上线前必须重构（T9）

1. **会重启错进程**：更新助手等/杀的是**后端 pid**（`server.py:5401`、`5536`），重启的是**exe**
   （`5210`、`5252`、`5469`、`5480`）。将来 exe 是 Electron 主进程，它不在这套逻辑里；
   壳的单实例锁还会吞掉助手发起的那次启动，退化成「壳还在、后端已死」。
2. **`--port` 语义失效**：助手用 `Start-Process <exe> --port <旧端口>` 重启，而 Electron
   **不读 `--port`、只认 `BFO_PORT` 环境变量**（`electron/main.js:16-19`）→ 壳会以 `--port 0`
   起一个**随机新端口**的后端，而页面仍指向旧端口 → 白屏/断连；前端 `location.reload()`
   也只是回到失效的旧 URL。而且 `_APP_PORT` 在托管模式下已被改写成系统随机端口
   （`server.py:5743-5762`），把它硬编码进重启命令本身就是错的。
3. **模型不兼容**：便携分支建立在「单进程、固定端口、覆盖后重启同一 exe + `/api/version` 健康检查」
   之上，与壳的「主进程托管、动态端口、`BFO_READY` 握手」是两套模型（还牵扯 15s 硬退兜底、
   后端由壳拉起等）。切 electron-builder 时必须整体重做这段，而不是改几个参数。

### 2.3 卫生问题

- **CI 不跑测试** ✅ 已修：新增 `.github/workflows/ci.yml`（主分支 push + 每个 PR 跑
  `python -m unittest discover -s tests`），并在 `release-windows.yml` 的打包之前插入同一道门禁。
  仓库 179 个用例（含专门校验发布产物的 `tests/test_attribution.py`）现在拦得住回归。
  顺带修掉当时阻碍加门禁的 4 个红灯：根因是 `_dek_selftest()` 重新拼了一遍
  `store.DATA_DIR/"library.sqlite3.enc"`，而写入/回读都用模块变量 `VAULT_DB_ENC` ——
  自检会去读**真实数据目录**里那份属于别的 DEK 的旧快照。现在统一用 `VAULT_DB_ENC`
  （两处测试 fixture 也跟着换成 patch 该变量）。加门禁前全量 179 用例全绿。
- **完全不签名**：无 signtool / Authenticode / 证书 secrets。讽刺的是 `BiliFavOrganizer.ps1:64-67`
  会校验 Python 安装包的签名——链路上唯一未签名的可执行体反而是我们自己。
- **校验信任根同源**：`.sha256` 与载荷来自同一个 Release、同一次 TLS，没有 Authenticode / minisign
  等独立信任根；账号或仓库被投毒即可成对替换。安装版更危险（静默执行 + 可能 `RunAs` 提权）。
- **安装标记错位**：`_installed_info()` 同时读 HKCU 与 HKLM（`server.py:169`），而 Inno 只写 HKCU
  （`BiliFavOrganizer.iss:80`）。装了安装版的机器上再跑**便携 exe**，会被判成 `installed`
  并去静默更新**注册表里记录的目录**，而不是当前正在运行的目录。
- **缺可写性预检**：便携分支把工作目录建在 `APP_DIR.parent`（`5367`）并 `Move-Item` 整个安装目录，
  只挡了「磁盘根目录」与「缺 exe」（`5362-5365`），没有检查可写性 → `Program Files` 下会失败后回滚。
- **PowerShell 依赖无降级**：只靠 `-ExecutionPolicy Bypass`；企业策略禁用 Bypass / AppLocker /
  `powershell.exe` 不在 PATH 时更新直接不可用，没有「直接 Python 覆盖」的兜底路径。
- **文档滞后**：`docs/快速上手.md` 与随包 `packaging/使用说明.txt` 仍以 **v0.206** 为例
  （README 更新日志已到 v0.208）；`README.md` / `docs/project-overview.md` 的产物清单**漏了安装包**；
  `package.json` 的版本号本批已从 `"0.0.0"` 提到 `"0.209.0"`（Electron 时代它就是壳的版本号），
  但注入路径仍然只覆盖 `server.py` 的 `BUILD_VERSION`（`release-windows.yml:36-40`），
  两个版本号还是得手工对齐。
- 文档里「浏览器自动打开 `http://127.0.0.1:8080`」「请保留控制台窗口」两处在 Electron 上线后即失效
  （改为应用窗口 + 动态端口 + 无控制台），T9/T10 必须同步改。

### 已排除的怀疑

原以为 CI 会因 `Copy-Item output/pdf/快速上手.pdf`（`release-windows.yml:92`）找不到文件而失败——
`git ls-files` 确认该 PDF 已入库（1.47MB），这步不会失败。

## 三、优先级建议

1. ~~先修 2.1 的三条~~ ✅ 2026-10-11 完成（含回归测试）。
2. T9 开工前，把 2.2 的三条写进 T9 的验收条件（已同步到 `docs/compose/spec/desktop-shell.md`）。
3. ~~给 workflow 加一步跑测试、并考虑加 `pull_request` 触发~~ ✅ 2026-10-11 完成
   （新增独立 CI workflow；发布流程内也加了同一道门禁）。
4. 签名（Authenticode）与独立校验（如 minisign）属发布信任链，建议在 T9 一并决策。
5. 待办：`docs/快速上手.md` 仍以 v0.206 为例（README 更新日志已到 v0.208）；`package.json` 的版本号
   仍靠手改（注入路径只覆盖 `server.py` 的 `BUILD_VERSION`）。README / overview 的产物清单与随包
   `packaging/使用说明.txt` 本批已同步。

## 四、已知问题清单（本批评审结论，未修）

`9789144..299c71e` 这一批过了一轮独立评审（只读，未改文件）。除第 1 条是真 bug，其余都是
「本环境无法验证」或「行为可接受但与预期有出入」。一并记在这里，另起一批处理。

1. **`_close_credential_dialog()` 可能关掉别人的 Hello 验证框**（`server.py:992-1007`，真 bug）。
   它按窗口类名 `Credential Dialog Xaml Host` 取**第一个**顶层窗口，并无条件 `PostMessage(WM_CLOSE)`。
   该类名是系统凭据 broker 的通用对话框、不是我们的子窗口：另一个 BFO 实例（浏览器模式、第二份副本）
   正在验证，或系统/别的应用正在弹 Hello，都会被我们某次请求的收尾顺手关掉。
   修法方向（已与用户确认，本批不实施）：流程开始前先快照已存在的句柄，raiser 线程只认「新出现的」，
   收尾只关它自己记录的那一个 hwnd，并复核 `IsWindow` + 类名。
   实际风险边界：壳内这条基本是死代码（`electron/main.js:168-185` 已改为「最小化壳窗口让开」），
   浏览器模式下才真正暴露。
2. **导入 / 清除没有真实百分比、不可取消**（`static/app.js:5131,1471`）。导出本批已改成真百分比 +
   可取消，这两项仍是步骤式进度 → 任务书里「数据导入导出」那条按严格解读只算**部分达成**。
3. **顶栏在最小宽度下会裁掉右侧按钮**：`static/style.css:79-80` 的 `white-space: nowrap` +
   `overflow: hidden` 对 `minWidth: 940`（`electron/main.js:572`），内容约 900px 且右侧另留 154px
   系统按钮位 —— 窄窗口不折行，直接裁掉。
4. **设置里点 Hello 立即解绑、无二次确认**（`static/app.js:4717`）。base 的行为是「只提示」，
   本批改成直接解绑 —— 误点一次就掉了设备绑定。
5. **Esc 在弹窗打开时也会收起背后的设置面板**（`static/app.js:4477`）：只该关最上层那一个。
6. **`streamToFile` 没监听写流 `'error'`**（`electron/main.js:279`）：磁盘满 / 目录不可写会变成主进程
   未捕获异常。
7. **壳层冒烟新增的 `panelFlow` 没有 try/catch**（`tools/shell-smoke.mjs:120`）：元素缺失时脚本会崩，
   而不是判 FAIL。不会误报 PASS，只是失败信息不干净。
8. **neo 线还没有自动化入口**：只能手敲 `npx electron-builder`（无 npm script）、CI 无 neo 线、
   版本号靠手改 —— 当前那个安装包是手搓出来的。
9. **还剩两条结论缺证据**（不算缺陷，只是没有可复现的证据）：
   - 暗色下标题栏按钮的悬停/按下反馈需人眼确认 —— 它依赖 Chromium 在 `color:'#00000000'` 的 overlay
     上自绘高亮（`electron/main.js:543-548`）；
   - CI 门禁从未在干净机器或真实 PR 上跑过。179 绿取自开发机，而本机存在真实数据目录与 DPAPI 材料，
     `tests/test_vault_api.py` 历史上正因读真实快照而红 —— 门禁在 CI 上是否真绿尚未被证明。

   原先还有第三条「壳层冒烟跑在 `electron/main.js` 后端解析改动**之前**、HEAD 未复跑」，**已在本 HEAD
   上复跑证实**：`node tools/shell-smoke.mjs` → 17/17 PASS（页面来自动态端口而非写死的 8080、
   设置浮层与弹窗交互四项全过、窗口尺寸改到 1120×720 并已还原成 1200×800）。

## 附：关键位置速查

| 主题 | 位置 |
|---|---|
| 注册表安装标记 | `server.py:155,158-189` |
| `supports_self_update` | `server.py:5576,5602` |
| 版本发现（无 token） | `server.py:5078-5097` |
| URL / asset 名模板 | `server.py:5100-5117` |
| zip 解压安全检查 | `server.py:5125-5152` |
| 便携 helper（覆盖 / 回滚 / 重启） | `server.py:5155-5269` |
| SHA256 取期望值 | `server.py:5281-5288` |
| 下载 + 续传 + 校验 | `server.py:5291-5351` |
| 便携 worker | `server.py:5354-5432` |
| 安装版 helper（**缺 `$WorkDir`**） | `server.py:5435-5496`（缺陷点 `5473`、`5494`） |
| 安装版 worker（`Popen` 未传 `-WorkDir`） | `server.py:5526-5548` |
| `/api/update/*` 端点 | `server.py:5565-5680` |
| 端口来源 / 动态端口回写 | `server.py:103,5405,5539,5743-5762,5804` |
| 残留清理（会误删回滚副本） | `server.py:121-147` |
| 前端 `checkForUpdates` | `static/app.js:758-822` |
| 前端进度 / 取消 | `static/app.js:656-756` |
| `#about-latest` 死元素 | `static/index.html:1098` |
| Electron 端口模型（不读 `--port`） | `electron/main.js:16-19,469-470` |
| Electron 单实例锁 | `electron/main.js:590-602` |
| Inno 注册表 / 静默开关 | `packaging/BiliFavOrganizer.iss:45-46,58,80-83` |
| CI 产物与校验文件格式 | `.github/workflows/release-windows.yml:99-105,123-128` |
| 更新链路回归测试 | `tests/test_update_flow.py:64-94,147-190,286-343` |
