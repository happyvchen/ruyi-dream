# Backend API Guideline

## 设计原则

- API 面向前台“拍摄任务工作台”，不暴露 prompt 工程细节。
- task package 可以作为调试字段存在，但默认不展示给普通用户。
- 所有会覆盖素材、结果或实验记录的操作必须显式确认。

## 访问控制

`/api/health` 保持匿名访问，用于容器和反向代理健康检查。生产环境默认使用 single-password app gate：

- 后端通过 `APP_GATE_PASSWORD` 或 `APP_GATE_PASSWORD_HASH` 校验访问密码。
- 登录成功后签发 `HttpOnly`、`SameSite=Lax` 的 `ruyi_session` cookie。
- 前端只保存短期 session cookie，不把部署密码或长期 token 编译进静态资源。

认证接口：

| Resource | Method | 说明 |
|---|---|---|
| `/api/auth/session` | GET | 查询是否需要门禁和当前是否已登录 |
| `/api/auth/login` | POST | 提交 `{ "password": "..." }` 并写入 session cookie |
| `/api/auth/logout` | POST | 清理 session cookie |

`APP_API_ACCESS_TOKEN` 只作为脚本/运维调用的可选 fallback。配置后可携带任一请求头：

```text
X-Ruyi-Access-Token: <token>
Authorization: Bearer <token>
```

生产环境必须配置 `APP_GATE_PASSWORD` 或 `APP_GATE_PASSWORD_HASH`，并配置 `APP_SESSION_SECRET`；如果外部反向代理/企业网关已经完成访问控制，可显式设置 `APP_TRUST_REVERSE_PROXY_AUTH=true` 跳过内置门禁。

## 当前资源

| Resource | Method | 说明 |
|---|---|
| `/api/health` | GET | 健康检查；只返回非敏感 readiness 状态 |
| `/api/styling-garments` | GET | 搭配服饰库列表，支持 `keyword` 搜索 |
| `/api/styling-garments` | POST | 创建一套搭配服饰并上传 1-6 张图片 |
| `/api/styling-garments/{garment_id}` | GET | 搭配服饰详情、图片、分层标签和打标 attempt 技术快照 |
| `/api/styling-garments/{garment_id}` | PATCH | 编辑 code 或人工业务标签；打标中禁止修改 |
| `/api/styling-garments/{garment_id}/save-and-tag` | POST | 系统默认触发 Gemini 打标；通过复核策略后进入 `READY`，否则进入 `TAGGING_READY` |
| `/api/styling-garments/{garment_id}` | DELETE | 归档搭配服饰；打标中禁止归档 |
| `/api/generation-tasks` | GET | AI 摄影棚任务列表 |
| `/api/generation-tasks` | POST | 上传主推款图片并创建 `DRAFT` 拍摄任务 |
| `/api/generation-tasks/{task_id}` | GET | 拍摄任务详情 |
| `/api/generation-tasks/{task_id}` | PATCH | 草稿期修改任务名；进入 AI 阶段后拒绝修改 |
| `/api/generation-tasks/{task_id}/images` | POST | 草稿期追加主推款图片，最多 8 张 |
| `/api/generation-task-images/{image_id}/replace` | POST | 草稿期替换单张主推款图片 |
| `/api/generation-task-images/{image_id}` | PATCH / DELETE | 草稿期调整排序或移除主推款图片 |
| `/api/generation-tasks/{task_id}/ready` | POST | 确认草稿，进入待智能选搭状态 |
| `/api/generation-tasks/{task_id}/select-styling` | POST | 冻结当前输入版本并从 READY 搭配服饰中智能选搭 |
| `/api/generation-tasks/{task_id}/preview` | POST | 生成 6 条 ShotPlan 和 3x2 六宫格预览 |
| `/api/generation-tasks/{task_id}/generate-final` | POST | 按前 5 条 ShotPlan 并发生成最终成片 |
| `/api/generation-result-images-new/{image_id}/retry` | POST | 重试单张最终图 |
| `/api/generation-records` | GET | 进入过 AI 阶段的生成记录列表 |
| `/api/generation-records/{task_id}` | GET | 生成记录详情和技术快照 |
| `/api/garment-skus` | GET | 货号图包列表 |
| `/api/garment-skus` | POST | 上传一个货号图包和多张素材图 |
| `/api/garment-skus/{sku_id}` | GET | 货号详情、素材图片、识别摘要 |
| `/api/garment-skus/{sku_id}` | PATCH | 编辑货号、标题、状态 |
| `/api/garment-skus/{sku_id}` | DELETE | 删除应用内素材副本；已有生成记录时禁止删除 |
| `/api/garment-skus/{sku_id}/images` | GET | 分页读取该货号素材图片 |
| `/api/garment-skus/{sku_id}/images` | POST | 给已有货号追加素材图 |
| `/api/garment-skus/{sku_id}/latest-preview-run` | GET | 获取货号最新可用分镜预览任务，供切换货号时恢复预览 |
| `/api/garment-images/{image_id}` | PATCH | 编辑单张图片角色和排序；当前 UI 不开放角色编辑 |
| `/api/garment-images/{image_id}` | DELETE | 删除应用内图片副本；每个货号至少保留一张 |
| `/api/media` | GET | 稳定媒体入口，按 `key` 和 `variant` 跳转到对象存储短期预签名 URL |
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
| `/api/generation-runs/{run_id}/status` | GET | 生成中轮询使用的轻量状态接口 |
| `/api/generation-runs/{run_id}` | DELETE | 归档生成记录；隐藏列表项但保留数据库记录、对象 key 和事件日志 |
| `/api/generation-runs/{run_id}/cancel` | POST | 取消该批次中仍处于 active 状态的图片任务 |
| `/api/generation-images/{image_id}/regenerate` | POST | 单张重生；不新增生成任务记录，在同一记录追加 attempt |
| `/api/generation-images/{image_id}/cancel` | POST | 取消单张 active 图片任务 |
| `/api/generation-images/{image_id}/final` | PATCH | 设置/取消最终图 |
| `/api/generation-images/{image_id}/review` | PATCH | 设置业务审核/交付状态 |
| `/api/logs` | GET | 事件日志 |
| `/api/exports/runs/{run_id}` | POST | 打包导出该 run 的最终图或全部图 |

