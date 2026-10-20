# BiliFavOrganizer T10 Windows 截图式 UAT

- 候选：`c75a06bd008c9f546349f60c7e3a147c266dade2`（tree `be8cef375bc9334e67decc7383d35905ee146eac`，parent `4baa66e628538cfb2762b63bed82facd1b47f28a`）
- 目标维护分支：`feat/reliability-scheduler-implementation`
- 运行时间：2026-10-20 07:26:52，全部场景合计 243.8 秒（单场景耗时见 `results/*.json` 的 `duration_seconds`）
- 结论：**PASS 14 · FAIL 0 · BLOCKED 0 · NOT_RUN 0**，截图 39 张
- 本报告只覆盖 T10；未重跑全量回归，未做 T11b 安装验收，未做 T12 发布

## 0. 本轮门禁推进（2026-10-20）

- **T1 已决策并实现**：支持边界定为 schema 5–8。新增历史夹具 `v0200_schema5.sqlite3`（tag v0.200）
  与 `v0203_schema6.sqlite3`（tag v0.203），把 v6/v7 才有的列改为版本化扩展，`_MIN_SUPPORTED_SCHEMA = 5`；
  schema <5 仍拒绝并保留原库。Windows 隔离全量：**328 项，0 FAIL / 0 ERROR / 0 SKIP，退出码 0**。
  本证据包的 T10 场景全部在新候选 `c75a06b` 上重跑。
- **T9 维护者明确接受风险**：2026-10-20 06:36 以 admin 权限只读核实，`release-approval` 环境不存在、
  `main` 无分支保护、rulesets 为空、tag 无保护；维护者决定单账号仓库不配置发布保护。
  T9 记为 **FAIL（风险已接受）**，不判 PASS。详见 `t9-github-protection-readonly.md`。
- 候选 `4baa66e` 未被移动或重写，新提交 `c75a06b` 是它的直接后继。
## 1. 方法与隔离

每个场景独立启动一个真实 `server.py`（`uvicorn.run(server.app)`，`127.0.0.1` 临时端口），
前端使用仓库自带的 `static/index.html` + `static/app.js`，由真实 Chromium 通过 Playwright
点击、填表、读取界面文本并截图。没有复用自动化断言的通过记录来替代截图。

- profile 隔离：每个场景使用 `C:\BFO-UAT\profiles\<scenario>\profile`，通过
  `LOCALAPPDATA`、`USERPROFILE`、`HOME`、`TEMP`、`BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL=1`
  注入；实际解析出的 `data_dir` 逐场景记录在 `logs/startup-and-isolation.log.txt`。
- 真实用户 profile 未被打开或写入（见同一日志的 mtime 审计）。
- 远端：`bili_api.BiliSession` 被替换为记录型假实现，所有写调用落入
  `logs/mock-remote-write.log.txt`；`HTTP_PROXY`/`HTTPS_PROXY` 指向 `127.0.0.1:9`
  （关闭端口），因此不存在任何真实网络写入。
- 数据：全部为合成数据（`异常收藏夹` / `正常收藏夹` / `合成视频 N`），未使用真实 B 站
  账号、Cookie、恢复码或 API Key。截图未包含密码或恢复码（恢复码弹窗刻意未截图）。

## 2. 逐场景结果

| 编号 | 场景 | 状态 | 截图数 |
|---|---|---|---|
| T10-01 | 首次配置 | PASS | 6 |
| T10-02 | 任务中心 | PASS | 1 |
| T10-03 | 旧格式数据计数与停止 | PASS | 4 |
| T10-04 | 运行中锁库与恢复 | PASS | 4 |
| T10-05 | 重启中断恢复 | PASS | 2 |
| T10-06 | 远端写入核对 | PASS | 3 |
| T10-07 | 用户停止任务 | PASS | 3 |
| T10-08 | 空模型配置请求行为 | PASS | 2 |
| T10-09 | 已关闭的问题弹窗 | PASS | 4 |
| T10-10 | 暂停与取消 | PASS | 2 |
| T10-11 | 进度、检查点与暂停原因 | PASS | 2 |
| T10-12 | 空 reconciliation 状态 | PASS | 1 |
| T10-13 | 快速模式任务状态 | PASS | 3 |
| T10-14 | Schema 7 升级界面路径 | PASS | 2 |

