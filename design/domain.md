# Ruyi-Dream 领域模型设计 - v1.0

本文档是 Ruyi-Dream 新版 MVP 的领域语言源头。产品流程、API、数据库、Prompt DTO、前端页面和后续服务拆分都应从这里延伸出去。

Ruyi-Dream 是面向服饰品牌视觉生产团队的 B2B AI 数字化摄影棚。当前服务单一试点客户和单一工作空间，但领域模型保留后续 SaaS 化、多客户、多租户、权限和计费扩展空间。新版主流程不是旧版“货号默认组图方案”，而是：

```text
上传主推款
-> AI Stylist 生成 StylingPlan
-> AI Director 生成 ShootScript + 6 个 ShotPlan
-> Image-2 生成 3x2 分镜预览
-> Image-2 并发生成前 5 张成片
-> 下载与生成记录复盘
```

核心原则：

```text
商品图片负责事实，AI Stylist 负责造型判断，ShootScript 负责导演，ShotPlan 负责单镜头，Image-2 负责视觉生成。
```

Image-2 不是选搭大脑、标签系统、历史记录系统或状态机。用户也不是 prompt 工程师；前台语言应是摄影、电商、图位和商品语言。

## 模块总览

基于高内聚、低耦合原则，当前领域对象划分为 6 个核心模块：

| # | 模块 | 领域对象 | 职责 |
|---|---|---|---|
| 1 | **workspace** | session, user role, workspace boundary | 当前固定 user/admin 双账号和单工作空间边界；为后续组织、租户、权限扩展预留语义 |
| 2 | **media** | MediaAsset | 所有输入图、数字模特图、分镜预览图、最终图和导出资产的统一媒体索引 |
| 3 | **brand** | BrandProfile, BrandStyleSample, ModelProfile, PromptConfig, ShootConfig | 品牌资料、品牌样本证据、默认数字模特、Prompt 模板和拍摄规则 |
| 4 | **styling** | StylingGarment, StylingGarmentImage, tag taxonomy, garment tag assignment | 搭配服装库、服饰图片、AI 打标、标签体系和选搭候选事实 |
| 5 | **generation** | GenerationTask, GenerationTaskInputVersion, GenerationStyling, StylingPlan, GenerationShootScript, ShootScript, GenerationShotPlan, GenerationStoryboardPreview, GenerationResultSet, GenerationResultImage | AI 摄影棚主流程、输入冻结、造型方案、拍摄剧本、分镜预览和最终成片 |
| 6 | **operations** | GenerationAttempt, EventLog, DeleteImpact | provider 调用审计、状态恢复、错误复盘、业务事件和删除影响预检 |

当前后端仍是 FastAPI 单体，SQLite 保存元数据，媒体走 S3 兼容对象存储；模块划分是领域边界，不要求当前代码按包物理拆分。

```text
backend/app/main.py   API 路由、生成编排、Prompt DTO、序列化
backend/app/db.py     SQLite schema、迁移、默认模特、事件日志
frontend/src/App.tsx  AI 摄影棚、搭配服装库、数字模特、生成记录
frontend/src/api.ts   前端 API 契约和领域类型
```

## 命名规范

1. `id` 是系统主键或外键。系统内部关联、数据库外键、快照关联和后端持久化以 `id` 为准。
2. `code` 是业务主键。搭配服装使用 `StylingGarment.code` 作为人类可读标识、AI Stylist prompt 输入输出标识和生成记录展示标识。
3. `xxx_json` 表示结构化 JSON 快照或原始模型返回，不是自由拼接文本。重要 JSON 要保留 `schema_version`。
4. `snapshot` 表示“当时版本事实”。历史再次生成默认读快照，不读素材库、品牌资料或数字模特最新状态。
5. `current_xxx_id` 表示当前输入版本指针；`active_xxx_id` 表示当前工作流主线上的成功 artifact 指针。
6. `attempt` 表示一次外部 provider 或 AI 调用。耗时、错误、provider request id、request/response 都记录在 attempt，不用业务对象 `created_at` 充当模型耗时。
7. 用户侧命名优先中文产品语言：`AI 智能选搭`、`拍摄剧本`、`分镜预览`、`成片`。系统侧命名使用 `StylingPlan`、`ShootScript`、`ShotPlan`、`GenerationResultImage`。
8. 避免把旧版词汇重新带回主流程：旧“六宫格预览”用户侧统一称为“分镜预览”；旧 `garment_skus`、旧 planner、旧 generation run/image 不是新版领域对象。

