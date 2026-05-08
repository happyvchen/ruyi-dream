# Backend Database Schema

当前数据库为 SQLite，路径默认：

```text
backend/data/app.sqlite3
```

数据库初始化入口：

```text
backend/app/db.py:init_db()
```

## 1. 表总览

当前一共 7 张业务表：

| 表名 | 业务对象 | 说明 |
|---|---|---|
| `garment_skus` | `GarmentSku` | 服装货号图包 |
| `garment_images` | `GarmentImage` | 货号下的单张素材图 |
| `model_profiles` | `ModelProfile` | 数字模特档案 |
| `shot_plans` | `ShotPlan` | 组图方案 / Gemini 策划结果 |
| `generation_runs` | `GenerationRun` | 一次组图生成批次 |
| `generation_images` | `GenerationImage` | 单张生成任务记录；重生通过 attempts 追加 |
| `event_logs` | `EventLog` | 事件日志 |

## 2. 表结构

### 2.1 `garment_skus`

```sql
CREATE TABLE garment_skus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT NOT NULL,
  title TEXT,
  source TEXT NOT NULL DEFAULT 'upload',
  storage_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'NEW',
  analysis_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `sku` | TEXT | 货号，来自用户输入 |
| `title` | TEXT | 展示名，当前默认等于货号 |
| `source` | TEXT | 来源，当前为 `upload` |
| `storage_path` | TEXT | 上传素材对象前缀 |
| `status` | TEXT | `NEW` / `ANALYZED` / `CONFIRMED` |
| `analysis_json` | TEXT | Gemini 输出的服装摘要 JSON |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

### 2.2 `garment_images`

```sql
CREATE TABLE garment_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'reference',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `sku_id` | INTEGER | 所属 `garment_skus.id` |
| `filename` | TEXT | 上传后规范化文件名 |
| `storage_path` | TEXT | 图片对象 key |
| `mime_type` | TEXT | 文件 MIME 类型 |
| `role` | TEXT | 图片角色，当前为 `main` / `reference` |
| `sort_order` | INTEGER | 上传顺序 |
| `created_at` | TEXT | 创建时间 |

外键：

- `sku_id -> garment_skus.id`
- `ON DELETE CASCADE`：删除 SKU 时图片元数据级联删除。当前已有删除 SKU / 删除单图接口，但为了保留生产历史，已有生成记录的 SKU 禁止删除。

实现备注：

- 当前 UI 不展示也不编辑 `role`。第一张图按 `sort_order, id` 作为封面和主要参考图。
- API 序列化时会动态计算图片 `fileSize`，该字段不落库。
- 图片尺寸由前端加载图片后读取，不落库。

### 2.3 `model_profiles`

```sql
CREATE TABLE model_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  asset_dir TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `model_id` | TEXT | 业务 ID，例如 `Face-01` |
| `name` | TEXT | 展示名 |
| `asset_dir` | TEXT | 模特头部素材目录 |
| `profile_json` | TEXT | 身体参数、头像映射、排序和元信息等 |
| `is_default` | INTEGER | 是否默认模特，0/1 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

约束：

- `model_id` 唯一。
- 启动时自动写入默认 `Face-01`，如果已存在则不重复写入。

实现备注：

- 当前基础字段使用 `profile_json` 保存：`height`、`weight`、`bodyShape`、`bust`、`waist`、`hips`、`notes`。
- `headReferences` 中的 key 不一定总是角度。未分配角度的头像会使用 `__unassigned__...` 形式的内部 ID。
- `headReferenceOrder` 也保存在 `profile_json` 中，用于拖拽排序。
- `headReferenceMeta`、`headReferenceUrls`、`headReferenceEntries` 为运行时序列化补充信息，不是独立表字段。

### 2.4 `shot_plans`

```sql
CREATE TABLE shot_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
  model_profile_id INTEGER NOT NULL REFERENCES model_profiles(id),
  status TEXT NOT NULL DEFAULT 'DRAFT',
  plan_json TEXT NOT NULL,
  planner_provider TEXT NOT NULL,
  raw_response TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `sku_id` | INTEGER | 关联货号 |
| `model_profile_id` | INTEGER | 关联数字模特 |
| `status` | TEXT | 当前为 `DRAFT`，预留 `CONFIRMED` |
| `plan_json` | TEXT | 结构化组图方案 JSON |
| `planner_provider` | TEXT | `gemini` / `fallback` / `gemini-error-fallback` / `gemini-json-parse-fallback` |
| `raw_response` | TEXT | Gemini/fallback 的完整调用快照，包含中文 prompt、输入图片对象 key 和预签名 URL和响应 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

外键：

- `sku_id -> garment_skus.id`
- `model_profile_id -> model_profiles.id`

默认方案规则：