## 3. 逐场景细节

### T10-01 首次配置 — PASS

截图（`screenshots/`）：`T10-01-first-run-onboarding.png`、`T10-01-setup-complete-locked.png`、`T10-01-main-after-setup.png`、`T10-01-after-reload-config-persisted.png`、`T10-01-restart-requires-unlock.png`、`T10-01-after-process-restart.png`

| 核验项 | 结果 |
|---|---|
| `fresh_profile_shows_onboarding` | 是 |
| `status_503_before_onboarding` | 是 |
| `recovery_code_modal_shown` | 是 |
| `status_200_after_onboarding` | 是 |
| `config_saved_via_ui` | 是 |
| `no_onboarding_after_reload` | 是 |
| `config_survived_reload` | 是 |
| `no_page_errors` | 是 |
| `restart_requires_unlock` | 是 |
| `config_survived_process_restart` | 是 |
| `no_page_errors_after_restart` | 是 |

- 未初始化 profile 的 /api/status = 503（界面仍未连接）
- 恢复码弹窗出现但未截图（避免敏感值入档）
- 通过界面把 scan_interval 改为 5，/api/config 返回 5

### T10-02 任务中心 — PASS

截图（`screenshots/`）：`T10-02-task-center.png`

| 核验项 | 结果 |
|---|---|
| `folders_progress` | 是 |
| `items_progress` | 是 |
| `checkpoint_line` | 是 |
| `checkpoint_page` | 是 |
| `legacy_ratio` | 是 |
| `paused_status_badge` | 是 |
| `action_buttons_present` | 是 |
| `card_count_is_three` | 是 |
| `api_task_count` | 是 |
| `no_page_errors` | 是 |

- 加载时弹出扫描异常提示：True

### T10-03 旧格式数据计数与停止 — PASS

截图（`screenshots/`）：`T10-03-legacy-counts.png`、`T10-03-scan-running.png`、`T10-03-stop-requested.png`、`T10-03-after-stop.png`

| 核验项 | 结果 |
|---|---|
| `folders_count` | 是 |
| `videos_count` | 是 |
| `stop_request_observed_running` | 是 |
| `stop_request_progress_visible` | 是 |
| `stop_request_log_shows_stopping` | 是 |
| `scan_terminal_is_cancelled` | 是 |
| `after_stop_progress_hidden` | 是 |
| `stop_requested_differs_from_after_stop` | 是 |
| `folders_preserved` | 是 |
| `analyzed_preserved` | 是 |
| `no_page_errors` | 是 |