## 核心术语区分

### 主推款 vs 搭配服装

- **主推款**：本次拍摄的核心商品，只属于某个 `GenerationTaskInputVersion`，MVP 不建立长期主推款商品库。
- **搭配服装**：搭配服装库中的真实配角商品，即 `StylingGarment`。只有 `READY` 且未归档、未删除的搭配服装可进入 AI Stylist 候选范围。

最终图里主推款和被选中搭配服装都需要商品级保真。鞋、包、首饰和帽子在 MVP 不入库，只能作为生成配饰文字边界，不作为商品事实源，也不做特写验收。

### StylingPlan vs ShootScript vs ShotPlan

- **StylingPlan**：AI Stylist 的造型阶段产物，回答“穿什么、怎么穿、为什么成立”。
- **ShootScript**：AI Director 的导演阶段产物，回答“整组怎么拍”。
- **ShotPlan**：ShootScript 中的单镜头方案，回答“这一格怎么取景、摆姿、展示哪些卖点”。

ShootScript 不重新选择库内搭配服装；它只能基于已选 StylingPlan 披露风险、调整生成配饰和安排镜头。

### 分镜预览 vs 成片

- **分镜预览**：Image-2 根据当前 ShootScript 生成的一张 3x2 总图，是导演、姿势、构图、光线和同组风格参考。
- **成片**：Image-2 按前 5 个 ShotPlan 逐张生成的最终电商棚拍图。

分镜预览不是商品事实来源，也不是模特身份事实来源。成片中的商品事实始终来自主推款原图和选中搭配服装原图；模特身份事实始终来自数字模特参考图和 `ModelIdentityContract`。

### Prompt DTO vs 审计快照

模型输入必须按阶段收敛为 runtime DTO：

| 阶段 | Runtime | 只传什么 |
|---|---|---|
| AI Stylist | `candidate_retrieval` | 未归档 READY 搭配库全量标签事实卡 |
| AI Director | `styling_result` + `model_identity_context` | 主推款理解、选中 outfit、选中服饰紧凑标签、质量信号、模特身份契约 |
| 分镜预览 | `shoot_script_result` + `image_reference_manifest` | 全局导演、风险、配饰、6 个镜头、参考图角色和事实源 |
| 最终成片 | `current_shot_context` + `image_reference_manifest` | 当前镜头、当前格子、选中 outfit、本镜头配饰、禁止项、参考图角色和事实源 |

审计快照可以保存完整 request/response、素材快照和 provider 调试信息，但下游 prompt 不应透传完整上游 JSON、旧 prompt 或历史 response。Prompt 正文只来自 `prompt_configs`；后端不内置、seed 或 fallback Prompt 默认值。

### 归档 vs 彻底删除

- **归档**：默认删除语义。业务对象从未来工作流隐藏，但历史记录和快照仍可复盘。
- **彻底删除**：破坏性操作，必须先做 DeleteImpact 预检并由用户确认。涉及对象存储文件时还要处理引用计数和级联影响。

## workspace 模块

### session / role（会话与角色）

当前 MVP 不实现注册、邀请、复杂 RBAC、多租户或计费。系统只有固定 `user` / `admin` 两类账号：

- `user`：使用 AI 摄影棚、搭配服装库、数字模特只读页、生成记录。
- `admin`：必须拥有 user 的完整工作流，并额外看到品牌信息、标签规则、拍摄配置、Prompt 维护等配置能力。

说明：

- 当前工作空间只有一套共享任务台和搭配库。
- 后续 SaaS 化时，应引入 `Organization` / `Workspace` 作为数据隔离边界，并让所有业务对象具备 workspace 外键。
- 认证信息不属于摄影棚核心领域；当前后端只保存会话角色，不保存密码业务模型。

## media 模块

### MediaAsset（媒体资产）

所有图片文件统一进入 `MediaAsset`。业务对象引用媒体资产，不把本地路径、外部 URL 或对象 key 当成唯一业务事实。

当前数据库字段：

