# SQLite 数据库存储结构

> 梳理范围：当前 `new_arch` 分支的 `store.py`。当前 schema 版本为 **v7**；表定义、迁移和读写行为以代码为准。配套运行架构见 [当前分支架构图](current-architecture-new_arch.svg)。

## 1. 总览

数据库是本机的 SQLite 文件，当前结构共 **16 张表**，按用途可分为三组：

1. **收藏数据索引**：收藏夹、视频、成员关系、扫描快照、画像与分析。
2. **方案和执行记录**：当前方案、方案版本、后台任务、执行批次与逐项远端操作日志。
3. **应用状态与模型配置**：界面/任务的兼容状态键值，以及供应商和模型目录。

核心关系可概括为：

`收藏夹 folders` 与 `视频 videos` 通过 `folder_items` 多对多关联；扫描先写入 `scan_stage`，完成后再更新正式成员索引。分析和当前计划按资源键保存；审批后的版本进入 `plan_versions`，执行过程进入 `execution_runs` 和 `execution_operations`。

~~~mermaid
flowchart LR
  subgraph Catalog["收藏数据与扫描索引"]
    F["folders<br/>收藏夹目录"]
    V["videos<br/>共享视频元数据"]
    FI["folder_items<br/>夹内成员关系"]
    FS["folder_scan_state<br/>扫描状态"]
    SS["scan_stage<br/>扫描暂存"]
    FP["folder_profiles<br/>收藏夹画像"]
    A["analyses<br/>归类结果"]
    P["plans<br/>当前内容计划"]
    F -. "media_id（逻辑关联）" .-> FI
    V -. "resource_key（逻辑关联）" .-> FI
    F -.-> FS
    F -.-> SS
    F -.-> FP
    V -.-> A
    V -.-> P
  end

  subgraph Workflow["方案与执行"]
    PV["plan_versions<br/>审批版本与快照"]
    J["jobs<br/>后台任务"]
    ER["execution_runs<br/>执行批次"]
    EO["execution_operations<br/>逐项远端操作"]
    PV -. "plan_version_id（逻辑关联）" .-> ER
    J -. "job_id（逻辑关联）" .-> ER
    ER -->|"run_id 外键"| EO
  end

  subgraph Settings["本机状态与模型配置"]
    S["app_state<br/>JSON 键值状态"]
    PR["providers<br/>供应商与 API Key"]
    M["models<br/>模型元数据"]
    PR -->|"provider_id 外键，删除时级联"| M
  end
~~~

虚线代表代码层约定的逻辑关联，**不是 SQLite 外键**。当前显式外键只有 `execution_operations.run_id → execution_runs.id` 和 `models.provider_id → providers.id`。

## 2. 表结构

### 2.1 收藏数据、扫描与分析

| 表 | 主键 / 唯一键 | 主要字段 | 用途 |
|---|---|---|---|
| `folders` | `media_id` | `title`、`remote_count`、`record_json`、`status`、`archived_at`、`directory_order` | 收藏夹目录缓存。已从远端消失的夹会标为 archived，保留目录记录。 |
| `videos` | `resource_key` | `bvid`、`resource_id`、`resource_type`、`record_json`、`updated_at` | 跨收藏夹共享的视频/资源元数据。常见键为 BV 号；没有 BV 号时按 `类型:资源ID` 形成资源键。 |
| `folder_items` | `(media_id, resource_key)` | `fav_time`、`scan_run_id` | 收藏夹与视频的成员关系。将目录成员从视频元数据中拆出，避免同一视频在多个夹重复存储。 |
| `folder_scan_state` | `media_id` | `status`、`strategy`、`expected_count`、`fetched_count`、`cursor_page`、起止时间、`last_error` | 每个收藏夹的扫描完成度、策略和断点信息。常见状态包括 never、partial、complete、stale。 |
| `scan_stage` | `(scan_run_id, media_id, resource_key)` | `record_json` | 单次扫描的暂存记录；只有扫描完整后才并入正式视频和成员索引。 |
| `folder_profiles` | `media_id` | `profile_json`、`updated_at` | 收藏夹内容画像；画像主体是 JSON。 |
| `analyses` | `resource_key` | `record_json`、`status`、`dependent_ids` | LLM 对资源的归类结论；`dependent_ids` 是分析时依赖的画像夹 ID 列表，以 JSON 文本保存。画像或候选范围变化后可将状态标为 stale。 |
| `plans` | `resource_key` | `record_json` | 当前生效的内容归类计划；方案历史和审批快照单独保存在 `plan_versions`。 |

`folders` 和 `videos` 保留便于检索的结构化列，同时用 `record_json` 保存完整业务记录。成员关系以 `folder_items` 为准；读取视频时，代码会从成员表重新组装 `folder_ids`。

### 2.2 方案、后台任务与执行日志

| 表 | 主键 / 唯一键 | 主要字段 | 用途 |
|---|---|---|---|
| `plan_versions` | `id`；唯一约束 `(plan_key, version)` | `status`、`base_snapshot_json`、`diff_json`、`items_json`、创建/审批/更新时间 | 内容计划和收藏夹合并计划的版本历史；保存审批时的基础快照、差异和完整计划内容。 |
| `jobs` | `id` | `kind`、`status`、`payload_json`、`progress_json`、`cancel_requested`、`error`、生命周期时间 | 后台任务队列和进度。正常运行时只允许一个 queued/running 任务。 |
| `execution_runs` | `id` | `plan_version_id`、`job_id`、`status`、`error`、起止时间 | 一次计划执行的批次记录；两个引用列在数据库中不设外键。 |
| `execution_operations` | `id`；唯一约束 `(run_id, sequence)` | `kind`、源/目标夹 ID、`resources_json`、`payload_json`、`status`、`result_json`、`error`、时间 | 每次远端写操作的发送意图、结果和核对信息。操作按批次内的 sequence 排序。 |
| `app_state` | `name` | `value_json` | 少量应用状态的 JSON 键值表，适合整体读写的小型状态。 |

