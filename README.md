# ruyi-dream 0.1.0

Ruyi-Dream 是面向服饰品牌视觉生产团队的 B2B AI 数字化摄影棚。0.1.0 面向单一客户和单一工作空间交付，工程设计保留后续 SaaS 化、多客户、多租户、权限和计费扩展空间。

0.1.0 的核心链路是：用户在 `AI 摄影棚` 上传本次主推款，系统通过 AI Stylist 工作流理解主推款、探索并批判多套造型方案，从提前维护的 `搭配服装库` 中选择真实可见且服务主推款的配角服饰，生成只读拍摄剧本和 3:2 分镜预览，并按前 5 个镜头并发生成电商棚拍成片。

## Agent 入口

如果由 Codex / Claude / 其他 agent 接手本项目，请先读：

- [AGENTS.md](AGENTS.md)：通用 agent 入口
- [CLAUDE.md](CLAUDE.md)：Claude/Codex 入口，与 `AGENTS.md` 同步
- [design/CLAUDE.md](design/CLAUDE.md)：产品设计索引与权威边界
- [frontend/CLAUDE.md](frontend/CLAUDE.md)：前端工程入口
- [backend/CLAUDE.md](backend/CLAUDE.md)：后端工程入口

README 面向人类快速了解项目；agent 的权威入口始终是 `AGENTS.md` / `CLAUDE.md` 和各目录 `CLAUDE.md`。

## 当前范围

- 服务对象：单一客户，单一工作空间
- 产品版本：0.1.0
- 主生产入口：`AI 摄影棚`
- 素材准备入口：`搭配服装库`、`数字模特`
- 输出目标：每个主推款任务生成 1 份拍摄剧本、1 张 3:2 分镜预览和前 5 个镜头最终棚拍成片
- 默认视觉生成器：`Image-2`，当前默认 provider 为 `toapis`，支持 `fal` provider 配置
- 对象存储：统一使用 S3 兼容对象存储；私有化默认 RustFS
- 暂不包含：复杂多租户权限、计费、自动质量检测、视频生成、鞋包饰品商品级入库

## 0.1.0 流程

```text
搭配服装库维护真实服饰素材与标签
-> 数字模特维护默认模特身份参考
-> AI 摄影棚上传本次主推款
-> 冻结主推款输入版本和默认模特快照
-> AI Stylist 理解主推款并检索候选搭配服装
-> Aesthetic Critic 批判审美成立性、品牌成立性和商品成立性
-> 输出可追溯 StylingPlan
-> AI Director 生成拍摄剧本 ShootScript
-> ShotPlan 将 StylingPlan 落到图位、穿着关系、镜头、姿势和走位
-> 生成 3:2 分镜预览
-> 按前 5 个镜头并发生成最终电商棚拍成片
-> 生成记录复盘任务、最终图、attempt 和 provider 调试信息
```

产品原则：

1. 用户不写长 prompt，用户确认拍摄信息和生成结果。
2. 系统生成的是一组电商棚拍成片，不是单张自由创作图。
3. 主推款和被选中的搭配服装都是真实商品，最终图中需要商品级保真。
4. 搭配服装必须服务主推款，而不是让主推款服务整套造型。
5. `Image-2` 是最终视觉生成器，不承担标签体系、选搭策略、历史追溯和业务状态管理。
6. 前台 UI 面向普通业务用户，AI 任务包、prompt、provider 请求等复杂细节默认隐藏。

## 配置管理

本项目支持两种本地运行方式：

- 日常开发：RustFS 用 Docker Compose 启动，前端和后端本机裸跑，见 [本地开发说明](docs/local-development.md)。
- 完整部署模拟：前端、后端、RustFS 全部由 Docker Compose 管理，见 [部署手册](docs/deployment.md)。

配置采用单一主配置源：

- 根目录 `.env.example` 是唯一提交到 Git 的环境变量模板。
- 根目录 `.env` 是本地开发、Docker Compose 和后端裸跑共同读取的主配置文件，不提交 Git。
- `backend/.env.local` 是可选的后端本地覆盖文件，只用于临时覆盖少量后端变量，不放共享密钥，不提交 Git。
- 生产环境应由部署平台、CI/CD 或 Secret Manager 注入环境变量，不依赖仓库中的真实 `.env` / `.env.prod`。

首次配置：

```bash
cd /path/to/ruyi-dream
cp .env.example .env
```

关键环境变量：

```env
GEMINI_PROVIDER=toapis
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3-flash-preview

APP_S3_ENDPOINT_URL=http://127.0.0.1:9000
APP_S3_PUBLIC_ENDPOINT_URL=http://127.0.0.1:9000
APP_S3_BUCKET=ruyi-dream
APP_S3_REGION=auto
APP_S3_FORCE_PATH_STYLE=true
APP_S3_PRESIGN_EXPIRES_SECONDS=900
APP_S3_ACCESS_KEY_ID=<local-or-production-access-key>
APP_S3_SECRET_ACCESS_KEY=<local-or-production-secret-key>
APP_USER_PASSWORD=<production-user-password>
APP_ADMIN_PASSWORD=<production-admin-password>
APP_SESSION_SECRET=<production-session-signing-secret>
APP_API_ACCESS_TOKEN=<optional-ops-script-token>
APP_TRUST_REVERSE_PROXY_AUTH=false

GPT_IMAGE2_PROVIDER=toapis
TOAPIS_API_KEY=
TOAPIS_BASE_URL=https://toapis.com
TOAPIS_MAX_CONCURRENCY=5
FAL_KEY=
GPT_IMAGE2_QUALITY=high
GPT_IMAGE2_OUTPUT_FORMAT=png
GPT_IMAGE2_RESOLUTION=1K
GENERATION_POLL_INTERVAL_SECONDS=5
```