- `id`：系统主键
- `object_key`：原图对象存储 key
- `thumb_key`：缩略图对象 key
- `preview_key`：预览图对象 key
- `original_filename`：原始文件名
- `mime_type`：MIME 类型
- `width` / `height`：图片尺寸
- `size_bytes` / `thumb_size_bytes` / `preview_size_bytes`：文件大小
- `hash`：内容 hash
- `status`：资产状态，默认 `READY`
- `deleted_at`：软删除时间
- `created_at` / `updated_at`

三层媒体结构：

| 档位 | 用途 |
|---|---|
| `thumb` | 列表、历史缩略图、结果小卡片 |
| `preview` | 详情预览、弹窗查看 |
| `original` | AI 输入、下载、质检和审计 |

规则：

- 原图必须保留。
- 历史快照只引用 asset id、object key、hash、尺寸、文件名，不复制图片二进制。
- 对象存储统一使用 S3 兼容接口，私有化默认 RustFS。
- 前端访问稳定 `/api/media?...`，不直接依赖短期预签名 URL。

## brand 模块

### BrandProfile（品牌资料）

`BrandProfile` 是 admin 维护的单一品牌资料，用于约束选搭和拍摄判断，不是品牌百科或营销话术库。

当前字段：

- `id`：固定为 1
- `name`：品牌名称
- `tagline`：品牌标语
- `brand_core`：品牌核心
- `style_dna`：风格 DNA
- `styling_guidelines`：选搭准则
- `shooting_guidelines`：拍摄准则
- `negative_constraints`：负向约束
- `created_at` / `updated_at`

规则：

- 初始为空，不 seed 默认品牌文案。
- 字段均为字符串，不做双语 key/value 结构。
- 删除后恢复为空状态，不影响已有历史任务快照。
- 进入生成链路时必须写入任务或 planner 快照，历史任务不读最新品牌资料覆盖旧结果。

### BrandStyleSample（品牌样本，后续对象）

`BrandStyleSample` 保存 Lily 官方或客户确认的风格证据，不是商品事实源。当前代码尚未实现独立表，设计上应作为后续品牌风格检索和配饰边界的基础。

建议字段：

- `source_type`
- `source_url` 或内部来源说明
- `season`
- `series`
- `category`
- `image_asset_id`
- `official_title`
- `visual_caption`
- `brand_style_tags`
- `accessory_boundary`
- `embedding`
- `confidence`
- `reviewed_by`
- `reviewed_at`

规则：

- 品牌样本只用于风格证据、姿态构图参考、配饰边界和未来检索。
- 不能把品牌样本里的商品当成本次可售服饰生成。

### ModelProfile（数字模特）

`ModelProfile` 描述默认数字模特的身份、身体参数和参考图。

当前字段：

- `id`
- `model_id`：业务标识，如 `Face-01`
- `name`
- `asset_dir`
- `profile_json`
- `is_default`
- `created_at` / `updated_at`

规则：

- MVP 只读展示当前默认数字模特。
- 生成时保存数字模特快照，历史再次生成默认使用当时快照。
- Image-2 输入中数字模特图是身份事实源，文本只能补充身体、发型、妆发和负向边界。
- `ModelIdentityContract` 是从模特快照派生的强制契约；ShootScript、分镜预览、品牌妆发和 provider 默认审美不得覆盖脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色和气质。

### PromptConfig（Prompt 配置）

`PromptConfig` 是可维护 Prompt 模板，不是任意策略表。

当前字段：

- `key`
- `group_type`：`segment` / `assembly`
- `title`
- `description`
- `content`
- `sort_order`
- `updated_at`

关键 assembly：

- `assembly.ai_styling`：AI Stylist 选搭规则、质量信号、配饰边界和输出要求。
- `assembly.shoot_script`：AI Director 拍摄剧本规则和输出要求。
- `assembly.storyboard_preview`：分镜预览 Image-2 输入文本。
- `assembly.final_image`：单张成片 Image-2 输入文本。

规则：

- 运行时占位符只注入阶段所需 DTO。
- 不恢复旧 `ai_policy_configs`。
- 不把业务审美规则散落成大量 Python if/else；硬编码只保留真实性、状态、可执行关联和安全边界。

### ShootConfig（拍摄配置）

`ShootConfig` 保存 AI Director 的全局规则、默认 6 镜头规则和按品类覆盖的 6 镜头规则。

当前字段：

- `id`：固定为 1
- `config_json`
- `created_at` / `updated_at`

规则：

