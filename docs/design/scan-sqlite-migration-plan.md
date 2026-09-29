# 收藏夹扫描改造与 SQLite 迁移方案

状态：首版已实施；接口边界仍待扩展实测

范围：新版收藏夹扫描流程、扫描结果存储、现有 JSON 数据迁移。

暂不包含：内容自动归类策略调整、收藏夹合并/重命名/删除流程、任何无人审核的 B 站写操作。

> 实际持久化位置、目录 active/archive 生命周期、视频索引、画像完整性门槛和内容批次上下文预检以[持久视频索引与收藏夹画像](persistent-index-folder-profiles.md)为准。本文件保留扫描策略的原始设计背景。

## 1. 目标与约束

1. 网页端为每个收藏夹选择合适的读取方式：小于等于保守候选阈值时，优先尝试“ID 清单 + 批量元数据”；超出阈值、计数不符或批量查询不完整时，回退到分页明细接口。候选阈值须经实测确认后启用。
2. 扫描数据按收藏夹保存权威成员关系，支持断点续扫、强制重扫、取消和失败恢复。
3. 以 SQLite 作为运行时的权威存储，减少大 JSON 文件反复重写，并通过事务维护视频、收藏夹、分析和任务状态的一致性。
4. 现有 JSON 导入/导出继续可用；迁移失败时可回到迁移前数据。
5. 遵守 B 站网页接口的实际边界；所有扫描请求默认至少间隔 2 秒，发现风控立即停止，不并发加压。

## 2. 接口调查结论与实施边界

项目当前使用的收藏夹明细接口为分页读取，单页上限 20，响应提供 `medias` 和 `has_more`。项目代码已固定每页 20 条。

本项目对账号中的「娱乐搞笑」夹做过只读实测：目录 `media_count=748`；ID 清单接口返回 748 个 ID；把全部 ID 一次传给批量元数据接口返回 748 条元数据，`(id,type)` 集合完全一致。分页接口第一页 20 条也全部能在该 ID 清单中找到。测试请求间隔为 2 秒。

这只能证明该夹的 748 条可以批量读取，不能证明批量接口的 1000 条边界，也不能覆盖更大的收藏夹。项目 2026-09-22 的历史账号探测记录显示，ID 清单接口对一个约 8493 条的收藏夹只返回 1000 条。它没有已知分页参数，因此**不能作为大夹的完整来源**。社区接口笔记并非 B 站稳定性承诺。

据此，首版规则：

- 当目录数量 `media_count <= 1000` 时可尝试 ID + 批量元数据路径；`1000` 暂作保守候选阈值，不宣称已经验证了 1000 条请求。
- 当 `media_count > 1000` 时直接使用分页明细接口扫描，不调用 ID 接口来猜测或补齐余下内容。
- 任一路径只要发现数量不符、重复 ID、返回字段缺失、接口报错或元数据缺项，就不得标记该收藏夹扫描完成；转用分页路径重建完整快照。
- 具体批量请求的最大安全条数和 GET URL 长度限制，实施前以只读探测逐步验证；每次请求间隔至少 2 秒。未验证前，不将 1000 条作为单次批量元数据接口请求上限。

## 3. 网页端新版扫描体验

### 3.1 操作流程

1. 用户点击“刷新收藏夹目录”，单独刷新目录及数量，不自动扫描内容。
2. “选择扫描范围”展示每夹名称、远端数量、本地数量、上次扫描时间、扫描状态和建议读取方式。
3. 用户选择“继续扫描”或“重扫所选夹”。默认只补齐未完成/计数落后的夹；重扫需要显式选择。
4. 后端为每夹选择读取策略，前端展示当前夹、读取策略、已取得/预计条数、请求数、耗时、跳过/失效/缺失元数据数和最近事件。
5. 用户可停止任务。每个已确认页面或批次立即提交 SQLite；停止时当前夹标为 `partial`，下次继续。
6. 只有完成性校验通过后，才把该夹快照标成 `complete`，并唤醒后续分析任务。

### 3.2 读取策略状态机

对每个用户选中的收藏夹单独执行：

```text
刷新目录
  ├─ media_count > 1000 ───────────────> 分页明细
  └─ media_count <= 1000 ─> 读取 IDs
                                 ├─ ID 数量/唯一性不合格 ─> 分页明细
                                 └─ 查询 infos（按已验证请求大小分批）
                                          ├─ 全部 ID 有元数据 ─> 完成性校验
                                          └─ 不完整/失败 ──────> 分页明细

分页明细：按 pn 递增，ps=20，以 has_more 为终止条件
完成性校验：校验 ID 集合、资源类型、目录计数差异及读取期间目录变化
  ├─ 通过 ─> 原子替换该夹成员关系，标记 complete
  └─ 不通过 ─> 标记 partial/inconsistent，不覆盖此前完整快照
```

批量接口的缺项只能按 `(id,type)` 对齐，不能只按数组位置对齐。视频资源 `type=2` 可映射 `bvid`；其他资源类型须保留原始 `id/type`，暂不支持的项目不应被静默丢弃。

