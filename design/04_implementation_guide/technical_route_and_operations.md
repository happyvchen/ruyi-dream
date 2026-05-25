# 技术路线与生产实操

## 1. 核心架构

新版 MVP 的工程链路：

```text
MediaAsset
-> StylingGarment 打标
-> GenerationTask / InputVersion
-> Candidate Retrieval 候选召回
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

MVP 不支持成功后重新搭配。失败重试可以新增 attempt 并更新当前 styling 状态。

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
- `candidate retrieval service`：基于 READY 服饰 tags、颜色、品类、季节、风险和未来 embedding 生成候选召回快照。
- `AI Stylist service`：组装主推款图片、候选卡片、品牌边界和商品验收维度，调用 Gemini 生成 `StylingPlan`。
- `styling critic service`：校验 `StylingPlan` 的基础业务事实、可穿性、主推款可见性和商业可用性阈值。
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
-> StylistCritic
-> StylingPlanSelection
```

工程上可以先用一次 Gemini 调用返回完整 JSON，但 `request_snapshot_json` 和 `response_snapshot_json` 必须保存分步结构，不能只保存最终 `selected_material_ids`。

### 4.2 候选召回

MVP 可先使用规则化召回：

- 只取 `READY` 搭配服饰。
- 压缩 `effective_tags`。
- 根据主推款品类和 tags 粗排互补品类。
- 降权 `coverage_risk` 高、风格风险高或季节冲突明显的服饰。
- 保留一定多样性，避免候选全是同一品类。

后续生产增强：

- CLIP / 多模态 embedding 召回。
- 历史人工采纳、拒绝、下载、复拍和销售/点击表现。
- 品牌专属偏好 profile。

### 4.3 业务校验

后端校验应覆盖真实性和生产边界，而不是硬编码审美：

- 选中 ID 必须来自本次 READY 候选。
- 不允许重复 ID。
- 空选中合法。
- 明显互斥穿搭必须在 `wearing_notes` 中解释，否则拒绝。
- `hero_visibility_score` 和 `commercial_readiness_score` 低于阈值时，不自动进入预览。
- `StylingPlan` 必须保存拒绝方案和原因，便于复盘。

### 4.4 ShotPlan 联动

ShotPlan 生成必须读取：

- `selected_outfit.mode`
- `hero_selling_points`
- `hero_visibility_risks`
- `wearing_notes`
- `generated_accessories`
- `risk_notes`

例如主推款是白色高腰阔腿裤时，搭配方案可以完整，但 ShotPlan 必须保证裤子的高腰、褶裥、垂坠和比例是图位重点，而不是让上装或外套成为主角。

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
- 候选召回快照。
- READY 搭配服饰压缩 tags。
- Lily 品牌边界。
- 商品主角验收维度。

输出：

- `hero_understanding`
- `styling_intents`
- `outfit_candidates`
- `selected_outfit_id`
- `selected_material_ids` 派生索引
- `rejected_candidates`
- `critic_scores`
- `warnings`

校验：

- 空集合允许。
- 非法 ID、非 READY、重复 ID、不可解析 JSON 失败。
- 明显互斥穿搭缺少穿着解释时失败。
- 商品可见性或商业可用性低于阈值时不自动进入预览。

### 5.3 ShotPlan 生成

输入：

- 主推款图片或极简视觉摘要。
- `StylingPlan`。
- 选中的搭配服饰快照。
- 数字模特快照。
- 品牌棚拍边界。

输出：6 条结构化 ShotPlan。

### 5.4 Image-2 六宫格预览

输出一张 3x2 六宫格总图。MVP 不裁切 frame。

### 5.5 Image-2 最终图

并发生成前 5 张。每张传同一张六宫格预览图，并在 prompt 中指定参考第 N 格。

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
- 成功后的“重新搭配 / 重新预览 / 整组换版”不进 MVP。
- 失败重试必须保留旧 attempt。
- `ImageCount` 不作为强字段，界面需要时统计。
- 生成记录默认隐藏技术详情。
- 搭配服饰详情展示完整分层标签；Gemini 打标请求/返回属于默认折叠的技术详情。
- 主推款、候选召回、StylingPlan、搭配服饰、数字模特、ShotPlan 和 provider 调用都要快照。