- ShootConfig 用于拍摄剧本 prompt 摘要、结构兜底和状态恢复。
- 真正的拍摄剧本产物是 `ShootScript`，不是配置模板本身。
- 品类 ShotPlan 规则必须服务主推款品类，不允许所有品类套同一组通用全身图。

## styling 模块

### StylingGarment（搭配服装）

`StylingGarment` 是搭配服装库中的真实配角服装。它是可被选中进入最终图的商品事实源。

当前字段：

- `id`
- `code`：全局唯一业务主键，可修改；历史快照保存当时 code
- `status`
- `manual_tags_json`
- `ai_tags_json`
- `official_title_tags_json`
- `effective_tags_json`
- `tag_meta_json`
- `tag_schema_version`
- `tag_completed_at`
- `tag_error`
- `archived_at` / `archive_reason`
- `deleted_at`
- `created_at` / `updated_at`

状态：

```text
DRAFT
TAGGING_RUNNING
TAGGING_READY
READY
TAGGING_FAILED
DELETED
```

说明：

- `READY` 且未归档、未删除，才进入 AI Stylist 候选。
- `TAGGING_READY` 表示已有打标结果但需要复核，不进入选搭。
- `ai_tags_json` 是 Gemini 打标原始事实账本，必须完整保存。
- `effective_tags_json` 是后端合成后的生效标签，用于压缩候选卡。
- AI Stylist prompt 使用 `code` 识别候选；后端执行关联时必须映射回内部 `id`。
- 鞋、包、首饰、帽子等在 MVP 不应成为 StylingGarment。

### StylingGarmentImage（搭配服装图片）

搭配服装和图片是 1:N。

字段：

- `id`
- `styling_garment_id`
- `media_asset_id`
- `sort_order`
- `status`
- `deleted_at`
- `created_at` / `updated_at`

规则：

- 单套最多 6 张，推荐 1-3 张。
- 不做正面、背面、细节角色识别；排序越靠前越重要，第一张默认主视图。
- 图片排序、替换、删除或追加会影响打标，保存时应提示是否重新打标。
- 选搭完成后，预览与成片使用已冻结图片快照，而不是当前库记录。

### Tag Taxonomy（标签体系）

标签体系用于管理可维护标签、标签关系、品类适用规则和服饰标签赋值。当前实现包含：

- `tag_dimensions`
- `tag_defs`
- `tag_relations`
- `tag_applicability_rules`
- `garment_tag_assignments`

规则：

- `ai_tags_json` 允许随 Prompt 演进完整保存原始结构。
- 后端对常见字段做最佳努力归一化，形成 `effective_tags_json` 和标签赋值。
- 标签事实是选搭输入，不是审美结论。
- 人工业务标签只补充业务上下文，不改写图片可见事实。

长期统一服装资产模型可以演进为 `GarmentAsset`，让同一服装在不同任务里扮演 `hero`、`styling`、`brand_reference` 或 `production_reference`。当前 MVP 落地对象仍是 `StylingGarment` 和任务内主推款输入版本，不应把未来统一资产模型误当成已实现表。

## generation 模块

### GenerationTask（拍摄任务）

`GenerationTask` 是 AI 摄影棚任务根节点。草稿阶段保存主推款图片；进入 AI 阶段后通过输入版本冻结事实。

当前字段：

- `id`
- `name`
- `status`
- `current_input_version_id`
- `active_styling_id`
- `active_shoot_script_id`
- `active_preview_id`：历史兼容字段，新增语义优先使用 storyboard preview
- `active_storyboard_preview_id`
- `active_result_set_id`
- `archived_at` / `archive_reason`
- `deleted_at`
- `created_at` / `updated_at`

权威任务主状态：

```text
DRAFT
READY_FOR_STYLING
STYLING_RUNNING
STYLING_READY
SCRIPT_RUNNING
SCRIPT_READY
PREVIEW_RUNNING
PREVIEW_READY
FINAL_RUNNING
FINAL_PARTIAL
COMPLETED
FAILED
CANCELLED
ABANDONED
```

说明：

- `FAILED` 不是终点，页面应根据最新 artifact 判断可重试动作。
- 页面恢复读取 active 指针作为当前主线，旧 artifact 保留在历史中。
- 取消运行中任务后，外部 provider 返回不得写回 ready/succeeded 状态，也不得更新 active 指针。

### GenerationTaskImage（主推款草稿图片）