`media_count` 是目录快照计数，扫描时可能发生变化；失效内容、特殊资源和并发收藏/取消也可能造成差异。计数不一致时应显示具体差异并保留上次完整快照，不以计数差异本身自动判定内容已删除。

### 3.3 页面状态和断点

- 总进度按“已完成收藏夹/所选收藏夹”与“已确认内容/当前夹预估内容”分开显示；扫描过程中总数动态变化时，不显示误导性的全局固定百分比。
- 读取方式显示为“批量元数据”或“分页明细”，同时显示已完成请求数，便于比较实际提速。
- `complete`、`partial`、`failed`、`stale`、`inconsistent` 分状态呈现；用户刷新页面后从后端恢复运行状态。
- 连续 LLM 分析只能在所有有内容的 active 收藏夹都有当前完整画像时启动；任何扫描开始都会让对应画像门槛暂时失效，需扫描完成并重建画像后才能继续归类。

### 3.4 与当前实现的衔接

当前 `POST /api/scan`、`GET /api/scan/tree` 和前端扫描控件可先保持兼容，后端内部改为创建 `scan_run` 并按夹调度策略。后续稳定后再调整 API 字段。旧的 `scan_done.json` 迁移为每夹扫描状态；不再仅凭一个完成 ID 跳过所有变化。

## 4. SQLite 数据模型

数据库文件建议为 `data/app.sqlite3`，只在本机使用。连接初始化时启用外键、WAL、合理的 `busy_timeout`，每次写入使用短事务。按 schema 版本执行迁移，版本记录在 `schema_migrations`。

### 4.1 核心表

```sql
folders (
  media_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  remote_count INTEGER NOT NULL DEFAULT 0,
  intro TEXT NOT NULL DEFAULT '',
  cover TEXT NOT NULL DEFAULT '',
  is_default INTEGER,
  remote_updated_at TEXT,
  updated_at TEXT NOT NULL
)

videos (
  resource_key TEXT PRIMARY KEY, -- 优先用 bvid；无 bvid 时用 type:id
  resource_id TEXT NOT NULL,
  resource_type INTEGER NOT NULL,
  bvid TEXT,
  aid TEXT,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  upper_name TEXT NOT NULL DEFAULT '',
  upper_mid TEXT NOT NULL DEFAULT '',
  duration INTEGER,
  pubtime INTEGER,
  raw_attr INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL
)

folder_items (
  media_id TEXT NOT NULL REFERENCES folders(media_id),
  resource_key TEXT NOT NULL REFERENCES videos(resource_key),
  fav_time INTEGER,
  scan_run_id TEXT,
  PRIMARY KEY (media_id, resource_key)
)

folder_scan_state (
  media_id TEXT PRIMARY KEY REFERENCES folders(media_id),
  status TEXT NOT NULL, -- never/partial/complete/failed/stale/inconsistent
  strategy TEXT,
  expected_count INTEGER,
  fetched_count INTEGER NOT NULL DEFAULT 0,
  cursor_page INTEGER,
  snapshot_started_at TEXT,
  snapshot_completed_at TEXT,
  last_error TEXT
)

analysis (
  resource_key TEXT PRIMARY KEY REFERENCES videos(resource_key),
  action TEXT,
  recommended TEXT,
  reason TEXT,
  confidence REAL,
  model TEXT,
  analyzed_at TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}'
)
```

另保留 `scan_runs`、`plans`、`plan_items`、`apply_runs`、`apply_items`、`folder_profiles`、`folder_merge_*` 等表/记录，用于当前已有功能。执行状态字段按现状迁移，特别保留 `done`、`unknown`、`failed`、`partial`，不能因普通编辑方案而重置不确定状态。

建议索引：`folder_items(resource_key)`、`videos(bvid)`、`videos(resource_type,resource_id)`、`analysis(action,recommended)`、`scan_runs(status,started_at)`。收藏夹成员关系以 `folder_items` 为准；`videos` 保存跨夹共享的资源元数据。

### 4.2 扫描事务与快照策略

- 单页或单个已返回的元数据批次使用短事务写入，避免扫描期间长时间锁表。
- 重扫期间写入本轮暂存成员（带 `scan_run_id`），不立即删除旧完整成员关系。
- 完成并校验成功后，在一个事务内替换对应夹的 `folder_items` 并将状态标为 `complete`。
- 停止或失败时保留旧完整快照和本轮已取得数据；本轮仅标 `partial`，恢复时从安全检查点继续或重启该夹。不要将不完整结果冒充新快照。
- 视频出现在多个收藏夹时，仅写一份 `videos` 元数据和多条 `folder_items` 关系。

## 5. JSON 到 SQLite 的可执行迁移步骤

### 阶段 A：盘点和备份

1. 停止扫描、分析、执行和合并任务。
2. 备份整个 `data/`，包括 JSON、JSONL、隐藏/旧任务状态文件；保留原文件不覆盖。
3. 记录迁移前各数据集条目数、视频唯一键数、成员关系数、方案状态分布和文件校验值。
4. 明确 `write_operations.jsonl` 作为审计日志迁移后的保留位置与读取方式；不得只迁移统计摘要而丢弃逐条记录。

