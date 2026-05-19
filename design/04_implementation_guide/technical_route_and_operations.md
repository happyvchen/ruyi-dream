# 技术路线与生产实操

## 1. 核心架构

新版 MVP 的工程链路：

```text
MediaAsset
-> StylingGarment 打标
-> GenerationTask / InputVersion
-> Gemini 选搭
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

智能选搭结果：

- id
- task_id
- input_version_id
- source_material_ids_json
- selected_material_ids_json
- selected_material_snapshot_json
- reason_summary
- warnings_json
- status
- attempt_id
- created_at / updated_at

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
- `styling selection service`：组装主推款图片和全量 tags，调用 Gemini 选搭。
- `shot plan service`：生成 6 条 ShotPlan。
- `image input selector`：按阶段选择最多 10 张 Image-2 输入图。
- `image generation service`：六宫格预览和最终图生成。
- `attempt service`：provider 调用、重试、耗时、错误和快照。
- `history service`：生成记录列表、详情、技术详情。

## 4. 模型调用

### 4.1 Gemini 打标

输入：一套搭配服饰图片。  
输出：稳定 schema 的 `ai_tags_json`。  
失败：`TAGGING_FAILED`，不进入选搭候选。

### 4.2 Gemini 智能选搭

输入：

- 主推款图片。
- 全部 `READY` 搭配服饰 tags。

输出：

- `selected_material_ids`
- `hero_visual_summary`
- `reason_summary`
- `warnings`

校验：

- 空集合允许。
- 非法 ID、非 READY、重复 ID、不可解析 JSON 失败。

### 4.3 ShotPlan 生成

输入：

- 主推款图片或极简视觉摘要。
- 选中的搭配服饰快照。
- 数字模特快照。
- 品牌棚拍边界。

输出：6 条结构化 ShotPlan。

### 4.4 Image-2 六宫格预览

输出一张 3x2 六宫格总图。MVP 不裁切 frame。

### 4.5 Image-2 最终图

并发生成前 5 张。每张传同一张六宫格预览图，并在 prompt 中指定参考第 N 格。

## 5. API 草案

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

## 6. 实现注意

- 所有状态推进由后端服务完成，前端不直接写阶段状态。
- 成功后的“重新搭配 / 重新预览 / 整组换版”不进 MVP。
- 失败重试必须保留旧 attempt。
- `ImageCount` 不作为强字段，界面需要时统计。
- 生成记录默认隐藏技术详情。
- 主推款、搭配服饰、数字模特、ShotPlan 和 provider 调用都要快照。