主推款图片在草稿阶段通过 `GenerationTaskImage` 维护。

字段：

- `id`
- `task_id`
- `media_asset_id`
- `sort_order`
- `status`
- `deleted_at`
- `created_at` / `updated_at`

规则：

- 最多 8 张，推荐 3-5 张。
- 支持散图或单文件夹上传。
- 不做图片角色识别，排序越靠前越重要，第一张默认主视图。
- 同一任务内文件名重名应提示。

### GenerationTaskInputVersion（输入版本）

进入 AI 阶段前，系统冻结当前主推款输入和默认模特快照，创建 `GenerationTaskInputVersion`。

字段：

- `id`
- `task_id`
- `version_number`
- `name_snapshot`
- `image_snapshot_json`
- `model_snapshot_json`
- `hero_visual_summary`
- `created_at`

规则：

- 主推款事实来自 `image_snapshot_json` 中的媒体资产引用。
- 用户返回上一步修改主推款图片时，应创建新输入版本，并使下游选搭、拍摄剧本、分镜预览和成片需要重新生成。
- 历史版本保留，不被当前素材库最新状态覆盖。

### GenerationStyling / StylingPlan（智能选搭）

`GenerationStyling` 是一次 AI Stylist 产物和快照记录；`StylingPlan` 是其核心 JSON。

当前字段：

- `id`
- `task_id`
- `input_version_id`
- `source_material_ids_json`
- `candidate_material_ids_json`
- `candidate_retrieval_snapshot_json`
- `selected_material_ids_json`
- `selected_material_snapshot_json`
- `styling_plan_json`
- `hero_understanding_json`
- `outfit_candidates_json`
- `critic_scores_json`
- `rejected_candidates_json`
- `reason_summary`
- `warnings_json`
- `status`
- `attempt_id`
- `created_at` / `updated_at`

StylingPlan 至少表达：

- 主推款理解：品类、视觉摘要、卖点、不可遮挡区域、展示风险。
- 造型意图：3-5 个方向。
- outfit 候选：真实搭配服装、生成配饰、穿着方式、主推款展示重点。
- Aesthetic Critic 诊断：色彩重量、廓形体量、腰线比例、季节材质、模板化风险、穿法合法性、官方样本证据和品牌采用可能性。
- 自评分数：审美、品牌贴合、主推款可见性、商业可用性。
- 选中方案、拒绝方案、风险、警告和原因。

关键规则：

- AI Stylist 接收未归档 READY 搭配库的全量标签事实卡；当前设计不由后端做预筛选、审美排序、候选评分或质量改选。
- Prompt 可见标识是 `code`，后端可执行关联必须用 `id`。
- `selected_material_ids` 可以为空。空库内搭配是合法优秀结果，不是失败。
- 非 READY、已归档、已删除、重复 ID、非法 ID 和当前主推款自己都不能作为选中搭配。
- 商品可见性只是底线；搭配沉闷、笨重、廉价、比例失衡、穿法别扭或不符合 Lily 时，仍应进入质量信号或失败复盘。
- `selected_material_ids_json` 是派生索引，不是完整选搭结果；下游必须读取 `styling_plan_json`。

### GenerationShootScript / ShootScript（拍摄剧本）

`GenerationShootScript` 是 AI Director 基于当前 StylingPlan 生成的拍摄剧本 artifact。

当前字段：

- `id`
- `task_id`
- `input_version_id`
- `styling_id`
- `status`
- `script_json`
- `summary_json`
- `styling_risks_json`
- `accessory_adjustments_json`
- `active_attempt_id`
- `locked_by` / `locked_until`
- `run_attempts`
- `superseded_at`
- `created_at` / `updated_at`

ShootScript 顶层结构：

```json
{
  "schema_version": "shoot-script-v1",
  "script_source_mode": "production",
  "input_refs": {},
  "global_directing": {},
  "styling_risks": [],
  "accessory_adjustments": [],
  "resolved_accessories": [],
  "shots": [],
  "user_visible_summary": {},
  "warnings": []
}
```

规则：

- 一次性生成全局导演设定和 6 个 ShotPlan。
- 用户可查看，不可编辑。
- 不重新选择库内搭配服装。
- 库内搭配不合适时，AI Director 只能披露风险和导演处理策略，不能自动替换库内服饰。
- 生成配饰可以调整或省略，但必须披露原因，并保持低存在感。
- 最小结构校验必须保证 JSON 可解析、schema 正确、6 个镜头齐全、镜头序号 1-6 且唯一。