### 阶段 B：实现可重复的导入器

1. 新增版本化 schema 和事务型 SQLite store，实现与现有 `store.py` 相同语义的读写接口。
2. 编写只读 JSON 导入器，将现有文件转换为规范化表；保持 BVID、文件夹 ID、任务 ID 为字符串，避免大整数或缺失类型造成键变化。
3. `videos.json` 中旧格式的 `source_folder_id` 与 `folder_ids` 合并成 `folder_items`；没有 `folder_ids` 时回退到旧 `source_folder_id`。
4. 将 `scan_done.json` 转为 `folder_scan_state`；缺少可验证快照覆盖信息的旧 `done` 只能作为兼容状态，标记为需校验，不能直接宣称新快照完整。
5. 迁移分析、方案、执行状态、收藏夹合并草稿/计划/状态和选择范围；未知字段放入 `raw_json` 保留。
6. 对空对象、损坏 JSON、重复键、孤立关系逐类报告；导入器应可重复执行且不重复生成记录。

### 阶段 C：校验与切换

1. 导入到新的临时数据库，不触碰原 JSON。
2. 对比各表行数、主键集合和关键字段；检查文件夹成员总数、跨夹重复数、分析关联、计划条目数与状态分布。
3. 抽样比对 JSON 与 SQLite 的视频标题、源夹/成员夹、分析建议和执行状态。
4. 全部校验通过后，保留 JSON 备份，将 SQLite 设置为唯一运行时写入源；不长期双写两个存储，避免分叉。
5. `/api/data/export` 从 SQLite 生成现有 JSON bundle 形状；`/api/data/import` 先校验、备份，再事务导入 SQLite。
6. 新版本第一次启动若无数据库但有 JSON，自动执行可见、可恢复的迁移并写入版本记录；若数据库和 JSON 同时存在，数据库优先，提示用户 JSON 作为备份保留。

### 阶段 D：回滚

若启动、统计或数据完整性检查失败：停止数据库写入，恢复迁移前代码/配置并从 `data/` 备份恢复 JSON。SQLite 文件只改名保留供诊断，不自动删除。回滚演练需覆盖：重复导入、空数据库启动、迁移中断、导入后导出、停止任务后重启。

## 6. 实施顺序与验收

### 里程碑 1：SQLite store 与迁移器

- 建立 schema、迁移版本、导入/导出与校验报告。
- 保证既有用户数据可往返导出，不改变计划状态和成员关系。

### 里程碑 2：扫描后端

- 接入两种只读扫描策略及分页回退。
- 2 秒全局最小请求间隔；风控停止；批量缺项和数量不符不能标完成。
- 重扫原子替换单夹快照，旧完整快照在失败时仍可查。

### 里程碑 3：网页扫描界面

- 展示目录计数、本地快照状态、扫描策略、进度和差异。
- 支持选择、继续、重扫、停止、刷新后恢复状态。
- 新扫描与旧 `/api/scan` 兼容，避免一次性重写前后端所有阶段。

### 验收标准

- 「娱乐搞笑」只读路径应能通过 ID 清单与批量元数据完整性校验；该实测结果作为样例，不写死为所有文件夹的假设。
- 大于 1000 条的夹必须走分页；分页按 `has_more` 结束，且测试包含中间短页。
- ID/元数据缺项、限流、停止、程序重启都不会把部分快照标为完整，也不会覆盖此前完整快照。
- 现有 JSON 数据迁移前后主键集合、文件夹多重归属、分析和执行状态一致；JSON 导出可恢复现有 bundle。
- 数据写入不再每 20 条重写整个 `videos.json`；每页/每批使用短事务提交。
- 不触发任何自动远端移动、删除、改名或简介写回。

## 7. 风险与待验证项

1.ID 清单接口的 1000 条截断来自项目历史账号探测，需要在将来账号状态变化后复测；首版对大夹采取分页规避该不确定性。
2.批量元数据接口的最大条数、URL 长度和特殊资源行为尚未验证。需要从小到大只读探测，找到接口/HTTP 客户端两侧的保守阈值。
3. `media_count` 与可读取资源数在失效内容或扫描期间变更时可能不同。应保留远端计数、返回条数和缺失列表，供界面解释，不擅自丢弃成员。
4. 当前项目有未提交的 `server.py` 修改。实施时需要在原工作树保留并审查该差异，不能用重置或覆盖方式迁移。
5. 数据库只能解决本地结构化存储和一致性；接口分页/风控仍由 B 站 API 约束。

## 8. 涉及代码区域

- `bili_api.py`：目录/ID/批量元数据读取方法、请求节流和错误映射。
- `store.py`：迁移到 SQLite repository，提供 JSON bundle 兼容层。
- `server.py`：扫描策略调度、运行状态、断点、数据统计/导入导出。
- `static/index.html`、`static/app.js`、`static/style.css`：选择扫描范围、策略与进度展示。
- `tests/`：接口结果对齐、阈值路由、分页回退、SQLite 迁移往返、部分扫描事务、重启恢复。