当前没有独立 `/api/brand-templates` 管理接口。默认品牌模板以 `brand_template_versions` 版本化保存，生成任务包和 `template_snapshot_json` 会记录模板 id/version。

新版前台“生成记录”列表使用 `/api/generation-records`，不是直接展示 `/api/logs`。旧版 `/api/generation-runs` 和 `/api/garment-skus` 仍作为兼容接口保留，不能作为新版页面主流程依据。`event_logs` 仍用于排查和审计。

新版任务在调用 `select-styling` 时冻结主推款图片和默认数字模特快照。六宫格与最终成片只读取该输入版本以及选搭快照；已成功的选搭、预览和整组成片不开放重新生成，部分失败只通过单张结果图重试恢复。

写接口在 cookie 会话下会校验 `Origin` / `Referer` 是否属于 `BACKEND_CORS_ORIGINS`，脚本调用应使用可选的 `APP_API_ACCESS_TOKEN`。

## 通用列表与媒体 URL 契约

### 分页响应

列表接口默认不再一次性返回全量数据。当前使用 offset 分页，响应保留 `items`，并附带下一页信息：

```json
{
  "items": [],
  "limit": 30,
  "offset": 0,
  "nextOffset": 30,
  "hasMore": true
}
```

约定：

- `limit` 有服务端上限；前端应按接口默认值或业务页设置传入。
- `offset` 从 `0` 开始。
- `hasMore = true` 时，前端可用 `nextOffset` 请求下一页。
- `hasMore = false` 时，`nextOffset = null`。

### 轻量状态接口

生成中页面轮询 `GET /api/generation-runs/{run_id}/status`。该接口只返回任务基础字段和每张图的状态、展示 URL、审核/最终图等前台必要字段，不返回任务包、provider 原始响应或 attempt 明细。

当前支持分页的接口：