### GenerationShotPlan（镜头方案）

`GenerationShotPlan` 从 ShootScript 派生，表示一个结构化镜头。

字段：

- `id`
- `task_id`
- `styling_id`
- `shoot_script_id`
- `sort_order`
- `shot_type`
- `plan_json`
- `created_at` / `updated_at`

规则：

- 每个 ShootScript 固定 6 条 ShotPlan。
- 前 5 条进入最终成片，第 6 条只用于分镜预览参考。
- ShotPlan 必须服务主推款品类和卖点，不能所有品类使用同一套全身图模板。
- 单镜头至少应表达取景、姿态、身位、镜头角度、主展示重点、可见服饰、穿着关系、配饰边界、构图和负向约束。

### GenerationStoryboardPreview（分镜预览）

当前数据库表名为 `generation_previews`，领域语义是 `GenerationStoryboardPreview`。

字段：

- `id`
- `task_id`
- `styling_id`
- `shoot_script_id`
- `input_version_id`
- `preview_asset_id`
- `status`
- `active_attempt_id`
- `locked_by` / `locked_until`
- `run_attempts`
- `created_at` / `updated_at`

规则：

- 只保存一张 3x2 分镜预览总图，不裁切 6 个 frame。
- 读取当前最新成功 ShootScript。
- 分镜预览失败不影响拍摄剧本。
- 成功重新生成预览后，旧成片标记失效或被新 active 结果集替代。

### GenerationResultSet / GenerationResultImage（最终成片）

`GenerationResultSet` 表示一次最终图生成批次，`GenerationResultImage` 表示批次中的一张图。

ResultSet 字段：

- `id`
- `task_id`
- `input_version_id`
- `styling_id`
- `shoot_script_id`
- `preview_id`
- `status`
- `created_at` / `updated_at`

ResultImage 字段：

- `id`
- `result_set_id`
- `shot_plan_id`
- `sort_order`
- `status`
- `current_asset_id`
- `active_attempt_id`
- `locked_by` / `locked_until`
- `run_attempts`
- `created_at` / `updated_at`

规则：

- 固定生成前 5 个 ShotPlan。
- 5 张图可并发生成。
- 每张图只传当前单镜头上下文和分镜预览对应第 N 格参考。
- 支持整组重新生成。
- 支持单张成片重生；单张重生不应影响其他已成功成片。

### Artifact 状态

阶段 artifact 使用统一语义：

```text
QUEUED
RUNNING
SUCCEEDED
FAILED
SUPERSEDED
```

当前部分表字段和代码中可能只落地了其中一部分状态，但领域语义应保持一致。`SUPERSEDED` 表示曾经有效，但被新的上游版本替代；旧数据必须保留，不能删除。

## operations 模块

### GenerationAttempt（AI/provider 调用）

所有外部 AI/provider 调用都应记录 attempt。

owner 类型：

- `styling_garment_tagging`
- `generation_styling`
- `generation_shoot_script`
- `generation_preview`
- `generation_result_image`

字段：

- `id`
- `owner_type`
- `owner_id`
- `provider`
- `provider_request_id`
- `status`
- `started_at`
- `completed_at`
- `failed_at`
- `cancelled_at`
- `error_code`
- `error_message`
- `request_snapshot_json`
- `response_snapshot_json`
- `output_asset_id`
- `retry_of_attempt_id`
- `created_at` / `updated_at`

规则：

- 普通前台默认不展示完整 request/response。
- 技术详情可以展示模型输入、模型输出、provider id、错误和实际输出资产。
- 禁止未经用户明确要求触发会消耗 Gemini、Image-2、ToAPIs、fal 等额度的调试动作。

### EventLog（业务事件）

`EventLog` 记录业务事件，用于审计和排查，不是普通用户的生成记录主视图。

字段：

- `id`
- `entity_type`
- `entity_id`
- `event_type`
- `payload_json`
- `created_at`

当前实现同时写入 SQLite 和 `backend/data/logs/*.jsonl`。

### DeleteImpact（删除影响预检）

`DeleteImpact` 是删除或彻底清理前的影响报告，不一定对应独立数据库表。

常见字段：

