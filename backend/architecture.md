# Backend Architecture

## 目标

后端负责把客户素材、数字模特、品牌模板和生成任务组织成可追踪的生产链路。MVP 阶段优先保证流程清晰、可调试、可复跑，同时保留后续 SaaS 化演进空间。

当前实现为 FastAPI 单体应用，代码集中在 `backend/app/`，元数据使用 SQLite，媒体文件统一使用 S3 兼容对象存储。私有化默认采用项目根目录 Docker Compose 统一编排前端、后端和 RustFS，部署设计见 `../docs/deployment-architecture.md`，对象存储单独说明见 `../deployment/rustfs/README.md`。

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | FastAPI app、API 路由、生成/重生/导出编排、后台生成状态 poller |
| `app/db.py` | SQLite 初始化、默认模特 seed、事件日志写入 |
| `app/planner.py` | 旧版货号组图策划兼容模块；新版 AI 摄影棚不以它作为主流程入口 |
| `app/image_provider.py` | GPT Image-2 provider adapters；默认 toapis，保留 fal，负责参考图上传、任务提交、状态查询和结果下载 |
| `app/image_derivatives.py` | `thumb` / `preview` WebP 衍生图生成、历史缺失衍生图按需补齐 |
| `app/config.py` | 环境变量、对象存储、数据库、模型配置 |
| `app/object_storage.py` | S3 兼容对象存储访问、预签名 URL、对象删除和读取 |
| `scripts/repair_media_metadata.py` | 历史素材和生成结果的衍生图路径、文件大小元数据补齐脚本 |
| `data/` | SQLite 和 JSONL 日志 |
| `Dockerfile` | 后端容器镜像定义，生产入口为 `uvicorn app.main:app` |
| `../deployment/rustfs/` | RustFS 私有化对象存储 Docker 部署示例 |
| `../docker-compose.yml` | MVP 单机部署编排：frontend/backend/rustfs |

## 业务模块

| 模块 | 职责 |
|---|---|
| `assets` | 主推款、搭配服饰、数字模特和生成结果的对象索引 |
| `model-profile` | 数字模特档案、头部角度、身体参数 |
| `styling-garment` | 搭配服饰库、Gemini 打标、生效标签和选搭候选管理 |
| `generation-task` | AI 摄影棚主推款任务、输入版本、StylingPlan、ShootScript、ShotPlan、分镜预览和最终图 |
| `shoot-config` | AI Director 全局导演规则、基础/默认 6 镜头规则、标签一级品类 6 镜头覆盖规则 |
| `legacy-generation` | 旧版 `garment-skus` / `generation-runs` 兼容接口，保留用于旧数据和工程参考 |
| `generation` | 提交 Image-2 provider 任务，管理生成状态、单张重试和结果下载 |
| `export` | 结果命名、打包、导出 |

## 数据对象

- `GenerationTask`：新版 AI 摄影棚主任务；草稿确认后冻结主推款输入，进入选搭、拍摄剧本、分镜预览和成片阶段
- `GenerationTaskImage`：主推款图片链接；AI 前可增删改排序，进入 AI 后由输入版本快照读取
- `GenerationTaskInputVersion`：主推款、默认数字模特和输入元数据快照
- `StylingGarment`：搭配服饰库真实商品；只有 `READY` 可进入候选漏斗
- `GenerationStyling`：AI Stylist 输出的 `StylingPlan`、候选召回、选中搭配和质量信号记录
- `GenerationShootScript`：AI Director 基于 `StylingPlan` 一次性生成的拍摄剧本，包含全局导演设定、搭配风险、配饰调整和 6 个镜头
- `GenerationShotPlan`：从 `ShootScript` 派生的单镜头图位、景别、取景、姿势、走位、穿着关系和展示重点
- `GenerationPreview`：分镜预览总图；第 6 格仅参考，最终不生成大图
- `GenerationResultSet` / `GenerationResultImageNew`：前 5 格最终成片集合和单图状态
- `GenerationAttempt`：AI Stylist、预览、最终图、打标等 AI 操作的请求/响应/错误快照
- `ModelProfile`：模特身份图、头部角度、身体参数、禁止项
- `GarmentSku` / `GarmentImage` / `GenerationRun` / `GenerationImage`：旧版兼容对象，不作为新版页面主流程依据
- `EventLog`：SQLite + JSONL 双写的事件留痕

实际字段和关系以 `domain_model.md` 与 `database_schema.md` 为准。

## 分层

```text
FastAPI Route (app/main.py)
  -> AI Studio / Styling Library / Model / Export orchestration
  -> Background poller for legacy generation images and new generation jobs
  -> AI Stylist and tagging via Gemini
  -> Image provider adapter (app/image_provider.py)
  -> SQLite repository helpers (app/db.py + SQL in app/main.py)
  -> S3 兼容对象存储 (RustFS / OSS / S3 / COS / GCS 等)
```

Docker Compose 中后端使用：

- `/app/data` 挂载到宿主机 `deployment/app-data/backend`，保存 SQLite 和应用日志。
- `APP_S3_ENDPOINT_URL`，后端访问 S3 兼容对象存储的地址；宿主机裸跑默认 `http://127.0.0.1:9000`，完整 Compose 启用 RustFS 时由 `docker-compose.rustfs.yml` 覆盖为容器内部地址 `http://rustfs:9000`。
- `APP_S3_PUBLIC_ENDPOINT_URL`，供后端生成对象存储短期预签名 URL；前台默认访问稳定的 `/api/media?...` 路径。
- `BACKEND_CORS_ORIGINS`，允许前端静态站点访问 API。

