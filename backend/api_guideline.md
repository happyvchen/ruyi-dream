# Backend API Guideline

## 设计原则

- API 面向前台“拍摄任务工作台”，不暴露 prompt 工程细节。
- task package 可以作为调试字段存在，但默认不展示给普通用户。
- 所有会覆盖素材、结果或实验记录的操作必须显式确认。

## 当前资源

| Resource | Method | 说明 |
|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/garment-skus` | GET | 货号图包列表 |
| `/api/garment-skus` | POST | 上传一个货号图包和多张素材图 |
| `/api/garment-skus/{sku_id}` | GET | 货号详情、素材图片、识别摘要 |
| `/api/garment-skus/{sku_id}` | PATCH | 编辑货号、标题、状态 |
| `/api/garment-skus/{sku_id}` | DELETE | 删除应用内素材副本；已有生成记录时禁止删除 |
| `/api/garment-skus/{sku_id}/images` | POST | 给已有货号追加素材图 |
| `/api/garment-images/{image_id}` | PATCH | 编辑单张图片角色和排序；当前 UI 不开放角色编辑 |
| `/api/garment-images/{image_id}` | DELETE | 删除应用内图片副本；每个货号至少保留一张 |
| `/api/model-profiles` | GET | 数字模特档案列表 |
| `/api/model-profiles` | POST | 新建数字模特档案 |
| `/api/model-profiles/{model_profile_id}` | PATCH | 编辑数字模特基础档案 |
| `/api/model-profiles/{model_profile_id}` | DELETE | 删除数字模特；至少保留一个，且已有历史记录时禁止删除 |
| `/api/model-profiles/{model_profile_id}/head-references` | POST | 上传头像；可单张替换指定角度，也可多张作为未设置头像上传 |
| `/api/model-profiles/{model_profile_id}/head-references/{reference_id}` | PATCH | 修改头像角度或排序 |
| `/api/model-profiles/{model_profile_id}/head-references/{reference_id}` | DELETE | 删除单张头像 |
| `/api/model-profiles/{model_profile_id}/assets/{filename}` | GET | 跳转到模特头像素材的预签名访问 URL |
| `/api/shot-plans` | POST | 根据货号和模特生成组图方案 |
| `/api/shot-plans/{shot_plan_id}` | GET | 查看组图方案 |
| `/api/garment-skus/{sku_id}/default-shot-plan` | GET | 获取货号默认组图方案，当前取最新一条 `shot_plans` |
| `/api/generation-runs` | POST | 根据组图方案创建生成批次，并提交每张图到后台生成队列 |
| `/api/generation-runs` | GET | 生成批次列表；返回前台历史任务摘要、缩略图和重生次数 |
| `/api/generation-runs/{run_id}` | GET | 生成批次详情和单张结果 |
| `/api/generation-images/{image_id}/regenerate` | POST | 单张重生；不新增生成任务记录，在同一记录追加 attempt |
| `/api/generation-images/{image_id}/final` | PATCH | 设置/取消最终图 |
| `/api/logs` | GET | 事件日志 |
| `/api/exports/runs/{run_id}` | POST | 打包导出该 run 的最终图或全部图 |

当前没有独立 `/api/brand-templates`，品牌模板暂时内嵌在 planner 输出和 task package 中。

前台“生成记录”列表使用 `/api/generation-runs`，不是直接展示 `/api/logs`。`event_logs` 仍用于排查和审计。

## 服装素材 CRUD 契约

### 上传与默认方案

`POST /api/garment-skus`

使用 multipart form-data：

| 字段 | 说明 |
|---|---|
| `sku` | 货号，必填 |
| `title` | 展示标题，可选 |
| `files` | 多张图片，必填 |

后端职责：

- 将图片写入对象存储 `uploads/{sku}-{timestamp}-{id}/`。
- 写入 `garment_skus` 和 `garment_images`。
- 第一张图片写入 `role = main`，其余图片写入 `role = reference`。
- 返回货号详情和封面 URL / 图片数量 / 文件大小等信息。

前端职责：

- 上传前先在浏览器本地形成待上传图包，支持散图、单文件夹、多文件夹。
- 用户可在“预览与编辑”阶段修改货号/标题、删除图片。
- 保存上传成功后立即调用 `POST /api/shot-plans` 创建默认组图方案。
- 上传图包的卡片样式与已保存素材卡片一致：展示文件名、尺寸、大小和删除入口。

### 获取默认组图方案

`GET /api/garment-skus/{sku_id}/default-shot-plan`

响应：

```json
{
  "item": {
    "id": 4,
    "skuId": 3,
    "modelProfileId": 6,
    "status": "DRAFT",
    "plan": {},
    "plannerProvider": "gemini"
  }
}
```

如果该货号还没有真实 Gemini 成功方案，`item` 返回 `null`。当前默认方案规则为：取该货号最新一条 `planner_provider = 'gemini'` 且未失败的 `shot_plans`。

### 编辑货号

`PATCH /api/garment-skus/{sku_id}`

```json
{
  "sku": "125309C7948510",
  "title": "展示标题",
  "status": "NEW | ANALYZED | CONFIRMED"
}
```

所有字段均可选。`sku` 会做文件名安全清洗；`status` 只允许上述三个枚举。

### 删除货号

`DELETE /api/garment-skus/{sku_id}`

删除的是应用内上传副本和对应元数据，不会删除客户原始素材目录。为了保留生产历史，如果该货号已经存在 `generation_runs`，接口返回 `409`，不允许删除。

### 追加素材图

`POST /api/garment-skus/{sku_id}/images`

使用 multipart form-data，字段名为 `files`，可一次追加多张图片。新图片默认 `role = reference`，排序追加到末尾。

### 编辑单张图片

`PATCH /api/garment-images/{image_id}`

```json
{
  "role": "main | reference | front | back | detail | fabric | color | 3d",
  "sortOrder": 0
}
```

说明：该接口保留给后续排序、角色维护或管理工具使用。当前产品 UI 已去掉“主参考/参考图/正面图/3D 图”等角色标签和角色选择，默认第一张图即封面。

### 删除单张图片

`DELETE /api/garment-images/{image_id}`

删除应用内上传副本。每个货号至少需要保留一张图片，否则返回 `409`。

## 状态

| 对象 | 状态 |
|---|---|
| `GarmentSku` | `NEW` / `ANALYZED` / `CONFIRMED` |
| `ShotPlan` | `DRAFT` / `CONFIRMED` |
| `GenerationRun` | `CREATED` / `RUNNING` / `PARTIAL_SUCCEEDED` / `SUCCEEDED` / `FAILED` / `CANCELLED` |
| `GenerationImage` | `QUEUED` / `SUBMITTED` / `IN_QUEUE` / `IN_PROGRESS` / `SUCCEEDED` / `FAILED` / `CANCELLED` |

单张重生不再创建新的 `GenerationRun` 或 `GenerationImage`。它会在原 `GenerationImage.attempts_json` 中追加一次 attempt，并更新当前输出图、provider 请求、provider 响应和状态。`regenerate_from_image_id` 为兼容历史记录保留。

## 生成与异步状态契约

### 创建生成批次

`POST /api/generation-runs`

请求：

```json
{
  "shotPlanId": 4
}
```

后端职责：

- 创建一条 `generation_runs`。
- 根据 `shot_plans.plan_json.shots` 创建多条 `generation_images`，每个图位一条任务记录。
- 组装单张 `task_package`，包括服装、模特、品牌模板、shot、输入图和约束快照。
- 合成中文 Image 2 prompt。
- 通过对象存储预签名 URL 将参考图交给 fal。
- 通过 fal queue 提交每张图。
- 保存 fal `request_id`、`status_url`、`response_url`、`cancel_url`。
- 立即返回 `GenerationRun`，不等待图片生成完成。

响应中的每张 `GenerationImage` 可能处于：

```text
SUBMITTED / IN_QUEUE / IN_PROGRESS / SUCCEEDED / FAILED
```

其中 placeholder provider 会直接返回 `SUCCEEDED` 并写入占位 SVG；fal provider 会进入 queue 状态。

### 查询生成批次

`GET /api/generation-runs`

返回前台历史任务列表。列表项包含任务基础字段和 `summary`：

```json
{
  "id": 7,
  "sku": "125329C0908714",
  "skuTitle": "125329C0908714",
  "status": "SUCCEEDED",
  "summary": {
    "imageCount": 5,
    "succeededCount": 5,
    "finalCount": 1,
    "regenerationCount": 2,
    "previews": []
  }
}
```

`GET /api/generation-runs/{run_id}`

前端应定时轮询该接口，而不是直接轮询 fal。接口会返回每张图的：

```json
{
  "status": "IN_PROGRESS",
  "providerRequestId": "fal request id",
  "providerStatus": "IN_PROGRESS",
  "providerUrls": {
    "statusUrl": "https://queue.fal.run/...",
    "responseUrl": "https://queue.fal.run/...",
    "cancelUrl": "https://queue.fal.run/..."
  },
  "submittedAt": "2026-05-07T16:00:00",
  "completedAt": null,
  "lastStatusCheckedAt": "2026-05-07T16:01:30",
  "prompt": "中文 Image 2 prompt",
  "imageInputs": [],
  "attempts": []
}
```

计时器由前端根据 `submittedAt` / `completedAt` 动态计算，不需要后端每秒更新。

### 后台状态更新

后端启动时会启动后台 poller。poller 周期由：

```env
GENERATION_POLL_INTERVAL_SECONDS=5
```

控制。poller 会扫描状态为 `SUBMITTED`、`IN_QUEUE`、`IN_PROGRESS`、`RUNNING` 且有 `provider_request_id` 的任务，调用 fal `status_url` 获取真实状态。

fal 状态映射：

| fal 状态 | 本地 `GenerationImage.status` |
|---|---|
| `IN_QUEUE` | `IN_QUEUE` |
| `IN_PROGRESS` | `IN_PROGRESS` |
| `COMPLETED` | `SUCCEEDED` |
| `FAILED` / `ERROR` | `FAILED` |
| `CANCELED` / `CANCELLED` | `CANCELLED` |

完成后，后端使用 fal `response_url` 获取结果，并下载第一张图片并写入 `output_path` 对象 key。

### 单张重生

`POST /api/generation-images/{image_id}/regenerate`

请求：

```json
{
  "reason": "姿势不合适",
  "note": "希望更自然一点"
}
```

后端职责：

- 读取原 `task_package_json`。
- 写入 `regeneration.reason` 和 `regeneration.note`。
- 重新合成中文 prompt。
- 提交新的 fal queue request。
- 不新增 `generation_images` 记录。
- 在原记录 `attempts_json` 中追加 attempt。
- 更新当前 `output_path`、`provider_request_id`、`provider_status`、provider 快照和状态。

返回仍然是原 `GenerationRun`。

## 数字模特 CRUD 契约

### 新建 / 编辑模特

`POST /api/model-profiles`

`PATCH /api/model-profiles/{model_profile_id}`

```json
{
  "modelId": "Face-01",
  "name": "Face-01 数字模特",
  "isDefault": true,
  "profile": {
    "height": "175cm",
    "weight": "48kg",
    "bodyShape": "H型",
    "bust": "80cm",
    "waist": "59cm",
    "hips": "89cm",
    "notes": "都市、通勤、轻熟、自然、克制"
  }
}
```

说明：

- `modelId` 全局唯一。
- `isDefault = true` 时会自动取消其他模特的默认标记。
- 基础档案和头像素材是分开保存的；前端允许用户同页编辑后再一次保存。

### 上传头像

`POST /api/model-profiles/{model_profile_id}/head-references`

multipart form-data：

| 字段 | 说明 |
|---|---|
| `files` | 多张头像 |
| `file` | 单张头像，兼容替换场景 |
| `angle` | 可选；指定时只能上传一张 |

规则：

- 不传 `angle` 时，新头像会以内部 `reference_id` 写入，并在 UI 中显示为 `未设置`。
- 传 `angle` 时，头像直接绑定该角度。
- 当前支持角度：`front`、`left45`、`right45`、`right90`、`left90`、`up45`、`down45`、`back`、`backTurnLeft45`、`backTurnRight45`。

### 更新头像角度 / 排序

`PATCH /api/model-profiles/{model_profile_id}/head-references/{reference_id}`

```json
{
  "angle": "front",
  "orderIndex": 0
}
```

说明：

- `reference_id` 是头像条目的稳定标识，不一定等于角度；未设置头像会使用 `__unassigned__...` 形式的内部 ID。
- 将 `angle` 设为空字符串时，已分配角度的头像会回到 `未设置` 状态。
- 同一具体角度只能绑定一张头像，冲突时返回 `409`。

### 删除头像

`DELETE /api/model-profiles/{model_profile_id}/head-references/{reference_id}`

删除头像元数据与素材文件，并更新 `headReferenceOrder`。

## 错误处理

- `400`：参数错误或缺少必要素材
- `404`：货号、模特、任务不存在
- `409`：状态不允许当前操作，例如删除已有生成记录的货号、删除货号下最后一张图
- `422`：素材可读但无法识别或不满足生成条件
- `500`：模型调用、对象存储或未知错误

## 数据契约

- Domain 对象以 `domain_model.md` 为准。
- SQLite 表结构以 `database_schema.md` 为准。
- 前端类型位于 `../frontend/src/types.ts`，修改 API 响应时需要同步。