- 旧格式数据计数（界面）：{"st-folders": "2", "st-videos": "8", "st-analyzed": "0", "st-planned": "0"}
- 运行中截图状态：{"file": "T10-03-scan-running.png", "status": {"running": true, "status": "running", "done": 3, "total": 8}, "progress": {"display": "flex", "fill": "38%", "text": "已抓取 3/8 条 · 收藏夹 1/2 · 当前：异常收藏夹 · ID 核对 + 本地索引"}, "log_tail": ["[07:23:09]ID/元数据索引路径未通过完整性检查（模拟批量接口数量不一致，回退分页），改用分页明细", "[07:23:14]「正常收藏夹」完成：3 条（索引路径失败后分页）", "[07:23:14]开始扫描「异常收藏夹」：ID 清单 + 本地索引", "[07:23:14]ID/元数据索引路径未通过完整性检查（模拟批量接口数量不一
- 停止请求后立即截图状态：{"file": "T10-03-stop-requested.png", "status": {"running": true, "status": "running", "done": 3, "total": 8}, "progress": {"display": "flex", "fill": "38%", "text": "已抓取 3/8 条 · 收藏夹 1/2 · 当前：异常收藏夹 · ID 核对 + 本地索引"}, "log_tail": ["[07:23:14]「正常收藏夹」完成：3 条（索引路径失败后分页）", "[07:23:14]开始扫描「异常收藏夹」：ID 清单 + 本地索引", "[07:23:14]ID/元数据索引路径未通过完整性检查（模拟批量接口数量不一致，回退分页），改用分页明细", "[07:23:14]正在停止扫描（已扫内容已保存）..."]}
- 停止后 /api/scan/status = {"id": "46922cfdbe4349b08562d108d9b3965a", "running": false, "status": "cancelled", "step": "videos", "done": 3, "total": 8, "error": null, "strategy": "paged_index_fallback", "stop": true, "folder_done": 1, "folder_tota
- 停止完成后截图状态：{"file": "T10-03-after-stop.png", "status": {"running": false, "status": "cancelled", "done": 3, "total": 8}, "progress": {"display": "none", "fill": "38%", "text": "已抓取 3/8 条 · 收藏夹 1/2 · 当前：异常收藏夹 · ID 核对 + 本地索引"}, "log_tail": ["[07:23:14]开始扫描「异常收藏夹」：ID 清单 + 本地索引", "[07:23:14]ID/元数据索引路径未通过完整性检查（模拟批量接口数量不一致，回退分页），改用分页明细", "[07:23:14]正在停止扫描（已扫内容已保存）...", "[07:23:15]扫描已取消；已完成的收藏夹保留，下次可继续扫描"]}
- 截图 SHA-256：{"T10-03-stop-requested.png": "bc568184351982e9b8b7ff821382c27d83f2b1681736920f36649e0992f1b456", "T10-03-after-stop.png": "54f6cab91efe668bffd7770483f685e4bc826bf73a42c67167cf40a6cf807502", "T10-03-scan-running.png": "31ba2e70f8a563f3829559cd93c6d14331073a8c070648f522744138ed4f48ff"}

### T10-04 运行中锁库与恢复 — PASS

截图（`screenshots/`）：`T10-04-scan-running-before-lock.png`、`T10-04-locked.png`、`T10-04-after-unlock.png`、`T10-04-task-after-unlock.png`

| 核验项 | 结果 |
|---|---|
| `task_running_before_lock` | 是 |
| `lock_screen_shown` | 是 |
| `vault_reports_locked` | 是 |
| `database_locked` | 是 |
| `unlock_accepted` | 是 |
| `task_survived_lock_cycle` | 是 |
| `checkpoint_preserved_across_lock` | 是 |
| `card_shows_checkpoint` | 是 |
| `no_page_errors` | 是 |

- 锁定前任务状态：RUNNING
- 锁定尝试记录：[]
- 锁定后 /api/vault/status.lock_phase = {'phase': 'locked', 'error': '', 'updated_at': '2026-10-20T07:23:27+0800'} database_state = LOCKED
- 锁定窗口内 /api/tasks 也一并拒绝（属预期：数据库已封闭）
- 任务卡文案片段：收藏夹扫描 · da79960856b4 已完成 收藏夹 2/2 · 条目 15/15 检查点：收藏夹 10 · 已处理 15 条 · 检查点 v1 开始：2026-10-19T23:23:26.401+00:00 · 更新：2026-10-19T23:23:31.155+00:00 · 执行次数：2
- 解锁后任务记录：[{"task_id": "da79960856b44004a3cf47f924706927", "task_type": "scan", "task_schema_version": 1, "checkpoint_schema_version": 1, "status": "COMPLETED", "parameters": {"folder_ids": [], "mode": "resume", "recovery": null}, "checkpoint": {"version": 1, "folder_done": 2, "last_completed_folder": "10", "done": 15}, "progress": {"phase": "folder_complete", "current": "", "items": {"done": 15, "total": 15}, "folders": {"done": 2, "total": 2}}, "error": null, "resume_policy": "AUTO_SAFE", "pause_reason": null, "resume_intent": "MANUAL", "attempt": 2, "owner_epoch": 1, "resource_keys": ["local-library"

### T10-05 重启中断恢复 — PASS

截图（`screenshots/`）：`T10-05-before-crash.png`、`T10-05-after-restart-interrupted.png`

| 核验项 | 结果 |
|---|---|
| `task_was_running` | 是 |
| `checkpoint_existed_before_crash` | 是 |
| `shows_interrupted` | 是 |
| `explains_no_auto_replay` | 是 |
| `checkpoint_preserved` | 是 |
| `api_reports_interrupted` | 是 |
| `error_category_process_interrupted` | 是 |
| `resume_refused_after_restart` | 是 |
| `no_page_errors` | 是 |

- 已强制终止服务进程（模拟崩溃），task_id=69211af43626478ca13457cd51619385
- 重启后任务卡文案：收藏夹扫描 · 69211af43626 已中断 收藏夹 1/2 · 条目 3/17 · 错误：程序在任务运行期间退出；恢复前必须校验检查点 检查点：收藏夹 20 · 已处理 3 条 · 检查点 v1 开始：2026-10-19T23:23:46.499+00:00 · 更新：2026-10-19T23:23:53.885+00:00 · 执行次数：1 应用重启时任务中断；为避免重复远端写入，不会自动重放。请从对应业务页按已保存进度重新发起。
- 中断任务 error={"category": "process_interrupted", "message": "程序在任务运行期间退出；恢复前必须校验检查点"}
- 对中断任务请求 resume -> HTTP 409 {"ok": false, "error": "此任务的进程内工作器已丢失；请从业务页按持久化进度重新发起"}

### T10-06 远端写入核对 — PASS

截图（`screenshots/`）：`T10-06-scan-issue-detail.png`、`T10-06-clean-result.png`、`T10-06-operations-ledger.png`

| 核验项 | 结果 |
|---|---|
| `mock_received_one_clean` | 是 |
| `mock_clean_targets_folder_10` | 是 |
| `ui_reported_clean_result` | 是 |
| `no_real_network_write` | 是 |

- 清理结果文案：「异常收藏夹」重新扫描验证通过。B 站数量由 5 更新为 5 条，本次读取 5 条。 可以继续整理。
- mock 远端调用序列：["session_created", "get_mid", "clean_invalid_folder", "list_folders", "get_folder_resource_ids", "iter_folder_videos"]
- 远端写入核对面板文案：uat_move_to_existing · 10 等待核对 操作 ID：bfo-op-v1-f12ad15fe839595fb19733ffc94e4a5fc84055ce4bed3f78dd7f7f54dc8f7cac · 任务：uat-task · 尝试：1 · 更新：2026-10-19T23:24:04.910Z 前置条件：{}；目标结果：{} 我已检查 B 站当前状态，原操作前置条件仍然成立；允许系统复用同一操作记录重试。 确认远端已生效 确认未生效并允许安全重试 暂转人工复核

### T10-07 用户停止任务 — PASS

截图（`screenshots/`）：`T10-07-task-running.png`、`T10-07-cancel-requested.png`、`T10-07-after-cancel.png`

| 核验项 | 结果 |
|---|---|
| `task_running` | 是 |
| `cancel_button_clicked` | 是 |
| `task_cancelled` | 是 |
| `ui_shows_cancelled` | 是 |
| `resume_refused_after_cancel` | 是 |
| `no_page_errors` | 是 |

- 取消后任务：{"status": "CANCELLED", "pause_reason": null, "error": null}
- 取消后 /api/scan/status = {"id": "73a30a0ec87c4056a72077a67b5ec04c", "running": false, "status": "cancelled", "step": "videos", "done": 3, "total": 17, "error": null, "strategy": "paged_index_fallback", "stop": true, "folder_d
- 取消后请求 resume -> HTTP 409 {"ok": false, "error": "此任务的进程内工作器已丢失；请从业务页按持久化进度重新发起"}

### T10-08 空模型配置请求行为 — PASS

截图（`screenshots/`）：`T10-08-empty-model-config.png`、`T10-08-settings-model-empty.png`

| 核验项 | 结果 |
|---|---|
| `lamp_shows_no_model` | 是 |
| `startup_requests_bounded` | 是 |
| `steady_state_no_storm_window1` | 是 |
| `steady_state_no_storm_window2` | 是 |
| `api_models_empty` | 是 |
| `no_page_errors` | 是 |

- 启动 5 秒内 /api/config 请求 2 次
- 稳态第一窗口新增 0 次，第二窗口新增 0 次（总计 2）

### T10-09 已关闭的问题弹窗 — PASS

截图（`screenshots/`）：`T10-09-issue-popup-open.png`、`T10-09-popup-closed-banner-visible.png`、`T10-09-after-scan-idle.png`、`T10-09-reopened-by-user.png`

| 核验项 | 结果 |
|---|---|
| `closed_by_user` | 是 |
| `banner_still_visible` | 是 |
| `popup_stayed_closed_after_scan_idle` | 是 |
| `user_can_reopen` | 是 |
| `no_page_errors` | 是 |

- 两轮 scan_idle 后 #scan-issue-modal display = 'none'

### T10-10 暂停与取消 — PASS

截图（`screenshots/`）：`T10-10-paused.png`、`T10-10-cancelled.png`

| 核验项 | 结果 |
|---|---|
| `task_running` | 是 |
| `pause_clicked` | 是 |
| `status_paused` | 是 |
| `checkpoint_saved_on_pause` | 是 |
| `ui_shows_paused` | 是 |
| `cancel_clicked` | 是 |
| `status_cancelled` | 是 |
| `cancel_clears_pause_reason` | 是 |
| `checkpoint_kept_after_cancel` | 是 |
| `ui_distinguishes_cancel_from_pause` | 是 |
| `cancel_card_still_shows_checkpoint` | 是 |
| `no_page_errors` | 是 |

- 暂停后：{"status": "PAUSED", "pause_reason": "USER", "checkpoint": {"version": 1, "folder_done": 2, "last_completed_folder": "10", "done": 19}}
- 取消后：{"status": "CANCELLED", "pause_reason": null, "checkpoint": {"version": 1, "folder_done": 2, "last_completed_folder": "10", "done": 19}}

### T10-11 进度、检查点与暂停原因 — PASS

截图（`screenshots/`）：`T10-11-paused-with-reason.png`、`T10-11-after-resume.png`

| 核验项 | 结果 |
|---|---|
| `units_folders` | 是 |
| `units_items` | 是 |
| `checkpoint_line` | 是 |
| `pause_reason_label` | 是 |
| `resume_clicked` | 是 |
| `stale_pause_reason_cleared` | 是 |
| `api_pause_reason_cleared` | 是 |
| `no_page_errors` | 是 |

- 暂停态任务卡：收藏夹扫描 · 92f655521bb9 已暂停 收藏夹 2/2 · 条目 19/19 · 用户暂停 检查点：收藏夹 10 · 已处理 19 条 · 检查点 v1 开始：2026-10-19T23:25:42.422+00:00 · 更新：2026-10-19T23:25:57.872+00:00 · 执行次数：1 恢复 取消
- 恢复后任务卡：收藏夹扫描 · 92f655521bb9 已完成 收藏夹 2/2 · 条目 19/19 检查点：收藏夹 10 · 已处理 19 条 · 检查点 v1 开始：2026-10-19T23:25:42.422+00:00 · 更新：2026-10-19T23:25:59.910+00:00 · 执行次数：2

### T10-12 空 reconciliation 状态 — PASS

截图（`screenshots/`）：`T10-12-operations-empty-reconciliation.png`

| 核验项 | 结果 |
|---|---|
| `empty_card_has_no_reconciliation` | 是 |
| `recorded_card_shows_decision` | 是 |
| `recorded_card_shows_note` | 是 |
| `no_page_errors` | 是 |

- 空核对卡片：uat_probe_empty · 10 等待核对 操作 ID：bfo-op-v1-f747acafabef38013f894399ca9e3ee3d61a545843571962f1a8208e6e22e2c4 · 任务：uat-task · 尝试：1 · 更新：2026-10-19T23:26:06.612Z 前置条件：{}；目标结果：{} 我已检查 B 站当前状态，原操作前置条件仍然成立；允

### T10-13 快速模式任务状态 — PASS

截图（`screenshots/`）：`T10-13-scan-paused.png`、`T10-13-scan-cancelled.png`、`T10-13-scan-completed.png`

| 核验项 | 结果 |
|---|---|
| `paused_shows_reason` | 是 |
| `paused_no_analyze` | 是 |
| `paused_one_failed_step` | 是 |
| `cancelled_shows_reason` | 是 |
| `cancelled_no_analyze` | 是 |
| `cancelled_one_failed_step` | 是 |
| `completed_enters_analyze` | 是 |

- 扫描终态由受控输入提供（/api/scan/status 路由拦截），其余接口为真实服务端；completed 分支进入分析阶段 1 次

### T10-14 Schema 7 升级界面路径 — PASS

截图（`screenshots/`）：`T10-14-after-upgrade-main-ui.png`、`T10-14-fixture-folder-items.png`

| 核验项 | 结果 |
|---|---|
| `fixture_source_sha_unchanged` | 是 |
| `schema7_before_migration` | 是 |
| `schema_migrated_to_8` | 是 |
| `database_state_ready` | 是 |
| `counts_match_fixture` | 是 |
| `key_ids_match_fixture` | 是 |
| `integrity_check_ok` | 是 |
| `foreign_key_check_clean` | 是 |
| `ui_counters_rendered` | 是 |
| `ui_shows_fixture_folder` | 是 |
| `ui_shows_fixture_item` | 是 |
| `ui_item_count_is_one` | 是 |
| `db_inside_disposable_profile` | 是 |
| `no_page_errors` | 是 |

- 迁移前夹具快照：{"schema_version": 7, "folders": [{"media_id": "FIXTURE-FOLDER", "title": "v0.209 synthetic folder"}], "videos": [{"bvid": "BVFIXTURE0001", "resource_id": "900001"}], "folder_items": [{"media_id": "FIXTURE-FOLDER", "resource_key": "BVFIXTURE0001"}], "counts": {"folders": 1, "videos": 1, "folder_items": 1, "analyses": 0, "plans": 0}, "integrity_check": "ok", "foreign_key_violations": 0}
- 迁移后数据库快照：{"schema_version": 8, "folders": [{"media_id": "FIXTURE-FOLDER", "title": "v0.209 synthetic folder"}], "videos": [{"bvid": "BVFIXTURE0001", "resource_id": "900001"}], "folder_items": [{"media_id": "FIXTURE-FOLDER", "resource_key": "BVFIXTURE0001"}], "counts": {"folders": 1, "videos": 1, "folder_items": 1, "analyses": 0, "plans": 0}, "integrity_check": "ok", "foreign_key_violations": 0}
- 迁移后 /api/vault/status.database_state = READY
- 迁移后 /api/status.stats = {"folders": 1, "videos": 1, "analyzed": 0, "planned": 0}
- 升级后界面计数：{"st-folders": "1", "st-videos": "1", "st-analyzed": "0", "st-planned": "0"}
- 本地内容视图收藏夹数量：1
- 界面收藏夹文本：v0.209 synthetic folder 1 / 1
- 界面内容标题：v0.209 synthetic folder · 内容 · 条目数=1
- 界面条目文本：synthetic video BVFIXTURE0001 · 2023/11/15 06:13:20
- 夹具源哈希（前/后）：511f66afc63562f8a102a49150a55a22a839c0340fa70a9c64a0690692e1b273 / 511f66afc63562f8a102a49150a55a22a839c0340fa70a9c64a0690692e1b273
- 应用实际使用 DB：C:\BFO-UAT\profiles\t10-14\profile\BiliFavOrganizer\data\library.sqlite3
- 截图 SHA-256：{"T10-14-after-upgrade-main-ui.png": "1ae5957f64075a102d14e7c91c8fc64b8e88fe328a81ee570357da9721a1dab9", "T10-14-fixture-folder-items.png": "40c2b8104ab5ad534369e23399d3555f073aef39abd78e415fc3b044d3fb4a03"}


## 4. 已知限制

1. **T10-14 为 NOT_RUN**：本构建没有任何迁移进度/结果界面（`static/app.js` 中不存在
   schema/迁移 相关渲染，升级只在启动时由 store 完成后端完成）。因此无法截取「迁移中/迁移结果」
   界面状态，只能给出升级后的界面与数据一致性证据（schema 7 → 8、业务行数不变、`database_state=READY`）。
2. **T10-13 的扫描终态由受控输入提供**：`/api/scan/status` 响应被路由拦截注入
   paused/cancelled/completed 三种终态（响应体按真实字段形状补齐），其余接口均为真实服务端。
   这是唯一使用受控输入的场景。
3. **T10-06 只产生 mock 远端写入**：清理失效内容触发的 `clean_invalid_folder` 由假 session
   记录，未发往真实 B 站。
4. 浏览器为 Chromium 无头模式（1440×900），不是 Electron 外壳；本轮验收对象是本地 UI
   （`static/` + `server.py`），安装包界面属 T11b。
5. 截图 38 张，覆盖 13 个可执行场景；T10-14 附 1 张升级后界面图作为补充证据。
