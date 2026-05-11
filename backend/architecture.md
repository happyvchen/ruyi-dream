# Backend Architecture

## 目标

后端负责把客户素材、数字模特、品牌模板和生成任务组织成可追踪的生产链路。MVP 阶段优先保证流程清晰、可调试、可复跑，同时保留后续 SaaS 化演进空间。

当前实现为 FastAPI 单体应用，代码集中在 `backend/app/`，元数据使用 SQLite，媒体文件统一使用 S3 兼容对象存储。私有化默认采用项目根目录 Docker Compose 统一编排前端、后端和 RustFS，部署设计见 `../docs/deployment-architecture.md`，对象存储单独说明见 `../deployment/rustfs/README.md`。

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | FastAPI app、API 路由、生成/重生/导出编排、后台生成状态 poller |
| `app/db.py` | SQLite 初始化、默认模特 seed、事件日志写入 |
| `app/planner.py` | Gemini 组图策划；读取 `backend/ref/` 下的客户实施引用文档 |
| `app/image_provider.py` | fal GPT Image-2 Provider adapter、参考图上传、queue 提交、状态查询和结果下载 |
| `app/config.py` | 环境变量、对象存储、数据库、模型配置 |
| `app/object_storage.py` | S3 兼容对象存储访问、预签名 URL、对象删除和读取 |
| `data/` | SQLite 和 JSONL 日志 |
| `Dockerfile` | 后端容器镜像定义，生产入口为 `uvicorn app.main:app` |
| `../deployment/rustfs/` | RustFS 私有化对象存储 Docker 部署示例 |
| `../docker-compose.yml` | MVP 单机部署编排：frontend/backend/rustfs |

## 业务模块

| 模块 | 职责 |
|---|---|
| `assets` | 服装图包、模特图包对象索引 |
| `model-profile` | 数字模特档案、头部角度、身体参数 |
| `garment-facts` | 商品事实卡、Gemini 识别摘要；当前不做素材角色解析 |
| `brand-template` | 品牌模板、品类/图位参考图检索；当前内嵌在 planner 输出和 task package |
| `planner` | 根据服装主图、品牌调性和品类规则生成默认 shot plan 和单张任务包 |
| `generation` | 提交 Image 2 / fal queue，管理生成状态、单张重生和结果下载 |
| `export` | 结果命名、打包、导出 |

## 数据对象

- `GarmentSku`：货号、素材目录、图片列表、识别状态
- `GarmentImage`：货号下的单张素材图；当前第一张图默认作为封面，图片角色字段仅保留兼容
- `GarmentAnalysis`：保存在 `garment_skus.analysis_json` 的服装摘要
- `ModelProfile`：模特身份图、头部角度、身体参数、禁止项
- `BrandTemplate`：品牌背景、光线、Pose、鞋包、构图边界；当前没有独立表
- `ShotPlan`：一组图的图位、镜头、角度、展示重点；当前最新一条作为货号默认组图方案
- `GenerationRun`：一次整组生成批次
- `GenerationImage`：单张生成任务记录、任务包、provider 请求/响应、fal request id、attempts、最终图标记
- `TaskPackage`：保存在 `generation_images.task_package_json` 的单张生成契约
- `EventLog`：SQLite + JSONL 双写的事件留痕

实际字段和关系以 `domain_model.md` 与 `database_schema.md` 为准。

## 分层

```text
FastAPI Route (app/main.py)
  -> Generation / Upload / Export orchestration
  -> Background poller for pending generation images
  -> Planner adapter (app/planner.py)
  -> Image provider adapter (app/image_provider.py)
  -> SQLite repository helpers (app/db.py + SQL in app/main.py)
  -> S3 兼容对象存储 (RustFS / OSS / S3 / COS / GCS 等)
```

Docker Compose 中后端使用：

- `/app/data` 挂载到宿主机 `deployment/app-data/backend`，保存 SQLite 和应用日志。
- `APP_S3_ENDPOINT_URL=http://rustfs:9000`，供容器内部访问 RustFS。
- `APP_S3_PUBLIC_ENDPOINT_URL`，供浏览器访问预签名图片 URL。
- `BACKEND_CORS_ORIGINS`，允许前端静态站点访问 API。