## 媒体访问与缓存边界

对象存储仍保存原图，应用层为上传素材、数字模特头像和生成结果生成两档 WebP 衍生图：

- `thumb`：长边约 240px，用于列表、卡片、历史记录缩略图和结果网格。
- `preview`：长边约 1280px，用于弹窗预览和详情查看。
- `original`：原始上传或生成图，仅用于下载、导出、AI 输入和必要质检。

前台 API 返回稳定的 `/api/media?key=...&variant=thumb|preview|original`，浏览器不会因为轮询拿到新的预签名 URL 而反复重下同一张图。`/api/media` 由后端校验对象 key 后跳转到对象存储短期预签名 URL；如果历史数据缺少衍生图，媒体访问层会尽量按需补生成，失败时回退原图保证可显示。

上传和生成完成时，后端会把 `thumb_path`、`preview_path`、`file_size`、`thumb_file_size`、`preview_file_size` 写入数据库。列表和详情序列化应优先读取数据库元数据，不在热路径里同步 `HEAD` 对象存储或现场生成缩略图。历史数据通过 `backend/scripts/repair_media_metadata.py` 批量补齐。

## 生成任务架构

新版 AI 摄影棚真实生成采用持久化任务记录加后端进程内 poller 恢复：

```text
POST /api/generation-tasks
  -> 创建草稿 GenerationTask
  -> 上传主推款图片到 media_assets

POST /api/generation-tasks/{id}/ready
  -> 冻结草稿编辑，进入 READY_FOR_STYLING

POST /api/generation-tasks/{id}/select-styling
  -> 创建 GenerationTaskInputVersion
  -> 创建 generation_styling attempt
  -> 调用 Gemini AI Stylist
  -> 校验 StylingPlan 结构并记录质量信号
  -> 写入 GenerationStyling

POST /api/generation-tasks/{id}/shoot-script
  -> 创建 GenerationShootScript 和 generation_shoot_script attempt
  -> 调用 Gemini AI Director
  -> 校验 ShootScript 结构并写入 6 条 GenerationShotPlan
  -> 旧分镜预览和旧成片失效但保留

POST /api/generation-tasks/{id}/preview
  -> 创建 GenerationPreview 和 generation_preview attempt
  -> 调用 Image-2 生成 3x2 分镜预览
  -> 写入 preview_asset_id

POST /api/generation-tasks/{id}/generate-final
  -> 创建 GenerationResultSet 和前 5 张 GenerationResultImageNew
  -> 并发调用 Image-2
  -> 聚合为 COMPLETED 或 FINAL_PARTIAL
```

`generation_shoot_scripts`、`generation_previews` 和 `generation_result_images_new` 使用 `locked_by` / `locked_until` / `run_attempts` 做单实例恢复。后端重启后，poller 会扫描锁过期的 RUNNING/QUEUED 新版任务，标记旧 attempt 为失败并创建新的 attempt 继续执行。多实例或高并发场景仍应升级为独立 worker 或外部队列。

前端不直接等待 provider，也不直接查询 provider。生成中页面轮询 `GET /api/generation-tasks/{task_id}`，只展示业务状态、AI 操作摘要、预览和结果图；候选召回、provider 请求/响应和 attempt 明细默认不返回，仅在 `includeDebug=true` 时返回。

状态恢复方式：

- `/studio?task={task_id}`：在 AI 摄影棚恢复指定任务。
- `/records?task={task_id}`：在生成记录页打开指定任务详情。

一次新版用户任务只对应一条 `generation_tasks`。拍摄剧本阶段保存一个 `generation_shoot_scripts` artifact 和 6 条 `generation_shot_plans`。分镜预览阶段保存一张 3x2 分镜预览总图，最终阶段固定读取当前拍摄剧本的前 5 条 `generation_shot_plans` 并发生成：

- 第 1-5 个镜头对应最终成片，第 6 个镜头忽略。
- 输入图优先使用分镜预览、冻结的主推款图片、选中搭配服饰快照和默认数字模特快照。
- `Image-2` 只负责最终视觉生成，不承担选搭、标签、历史追溯或业务状态管理。
- provider 默认 `toapis`，保留 `fal` 兼容适配。

旧版 `/api/garment-skus`、`/api/shot-plans` 和 `/api/generation-runs` 仍保留兼容旧数据、历史导出和工程回归测试，但不作为新版前台主流程设计依据。

## MVP 约束

- 当前按单一试点客户和单一工作空间实现；产品定位保留后续 tenant 能力。
- 当前代码统一使用 S3 兼容对象存储，不再设计并行存储业务分支。
- 任务状态、生成记录、重生 attempts 和事件日志要可恢复、可复查。
- AI provider 通过 adapter 包装，避免业务代码绑定单一模型 SDK。
- 真实生成必须走持久化任务记录；当前使用 FastAPI 后台 poller，后续可升级独立 worker / webhook。
- 当前不做素材解析与服装图片类型识别，默认第一张图片作为封面。
- 破坏性操作需要确认；已有生成记录的货号禁止删除。
