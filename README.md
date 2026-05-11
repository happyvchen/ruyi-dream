# ruyi-dream MVP

本项目是 ruyi-dream 的标准化 MVP。当前阶段先服务单一试点客户和单一工作空间，但产品设计应保留后续 SaaS 化、多客户、多租户、权限、计费和主视频生成扩展空间。

项目目标是：基于 `1 个服装货号图包` 及其默认组图方案，自动生成一组可用于淘宝、天猫等电商场景的品牌风格模特服装展示图；后续可结合 Seedance 2.0 扩展电商主视频生成。

## Agent 入口

如果由 Codex / Claude / 其他 agent 接手本项目，请先读：

- [AGENTS.md](AGENTS.md)：通用 agent 入口
- [CLAUDE.md](CLAUDE.md)：Claude/Codex 入口，与 `AGENTS.md` 同步
- [design/CLAUDE.md](design/CLAUDE.md)：产品设计索引与权威边界
- [frontend/CLAUDE.md](frontend/CLAUDE.md)：前端工程入口
- [backend/CLAUDE.md](backend/CLAUDE.md)：后端工程入口

## 当前范围

- 服务对象：当前为单一试点客户，产品定位为标准化工具
- 产品阶段：MVP
- 主生产入口：生成组图
- 素材准备入口：服装素材
- 数字模特：产品支持多数字模特，当前阶段先维护 1 个默认数字模特
- 风格方向：品牌白/浅灰棚拍主图风格
- 输出目标：每个货号生成一套主图
- 暂不包含：自动质量检测、多客户 SaaS、复杂权限、外景大片、视频生成、ZIP 上传、素材图片角色识别

## 当前 MVP 流程

```text
服装素材页上传图包
-> 浏览器本地预览与编辑
-> 保存上传
-> 生成/更新该货号默认组图方案
-> 生成组图页选择货号
-> 点击“预览”创建生成任务并生成 2x3 分镜预览
-> 满意后点击“生成”，在同一任务下追加 5 张最终大图
-> 提交 GPT Image-2 / fal queue
-> 后台轮询状态并将结果写入对象存储
-> 单张重生 / 设为最终图 / 导出
-> 生成记录中复盘任务，必要时恢复到生成组图页继续操作
```

上传支持：

- 上传 N 张图片，并填写货号，对应一个图包。
- 上传一个文件夹，货号默认使用文件夹名并可修改。
- 上传 N 个文件夹，对应 N 个图包。
- 保存前可编辑货号/标题、删除图片。
- 第一张图片默认作为封面。

## 配置管理

本项目支持两种本地运行方式：

- 日常开发：RustFS 用 Docker Compose 启动，前端和后端本机裸跑，见 [本地开发说明](docs/local-development.md)。
- 完整部署模拟：前端、后端、RustFS 全部由 Docker Compose 管理，见 [Docker Compose 部署与管理](docs/docker-compose-operations.md)。

配置采用单一主配置源：

- 根目录 `.env.example` 是唯一提交到 Git 的环境变量模板。
- 根目录 `.env` 是本地开发、Docker Compose 和后端裸跑共同读取的主配置文件，不提交 Git。
- `backend/.env.local` 是可选的后端本地覆盖文件，只用于临时覆盖少量后端变量，不放共享密钥，不提交 Git。
- 生产环境应由部署平台、CI/CD 或 Secret Manager 注入环境变量，不依赖仓库中的真实 `.env`。

首次配置：

```bash
cd /path/to/ruyi-dream
cp .env.example .env
```

关键环境变量：

```env
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3-flash-preview

APP_S3_ENDPOINT_URL=http://127.0.0.1:9000
APP_S3_PUBLIC_ENDPOINT_URL=http://127.0.0.1:9000
APP_STORAGE_BUCKET=ruyi-dream
APP_S3_REGION=auto
APP_S3_FORCE_PATH_STYLE=true
APP_S3_PRESIGN_EXPIRES_SECONDS=900
RUSTFS_ACCESS_KEY=<local-or-production-access-key>
RUSTFS_SECRET_KEY=<local-or-production-secret-key>
APP_API_ACCESS_TOKEN=<production-api-access-token>
APP_TRUST_REVERSE_PROXY_AUTH=false

GPT_IMAGE2_PROVIDER=fal
GPT_IMAGE2_MODEL_ID=openai/gpt-image-2/edit
GPT_IMAGE2_API_BASE_URL=https://fal.run/openai/gpt-image-2/edit
GPT_IMAGE2_QUEUE_URL=https://queue.fal.run/openai/gpt-image-2/edit
FAL_KEY=
GPT_IMAGE2_QUALITY=high
GPT_IMAGE2_OUTPUT_FORMAT=png
GENERATION_POLL_INTERVAL_SECONDS=5
```

