# 面向多平台、多内容类型的可扩展架构

> 状态：架构提案，尚未实施。基于当前 new_arch 分支；本文中的模块、能力名称和新表结构是建议，不代表已接通对应平台或模型协议。
>
> 目标：让 B 站收藏夹整理逐步成为通用内容整理工具，支持多账户、视频/图文/文档/文献，并复用扫描、画像、分析、人工审批和执行核对。
>
> 当前实现参见 [运行架构](current-architecture-new_arch.svg) 和 [现有数据库结构](database-storage-structure.md)。

## 1. 核心决策

采用**模块化单体 + 能力接口 + 适配器注册**。继续使用本地 FastAPI、SQLite 和持久任务运行时；按清晰接口组织代码，让业务流程可以替换平台、模型和内容提取方式。

需要分别建立三个边界：

1. **平台能力**：内容和集合如何读取、同步和修改。
2. **模型能力**：一个任务需要哪些输入/输出能力，以及请求采用哪种协议。
3. **内容模型**：内容的身份、正文与素材、归属集合、原始来源和派生分析如何保存。

连接配置管理账户、地址和凭据；应用服务根据能力选择适配器。业务代码使用本地 item_id / collection_id，不把 BV、aid、media_id 或模型供应商名称作为通用规则。

~~~mermaid
flowchart TB
  UI["工作台<br/>连接 / 内容库 / 复核 / 执行"]
  API["API 层"]
  APP["应用服务<br/>同步 / 提取 / 画像 / 归类 / 计划 / 执行"]
  DOMAIN["领域对象与规则<br/>Item / Collection / Plan / Action"]
  REG["连接与能力注册表"]
  PP["平台接口<br/>集合读取 / 内容读取 / 成员修改 / 操作核对"]
  MP["模型接口<br/>生成 / 嵌入 / 转写等"]
  EP["提取接口<br/>正文 / OCR / 字幕 / PDF"]
  PA["平台适配器<br/>Bilibili / 未来平台 / 本地文件与导入"]
  MA["模型协议适配器<br/>Chat Completions / Responses / Messages / GenerateContent"]
  EA["内容提取器"]
  DB["Repository + SQLite<br/>资源 / 素材 / 集合 / 版本 / 执行日志"]
  RT["Job Runtime<br/>进度 / 取消 / 限流 / 恢复"]
  UI --> API --> APP
  APP --> DOMAIN
  APP --> REG
  APP --> PP --> PA
  APP --> MP --> MA
  APP --> EP --> EA
  APP --> DB
  APP --> RT
~~~

当前可以先用显式 Python 注册表。能力配置是数据，适配器是经过注册的代码；暂时没有必要引入动态插件安装、微服务、消息中间件或独立向量数据库。

## 2. 连接配置与能力定义分开

| 概念 | 回答的问题 | 例子 |
|---|---|---|
| Connection | 使用哪个账户/地址和凭据？ | B 站账户 A、模型网关 B、本地目录 C |
| Adapter | 谁实现具体调用和数据转换？ | BilibiliAdapter、OpenAIChatAdapter、LocalFilesAdapter |
| Capability | 当前连接能做什么，限制是什么？ | collections.list、membership.add、文本生成、图像输入 |
| Provider | 模型来自哪个供应商或服务？ | 某云厂商、某自建网关 |
| Protocol | 请求、响应和流式事件是什么格式？ | openai_chat、openai_responses、anthropic_messages、gemini_generate |
| Model binding | 这个端点上实际使用哪个模型？ | endpoint_id + external_model_name |
| Task policy | 某个业务任务使用哪些连接和能力？ | 归类用模型 X；图片说明用模型 Y |

一个 provider 可以提供多个协议端点，同一个模型名字也可以出现在不同网关中。因此 provider + model + 模糊的 type 不足以唯一确定请求格式。

建议将 type 拆成明确字段：

- connection_kind：platform / model / local。
- protocol：请求协议。
- input_modalities / output_modalities：输入、输出模态。
- task_kind：归类、画像、摘要、嵌入、OCR、转写等应用任务。