| Resource | 查询参数 |
|---|---|
| `GET /api/garment-skus` | `includeArchived`、`limit`、`offset`、`keyword` |
| `GET /api/garment-skus/{sku_id}/images` | `limit`、`offset` |
| `GET /api/model-profiles` | `limit`、`offset`、`keyword` |
| `GET /api/generation-runs` | `includeArchived`、`limit`、`offset`、`skuId`、`keyword` |

搜索框只要表现为全局搜索，就必须调用对应列表接口的 `keyword` 参数；不能只过滤前端已加载的首屏数据。

### 三档图片 URL

后端为上传素材、数字模特头像和生成结果维护三档图片访问 URL：

| 字段 | 用途 | 前端场景 |
|---|---|---|
| `thumbUrl` | 小缩略图，WebP，长边约 240px | 列表卡片、历史记录缩略图、素材卡片、结果网格 |
| `previewUrl` | 中等预览图，WebP，长边约 1280px | 弹窗普通预览、详情查看 |
| `originalUrl` | 原始上传或生成图 | 下载、导出、AI 输入和质检 |

兼容字段：

- `url` 仍保留给旧前端使用；对素材和生成图通常等价于 `previewUrl`。
- `coverUrl` 仍保留给旧前端使用；当前等价于 `coverThumbUrl`。
- `ModelProfile.previewUrl` 仍保留；当前是真正的预览衍生图，不再表示原图。

对象存储约定：

- 原图仍保存在原有对象 key，不改动历史路径。
- 衍生图保存在同目录 `_derived/thumb/*.webp` 和 `_derived/preview/*.webp`。
- 新上传、新生成图片会同步生成衍生图。
- 前台 API 返回稳定的 `/api/media?key=...&variant=...` 路径，避免轮询时预签名 URL 变化导致图片 `src` 抖动。
- 上传/生成完成时会把 `thumb_path`、`preview_path` 和三档文件大小落库；历史数据可用 `backend/scripts/repair_media_metadata.py` 补齐。
- 旧数据若缺少衍生图，媒体访问层会尽量补生成；补生成失败时回退到原图，保证旧记录仍可显示。
- 删除单张素材或替换/删除头像时，会同步清理对应衍生图。删除整个货号或模特目录时，会通过对象前缀清理。

## 搭配服饰库契约

新版页面使用 `StylingGarment`：

- `GET /api/styling-garments` 支持 `limit`、`offset`、`keyword`，搜索必须走服务端，不能只过滤首屏数据。
- `POST /api/styling-garments` 接收 `code` 和 `files`，单套最多 6 张，写入媒体资产后创建 `DRAFT` 记录。
- 前端导入成功后默认立即调用 `POST /api/styling-garments/{id}/save-and-tag`，不要求用户确认是否打标。
- `TAGGING_RUNNING` 期间服务端拒绝 code/标签修改、重复打标和归档。
- 打标输出分层返回 `aiTags`、`officialTitleTags`、`manualTags`、`effectiveTags` 和 `tagMeta`。
- `GET /api/styling-garments/{id}` 额外返回 `tagAttempts`；每项包含 provider、状态、时间、错误以及 `requestSnapshot` / `responseSnapshot`，供折叠技术详情查看。
- 低置信度、`unknown`、`unsupported_accessory`、provider fallback 或模型显式要求复核时，状态为 `TAGGING_READY`。
- 只有 `READY` 状态进入智能选搭候选；`TAGGING_READY`、`TAGGING_FAILED` 和 `DELETED` 不参与选搭。

## 旧版服装素材 CRUD 契约

### 上传与默认方案

`POST /api/garment-skus`

使用 multipart form-data：

| 字段 | 说明 |
|---|---|
| `sku` | 货号，必填 |
| `title` | 展示标题，可选 |
| `files` | 多张图片，必填 |

后端职责：

- 写入对象存储前先校验所有上传图片：仅支持 JPG / PNG / WebP，默认单张不超过 15MB、不超过 25MP，单个货号最多 20 张。
- 将图片写入对象存储 `uploads/{sku}-{timestamp}-{id}/`。
- 为每张图片生成 `thumb` / `preview` WebP 衍生图。
- 写入 `garment_skus` 和 `garment_images`。
- 第一张图片写入 `role = main`，其余图片写入 `role = reference`。
- 返回货号详情、封面三档 URL、图片数量、文件大小等信息。