- 当前没有单独 `is_default` 字段。
- 一个货号可保留多条历史 `shot_plans`。
- 业务上的“默认组图方案”由查询规则决定：`WHERE sku_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1`。
- 上传新货号后，前端会立即创建一条默认方案；用户在“服装素材”页点击“更新默认方案”会再生成一条新方案，旧方案保留以便追溯。

### 2.5 `generation_runs`

```sql
CREATE TABLE generation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
  shot_plan_id INTEGER NOT NULL REFERENCES shot_plans(id),
  model_profile_id INTEGER NOT NULL REFERENCES model_profiles(id),
  kind TEXT NOT NULL DEFAULT 'group',
  status TEXT NOT NULL DEFAULT 'CREATED',
  output_dir TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `sku_id` | INTEGER | 关联货号 |
| `shot_plan_id` | INTEGER | 本次生成使用的组图方案 |
| `model_profile_id` | INTEGER | 本次生成使用的模特 |
| `kind` | TEXT | 当前主要为 `group`；单张重生不再新建 run |
| `status` | TEXT | `CREATED` / `RUNNING` / `PARTIAL_SUCCEEDED` / `SUCCEEDED` / `FAILED` / `CANCELLED` |
| `output_dir` | TEXT | 本次输出目录 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

### 2.6 `generation_images`

```sql
CREATE TABLE generation_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
  shot_index INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  output_path TEXT,
  task_package_json TEXT NOT NULL,
  provider_request_json TEXT,
  provider_response_json TEXT,
  regenerate_from_image_id INTEGER REFERENCES generation_images(id),
  regenerate_reason TEXT,
  is_final INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

启动迁移会通过 `ALTER TABLE` 补充以下列：

```sql
ALTER TABLE generation_images ADD COLUMN prompt_text TEXT;
ALTER TABLE generation_images ADD COLUMN image_inputs_json TEXT;
ALTER TABLE generation_images ADD COLUMN template_snapshot_json TEXT;
ALTER TABLE generation_images ADD COLUMN model_snapshot_json TEXT;
ALTER TABLE generation_images ADD COLUMN garment_snapshot_json TEXT;
ALTER TABLE generation_images ADD COLUMN attempts_json TEXT;
ALTER TABLE generation_images ADD COLUMN current_attempt_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE generation_images ADD COLUMN provider_request_id TEXT;
ALTER TABLE generation_images ADD COLUMN provider_status TEXT;
ALTER TABLE generation_images ADD COLUMN provider_urls_json TEXT;
ALTER TABLE generation_images ADD COLUMN provider_status_json TEXT;
ALTER TABLE generation_images ADD COLUMN submitted_at TEXT;
ALTER TABLE generation_images ADD COLUMN completed_at TEXT;
ALTER TABLE generation_images ADD COLUMN last_status_checked_at TEXT;
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `run_id` | INTEGER | 所属生成批次 |
| `shot_index` | INTEGER | 图位序号 |
| `title` | TEXT | 图位标题 |
| `status` | TEXT | `QUEUED` / `SUBMITTED` / `IN_QUEUE` / `IN_PROGRESS` / `SUCCEEDED` / `FAILED` / `CANCELLED` |
| `output_path` | TEXT | 生成图片对象 key |
| `task_package_json` | TEXT | 单张生成任务包；包含 SKU、服装、模特、shot、输入图、生成约束 |
| `provider_request_json` | TEXT | 最终发给 provider 的请求快照，例如 fal 的 `prompt + image_urls + quality` |
| `provider_response_json` | TEXT | provider 最终响应快照；fal 完成后保存 response_url 返回内容 |
| `regenerate_from_image_id` | INTEGER | 重生来源图片 |
| `regenerate_reason` | TEXT | 最近一次重生原因 |
| `is_final` | INTEGER | 是否最终图，0/1 |
| `prompt_text` | TEXT | 本次当前 attempt 的中文 Image 2 prompt |
| `image_inputs_json` | TEXT | 输入图片映射，包含对象 key、预签名 URL、角色、fal URL |
| `template_snapshot_json` | TEXT | 本次生成使用的品牌模板、shot plan 等快照 |
| `model_snapshot_json` | TEXT | 本次生成使用的数字模特快照 |
| `garment_snapshot_json` | TEXT | 本次生成使用的服装事实快照 |
| `attempts_json` | TEXT | 初次生成和每次重生的历史 attempt 数组 |
| `current_attempt_index` | INTEGER | 当前生效 attempt 序号 |
| `provider_request_id` | TEXT | fal queue `request_id` |
| `provider_status` | TEXT | fal 原始状态，例如 `IN_QUEUE` / `IN_PROGRESS` / `COMPLETED` |
| `provider_urls_json` | TEXT | fal `status_url` / `response_url` / `cancel_url` |
| `provider_status_json` | TEXT | 最近一次 fal status 响应 |
| `submitted_at` | TEXT | 提交 provider 的时间 |
| `completed_at` | TEXT | 完成、失败或取消时间 |
| `last_status_checked_at` | TEXT | 最近一次状态查询时间 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

外键：

- `run_id -> generation_runs.id`
- `regenerate_from_image_id -> generation_images.id` 为历史兼容字段。当前重生默认不新增新的 `generation_images`，而是在同一记录的 `attempts_json` 中追加。

`attempts_json` 示例：

```json
[
  {
    "attemptIndex": 1,
    "kind": "initial",
    "prompt": "中文 Image 2 prompt",
    "imageInputs": [],
    "providerRequest": {},
    "providerResponse": {},
    "providerRequestId": "fal request id",
    "providerStatus": "SUBMITTED",
    "outputPath": ".../01-首图.png",
    "createdAt": "2026-05-07T16:00:00"
  },
  {
    "attemptIndex": 2,
    "kind": "regeneration",
    "reason": "姿势不合适",
    "note": "更自然一点",
    "prompt": "追加重生要求后的中文 prompt",
    "providerRequest": {},
    "providerRequestId": "fal request id",
    "providerStatus": "IN_PROGRESS",
    "outputPath": ".../01-首图-regen-160500.png",
    "createdAt": "2026-05-07T16:05:00"
  }
]
```

### 2.7 `event_logs`

```sql
CREATE TABLE event_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 主键 |
| `entity_type` | TEXT | 实体类型，例如 `generation_run` |
| `entity_id` | INTEGER | 实体 ID，可为空 |
| `event_type` | TEXT | 事件动作，例如 `started`、`succeeded` |
| `payload_json` | TEXT | 事件负载 |
| `created_at` | TEXT | 创建时间 |