凭据使用 credential_ref，由凭据存储解析；领域对象、计划和原始响应不持有 Cookie/API Key。连接界面根据适配器的认证字段描述展示表单，并提供独立的连接测试、账户识别和能力检查。

## 3. 平台接口：统一能力，保留真实操作语义

平台模块提供小型接口，不要求所有平台实现一个包含全部方法的大接口：

| 接口组 | 建议能力 | 标准输出 |
|---|---|---|
| 集合读取 | collections.list、collections.read、collection.members.list | Collection、带 cursor 的 Page |
| 内容读取 | items.read、items.batch_read、items.search | ContentItem、原始来源信息 |
| 素材读取 | assets.resolve、assets.fetch | 素材引用、可用期和获取结果 |
| 集合修改 | collections.create / rename / delete / description.update | 操作回执 |
| 成员修改 | membership.add / remove / move | 操作回执和逐项结果 |
| 结果核对 | membership.read、operation.verify 等可用核对途径 | observed_state + verified / pending / unknown |

cursor 对应用层是不可解析的字符串，由适配器封装页码、游标或 continuation token。Page 同时携带 next_cursor、枚举完整性和一致性说明。只有完整且可信的同步结果才允许归档已消失成员；部分失败或不具备稳定快照的枚举不能仅凭“本页没看到”删除本地记录。

能力描述至少包含：

- 支持状态：supported / unsupported / unknown。
- 实现方式：native / composed / local。
- 读写权限、支持的内容类型、批量上限、分页/限流约束。
- 幂等支持、核对途径、读取一致性和保护规则。
- 能力版本、依据及最近检查时间。

有效能力由适配器声明、账户权限、对象自身限制和调用策略共同决定。unknown 不按 supported 使用；写权限在执行前需要重新校验。

### 查询收藏夹的具体调用

~~~text
CollectionService.list(connection_id)
  → ConnectionRegistry.resolve(connection_id)
  → 检查 collections.list
  → BilibiliAdapter.list_collections(...)
  → BiliSession.list_folders(...)
  → 转换为统一 Collection
~~~

其他平台实现同一读取接口，并转换成相同输出。它可以把“收藏夹”“列表”“书签集合”等平台概念映射成 Collection，但必须保留对象类型和权限；搜索结果、公开频道等不会自动变成可写的个人收藏夹。

B 站原生调用仍由专门模块维护：Cookie/CSRF/WBI、media_id/aid 区别、分页、资源类型、节流和错误解释。可先包装现有 bili_api.py，之后按复杂度分成 client.py、adapter.py、normalize.py、capabilities.py。模块承载本工具需要的能力，无需先实现平台的所有 API。

### 移动、删除和跨平台整理

“移除集合成员”“删除本地记录”“删除远端原始内容”是不同动作，使用不同的能力名和审核说明。

membership.move 若平台原生支持，执行器记录原生操作；若只能由 add + remove 组合，计划器生成：

~~~text
添加目标成员 → 核对目标成员存在 → 移除来源成员 → 核对来源成员消失
~~~

每个子步骤独立落日志、保存回执和依赖。添加成功而移除失败时记录 partial，并保留已有结果。结果未知时先核对；幂等键只有在平台确实支持时才具有远端去重作用。

跨平台归类默认改变本地集合归属，保留来源链接。把 B 站视频纳入本地“机器学习”集合，不要求其他平台收藏该视频。远端跨平台搬运需另外定义 export/import 或 publish 能力，不能从两个平台都有“收藏”推导出可搬运。

小红书、抖音等平台的实际读写能力在接入时确认；设计应允许只读接入、链接导入或文件导入仍参与本地整理。

默认收藏夹规则放入平台适配/策略：映射为 collection.role=inbox、允许来源、禁止移入等限制，不把中文夹名写入通用领域规则。

## 4. 模型接口：任务与协议分别扩展