前端职责：

- 上传前先在浏览器本地形成待上传图包，支持散图、单文件夹、多文件夹。
- 用户可在“预览与编辑”阶段修改 code、删除图片、替换图片、拖动排序。
- 批量上传图片或文件夹时允许先列出超过 6 张图片，便于用户整理；超过 6 张不允许保存/导入。
- 点击 `导入` 后保存搭配服饰并触发打标任务。打标接口先把状态置为 `TAGGING_RUNNING` 并立即返回，新建卡片显示 `正在打标` 等状态，后台继续完成打标。
- 已保存搭配服饰图片发生排序、替换、删除或追加后，前端提示“此次修改会影响打标”，并提供 `仅保存` 与 `保存并重新打标`。
- `TAGGING_RUNNING` 期间服务端拒绝 code/标签/图片编辑、再次打标和删除/归档；前端轮询卡片状态并禁用冲突操作。
- 单图替换使用原子替换接口，不通过“先删后加”模拟，确保仅剩一张图片时也能替换。
- 已完成智能选搭的任务在预览和成片阶段读取 `selected_material_snapshot_json` 中冻结的素材资产，不回读可能已归档的当前库对象。
- `级联删除` 删除搭配服饰时，同时删除实际选中过它的生成任务、冻结快照、任务上传的主推款素材、生成输出和对应媒体记录；仅曾作为候选但未选中的任务不受影响。
- 保存上传成功后立即调用 `POST /api/shot-plans` 创建默认组图方案。
- 上传图包的卡片样式与已保存素材卡片一致：展示文件名、尺寸、大小和删除入口。

### 货号列表与详情

`GET /api/garment-skus`

查询参数：

| 参数 | 说明 |
|---|---|
| `includeArchived` | 是否包含已归档货号，默认 `false` |
| `limit` | 每页数量，默认 30，最大 100 |
| `offset` | 起始偏移，默认 0 |
| `keyword` | 按货号、标题或状态模糊搜索 |

列表项包含封面三档 URL：

```json
{
  "items": [
    {
      "id": 3,
      "sku": "125329C0908714",
      "title": "125329C0908714",
      "status": "ANALYZED",
      "coverUrl": "https://...",
      "coverThumbUrl": "https://...",
      "coverPreviewUrl": "https://...",
      "coverOriginalUrl": "https://...",
      "coverFileSize": 123456,
      "imageCount": 8,
      "archivedAt": null
    }
  ],
  "limit": 30,
  "offset": 0,
  "nextOffset": null,
  "hasMore": false
}
```

`GET /api/garment-skus/{sku_id}`

查询参数：

| 参数 | 说明 |
|---|---|
| `imageLimit` | 详情首屏返回的素材图数量，默认 24，最大 120 |
| `imageOffset` | 素材图起始偏移，默认 0 |

响应中的 `item.images` 只包含当前图片页，`item.imagePage` 描述下一页：

```json
{
  "item": {
    "id": 3,
    "sku": "125329C0908714",
    "images": [
      {
        "id": 10,
        "filename": "001-main.jpg",
        "url": "https://...",
        "thumbUrl": "https://...",
        "previewUrl": "https://...",
        "originalUrl": "https://...",
        "fileSize": 123456,
        "thumbFileSize": 12345,
        "previewFileSize": 67890
      }
    ],
    "imagePage": {
      "limit": 24,
      "offset": 0,
      "nextOffset": 24,
      "hasMore": true
    }
  }
}
```

`GET /api/garment-skus/{sku_id}/images`

分页读取某个货号的素材图：

```json
{
  "items": [
    {
      "id": 10,
      "filename": "001-main.jpg",
      "url": "https://...",
      "thumbUrl": "https://...",
      "previewUrl": "https://...",
      "originalUrl": "https://..."
    }
  ],
  "limit": 24,
  "offset": 24,
  "nextOffset": null,
  "hasMore": false
}
```

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