常用 `app_state.name` 包括：

- `apply_state`：执行清单/进度兼容状态。
- `scan_done`、`scan_selection`：已完成扫描夹和扫描选择。
- `folder_merge_plan`、`folder_merge_draft`、`folder_merge_state`：合并计划、草稿和运行状态。
- `analysis_scope_key`：归类候选范围指纹。

`analysis` 和 `plan` 虽然出现在通用数据默认值/旧 JSON bundle 兼容逻辑中，当前实际记录分别在 `analyses` 和 `plans` 表，不是 `app_state` 键。

执行操作在发请求前以 `sending` 写入日志。远端回读符合预期后记为 `verified`；明确失败记为 `failed`；结果不确定记为 `unknown`。进程重启时，仍为 `sending` 的操作会转成 `unknown`，运行中的批次和任务会转成 `interrupted`，不会自动重放远端写入。

### 2.3 供应商和模型配置

| 表 | 主键 / 外键 | 主要字段 | 用途 |
|---|---|---|---|
| `providers` | `id` | `name`、`base_url`、`api_key`、创建/更新时间 | LLM 供应商连接配置。API Key 存在数据库中。 |
| `models` | `id`；`provider_id → providers.id` | `name`、上下文/输出 token 上限、来源标记、思考强度、连通测试状态和时间 | 某供应商下的模型及其能力元数据。删除供应商时模型行级联删除。 |

`context_source` 和 `output_source` 是后续迁移追加的列，用于区分模型能力上限来自手工填写、API 返回或未知来源。

### 2.4 迁移元数据

| 表 | 主键 | 字段 | 用途 |
|---|---|---|---|
| `schema_migrations` | `version` | `applied_at` | 记录数据库 schema 迁移版本；当前最高版本为 7。 |

应用启动时会创建缺失的表和索引，并按需要追加列。迁移过程中还包含对旧目录状态、排序字段和分析状态的补齐。旧项目目录中的 SQLite 库可迁至用户数据目录；旧 JSON 数据用于兼容导入，旧数据文件会先备份。

## 3. 关键数据流

### 收藏夹扫描

1. 刷新目录时按 `media_id` upsert 当前收藏夹；远端已消失的目录转为 archived。
2. 开始扫描时把夹状态置为 partial，清理该夹旧暂存，再将分页结果逐批写入 `scan_stage`。
3. 扫描完整后，在一个事务中替换该夹的 `folder_items`，upsert 共享 `videos` 元数据，删除暂存并标记 complete。
4. 若收藏夹消失，会清除该夹的成员和暂存记录、将扫描状态标为 stale，但保留共享视频元数据。

因此，`scan_stage` 是未完成扫描的工作区，`folder_items` 是已提交扫描结果的正式成员索引。

### 方案审批与执行

1. 计算方案基础快照：包括 active 收藏夹、扫描状态、成员关系、收藏夹和视频记录指纹及画像版本。
2. 保存审批版本：内容计划同步写入 `plans`；合并计划同步写入 `app_state.folder_merge_plan`；版本内容和差异写入 `plan_versions`。
3. 执行前重新计算快照指纹；若与审批版本不一致，计划不能直接执行。
4. 建立 `execution_runs`，每个远端操作先写 `execution_operations`，再执行请求和远端状态核对。

### 分析失效

`analyses.dependent_ids` 冻结分析生成时参考的收藏夹画像依赖。画像或可选目标范围变化时，相关结果可标记为 stale，供应用重新分析；不会单靠扫描造成的成员变化追溯性地让所有历史结论失效。

## 4. 文件位置与隐私边界

默认 Windows 数据位置：

- SQLite：`%LOCALAPPDATA%/BiliFavOrganizer/data/library.sqlite3`
- 应用配置：`%LOCALAPPDATA%/BiliFavOrganizer/config.json`
- B 站 Cookie：`%LOCALAPPDATA%/BiliFavOrganizer/secrets.json`

设置环境变量 `BILI_FAV_ORGANIZER_DATA_DIR` 可覆盖 SQLite 的数据目录。Cookie 与数据库分开保存；LLM API Key 保存在 `providers.api_key`。数据库和 Cookie 文件都属于敏感本机数据。

JSON bundle 导出不包含 Cookie 或供应商 API Key。执行任务、方案版本和执行日志不在当前导出 bundle 中；`providers` 仅导出名称、地址等非密钥信息。

## 5. SQLite 运行方式与索引

- 每个连接启用外键检查、30 秒 busy timeout；数据库使用 WAL。
- 写入通过进程内可重入锁和 `BEGIN IMMEDIATE` 事务串行化。
- 常用索引覆盖视频 BV 号、资源到收藏夹成员关系、任务状态/创建时间、方案版本、执行批次操作、扫描暂存目录、分析状态和模型供应商。
- 多数业务状态以 JSON 文本保存，Python 读写函数负责序列化、默认值和状态约束；状态枚举并非全部由 SQL `CHECK` 约束保证。
- 除模型归属和执行操作归属之外，多数表间关系由 `store.py` 的读写流程维护，没有数据库级外键级联。

这套结构让视频元数据跨收藏夹复用，让扫描可暂存/断点恢复，并为审批计划和远端写入保留可追踪记录；代价是 JSON 内容形状、状态枚举和逻辑关联需要继续由应用层维护。