输入图片、生成结果和导出 ZIP 统一写入 S3 兼容对象存储。私有化部署默认使用 RustFS；也可以通过同一组 `APP_S3_*` 配置切换到 OSS、COS、AWS S3 或其它 S3 兼容服务。生产部署方式见 [RustFS 对象存储部署](deployment/rustfs/README.md)。

## Docker Compose 部署

项目根目录提供应用 `docker-compose.yml` 和可选 RustFS `docker-compose.rustfs.yml`。`.env.example` 默认通过 `COMPOSE_FILE` 同时启用两者；宿主机裸跑后端使用 `APP_S3_ENDPOINT_URL=http://127.0.0.1:9000`，容器内 backend 会由 `docker-compose.rustfs.yml` 覆盖为 `http://rustfs:9000`：

```bash
cp .env.example .env
# 编辑 .env，设置 APP_S3_*、PUBLIC_BACKEND_URL、PUBLIC_S3_URL、GEMINI_API_KEY、TOAPIS_API_KEY 或 FAL_KEY
docker compose up -d --build
docker compose ps
```

默认端口：

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8000`
- RustFS S3 API：`http://127.0.0.1:9000`
- RustFS Console：`http://127.0.0.1:9001`

使用外部 S3 兼容对象存储时，把 `.env` 里的 `COMPOSE_FILE` 改为只包含应用 Compose 文件，并配置同一组 `APP_S3_*`：

```env
COMPOSE_FILE=docker-compose.yml
APP_S3_ENDPOINT_URL=https://<s3-compatible-endpoint>
APP_S3_PUBLIC_ENDPOINT_URL=https://<browser-reachable-s3-endpoint>
APP_S3_BUCKET=ruyi-dream-prod
APP_S3_REGION=<region-or-auto>
APP_S3_FORCE_PATH_STYLE=false
APP_S3_ACCESS_KEY_ID=<access-key-id>
APP_S3_SECRET_ACCESS_KEY=<secret-access-key>
```

RustFS/MinIO 常用 `APP_S3_FORCE_PATH_STYLE=true`；阿里 OSS 等要求 virtual-hosted style 的服务应设为 `false`。

部署设计、日常运维、备份恢复见：

- [部署手册](docs/deployment.md)
- [本地开发说明](docs/local-development.md)

## 前端路由

前端支持可恢复 URL：

```text
/studio
/studio?task={taskId}
/library
/model
/records
/records?task={taskId}
```

其中：

- `/studio?task={taskId}` 会在 AI 摄影棚恢复指定任务。
- `/library` 用于维护搭配服装库。
- `/model` 用于维护默认数字模特。
- `/records?task={taskId}` 会打开指定生成记录详情。

## 项目结构

```text
.
├── AGENTS.md
├── CLAUDE.md
├── README.md
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
│   ├── CLAUDE.md
│   ├── user-guide.md
│   ├── local-development.md
│   ├── deployment.md
│   ├── loadtest.md
│   └── implementation-samples/
└── docker-compose.yml
```

当前 `frontend/` 是 React + Vite + TypeScript 单页应用；`backend/` 是 FastAPI + SQLite + S3 兼容对象存储服务。

## 文档目录

- [设计总索引](design/CLAUDE.md)
- [用户使用教程](docs/user-guide.md)
- [产品总览](design/00_product_overview/product_overview.md)
- [0.1.0 产品需求](design/00_product_overview/product_requirements.md)
- [生成流程](design/01_generation_flow/generation_flow.md)
- [Image-2 输入职责拆分与提示词策略](design/01_generation_flow/image2_input_strategy.md)
- [生成要素职责拆分总表](design/01_generation_flow/generation_responsibility_matrix.md)
- [品类图位规则](design/02_brand_and_assets/category_shot_rules.md)
- [素材与参数规范](design/02_brand_and_assets/assets_and_parameters.md)
- [普通用户视角 UI/UX 设计](design/03_ux_guide/user_uiux.md)
- [技术路线与生产实操](design/04_implementation_guide/technical_route_and_operations.md)
- [本地开发说明](docs/local-development.md)
- [部署手册](docs/deployment.md)
- [压测与体验评估](docs/loadtest.md)
- [实施样例](docs/implementation-samples/README.md)
- [后端领域模型](backend/domain_model.md)
- [后端数据库结构](backend/database_schema.md)
- [RustFS 对象存储部署](deployment/rustfs/README.md)