默认操作为归档货号：从日常素材列表隐藏该货号，但保留素材、方案、生成记录、对象 key 和事件日志。归档后的货号可通过 `GET /api/garment-skus?includeArchived=true` 查看。

`DELETE /api/garment-skus/{sku_id}?purge=true&confirmSku={sku}`

当用户在二次确认中勾选“同时删除关联记录和文件”时使用。接口要求 `confirmSku` 与货号完全一致，会级联删除：

- 货号元数据与上传素材记录。
- 该货号的拍摄方案和分镜明细。
- 该货号的生成记录、生成图片记录和已归档记录。
- 对象存储中的应用上传副本和该货号生成输出目录。

该操作不生成 fallback/mock 数据，不保留可恢复业务记录；只保留事件日志中的清理摘要用于审计。

### 追加素材图

`POST /api/garment-skus/{sku_id}/images`

使用 multipart form-data，字段名为 `files`，可一次追加多张图片。新图片默认 `role = reference`，排序追加到末尾。

追加图片会同步生成 `thumbUrl` / `previewUrl` 对应的 WebP 衍生图；如果写数据库失败，后端会清理已写入的原图和衍生图。

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

删除单张图片时会同步清理该图片的 `thumb` / `preview` 衍生图。

## 状态

| 对象 | 状态 |
|---|---|
| `GarmentSku` | `NEW` / `ANALYZED` / `CONFIRMED` |
| `ShotPlan` | `PLANNING` / `DRAFT` / `CONFIRMED` / `FAILED` |
| `GenerationRun` | `CREATED` / `RUNNING` / `PARTIAL_SUCCEEDED` / `SUCCEEDED` / `FAILED` / `CANCELLED` |
| `GenerationImage` | `QUEUED` / `SUBMITTED` / `IN_QUEUE` / `IN_PROGRESS` / `SUCCEEDED` / `FAILED` / `CANCELLED` |

单张重生不再创建新的 `GenerationRun` 或 `GenerationImage`。它会在原 `GenerationImage.attempts_json` 中追加一次 attempt，并更新当前输出图、provider 请求、provider 响应和状态。`regenerate_from_image_id` 为兼容历史记录保留。

## 生成与异步状态契约

### 创建分镜预览

`POST /api/preview-runs`

请求：

```json
{
  "shotPlanId": 4
}
```

后端职责：

- 创建一条 `generation_runs.kind = preview_grid` 的任务记录，并立即返回；这条记录后续会继续承载最终组图，不应再为同一次用户任务创建第二条记录。
- 只创建一条 `generation_images.status = QUEUED`，由后台 poller 提交 Image-2，输出一张 `2x3 分镜预览 / Contact Sheet`。
- 前 5 格严格对应当前方案的 5 条分镜，第 6 格为备用候选姿势。
- 图片入参顺序为：数字模特身份参考图在前，当前货号服装素材图在后，总数最多 10 张。
- 数字模特图按 UI 当前显示顺序取前 4 张；服装图按 UI 当前显示顺序使用剩余额度，通常取 6 张，但当模特图少于 4 张时可超过 6 张，总数仍不超过 10。
- prompt 中的图片编号必须与最终传给 Image-2 的 provider 图片 URL 顺序一致。
- prompt 必须直接引用当前货号已保存的拍摄方案、服饰搭配、统一背景和 5 条分镜剧本，并声明服装素材图是商品事实来源；缺少关键方案字段时任务应失败，不允许使用通用兜底搭配文案。
- 所有输入图先上传到当前 Image-2 provider，最终只把 provider 返回的 URL 传给 Image-2。toapis 使用 `reference_images`，fal 使用 `image_urls`。
- 预览任务的 Image-2 请求使用当前 provider 的正方形安全配置；fal 为 `image_size = square_hd`、`quality = high`，toapis 为 `size = 1:1`。
- `provider_request_json`、`image_inputs_json` 和 `attempts_json` 保留图片角色、源对象 key、文件名、provider URL 与最终 prompt。

预览图用于确认姿势、身位、构图、搭配和整体调性，不作为最终交付成片。