## 生成任务架构

真实 GPT Image-2 生成采用持久化异步任务：

```text
POST /api/generation-runs
  -> 创建 generation_run
  -> 创建每张图的 generation_image(status=QUEUED)
  -> 组装 task_package 和中文 prompt
  -> 立即返回 run

后台 poller
  -> 扫描 QUEUED generation_images
  -> 先上传参考图到 fal，拿到 fal Image URLs
  -> 提交 fal queue
  -> 保存 request_id/status_url/response_url/cancel_url
  -> 扫描未完成 generation_images
  -> 查询 fal status_url
  -> 更新 provider_status 和应用内 status
  -> 完成后读取 response_url
  -> 下载图片并写入对象存储 output_path
  -> 聚合更新 generation_run.status
```

Gemini 组图策划同样采用持久化异步任务：`POST /api/shot-plans` 先创建 `shot_plans.status = PLANNING` 并返回，后台 planner poller 调用 Gemini，成功后更新为 `DRAFT`，失败后更新为 `FAILED`。页面刷新只恢复状态，不承载第三方调用生命周期。

前端不直接等待 fal，也不直接查询 fal。前端轮询 `GET /api/generation-runs/{run_id}`，并根据 `submitted_at` / `completed_at` 动态显示计时器。

状态恢复方式：

- `/generate?run={run_id}`：在生成组图页恢复指定历史任务。
- `/generation-runs/{run_id}`：在生成记录页打开指定任务详情。
- 浏览器最近一次 `runId` 只作为无 URL 指定时的便利恢复，不是生成记录的唯一入口。

预览任务使用同一套持久化异步机制。一次用户任务只对应一条 `generation_runs`：先以 `kind = preview_grid` 创建，提交一张 `shot_index = 0` 的 `2x3 Contact Sheet`；用户点击“生成”后在同一条 run 下追加 `shot_index = 1..5` 的最终图，并将 run 升级为 `kind = group`：

- 前 5 格对应最终组图 5 条分镜，第 6 格为备用候选姿势。
- 模特身份参考图按数字模特 UI 当前显示顺序最多取 4 张，并置于输入图序列最前。
- 当前货号服装素材图紧随其后，按素材 UI 当前显示顺序使用剩余额度；总参考图数量最多 10 张。
- prompt 里的“图1~N”编号必须与 provider request 的 `image_urls` 顺序完全一致。
- prompt 中的服饰搭配、统一背景和分镜剧本来自当前货号最新可用 Gemini 方案；关键字段缺失时任务失败并显示错误，不使用兜底生成内容。
- provider request 使用 fal 支持的 `image_size = square_hd` 方图枚举和 `quality = high`，不使用自定义尺寸字符串。
- 预览图“重生”与首次“预览”共用同一个 task package 构建路径；重生只额外追加用户的 `reason/note`，不得复用历史记录中的旧预览规则，并在历史记录绑定旧方案时自动改用最新可用方案。

最终生图任务在同一条 run 下追加 5 张 `shot_index = 1..5` 的单图任务。每张图单独调用 Image-2，图片输入以九宫格预览图开头，随后是模特身份图和服装素材图。最终 prompt 使用“图片事实授权型”结构：九宫格只定义导演分镜，服装素材图定义商品事实；prompt 不默认写入 Gemini 自动识别出的具体服装事实。

## MVP 约束

- 当前按单一试点客户和单一工作空间实现；产品定位保留后续 tenant 能力。
- 当前代码统一使用 S3 兼容对象存储，不再设计并行存储业务分支。
- 任务状态、生成记录、重生 attempts 和事件日志要可恢复、可复查。
- AI provider 通过 adapter 包装，避免业务代码绑定单一模型 SDK。
- 真实生成必须走持久化异步任务；当前使用 FastAPI 后台 poller，后续可升级独立 worker / webhook。
- 当前不做素材解析与服装图片类型识别，默认第一张图片作为封面。
- 破坏性操作需要确认；已有生成记录的货号禁止删除。