- `garmentCount`
- `dependentTaskIds`
- `taskCount`
- `mediaAssetCount`
- `estimatedBytes`
- `stylingCount`
- `shootScriptCount`
- `previewCount`
- `resultSetCount`
- `resultImageCount`

规则：

- 默认归档，不物理删除。
- 彻底删除必须要求确认词或确认名称。
- 删除搭配服装若影响历史任务，必须展示依赖任务数量和级联媒体影响。

## 主状态流转

任务主线：

```text
DRAFT
-> READY_FOR_STYLING
-> STYLING_RUNNING
-> STYLING_READY
-> SCRIPT_RUNNING
-> SCRIPT_READY
-> PREVIEW_RUNNING
-> PREVIEW_READY
-> FINAL_RUNNING
-> FINAL_PARTIAL
-> COMPLETED
```

异常分支：

```text
任意运行阶段 -> FAILED
任意运行阶段 -> CANCELLED
用户放弃 -> ABANDONED
```

重新生成与失效：

| 动作 | 新建 artifact | 成功后失效 |
|---|---|---|
| 重新选搭 | GenerationStyling | 旧 ShootScript、旧分镜预览、旧结果集 |
| 重新生成拍摄剧本 | GenerationShootScript | 旧分镜预览、旧结果集 |
| 重新生成分镜预览 | GenerationStoryboardPreview | 旧结果集 |
| 重新生成成片 | GenerationResultSet | 旧结果集从当前主线移除 |
| 单张重生 | GenerationResultImage attempt 或单图版本 | 不影响同组其他成功图 |

## Image-2 输入预算

Image-2 单次最多 10 张输入图。MVP 选图策略：

| 角色 | 数量 |
|---|---:|
| 分镜预览图 | 最终成片时 1 张，位于数字模特之后 |
| 主推款 | 按排序最多 3 张 |
| 搭配服装 | 每套至少第 1 张，总计最多 4 张 |
| 数字模特 | 最多 2 张，固定作为 `reference_images` 前 2 位 |

如果预算冲突，当前先固定截断并记录实际输入资产；后续可引入更精细的选图器。每次 Image-2 任务包必须记录 `image_reference_manifest`，声明每张输入图的序号、role 和事实源。

## 关键业务禁止项

- 不把用户训练成 prompt 工程师。
- 不把 Image-2 当成选搭策略、标签体系、历史状态机或商品事实判断器。
- 不把全量搭配服装库图片直接喂给 Gemini。
- 搭配库 `ai_tags` 标签事实只进入 AI Stylist 阶段；不把历史 prompt、旧候选快照和 provider response 透传到下游 prompt。
- 不把 `selected_material_ids` 当成 StylingPlan 的全部内容。
- 不让 `category = unknown` 的主推款直接进入正常品类 ShotPlan。
- 不让当前主推款自己作为搭配服装候选。
- 不把搭配失败只归因于模型审美；必须区分库内供给不足、Prompt 规则不足和模型判断失败。
- 不为露出腰线强行生成服饰事实不支持的穿法。
- 不用通用全身图模板覆盖裤装、半裙、上衣、外套、连衣裙等不同品类。
- 不把鞋、包、首饰当作商品级事实源，除非未来正式入库。
- 不用当前素材库、品牌资料或数字模特最新状态覆盖历史快照。
- 不把第 6 格纳入最终成片。

## 当前实现与后续演进

当前已实现：

- FastAPI + SQLite + React/Vite 单体 MVP。
- S3/RustFS 对象存储和三层媒体资产。
- 搭配服装库、Gemini 打标、标签规则、Prompt 维护。
- AI 摄影棚五步工作流。
- `GenerationTaskInputVersion` 输入冻结。
- `GenerationStyling`、`GenerationShootScript`、`GenerationShotPlan`、`GenerationStoryboardPreview`、`GenerationResultSet` 和单张重试。
- provider attempt 快照和生成记录复盘。

仍需收敛或后续实现：

- 独立 HeroUnderstanding provider，替代任务名或同 code 启发式 fallback。
- Lily Brand Style Index 独立对象、样本导入和检索。
- 前台展示 `StylingPlan` 候选比较、候选排除原因、质量信号和补充素材建议。
- 多模态 embedding、向量检索和反馈闭环。
- 单张重生版本与当前成功图更清晰地分离，避免失败重生覆盖已成功结果。
- 生产级错误码、事件等级、可恢复操作指引和日志检索。
