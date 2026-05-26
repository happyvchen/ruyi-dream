# 技术路线与生产实操

## 1. 核心架构

新版 MVP 的工程链路：

```text
MediaAsset
-> StylingGarment 打标
-> GenerationTask / InputVersion
-> Brand Style Index / Retrieval Features
-> Candidate Funnel 候选漏斗
-> AI Stylist / StylingPlan
-> ShotPlan
-> Image-2 六宫格预览
-> Image-2 前 5 张并发生成
-> 生成记录复盘
```

保留旧 demo 中可复用的工程手段：

- S3 兼容对象存储，私有化默认 RustFS。
- 原图 / preview / thumb 三层媒体结构。
- 后台任务与状态轮询。
- provider request/response 快照。
- 前端稳定媒体读取路径，不直接依赖短期预签名 URL。

不延续旧版“货号默认组图方案”作为主流程。

## 2. 建议数据模型

### 2.1 MediaAsset

统一保存媒体文件：

- id
- object_key
- thumb_key
- preview_key
- original_filename
- mime_type
- width
- height
- size_bytes
- hash
- status
- deleted_at
- created_at / updated_at

### 2.2 StylingGarment

搭配服饰库对象：

- id
- code
- status
- manual_tags_json
- ai_tags_json
- official_title_tags_json
- effective_tags_json
- tag_meta_json
- tag_schema_version
- tag_completed_at
- created_at / updated_at / deleted_at

`code` 全局唯一、可修改；历史生成快照保存当时 code。

### 2.3 StylingGarmentImage

- id
- styling_garment_id
- media_asset_id
- sort_order
- status
- created_at / updated_at / deleted_at

### 2.4 GenerationTask

AI 摄影棚任务外壳：

- id
- name
- status
- current_input_version_id
- active_styling_id
- active_preview_id
- active_result_set_id
- created_at / updated_at

状态：

```text
DRAFT
READY_FOR_STYLING
STYLING_RUNNING
STYLING_READY
PREVIEW_RUNNING
PREVIEW_READY
FINAL_RUNNING
FINAL_PARTIAL
COMPLETED
FAILED
CANCELLED
ABANDONED
```

### 2.5 GenerationTaskInputVersion

主推款输入版本：

- id
- task_id
- version_number
- name_snapshot
- image_snapshot_json
- hero_visual_summary
- created_at

主推款图片可另建 `GenerationTaskInputImage`，也可在 MVP 用 JSON 快照引用 `MediaAsset`。

### 2.6 GenerationStyling

AI Stylist 造型方案结果：

- id
- task_id
- input_version_id
- source_material_ids_json
- candidate_material_ids_json
- candidate_retrieval_snapshot_json
- outfit_drafts_json
- brand_evidence_snapshot_json
- selected_material_ids_json
- selected_material_snapshot_json
- styling_plan_json
- hero_understanding_json
- outfit_candidates_json
- critic_scores_json
- rejected_candidates_json
- reason_summary
- warnings_json
- status
- attempt_id
- created_at / updated_at

`selected_material_ids_json` 只是从 `StylingPlan` 派生出的关系索引，用于素材快照、查询和前端卡片展示，不是选搭阶段的完整结果。生产排查和后续 ShotPlan 必须读取 `styling_plan_json`。

成功后是否允许重新搭配仍是待拍板决策；当前代码暂时允许重新选搭并保留历史结果。失败重试可以新增 attempt 并更新当前 styling 状态。

### 2.6.1 StylingGarmentRetrievalFeature

阶段 1 可直接并入 `effective_tags_json`，阶段 2/3 建议拆表。字段必须覆盖“能不能搭”和“怎么穿才成立”，不能只保存视觉 caption：

- id
- styling_garment_id
- visual_caption
- volume_tags_json
- length_tags_json
- coverage_risk
- wearing_affordance_json
- wearing_forbidden_json
- tuck_affordance
- tuck_risk_json
- retrieval_risk_json
- embedding_json
- embedding_model
- embedding_version
- created_at / updated_at

这些字段用于候选漏斗，不直接展示给普通用户。

### 2.6.2 BrandStyleSample

Lily 官方和客户确认的品牌样本：

- id
- source_type
- source_url
- season
- series
- category
- image_asset_id
- official_title
- visual_caption
- brand_style_tags_json
- accessory_boundary_json
- embedding_json
- embedding_model
- embedding_version
- confidence
- reviewed_by
- reviewed_at
- created_at / updated_at

品牌样本是风格证据，不是商品事实源。生成时不能把品牌样本中的商品当作本次可售服饰生成。

### 2.6.3 HeroUnderstanding

主推款视觉理解结果。它必须在候选漏斗、AI Stylist 和 ShotPlan 之间复用，不能只靠任务名推断。