路由过程建议为：

~~~text
业务任务声明要求
  → 选择用户配置的 model binding
  → 检查模型、端点和协议的能力交集
  → 准备统一 ModelRequest
  → 协议适配器编码
  → 应用 provider/model 参数差异
  → 请求
  → 协议适配器解码为统一结果
  → 业务校验、持久化
~~~

例如 classify_items 需要文本输入与符合 schema 的分类结果；describe_images 需要图像输入；embed_texts 需要向量输出。生成和嵌入使用不同接口，避免把所有能力塞进一个 chat() 方法。

统一生成请求保存消息角色、文本/图像/音频/文件等输入块、输出 schema、预算和选项；统一结果保留内容块、工具调用、用量、完成/截断/拒绝原因，以及实际端点和模型。费用或 token 用量未知时保留 null；不能因解析出了半个 JSON 就把截断结果当成功。

协议适配器负责结构差异，供应商 profile 负责同协议下的差异，例如 token 上限字段、thinking 参数、文件输入方式。归类提示词、画像提示词和业务结果校验放在 application 服务中，不放入协议适配器。

初步协议目录可以列出：

| 协议 | 适配器职责 | 初期状态 |
|---|---|---|
| OpenAI Chat Completions | messages / choices、流式 delta 等转换 | 包装现有代码，优先落地 |
| OpenAI Responses | input / output items 等转换 | 独立扩展项 |
| Anthropic Messages | content blocks 等转换 | 独立扩展项 |
| Gemini GenerateContent | contents / parts / candidates 等转换 | 独立扩展项 |