对预览图点击“重生”时，后端仍使用同一套预览构建规则重新生成 task package：最多 4 张模特图在前、服装图按剩余额度补齐到最多 10 张、2x3 Contact Sheet prompt、全部输入图先上传当前 Image-2 provider。历史图片记录里的旧 `task_package_json` 只作为审计记录，不作为预览重生的规则来源；若历史预览属于旧方案，后端应使用当前货号最新可用方案重建任务。

### 创建生成批次

`POST /api/generation-runs` 如果携带 `previewRunId`，后端必须复用这条预览 run，在同一条 `generation_runs` 下追加 5 张最终图，并将 `kind` 更新为 `group`。如果该 run 已经存在最终图，接口直接返回现有 run，不重复追加。`previewRunId` 缺失时才允许创建新的 run，但产品主流程应始终从预览 run 进入生成。

最终成片的 Image-2 入参规则：

- 每个最终图位单独创建一条 `generation_images`，每张图单独调用一次 Image-2。
- 图片顺序为：图1 是已确认的 2x3 分镜预览图；图2~M 是数字模特身份参考图，最多 4 张；图(M+1)~10 是服装素材参考图，使用剩余额度。
- 5 张最终图共享同一九宫格、同一批模特图、同一批服装图和同一个公共 prompt 前缀；每张只替换当前图位脚本。
- 九宫格预览图只用于参考当前图位的姿势、身位、景别、构图、背景光线和搭配方向，不得作为服装商品事实来源。
- 服装素材参考图是唯一商品事实来源。最终 prompt 不默认复述 Gemini 自动识别出的具体颜色、扣子数量、刺绣位置、口袋数量或面料名称。
- prompt 只声明图片角色、事实优先级、当前图位脚本、服装保真范围和禁止项；只有用户人工确认的“硬保真点”才允许作为具体服装事实进入 prompt。

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
- 每张图先以 `QUEUED` 状态持久化，并立即返回 `GenerationRun`。
- 后台 poller 捡起 `QUEUED` 图片后，先将参考图上传到当前 Image-2 provider，再把 provider URL 交给 Image-2。
- 通过当前 provider 提交每张图，保存 provider task id、状态 URL 和原始响应快照。
- 立即返回 `GenerationRun`，不等待图片生成完成。

响应中的每张 `GenerationImage` 可能处于：

```text
SUBMITTED / IN_QUEUE / IN_PROGRESS / SUCCEEDED / FAILED
```

生产实现支持 toapis 和 fal provider，默认 toapis，可通过 `.env` 的 `GPT_IMAGE2_PROVIDER` 切换。任务会进入 queue 状态，并通过后台 poller 更新为 `SUBMITTED` / `IN_QUEUE` / `IN_PROGRESS` / `SUCCEEDED` / `FAILED` 等应用内状态。

### 查询生成批次

`GET /api/generation-runs`

查询参数：

| 参数 | 说明 |
|---|---|
| `includeArchived` | 是否包含已归档记录，默认 `false` |
| `limit` | 每页数量，默认 30，最大 100 |
| `offset` | 起始偏移，默认 0 |
| `skuId` | 只返回指定货号的生成记录 |
| `keyword` | 按货号、标题、状态或记录 ID 模糊搜索 |

返回前台历史任务列表。列表项包含任务基础字段和轻量 `summary`；列表接口不应现场访问对象存储做 HEAD、文件大小查询或缩略图生成。

```json
{
  "items": [
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
        "previews": [
          {
            "id": 31,
            "title": "全身主视觉",
            "url": "https://...",
            "thumbUrl": "https://...",
            "previewUrl": "https://...",
            "originalUrl": "https://..."
          }
        ]
      }
    }
  ],
  "limit": 30,
  "offset": 0,
  "nextOffset": null,
  "hasMore": false
}
```

`GET /api/generation-runs/{run_id}`

详情页、恢复历史任务和排查场景使用该接口，而不是直接轮询 fal。默认响应为轻量业务字段；内部调试字段仅在 `includeDebug=true` 时返回。生成中高频刷新应使用 `GET /api/generation-runs/{run_id}/status`。