- id
- task_id
- input_version_id
- provider
- category
- sub_category
- visual_summary
- selling_points_json
- visibility_risks_json
- forbidden_occlusion_zones_json
- shot_requirements_json
- wearing_constraints_json
- confidence
- request_snapshot_json
- response_snapshot_json
- created_at / updated_at

任务名、文件夹名和 SKU 只能作为辅助证据；主推款品类、卖点和不可遮挡区域以图片理解为准。

### 2.7 GenerationShotPlan

- id
- task_id
- styling_id
- sort_order
- shot_type
- plan_json
- created_at / updated_at

固定 6 条，最终只用前 5 条。

### 2.8 GenerationPreview

- id
- task_id
- styling_id
- input_version_id
- preview_asset_id
- status
- active_attempt_id
- created_at / updated_at

MVP 只保存一张六宫格总图，不裁 frame。未来支持重新预览时，可允许一个 styling 下多条 preview。

### 2.9 GenerationResultSet

- id
- task_id
- input_version_id
- styling_id
- preview_id
- status
- created_at / updated_at

### 2.10 GenerationResultImage

- id
- result_set_id
- shot_plan_id
- sort_order
- status
- current_asset_id
- created_at / updated_at

固定 5 条，对应前 5 个 ShotPlan。

### 2.11 GenerationAttempt

可统一承载 Gemini 和 Image-2 调用：

- id
- owner_type
- owner_id
- provider
- provider_request_id
- status
- started_at
- completed_at
- failed_at
- cancelled_at
- error_code
- error_message
- request_snapshot_json
- response_snapshot_json
- output_asset_id
- retry_of_attempt_id
- created_at / updated_at

页面耗时以 attempt 时间为准。

## 3. 服务模块

建议拆分：

- `media service`：上传、衍生图、预签名、稳定读取路径。
- `styling garment service`：搭配服饰草稿、保存、打标、状态推进。
- `generation task service`：任务草稿、输入版本、状态机。
- `brand style service`：维护 Lily 官方样本、客户 lookbook、品牌 caption、配饰边界和未来 embedding。
- `candidate funnel service`：基于 READY 服饰 tags、caption、颜色、品类、季节、遮挡风险、品牌样本相似度和未来 embedding，生成候选召回、兼容性排序和 outfit 草案快照。
- `AI Stylist service`：组装主推款图片、小候选集、候选缩略图、outfit 草案、品牌证据和商品验收维度，调用 Gemini 生成 `StylingPlan`。
- `aesthetic critic service`：校验 `StylingPlan` 的基础业务事实、可穿性、审美成立性、品牌成立性、主推款可见性和商业可用性阈值。
- `shot plan service`：基于 `StylingPlan` 生成 6 条 ShotPlan。
- `image input selector`：按阶段选择最多 10 张 Image-2 输入图。
- `image generation service`：六宫格预览和最终图生成。
- `attempt service`：provider 调用、重试、耗时、错误和快照。
- `history service`：生成记录列表、详情、技术详情。

## 4. AI Stylist 实现建议

### 4.1 调用结构

优先实现为可追溯的分步工作流：

```text
HeroUnderstanding
-> StylingIntentExploration
-> OutfitCandidateGeneration
-> AestheticCritic
-> StylingPlanSelection
```

工程上可以先用一次 Gemini 调用返回完整 JSON，但 `request_snapshot_json` 和 `response_snapshot_json` 必须保存分步结构，不能只保存最终 `selected_material_ids`。

### 4.2 候选漏斗

MVP 不再把全量 READY tags 直接交给 Gemini。候选漏斗先把全量库缩小为少量候选，再由 Gemini 做审美决策。

阶段 1 使用规则化漏斗，但必须以前置 `HeroUnderstanding` 为输入：

- 只取 `READY` 搭配服饰。
- 压缩 `effective_tags`。
- 使用主推款视觉理解出的品类、卖点和不可遮挡区域做硬过滤。
- 根据品类互补、颜色、季节、廓形和穿着方式粗排。
- 降权 `coverage_risk` 高、长外套、深色厚重、大体量、风格风险高或季节冲突明显的服饰。
- 校验穿法能力：候选草案不得建议搭配服饰未支持的穿法；开衫、外套、厚针织和有明显门襟/纽扣/镂空下摆结构的上衣默认禁止全塞。
- 生成 3-8 套 outfit 草案，并保留一定多样性，避免候选全是同一公式。
- AI Stylist 输入控制在 12-20 件候选服饰，并附必要主图或缩略图。

后续生产增强：

- Lily Brand Style Index 官方样本检索。
- CLIP / 多模态 embedding 召回与相似度排序。
- 历史人工采纳、拒绝、下载、复拍和销售/点击表现。
- 品牌专属偏好 profile 和 compatibility ranker。

### 4.3 业务校验

后端校验应覆盖真实性和生产边界，而不是硬编码审美：

