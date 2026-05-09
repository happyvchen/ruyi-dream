# Backend Architecture

## 目标

后端负责把客户素材、数字模特、品牌模板和生成任务组织成可追踪的生产链路。MVP 阶段优先保证流程清晰、可调试、可复跑，同时保留后续 SaaS 化演进空间。

当前实现为 FastAPI 单体应用，代码集中在 `backend/app/`，元数据使用 SQLite，媒体文件统一使用 S3 兼容对象存储。私有化默认采用 RustFS Docker 部署，见 `../deployment/rustfs/README.md`。

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | FastAPI app、API 路由、生成/重生/导出编排、后台生成状态 poller |
| `app/db.py` | SQLite 初始化、默认模特 seed、事件日志写入 |
| `app/planner.py` | Gemini 组图策划；读取 `backend/ref/` 下的客户实施引用文档 |
| `app/image_provider.py` | GPT Image-2 Provider adapter，占位 SVG 输出、fal queue 提交和状态查询 |
| `app/config.py` | 环境变量、对象存储、数据库、模型配置 |
| `app/object_storage.py` | S3 兼容对象存储访问、预签名 URL、对象删除和读取 |
| `data/` | SQLite 和 JSONL 日志 |
| `../deployment/rustfs/` | RustFS 私有化对象存储 Docker 部署示例 |

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

## 生成任务架构

真实 GPT Image-2 生成采用持久化异步任务：

```text
POST /api/generation-runs
  -> 创建 generation_run
  -> 创建每张图的 generation_image
  -> 组装 task_package 和中文 prompt
  -> 通过对象存储预签名 URL 交给 fal
  -> 提交 fal queue
  -> 保存 request_id/status_url/response_url/cancel_url
  -> 立即返回 run

后台 poller
  -> 扫描未完成 generation_images
  -> 查询 fal status_url
  -> 更新 provider_status 和应用内 status
  -> 完成后读取 response_url
  -> 下载图片并写入对象存储 output_path
  -> 聚合更新 generation_run.status
```

前端不直接等待 fal，也不直接查询 fal。前端轮询 `GET /api/generation-runs/{run_id}`，并根据 `submitted_at` / `completed_at` 动态显示计时器。

状态恢复方式：

- `/generate?run={run_id}`：在生成组图页恢复指定历史任务。
- `/generation-runs/{run_id}`：在生成记录页打开指定任务详情。
- 浏览器最近一次 `runId` 只作为无 URL 指定时的便利恢复，不是生成记录的唯一入口。

## MVP 约束

- 当前按单一试点客户和单一工作空间实现；产品定位保留后续 tenant 能力。
- 当前代码统一使用 S3 兼容对象存储，不再设计并行存储业务分支。
- 任务状态、生成记录、重生 attempts 和事件日志要可恢复、可复查。
- AI provider 通过 adapter 包装，避免业务代码绑定单一模型 SDK。
- 真实生成必须走持久化异步任务；当前使用 FastAPI 后台 poller，后续可升级独立 worker / webhook。
- 当前不做素材解析与服装图片类型识别，默认第一张图片作为封面。
- 破坏性操作需要确认；已有生成记录的货号禁止删除。