上述协议的输入、输出组织确实不同，详见 [OpenAI 协议迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create) 和 [Gemini GenerateContent](https://ai.google.dev/api/generate-content)。这些官方资料用来确认协议边界，本文的路由和存储设计是本项目的架构建议。

模型能力记录要区分已知、未知和不支持，并注明来源：文档、接口、人工设置或实际探测。当前模型列表接口不能被默认当成完整能力表。任务降级如“图片只取正文”需显式配置；不能悄悄把图片省略，或自动切到用户未授权的服务。

结构化输出也要记录保证级别：原生 schema 约束、JSON 模式和仅提示词要求不是同一种能力。应用服务始终做结果 schema 校验，并据此决定是否可进入人工复核。

## 5. 统一内容：条目 + 内容块 + 素材 + 来源

ContentItem 是本地整理条目，使用 item_id；它的业务种类和素材类型分别描述：

- kind：post / video / document / paper / webpage / audio 等，用于语义和界面。
- blocks：有序正文块，可为 text、image_ref、video_ref、audio_ref、file_ref。
- assets：图片、视频、音频、PDF 等素材的引用和可选缓存，用 MIME 类型和角色描述。
- source_refs：平台原生身份、URL、来源记录和获取时间。
- metadata：作者、发表时间、摘要等常用字段，以及有命名空间的类型扩展字段。

kind 不决定所有素材。一条图文笔记可以包含正文和多张图片；论文可以同时拥有摘要、PDF、图表和补充视频。素材是否已下载、是否可提取、内容是否被模型处理过是独立状态。

| 条目例子 | kind | blocks / assets | 特有信息 |
|---|---|---|---|
| B 站视频 | video | 简介、封面、视频引用、可选字幕 | 原生 BV/aid、分 P 信息 |
| 小红书图文笔记 | post | 正文、按顺序排列的图片 | 笔记原生 ID、平台标签 |
| 抖音视频或图文帖子 | post 或 video | 文本、视频或图片素材 | 原生作品 ID、平台元数据 |
| PDF 文档 | document | 文件引用、提取后的页内容 | 页数、文件哈希 |
| 学术论文 | paper | 摘要、PDF、图表、补充材料 | DOI、作者、年份等文献信息 |

这里举的是内容表达方式，不声明这些平台已经提供相应读取接口。

身份不能只使用 BV 号或平台短 ID：本地统一使用 UUID，外部身份以 (source_namespace, external_type, external_id) 唯一定位。公开资源使用平台公共命名空间；账户私有/账户内唯一的对象使用账户命名空间。这样同一公开视频可在多个账户集合里出现而不复制完整元数据，也不会把两个平台相同短 ID 误当成同一资源。

公开元数据与账户私有的收藏、备注和访问状态分开。某条内容对账户 A 不可见，并不代表它对所有账户都已删除；访问观察应记录 connection_id、source_object_id、观察结果与时间，远端集合成员始终带账户归属。

论文预印本、正式发表版本和其他转载允许以 item_relations / identifiers 建立版本、引用或相似关系；不要仅凭标题或正文相似自动合并身份。DOI 等标识是可索引字段，平台原始响应是 provenance 数据，二者分开保存。

### 提取与模型输入准备

~~~text
平台条目 / 文件
  → 标准化条目与素材引用
  → 按任务取得必要素材
  → 提取正文 / OCR / 字幕 / 转写 / PDF 页内容
  → EvidenceBundle（可追溯的证据）
  → 模型任务
  → 分类 / 摘要 / 标签 / 关系
~~~

提取结果保留 source_revision、提取器/版本、时间，以及页码、时间段或图片区域等定位信息。原始正文和提取结果不会相互覆盖。长文档再按块处理；只有用到检索任务时才增加 embedding 和向量索引。

## 6. 数据库存储方向

SQLite 可以继续使用。需要改变的是领域结构、身份和版本关系；新增条目种类通常只增加素材/提取器和专用元数据，核心表保持稳定。

| 建议对象 / 表 | 核心字段或关系 | 对应当前结构 |
|---|---|---|
| connections | kind、adapter_key、config、account_identity、credential_ref | B 站连接配置和会话 |
| items | id、kind、title、作者/时间、metadata、current_revision | videos |
| source_objects | item_id、source_namespace、external_type、external_id、URL、原始响应/版本 | bvid / aid / resource_type / record_json |
| collections | id、scope=local/remote、connection_id、外部身份、role、权限/状态 | folders |
| collection_members | collection_id、item_id、收藏时间/顺序、来源快照 | folder_items |
| item_revisions | item_id、revision、内容指纹、时间 | 元数据变化和画像新鲜度依据 |
| assets / item_assets | 素材类型、URL/本地路径、哈希、状态；item_id / revision_id + role + ordinal 关联 | 目前内嵌于视频记录的素材 |
| content_blocks | item_id、revision、ordinal、块类型、文本或 asset_id | 正文与混合素材表达 |
| sync_runs / sync_states / sync_stage | connection/collection、cursor、完整性、工作暂存 | folder_scan_state / scan_stage |
| collection_profiles | collection_id、输入成员/内容版本、画像与模型来源 | folder_profiles |
| analyses | 目标条目/集合、task_kind、输入指纹、prompt_version、model_binding、结果 | analyses |
| plan_versions / plan_actions | 审批版本、动作类型、目标 ID、依赖、前置条件 | plans / plan_versions / 合并计划 JSON |
| jobs / execution_runs / execution_operations | 生命周期、动作身份、回执、核对结果 | 保留既有机制并改用通用 ID |
| model_endpoints / model_bindings / task_model_routes | 协议端点、外部模型名、能力/限额、任务绑定 | providers / models / 单一活动模型 |

以上是完整目标对象清单，不要求第一轮建立全部独立表。最小迁移先建立 items、source_objects、collections、collection_members、连接记录和基本素材关系；块、提取历史、文献关系及检索索引按实际功能逐步加入。

核心可查询的身份、关联、排序、版本、状态使用结构化列；平台扩展字段和低频专用元数据使用 JSON。避免把所有字段都装进一个 payload，也避免引入无限属性名的通用键值模型。

多账户访问状态可增加 source_observations；正文检索先针对可用文本和提取结果建立 SQLite 全文索引，语义检索出现实际需求后再增加 embedding 索引。

新增表为稳定归属关系设置真实外键和唯一约束，保留执行历史时用归档状态和合理删除策略。原始远端响应与凭据数据必须分开；媒体主体放文件缓存，SQLite 保存引用、指纹和元数据。

### 版本与执行完成状态

需要区分内容修订、集合成员修订、画像修订和审批计划修订。审批记录保存所依赖的具体版本及 capability/adapter 版本；执行前检查相关对象的前置条件。

“某内容执行过”不能代替“某动作完成过”。同一 item 再次移到另一个目标集合是新的动作。完成状态绑定 action_id 和 intent_fingerprint，指纹包括动作、源/目标、参数与前置条件。重建计划不能只因为资源 ID 相同就继承旧动作的 done 状态。

LLM 的建议先写分析结果，人工确认后生成 PlanAction；运行时记录操作结果不会修改已审批版本的原始意图。

## 7. 模块目录建议

~~~text
content_hub/
  domain/                    # Item、Collection、PlanAction、规则
  application/               # 同步、提取、画像、归类、计划、执行服务
  ports/                     # 平台、模型、提取、Repository 接口
  adapters/
    platforms/
      bilibili/              # 原生 client、标准化、能力实现
      local_files/           # 只读导入即可验证多类型内容
      other_platform/        # 后续按实际能力接入
    models/
      protocols/             # 各请求协议
      provider_profiles/     # 同协议的供应商/模型差异
    extractors/              # HTML、PDF、OCR、字幕等
  persistence/sqlite/         # 按领域拆分 Repository 与迁移
  runtime/                   # 持久任务、限流、锁、恢复
  api/                       # 薄 HTTP 路由、依赖组装
~~~

依赖方向：domain 不依赖 HTTP、SQLite 或原生平台 SDK；application 依赖接口；adapters 和 persistence 实现接口；启动入口组装具体对象。前端访问标准化 DTO，按能力展示动作，保留按 kind 选择的内容预览组件。

初期保留单 Worker，避免迁移同时改变并发行为。后续需要并发时，按平台账户的写操作和供应商端点的请求限额建立调度范围，集合/成员修改采用资源级互斥；跨远端接口仍以分步骤日志和核对保证可恢复性。

## 8. 从 new_arch 迁移的顺序

1. **定义最小通用契约并包装 B 站**：ContentItem / Collection / Page / Capability / OperationReceipt；BilibiliAdapter 复用现有 client，先打通集合列表和成员读取。
2. **引入通用身份与内容存储**：新表增量建库并回填 B 站数据；旧字段通过兼容层映射。每项数据由一个明确写入口维护，避免长期双写。确认回填和完整业务流程一致后再切换读取和后续写入；迁移前作一致性备份。
3. **提取模型调用边界**：现有 Chat Completions 实现包装成协议适配器，把画像/归类提示词和参数兼容逻辑分开，增加 task → model binding 的配置。
4. **迁移归类与执行**：使用 item_id / collection_id，审批 PlanAction；将 B 站核对逻辑纳入适配器，通用执行器负责前置条件、子步骤日志、结果和恢复。
5. **验证第二种来源与第二种内容类型**：优先本地 Markdown/图片/PDF 导入。它不依赖新平台接口可用性，能直接暴露视频专用假设；之后接入一个已确认能力的真实平台。
6. **按需求扩展协议和提取器**：新增模型协议、图像理解/OCR、文献字段、搜索等。每次新增优先验证适配器契约与原有工作流复用。

阶段 1 的具体切口建议是：把“查询收藏夹”改为 CollectionService → 平台接口 → BilibiliAdapter，并把 BV/aid/media_id 放到 SourceRef。这一段同时验证能力分发和身份边界，范围小而且能沿用已有 B 站行为。

判断架构扩展是否成功，看三个实际结果：新增平台主要改适配器及其配置；新增内容类型主要改素材/提取与预览；新增模型协议主要改协议适配器。归类、审批、计划版本、任务和执行日志应能持续复用。