- 选中 ID 必须来自本次 READY 候选。
- 不允许重复 ID。
- 空选中合法。
- 明显互斥穿搭必须在 `wearing_notes` 中解释，否则拒绝。
- `wearing_notes` 中的穿法必须被搭配服饰 `wearing_affordance` 支持；模型新增穿法必须说明结构依据并进入风险提示。
- 推荐穿法不包含 `tucked` 的开衫、外套、厚针织、明显门襟/纽扣/镂空下摆上衣，不得被要求全塞。
- `aesthetic_score`、`brand_fit_score`、`hero_visibility_score` 和 `commercial_readiness_score` 任一低于阈值时，不自动进入预览。
- 满分评分但缺少 `aesthetic_diagnosis` 或风险解释时，视为低可信模型输出，应重试或降级为空选搭。
- 主推款是高腰裤、连衣裙、收腰上衣等强腰线商品时，选择外套必须说明如何避免色块压迫、腰线遮挡和比例失衡。
- 有遮挡风险、穿法风险、模板化风险、配饰遮挡风险或品牌证据不足时，不允许四项满分。
- 公文包、大体量手提包、夸张首饰等生成配饰必须受控，不能由 Gemini 自由补全为传统办公室套装。
- `StylingPlan` 必须保存拒绝方案和原因，便于复盘。

### 4.4 ShotPlan 联动

ShotPlan 生成必须读取：

- `HeroUnderstanding.category`
- `selected_outfit.mode`
- `hero_selling_points`
- `hero_visibility_risks`
- `wearing_notes`
- `generated_accessories`
- `risk_notes`

例如主推款是白色高腰阔腿裤时，搭配方案可以完整，但 ShotPlan 必须保证裤子的高腰、褶裥、垂坠和比例是图位重点，而不是让上装或外套成为主角。

ShotPlan 不能使用一套通用模板服务所有品类。裤装必须至少覆盖：

- 全身或近全身主视觉，裤脚和鞋完整。
- 腰脚下装图或近全身，突出腰头、门襟、裤褶、裤腿和裤长。
- 腰膝或腰胯近景，突出腰带扣、褶位、口袋和面料。
- 3/4 或侧身轻走动，展示侧缝、垂坠和裤脚动态。
- 正面颜色/版型图，稳定、平台友好。

如果第 6 格备用姿态明显优于前 5 格，前 5 格 ShotPlan 应重排，而不是让第 6 格继续被忽略。

## 5. 模型调用

### 5.1 Gemini 打标

输入：

- 一套搭配服饰的前 3-6 张图片及图片元数据。
- 可选官方标题或商品信息。
- Lily 品牌边界。
- `lily-garment-tags-v1` 的完整枚举、字段判定规则、复核条件和 JSON 输出模板。

输出：

- `ai_tags_json`
- `official_title_tags_json`
- `tag_meta_json`
- 后端合成的 `effective_tags_json`

治理：

- 后端对枚举、嵌套字段、置信度和短文本做归一化与校验。
- 低置信度、配饰误入、品类不明确、模型标记复核或 fallback 结果进入 `TAGGING_READY`，不进入选搭候选。
- 解析/调用失败进入 `TAGGING_FAILED`。
- 每次调用以 `GenerationAttempt.owner_type = styling_garment_tagging` 保存 prompt、图片输入元数据、原始/归一化返回和错误信息。

### 5.2 AI Stylist / StylingPlan

输入：

- 主推款图片。
- 候选漏斗快照。
- 小候选集搭配服饰压缩 tags、caption 和必要主图/缩略图。
- 后端生成的 outfit 草案。
- 与本次任务相关的 Lily 品牌样本证据。
- Lily 品牌边界。
- 商品主角验收维度。

输出：

- `hero_understanding`
- `styling_intents`
- `outfit_candidates`
- `aesthetic_diagnosis`
- `selected_outfit_id`
- `selected_material_ids` 派生索引
- `rejected_candidates`
- `critic_scores`
- `warnings`
- `brand_evidence` 或等价品牌证据引用

校验：

- 空集合允许。
- 非法 ID、非 READY、重复 ID、不可解析 JSON 失败。
- 明显互斥穿搭缺少穿着解释时失败。
- 审美、品牌贴合、商品可见性或商业可用性任一低于阈值时不自动进入预览。

### 5.3 ShotPlan 生成

输入：

- `HeroUnderstanding` 和必要主推款图片。
- `StylingPlan`。
- 选中的搭配服饰快照。
- 数字模特快照。
- 品牌棚拍边界。
- 主推品类对应的 `category_shot_rules.md` 图位矩阵。

输出：6 条结构化 ShotPlan。

校验：