## 3. 关系图

```mermaid
erDiagram
  garment_skus {
    integer id PK
    text sku
    text title
    text source
    text storage_path
    text status
    text analysis_json
    text created_at
    text updated_at
  }

  garment_images {
    integer id PK
    integer sku_id FK
    text filename
    text storage_path
    text mime_type
    text role
    integer sort_order
    text created_at
  }

  model_profiles {
    integer id PK
    text model_id
    text name
    text asset_dir
    text profile_json
    integer is_default
    text created_at
    text updated_at
  }

  shot_plans {
    integer id PK
    integer sku_id FK
    integer model_profile_id FK
    text status
    text plan_json
    text planner_provider
    text raw_response
    text created_at
    text updated_at
  }

  generation_runs {
    integer id PK
    integer sku_id FK
    integer shot_plan_id FK
    integer model_profile_id FK
    text kind
    text status
    text output_dir
    text created_at
    text updated_at
  }

  generation_images {
    integer id PK
    integer run_id FK
    integer shot_index
    text title
    text status
    text output_path
    text task_package_json
    text provider_request_json
    text provider_response_json
    text prompt_text
    text image_inputs_json
    text template_snapshot_json
    text model_snapshot_json
    text garment_snapshot_json
    text attempts_json
    integer current_attempt_index
    text provider_request_id
    text provider_status
    text provider_urls_json
    text provider_status_json
    text submitted_at
    text completed_at
    text last_status_checked_at
    integer regenerate_from_image_id FK
    text regenerate_reason
    integer is_final
    text created_at
    text updated_at
  }

  event_logs {
    integer id PK
    text entity_type
    integer entity_id
    text event_type
    text payload_json
    text created_at
  }

  garment_skus ||--o{ garment_images : has
  garment_skus ||--o{ shot_plans : has
  model_profiles ||--o{ shot_plans : used_by
  garment_skus ||--o{ generation_runs : has
  shot_plans ||--o{ generation_runs : used_by
  model_profiles ||--o{ generation_runs : used_by
  generation_runs ||--o{ generation_images : has
  generation_images ||--o{ generation_images : regenerates
```

## 4. 当前缺口和后续候选表

以下对象当前没有独立表，后续如果业务需要再拆：

| 候选表 | 当前处理方式 | 什么时候需要拆表 |
|---|---|---|
| `brand_templates` | 写在 planner prompt / `plan_json.brandStyle` 中 | 品牌模板需要可视化维护、多模板切换 |
| `pose_templates` | 写在 Gemini 输出的 shot 中 | Pose 需要沉淀、复用、人工维护 |
| `exports` | 写入 `event_logs`，ZIP 写入对象存储 | 需要导出列表、下载历史、过期清理 |
| `image_providers` | 写在代码 adapter 和请求快照中 | 多平台 A/B、成本、失败率统计 |
| `garment_image_roles` | `garment_images.role` 字段 | 图片角色需要人工精修和历史追踪 |
| `measurement_profiles` | 尚未实现 | 尺寸增强进入 MVP 主流程 |