图片素材、生成结果和导出 ZIP 统一写入 S3 兼容对象存储。私有化部署默认使用 RustFS，生产部署方式见 [RustFS 对象存储部署](deployment/rustfs/README.md)。

真实生图只支持 fal queue。点击生成后后端立即返回任务记录，后台会先把参考图上传到 fal，再提交 Image-2 queue 并持续查询状态。`FAL_KEY` 未配置时，预览/生成任务会失败并在 UI 中显示错误，不再使用占位图或本地 fallback。

## Docker Compose 部署

项目根目录提供完整 `docker-compose.yml`：

```bash
cp .env.example .env
# 编辑 .env，设置 RustFS 密钥、PUBLIC_BACKEND_URL、PUBLIC_S3_URL、GEMINI_API_KEY、FAL_KEY
docker compose up -d --build
docker compose ps
```

默认端口：

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8000`
- RustFS S3 API：`http://127.0.0.1:9000`
- RustFS Console：`http://127.0.0.1:9001`

部署设计、日常运维、备份恢复见：

- [Docker Compose 部署设计](docs/deployment-architecture.md)
- [Docker Compose 部署与管理](docs/docker-compose-operations.md)
- [生产故障 Runbook](docs/runbook.md)
- [本地开发说明](docs/local-development.md)

## 前端路由

前端支持可恢复 URL：

```text
/generate
/generate?sku=125329C0908714
/generate?run=7
/garment-skus
/garment-skus/125329C0908714
/model-profiles
/model-profiles/Face-01
/generation-runs
/generation-runs/7
```

其中：

- `/generate?run={runId}` 会在生成组图页恢复指定历史任务。
- `/garment-skus/{sku}` 会打开指定货号图包。
- `/model-profiles/{modelId}` 会打开指定数字模特。
- `/generation-runs/{runId}` 会打开指定生成记录详情。

前台“生成记录”以任务为主粒度：一次“预览 -> 生成”的完整链路只对应一条记录；预览图、最终 5 张图以及单张重生都收纳在该任务内对应图位的 attempts 中，不额外新增任务记录。

## 项目结构

```text
.
├── AGENTS.md
├── CLAUDE.md
├── README.md
├── tasks.md
├── design/
│   ├── 00_product_overview/
│   ├── 01_generation_flow/
│   ├── 02_brand_and_assets/
│   ├── 03_ux_guide/
│   └── 04_implementation_guide/
├── frontend/
├── backend/
├── deployment/
├── docs/
└── docker-compose.yml
```

当前 `frontend/` 已初始化为 React + Vite + TypeScript 单页 MVP；`backend/` 已初始化为 FastAPI + SQLite + S3 兼容对象存储 MVP。

## 文档目录

- [设计总索引](design/CLAUDE.md)
- [产品总览](design/00_product_overview/product_overview.md)
- [MVP PRD](design/00_product_overview/mvp_prd.md)
- [生成流程](design/01_generation_flow/generation_flow.md)
- [Image 2 输入职责拆分与提示词策略](design/01_generation_flow/image2_input_strategy.md)
- [生成要素职责拆分总表](design/01_generation_flow/generation_responsibility_matrix.md)
- [品类组图规则](design/02_brand_and_assets/category_shot_rules.md)
- [素材与参数规范](design/02_brand_and_assets/assets_and_parameters.md)
- [品牌调性模板库](design/02_brand_and_assets/brand_style_templates.md)
- [尺寸可选增强模块](design/02_brand_and_assets/optional_measurement_enhancement.md)
- [普通用户视角 UI/UX 设计](design/03_ux_guide/user_uiux.md)
- [技术路线与生产实操](design/04_implementation_guide/technical_route_and_operations.md)
- [Docker Compose 部署设计](docs/deployment-architecture.md)
- [本地开发说明](docs/local-development.md)
- [Docker Compose 部署与管理](docs/docker-compose-operations.md)
- [生产故障 Runbook](docs/runbook.md)
- [后端领域模型](backend/domain_model.md)
- [后端数据库结构](backend/database_schema.md)
- [RustFS 对象存储部署](deployment/rustfs/README.md)

## 核心原则

1. 用户不写 prompt，用户确认拍摄信息。
2. 系统不是生成单张图，而是生成一组电商主图。
3. 产品优先复刻客户既有棚拍语言，而不是扩展创意场景。
4. MVP 只保留必要功能，避免 SaaS 化过度设计。
5. 服装保真优先于画面炫技。
6. Image 2 是最终视觉生成器，不承担品牌策略、搭配判断、Pose 策划和镜头规划。
7. 品牌调性、品类规则、Pose、搭配和镜头方案应由前置推理/策划层生成，再交给 Image 2 执行。
8. 前台 UI 面向普通业务用户，默认只展示服装、模特、组图方案、生成结果；AI 任务包、prompt、策划器等复杂细节默认隐藏。
9. 真实生图是持久化异步任务，不在前端 HTTP 请求内长时间等待。