- 前 5 条必须覆盖主推品类的核心图位，不得大量重复全身图。
- 每条 `main_focus` 必须服务主推款卖点，不能让搭配服饰成为默认主角。
- 裤装、半裙、短裤等下装必须包含腰脚或腰膝近景；上衣必须包含臀上/胸腰近景；连衣裙必须保留完整比例和腰线/裙摆说明。

### 5.4 Image-2 六宫格预览

输出一张 3x2 六宫格总图。MVP 不裁切 frame。

### 5.5 Image-2 最终图

并发生成前 5 张。每张传同一张六宫格预览图，并在 prompt 中指定参考第 N 格。

### 5.6 阶段性实施计划

#### 阶段 1：修正主推款理解、穿法质量门和品类 ShotPlan

不引入新基础设施，先修正当前白色高腰阔腿裤案例中的明确失败：

- 新增或固化 `HeroUnderstanding` 步骤，以图片识别主推品类、卖点、不可遮挡区域和图位需求。
- 将 `HeroUnderstanding` 作为候选漏斗、AI Stylist 和 ShotPlan 的共同输入，删除任务名启发式作为主来源的旧口径。
- 扩展搭配服饰穿法能力和穿法禁忌字段，区分 `tucked`、`half_tucked`、`untucked`、`open`、`buttoned`、`layered`。
- 增加穿法合法性质量门，禁止未支持 `tucked` 的开衫/外套/厚针织被要求全塞。
- 扩展 Aesthetic Critic：`wearing_method_validity`、`tuck_quality`、`official_style_evidence`、`shotplan_readiness`。
- ShotPlan 生成读取主推品类矩阵；裤装必须覆盖全身、腰脚、腰膝/腰胯、侧身/动态和标准正面颜色图。
- 第 6 格备用姿态若优于前 5 格，前 5 格重排。

验收：

- 白色高腰阔腿裤不再默认把镂空开衫强行全塞。
- 裤装六宫格不再出现 4 张以上重复全身图。
- AI Stylist 不允许在缺少穿法证据或官方样本证据时给四项满分。

#### 阶段 2：Lily 官方样本库

继续沿用现有数据库和对象存储：

- 新增品牌样本数据结构。
- 导入 Lily 中国官网、官方天猫/淘宝、海外官方店、官方社媒和客户 lookbook 的可验证样本。
- 为品牌样本生成品类、图位、取景、姿势、穿法、鞋包、背景、style tags 和人工复核状态。
- AI Stylist 输入携带 3-5 个相关品牌样本摘要。
- ShotPlan 生成按主推品类检索同品类同图位样本。

验收：

- 方案能说明“像哪类 Lily 官方造型”，而不是泛泛写“符合 Lily”。
- 配饰建议能对齐官方样本，不再默认公文包化。

#### 阶段 3：多模态 embedding 和向量检索

当搭配服饰库和品牌样本规模扩大到几百或上千件时引入：

- 多模态 embedding 模型。
- 向量存储，可从数据库 JSON/内存计算起步，后续迁移到 pgvector、Qdrant、Milvus 或 Vertex AI Vector Search。
- 图像相似度、品牌样本相似度和主推款视觉兼容度排序。
- embedding 版本管理和召回快照。

#### 阶段 4：反馈闭环与品牌偏好学习

- 记录用户接受、拒绝、重选、下载、复拍和人工否决原因。
- 维护 compatibility ranker 或可配置排序规则。
- 对不同系列、季节、品类维护品牌偏好 profile。

## 6. API 草案

仅作为实现方向：

```text
POST   /api/media

GET    /api/styling-garments
POST   /api/styling-garments
PATCH  /api/styling-garments/{id}
POST   /api/styling-garments/{id}/save-and-tag
POST   /api/styling-garments/{id}/retry-tagging

POST   /api/generation-tasks
PATCH  /api/generation-tasks/{id}
POST   /api/generation-tasks/{id}/input-versions
POST   /api/generation-tasks/{id}/select-styling
POST   /api/generation-tasks/{id}/preview
POST   /api/generation-tasks/{id}/generate-final
POST   /api/generation-result-images/{id}/retry

GET    /api/generation-records
GET    /api/generation-records/{taskId}
GET    /api/generation-records/{taskId}?includeDebug=true
```

## 7. 实现注意

- 所有状态推进由后端服务完成，前端不直接写阶段状态。
- 成功后的“重新搭配 / 重新预览 / 整组换版”策略待产品拍板；当前代码暂时允许整阶段重跑，若决定禁止需同步前后端状态机。
- 失败重试必须保留旧 attempt。
- `ImageCount` 不作为强字段，界面需要时统计。
- 生成记录默认隐藏技术详情。
- 搭配服饰详情展示完整分层标签；Gemini 打标请求/返回属于默认折叠的技术详情。
- 主推款、候选召回、StylingPlan、搭配服饰、数字模特、ShotPlan 和 provider 调用都要快照。