```json
{
  "status": "IN_PROGRESS",
  "providerRequestId": "fal request id",
  "providerStatus": "IN_PROGRESS",
  "submittedAt": "2026-05-07T16:00:00",
  "completedAt": null,
  "lastStatusCheckedAt": "2026-05-07T16:01:30",
  "url": "https://...",
  "thumbUrl": "https://...",
  "previewUrl": "https://...",
  "originalUrl": "https://...",
  "prompt": "中文 Image 2 prompt"
}
```

当请求 `GET /api/generation-runs/{run_id}?includeDebug=true` 时，图片记录额外返回 `taskPackage`、`providerRequest`、`providerResponse`、`providerUrls`、`providerStatusResponse`、`imageInputs`、`templateSnapshot`、`modelSnapshot`、`garmentSnapshot`、`attempts` 等内部排查字段。

计时器由前端根据 `submittedAt` / `completedAt` 动态计算，不需要后端每秒更新。页面不可见时前端应暂停轮询，恢复可见后再刷新。

`GET /api/generation-runs/{run_id}/status`

生成中页面的高频轮询接口。该接口只返回任务状态、时间戳、每张图状态、展示 URL、最终图/审核状态等前台必要字段，不返回 `taskPackage`、provider 原始响应、`attempts` 或调试快照。

`GET /api/garment-skus/{sku_id}/latest-preview-run`

切换货号时用于恢复最新可用分镜预览。可选查询参数：

| 参数 | 说明 |
|---|---|
| `shotPlanId` | 指定拍摄方案时，只查该方案对应的最新预览 |

没有可用预览时返回 `null` 语义的响应体，前端不应通过拉取大量 `listRuns` 来查找最新预览。

### 后台状态更新

后端启动时会启动后台 poller。poller 周期由：

```env
GENERATION_POLL_INTERVAL_SECONDS=5
```

控制。poller 会先扫描 `QUEUED` 且尚无 `provider_request_id` 的图片并提交当前 Image-2 provider；随后扫描状态为 `SUBMITTED`、`IN_QUEUE`、`IN_PROGRESS`、`RUNNING` 且有 `provider_request_id` 的任务，调用 provider status URL 获取真实状态。页面刷新不影响后台提交和状态推进。

fal 状态映射：

| fal 状态 | 本地 `GenerationImage.status` |
|---|---|
| `IN_QUEUE` | `IN_QUEUE` |
| `IN_PROGRESS` | `IN_PROGRESS` |
| `COMPLETED` | `SUCCEEDED` |
| `FAILED` / `ERROR` | `FAILED` |
| `CANCELED` / `CANCELLED` | `CANCELLED` |

完成后，后端使用 fal `response_url` 获取结果，并下载第一张图片写入 `output_path` 对象 key，同时生成 `thumb` / `preview` 衍生图。

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

`GET /api/model-profiles`

查询参数：

| 参数 | 说明 |
|---|---|
| `limit` | 每页数量，默认 30，最大 100 |
| `offset` | 起始偏移，默认 0 |
| `keyword` | 按模特 ID 或名称模糊搜索 |

响应为通用分页结构。每个 `ModelProfile` 会返回：

- `thumbUrl`：模特列表头像缩略图。
- `previewUrl`：头像角度面板和普通预览使用。
- `originalUrl`：原始正脸头像。
- `profile.headReferenceEntries[]`：每张头像条目，包含 `url`、`thumbUrl`、`previewUrl`、`originalUrl`。

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

头像上传复用素材图片安全校验：仅支持 JPG / PNG / WebP，默认单张不超过 15MB、不超过 25MP，单个数字模特最多 20 张头像。

规则：

- 不传 `angle` 时，新头像会以内部 `reference_id` 写入，并在 UI 中显示为 `未设置`。
- 传 `angle` 时，头像直接绑定该角度。
- 头像上传或替换时会同步生成 `thumbUrl` / `previewUrl` 衍生图。
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

删除头像元数据、原始素材、`thumb` / `preview` 衍生图，并更新 `headReferenceOrder`。

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
