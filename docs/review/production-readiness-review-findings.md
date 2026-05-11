# ruyi-dream Production Readiness Review Findings

## Status

- Branch: `codex/production-readiness-review`
- Review started: 2026-05-11
- Current phase: first production-readiness repair batch committed; review follow-up auth fixes applied

## Repair Progress

### Fixed in repair batch 2026-05-11

- F-R02-001: 生成记录从物理删除改为归档隐藏，保留数据库记录、对象 key 和事件日志。
- F-R05-002: `/api/health` 不再返回数据库路径、S3 endpoint、bucket 等内部部署信息。
- F-R07-002: shutdown 分别 cancel/await generation poller 与 planner poller。
- F-R09-001: 上传增加文件大小、数量、真实图片解码、像素和格式校验；对象写入或后续 DB 写入失败都会清理本批新增对象。
- F-R11-001: 移除代码内默认对象存储密钥，并在启动时校验必填配置；生产模式拒绝默认、占位、过短和带首尾空白的密钥。
- F-R13-001: 增加 `docs/runbook.md`，覆盖故障分级、排查命令、常见 provider/storage/SQLite/auth 问题和升级包信息。
- F-R10-002: 后端容器创建非 root 用户并仅让 `/app/data` 可写；前端切换到 unprivileged Nginx 镜像并监听 8080。

### Follow-up fixes from post-commit review 2026-05-11

- 生产模式拒绝 `.env.example` 的 `replace-with-*` 占位密钥、`change-me`、`admin`、空白/过短等弱值。
- 新建 SKU、追加图片、头像上传在 DB 写入失败时会删除本次已写入对象，降低孤儿对象风险。
- 多头像批量上传逐个保留临时 reference id，避免多个未设置头像拿到同一个 `__unassigned__...` key。
- 增加 MVP single-password app gate：后端校验 `APP_GATE_PASSWORD` / `APP_GATE_PASSWORD_HASH` 后签发 `HttpOnly` session cookie；`APP_API_ACCESS_TOKEN` 仅保留为脚本/运维 fallback。
- 增加 PR template 与 CODEOWNERS，配合现有 CI workflow 补齐仓库内协作基线。

### Partially addressed in repair batch 2026-05-11

- F-R10-001: RustFS 镜像改为固定 tag，后端直接依赖改为 pin；仍需进一步引入完整 lock/constraints 或 digest 固定。
- F-R05-001: 已加入 single-password app gate / 反向代理鉴权边界；完整用户、角色、workspace 仍是后续 SaaS 化工作。
- F-R11-002: CORS method/header 已收紧；浏览器 API 改为 session cookie 访问，CSRF/CORS 与正式用户鉴权联动仍待后续权限设计。
- F-R12-001: 已加入最小 CI（backend compile/import、frontend build、compose config）；核心业务测试仍待补齐。
- F-R14-001: 已加入 CI workflow、PR template、CODEOWNERS；branch protection 和 automatically delete head branches 仍需仓库管理员开启。

### Product/security decisions added 2026-05-11

- D03: MVP 阶段增加前端应用门禁。用户进入 ruyi-dream 前必须输入部署级访问密码；暂不引入用户、角色、workspace、审计身份和多租户隔离。
- D04: 不再把 `APP_API_ACCESS_TOKEN` 打进前端静态包作为生产门禁；PR-002 调整为“服务端校验密码并签发会话”，前端只保存短期会话，不保存部署密钥。

## Priority Legend

- P0: 上线阻断，必须先解决。
- P1: 生产高风险，建议进入上线前修复清单。
- P2: 质量、体验、可维护性问题，可进入近期迭代。
- P3: 后续优化或规模化前准备。

## Executive Summary

当前项目已经具备 MVP 端到端链路，但距离“生产级可交付”还有明显差距。主要风险集中在四类：

- 产品正式定位应升级为“电商视觉生产系统”；当前实现仍停留在“主图/组图 MVP”，Seedance 2.0 视频能力本轮先作为 review 后的设计开发入口备注。
- 关键生产能力缺失：审核流、任务取消、批量处理、质检、权限、审计保留、失败恢复和可观测性还不足。
- 工程结构处于单体 MVP 状态：前端 3000 行 `App.tsx`、后端 2200 行 `main.py`，没有测试、CI、正式迁移体系，异步任务仍依赖后端进程内 poller。
- 安全与交付基线不足：无鉴权、上传校验弱、默认密钥、Docker 镜像未固定 digest、健康接口泄露部署信息，GitHub 分支保护尚未由管理员开启。

## Findings

### R01 产品定位与范围

#### F-R01-001 [P1] 产品已定位为电商视觉生产系统，但当前架构仍按图片组图工具建模

Evidence:

- `design/00_product_overview/product_overview.md:5` 把 ruyi-dream 定义为电商视觉生产工具，并提到后续结合 Seedance 2.0 扩展电商主视频。
- 同一文档 `design/00_product_overview/product_overview.md:13-21` 和 PRD `design/00_product_overview/mvp_prd.md:36-45` 又明确把视频生成排除在当前范围外。
- 当前技术分工 `design/00_product_overview/product_overview.md:81-87` 只有 Image 2 图片生成层，没有抽象出 media generation provider、shot-to-video、首帧/参考视频资产、视频任务状态、视频输出交付等边界。
- Product owner 已确认 ruyi-dream 正式定位为“电商视觉生产系统”；Seedance 2.0 主视频能力在本轮 review 后进入设计/开发。

Risk:

- 如果后续接入 Seedance 2.0 时仍沿用当前 `GenerationRun/GenerationImage` 图片模型，容易把视频任务硬塞进图片模型，导致领域模型、UI、导出、对象存储和任务状态再次重构。
- 产品名已经变成 ruyi-dream，但前台主入口仍是“生成组图”，用户认知仍被锁在图片工具。

Recommendation:

- 本轮 review 后，在产品/架构设计中增加 `MediaRun` / `MediaAsset` / `ImageGenerationTask` / `VideoGenerationTask` 的概念边界。
- 当前 review 暂不展开 Seedance 2.0 代码设计，只在交付清单中保留“视频能力设计前置项”。

Decision:

- D01: ruyi-dream 正式定位为“电商视觉生产系统”。
- D02: Seedance 2.0 主视频能力在本轮 review 后进入设计/开发；本轮 review 仅备注架构前置风险。

#### F-R01-002 [P1] MVP 成功标准偏主观，缺少生产交付验收指标

Evidence:

- `design/00_product_overview/product_overview.md:152-158` 的成功标准是“客户认为其中部分图片可进入实际使用或轻后期使用”。

Risk:

- 该标准适合早期验证，但不适合作为生产交付验收。没有定义可用率、失败率、单货号成本、平均生成时长、重生次数、人工后期占比、商品保真通过率。

Recommendation:

- 增加生产验收 KPI：例如“每个货号 5 张图中至少 3 张可轻后期使用”“单货号从上传到可导出 <= X 分钟”“服装结构重大错误率 <= Y%”“任务失败可恢复率 100%”。

### R02 核心业务流程

#### F-R02-001 [P1] 生产记录允许删除，与“可复盘/可追溯”的生产原则冲突

Status: Fixed in repair batch 2026-05-11.

Evidence:

- 产品总览要求记录保存输入输出和重生过程，方便恢复与复盘：`design/00_product_overview/product_overview.md:130-138`。
- 后端提供删除生成记录接口并删除输出目录：`backend/app/main.py:961-988`。
- 前端历史详情页直接暴露“删除记录”按钮：`frontend/src/App.tsx:2120-2126`。

Risk:

- 生产环境中删除生成记录会破坏审计链路，也会导致后续难以排查某张商品图来自哪次输入、哪次 provider 响应。

Recommendation:

- 生产级应改为“归档/隐藏/作废”，保留数据库记录、provider 快照和对象 key。
- 真删除仅允许管理员在数据清理工具中执行，并产生不可变审计日志。

#### F-R02-002 [P2] 核心流程缺少“审核/交付状态”，只有生成态和最终图标记

Evidence:

- 当前流程到“设为最终图 / 导出”结束，见 `README.md` 当前 MVP 流程。
- 数据模型只有 `is_final`，没有 review status、approval status、handoff status。

Risk:

- 真实电商生产通常需要“可用/需重生/需精修/已交付/已上传”等状态。只靠 `is_final` 无法支撑视觉团队协作和交付管理。

Recommendation:

- 增加结果图业务状态：`candidate`、`approved`、`needs_regen`、`needs_retouch`、`delivered`。
- 生成任务增加交付状态和 reviewer 字段，至少先在文档中定义。

### R03 UI/UX 工作台

#### F-R03-001 [P2] UI 仍暴露较多 AI/内部实现语言，与“拍摄任务工作台”原则有偏差

Evidence:

- UX 文档要求默认隐藏 prompt、前置策划器状态、Image 2 任务包等内部细节：`design/03_ux_guide/user_uiux.md:95-105`。
- 当前前端在普通素材页直接显示“生图方案”“Gemini 会根据服装主图识别品类、服饰搭配和分镜剧本”：`frontend/src/App.tsx:1512-1523`。
- `ShotPlanBoard` 默认区域使用“生图方案”“Gemini 调用记录”等表达：`frontend/src/App.tsx:2219-2265` 以及后续 details。

Risk:

- 普通业务用户会被模型和内部生成机制干扰，不利于把产品定位成稳定生产工具。

Recommendation:

- 前台将“生图方案”改为“拍摄方案”或“组图方案”。
- Gemini 调用记录、raw response、provider request 只在调试模式显示，并加环境开关。

#### F-R03-002 [P2] 主流程步骤实际是 5-6 个动作，和“默认路径短”的目标不一致

Evidence:

- UX 文档说默认路径应短：`design/03_ux_guide/user_uiux.md:63-71`。
- 当前主流程需要去服装素材页上传、保存、生成默认方案，再回生成页预览、生成、确认、导出。生成页里还提示“请到服装素材页完成预览与编辑后保存上传”：`frontend/src/App.tsx:974-980`。

Risk:

- 对运营用户来说，跨页面往返会降低第一次成功率；批量货号生产时也会低效。

Recommendation:

- 设计“创建拍摄任务”入口，把上传/选货号、方案确认、预览和生成收拢到一个任务流中。
- 服装素材页保留为素材库管理，不作为每次生产的必经页面。

### R04 前端架构与状态管理

#### F-R04-001 [P1] 前端主文件过大，业务状态、路由、轮询、上传、模型管理和历史详情高度耦合

Evidence:

- `frontend/src/App.tsx` 当前 3001 行，包含 route parsing、状态管理、上传、生成、模特维护、历史详情、图片预览等全部逻辑。
- `frontend/src/App.tsx:1-80` 已定义大量跨页面类型和状态形态；`frontend/src/App.tsx:953-1085` 直接渲染主工作台；`frontend/src/App.tsx:2923-2955` 同文件处理路由。

Risk:

- 后续接入 Seedance、批量任务、审核流时，单文件会迅速变成不可安全修改的核心风险点。
- 难以写组件级测试和复用业务逻辑。

Recommendation:

- 拆分为 `pages/GeneratePage`、`pages/GarmentLibraryPage`、`pages/ModelProfilesPage`、`pages/GenerationHistoryPage`。
- 把生成任务恢复、轮询、上传草稿、模型头像管理拆为 hooks。
- 将路由解析和业务状态恢复从 UI 组件中移出。

#### F-R04-002 [P1] 前端没有测试框架和关键交互测试

Evidence:

- `frontend/package.json:6-10` 只有 `dev/build/preview`，没有 `test`、`lint`、`typecheck` 独立脚本。
- 仓库内未发现 test/spec 或 Vitest/Playwright 配置。

Risk:

- 生成恢复、轮询、深链接、删除确认、导出这些核心行为没有回归保障。

Recommendation:

- 加最小测试栈：Vitest + React Testing Library + Playwright smoke。
- 第一批覆盖：路由恢复、生成按钮禁用条件、历史任务恢复、上传图包预览、错误提示。

### R05 后端 API 契约

#### F-R05-001 [P1] API 没有鉴权和用户上下文，无法作为生产系统暴露

Status: Fixed for MVP private production baseline 2026-05-11. A single-password app gate now protects browser API access with a server-signed session cookie; `APP_API_ACCESS_TOKEN` remains only as an optional ops/script fallback. Formal users, roles, workspaces, and per-user audit identity remain future SaaS work.

Evidence:

- FastAPI app 全局只配置 CORS：`backend/app/main.py:37-45`，所有 `/api/*` 路由没有 auth dependency。
- 文档当前排除复杂权限，但并没有定义单用户私有部署鉴权边界。

Risk:

- 只要后端端口可访问，任何人都可以上传、删除素材、删除生成记录、导出图片、查看日志。

Recommendation:

- 即使 MVP 私有化，也应至少支持部署级访问密码门禁或反向代理鉴权。
- 推荐方案：增加 `/api/auth/login`、`/api/auth/logout`、`/api/auth/session`。登录时后端校验 `APP_GATE_PASSWORD` 或其哈希，成功后设置 `HttpOnly`、`SameSite=Lax`、短期有效的 session cookie；后续 `/api/*` 只接受有效 session。前端只负责展示登录页和会话状态，不把部署密钥写进静态 bundle。
- 生产版应引入用户、角色、workspace，或明确要求只能部署在受控内网并由网关保护。

MVP 门禁边界:

- 只解决“进入应用前需要知道访问密码”的问题，不区分用户身份。
- 不提供注册、找回密码、用户管理、角色权限、工作空间隔离。
- 不把密码或 API token 编译进前端 JS。
- 失败登录需要统一错误提示，并增加基础速率限制或冷却时间，避免简单爆破。
- 登录态建议默认 8-24 小时有效；退出登录必须清理服务端 session 或使签名 cookie 失效。
- `/api/health` 继续匿名，用于容器和反向代理健康检查。

Migration note:

- `APP_API_ACCESS_TOKEN` 可短期保留为脚本/运维 API token，但不再作为浏览器前端的主要访问方式。
- 如果部署在企业网关后，可继续用 `APP_TRUST_REVERSE_PROXY_AUTH=true` 跳过内置门禁，但文档必须明确外部鉴权责任。

#### F-R05-003 [P1] 当前前端门禁设计缺失，静态 API token 不能替代登录门禁

Status: Fixed for MVP private production baseline 2026-05-11. The Compose/Docker frontend build no longer injects `APP_API_ACCESS_TOKEN`; browser access uses `/api/auth/login`, `/api/auth/session`, `/api/auth/logout`, and the `ruyi_session` `HttpOnly` cookie.

Evidence:

- 修复前 `frontend/src/api.ts` 通过 `VITE_API_ACCESS_TOKEN` 把 token 加到请求头。
- 修复前 `docker-compose.yml` 和 `frontend/Dockerfile` 会在构建前端时注入 `VITE_API_ACCESS_TOKEN`。
- 浏览器前端是公开静态资源，任何能访问应用的人都可以从构建产物或 DevTools 中获得该 token。

Risk:

- 该方案阻挡不了已经能打开前端页面的人直接调用 API，只能减少无 token 的脚本误扫。
- 对客户交付时容易产生“已有鉴权”的误解，但实际没有登录门禁、会话过期、退出登录和密码轮换体验。

Recommendation:

- 新增“应用门禁”作为 PR-002 的修正目标：用户进入任何业务页面前必须先通过密码验证。
- 后端用服务端环境变量保存门禁密码或密码哈希，前端永不接触明文密码以外的长期密钥。
- 前端提供独立登录页；未登录访问业务路由时停留在登录页，登录成功后恢复原目标路径。
- API 统一通过 session cookie 校验；导出、上传、生成、日志等接口同样受保护。
- 文档中把此能力命名为“single-password app gate”，避免和正式 user auth 混淆。

#### F-R05-002 [P2] 健康检查接口泄露内部路径和存储配置

Status: Fixed in repair batch 2026-05-11.

Evidence:

- `/api/health` 返回数据库路径、S3 endpoint、bucket、provider 配置状态：`backend/app/main.py:135-144`。

Risk:

- 对公网或弱内网暴露时，会泄露部署拓扑和存储信息。

Recommendation:

- 对匿名健康检查只返回 `ok`、版本、必要 readiness 状态。
- 详细诊断信息放到管理员诊断接口并鉴权。

### R06 数据模型与迁移

#### F-R06-001 [P1] 数据库迁移依赖运行时 schema patch/rebuild，缺少可审计迁移版本

Status: Fixed MVP baseline 2026-05-11. `schema_migrations` now records applied migrations, and existing schema patch/rebuild steps are versioned as `001`-`003`.

Evidence:

- 修复前 `backend/app/db.py:41-170` 在启动时 `CREATE TABLE IF NOT EXISTS` 并直接 `ensure_column`。
- 修复前 `backend/app/db.py:171-348` 会直接重建 planning schema。

Risk:

- 生产数据升级不可预测，无法在上线前 dry-run，也难以回滚。
- 多人或多环境部署时，不知道数据库处于哪个 migration version。

Recommendation:

- 引入 Alembic 或轻量 migration 表。
- 所有 schema 变更进入版本化 migration，启动只执行已验证迁移。

### R07 异步任务与生成链路

#### F-R07-001 [P1] 后台 poller 在 API 进程内运行，缺少任务锁；多副本或重启时有重复提交风险

Evidence:

- startup 直接创建两个 poller task：`backend/app/main.py:114-120`。
- poller 扫描 `QUEUED` 图片并提交 provider：`backend/app/main.py:1381-1410`。
- 部署文档也承认多后端副本会造成重复扫描风险：`docs/deployment-architecture.md:94-95`。

Risk:

- 一旦为了稳定性运行多个 backend，或重启窗口出现并发进程，可能重复提交 fal 任务，造成成本浪费和状态覆盖。

Recommendation:

- 生产前拆出 worker，或增加数据库级 claim/lease 字段，例如 `locked_by`、`locked_until`、`submit_attempts`。
- 提交 provider 前用事务原子抢占任务。

#### F-R07-002 [P2] shutdown 里取消 planner task 后等待了错误变量

Status: Fixed in repair batch 2026-05-11.

Evidence:

- `backend/app/main.py:123-132` 取消 `planner_task` 后 `await task`，而不是 `await planner_task`。

Risk:

- 关闭时 planner poller 可能没有被正确等待，导致不干净退出或吞掉取消异常。

Recommendation:

- 改为分别 cancel/await generation task 和 planner task，并补一个生命周期测试。

#### F-R07-003 [P2] 任务缺少取消、重试上限和超时策略

Evidence:

- 状态机文档有 `CANCELLED`，但当前 API 中没有取消 generation run 或 generation image 的接口。
- poller 每轮查询 active rows，但没有看到全局超时、最大 submit/status retry 或指数退避策略。

Risk:

- provider 长时间不返回或状态 URL 异常时，任务可能长期占据 active 状态，用户无法自行止损。

Recommendation:

- 增加取消接口、超时策略、最大重试次数、失败分类和可手动重试。

### R08 AI 策划层与任务包

#### F-R08-001 [P1] 品牌模板仍以内嵌文档和 planner 输出为主，没有形成可版本化模板资产

Evidence:

- 后端架构承认 `BrandTemplate` 当前没有独立表：`backend/architecture.md` 数据对象部分。
- 产品总览把品牌调性模板作为核心能力：`design/00_product_overview/product_overview.md:46-49`。

Risk:

- 生成结果无法追溯“当时使用的是哪个版本的品牌模板”，后续多品牌或模板更新会影响历史复现。

Recommendation:

- 增加 `brand_templates` / `brand_template_versions` 概念，至少先在文档和 task package snapshot 中明确版本号。

### R09 对象存储与媒体安全

#### F-R09-001 [P1] 上传校验只依赖 content-type，缺少文件大小、像素、解码和恶意文件检查

Status: Fixed in repair batch 2026-05-11.

Evidence:

- 服装上传只检查 `content_type.startswith("image/")`：`backend/app/main.py:179-184`。
- 追加素材同样只检查 content type：`backend/app/main.py:303-310`。
- 模特头像上传同样只检查 content type：`backend/app/main.py:546-567`。

Risk:

- 用户可上传超大文件、伪造 MIME、损坏图片或异常图片，导致内存/存储/模型调用成本风险。

Recommendation:

- 增加最大文件大小、最大图片数量、真实图片解码校验、像素尺寸限制、格式白名单。
- 上传失败时回滚已写入对象，避免孤儿对象。

### R10 部署与运维

#### F-R10-001 [P1] Docker 镜像和 Python 依赖没有完全固定，生产可复现性不足

Status: Partially addressed in repair batch 2026-05-11. Direct backend dependencies, backend transitive constraints, and RustFS image tag are pinned; image digest pinning remains.

Evidence:

- RustFS 使用 `rustfs/rustfs:latest`：`docker-compose.yml:3`。
- 修复前后端 requirements 使用 `fal-client>=0.7.0`、`boto3>=1.35.0`，不是完全 pin。

Risk:

- 相同代码在不同时间构建可能拉到不同行为的镜像或依赖，影响生成链路和对象存储。

Recommendation:

- 固定镜像 tag 或 digest。
- 后端依赖全部 pin，并维护 lock/constraints 文件。

#### F-R10-002 [P2] 容器默认 root 运行，缺少生产安全基线

Status: Fixed in repair batch 2026-05-11.

Evidence:

- `backend/Dockerfile:1-22` 未创建非 root 用户。
- `frontend/Dockerfile:13-18` 使用默认 nginx 镜像运行方式。

Risk:

- 私有化交付中容器逃逸或挂载目录权限风险更高。

Recommendation:

- 后端创建 non-root user，限制可写目录到 `/app/data`。
- 前端确认 nginx 非 root 配置或使用受限镜像。

### R11 安全与配置

#### F-R11-001 [P0] 默认对象存储密钥写在代码配置中

Status: Fixed in repair batch 2026-05-11.

Evidence:

- `backend/app/config.py:21-22` 默认 access key 为 `admin`，secret 为固定字符串 `DQugmV9crHkkYYjG`。

Risk:

- 如果部署时漏配环境变量，服务会使用公开默认密钥，属于生产阻断级安全问题。

Recommendation:

- 生产模式下必须要求显式配置密钥。
- 代码默认值改为空或开发专用，并在启动时根据环境校验。

#### F-R11-002 [P1] CORS 允许 credentials + 全方法全 header，但没有鉴权边界

Status: Partially addressed in repair batch 2026-05-11. CORS methods/headers are narrowed and browser API access now uses a signed session cookie; full CSRF/CORS strategy should be revisited with formal user auth.

Evidence:

- CORS middleware 设置 `allow_credentials=True`、`allow_methods=["*"]`、`allow_headers=["*"]`：`backend/app/main.py:39-44`。

Risk:

- 当前没有 cookie auth 时影响有限，但一旦加入鉴权，宽松 CORS 容易成为跨站请求入口。

Recommendation:

- 生产配置中收紧 method/header。
- 鉴权设计时同步设计 CSRF/CORS 策略。

### R12 测试策略

#### F-R12-001 [P1] 当前没有自动化测试和 CI，无法支撑生产级迭代

Status: Partially addressed in repair batch 2026-05-11. Minimal CI exists; business regression tests remain open.

Evidence:

- 仓库内未发现 test/spec、pytest、Vitest、Playwright 或 `.github/workflows`。
- 前端 package scripts 只有 build 相关：`frontend/package.json:6-10`。

Risk:

- 每次修改生成链路、状态恢复、任务删除、导出、上传都只能人工验证，PR review 无法建立质量门禁。

Recommendation:

- 先建立最小 CI：backend compile/import + API contract smoke，frontend typecheck/build，Docker compose config。
- 再补核心业务测试：上传、生成任务创建、poller 状态转换、导出、历史恢复。

### R13 文档与交接

#### F-R13-001 [P2] 文档比较完整，但缺少生产操作 runbook 和故障分级

Status: Fixed in repair batch 2026-05-11.

Evidence:

- 已有部署、开发、架构文档，但没有故障处理决策树：fal 失败、Gemini 失败、RustFS 不可用、预签名 URL 不可访问、SQLite 损坏等。

Risk:

- 客户现场交付时，非开发人员难以判断问题属于配置、模型、存储还是应用 bug。

Recommendation:

- 增加 `docs/runbook.md`：常见故障、排查命令、恢复步骤、何时升级给研发。

### R14 GitHub 协作规范

#### F-R14-001 [P1] 仓库规则尚未落地到 GitHub 配置

Status: Partially addressed in repair batch 2026-05-11. CI workflow, PR template, and CODEOWNERS are in-repo; branch protection and automatic branch deletion require repository admin action.

Evidence:

- 本次迁移时当前 `gh` 登录用户对私有仓库只有 WRITE 权限，无法开启 branch protection。
- 仓库内没有 PR template、CODEOWNERS、CI workflow。

Risk:

- “不基于 main 修改、每次 PR、必须 review、合并后删分支”的规范没有技术约束，容易被绕过。

Recommendation:

- 管理员开启 main branch protection：require PR、require 1 approval、dismiss stale review、require conversation resolution、disable force push。
- 开启 automatically delete head branches。
- 仓库加入 `.github/pull_request_template.md`、`CODEOWNERS`、CI workflow。

### R15 生产级交付清单

#### 上线前必须完成

| Priority | Item | Suggested PR | Status |
| --- | --- | --- | --- |
| P0 | 移除代码内默认对象存储密钥，启动时校验生产必填配置 | PR-001 | Fixed 2026-05-11 |
| P1 | 增加部署级鉴权或明确由反向代理保护，并收紧 `/api/health` | PR-002 | Fixed MVP baseline 2026-05-11: single-password app gate + server-side session; formal user auth pending |
| P1 | 上传文件增加大小、数量、真实图片解码、像素和格式校验 | PR-003 | Fixed 2026-05-11 |
| P1 | 生成任务增加 claim/lock、超时、取消、重试上限 | PR-004 | Open |
| P1 | 建立最小 CI：backend compile/import、frontend build/typecheck、compose config | PR-005 | Fixed minimal baseline; business tests pending |
| P1 | 建立正式数据库迁移机制或 migration version 表 | PR-006 | Fixed MVP baseline 2026-05-11: lightweight `schema_migrations` table |
| P1 | 固定 Docker 镜像和 Python 依赖版本 | PR-007 | Partial: Python constraints and image tags done; digest pinning pending |
| P1 | GitHub 分支保护、PR 模板、CODEOWNERS、自动删分支 | PR-008 | Partial: CI/PR template/CODEOWNERS added; admin settings pending |

#### 近期质量提升

| Priority | Item | Suggested PR | Status |
| --- | --- | --- | --- |
| P1 | 把 ruyi-dream 产品定位统一为“电商视觉生产系统”，并加入视频能力设计前置边界 | PR-009 | Open |
| P1 | 生产记录从硬删除改为归档/作废，保留审计链路 | PR-010 | Fixed 2026-05-11 |
| P1 | 品牌模板版本化，task package 记录模板版本 | PR-011 | Open |
| P2 | UI 文案从“生图/Gemini”调整为“拍摄/组图/调试模式” | PR-012 | Open |
| P2 | 增加审核/交付状态：可用、需重生、需精修、已交付 | PR-013 | Open |
| P2 | 拆分 `frontend/src/App.tsx` 页面和 hooks | PR-014 | Open |
| P2 | 拆分 `backend/app/main.py` 路由、service、repository、worker | PR-015 | Open |
| P2 | 增加生产 runbook 和故障排查决策树 | PR-016 | Fixed 2026-05-11 |

#### 后续架构演进

| Priority | Item | Suggested PR |
| --- | --- | --- |
| P2 | 定义 `MediaRun` / `MediaAsset` / image task / video task 概念模型，为 Seedance 2.0 做设计准备 | PR-017 |
| P2 | 设计任务队列 worker 化方案，替换进程内 poller | PR-018 |
| P3 | 规划 PostgreSQL、多 workspace、多用户权限和计费边界 | PR-019 |

#### Review-to-PR Plan

- PR-001 到 PR-008 应作为生产交付前的硬门槛。
- PR-009 到 PR-013 需要产品/视觉生产负责人参与确认，尤其是“审核/交付状态”和“电商视觉生产系统”信息架构。
- PR-014 到 PR-015 是工程结构治理，不应和功能需求混在同一个 PR。
- PR-017 在本次 review 完成后启动，作为 Seedance 2.0 视频能力正式设计的前置 PR。

#### Original draft notes

- P0: 移除代码内默认密钥；定义部署必填配置校验。
- P1: 明确产品定位和 Seedance 架构边界。
- P1: 引入鉴权或部署级访问保护。
- P1: 为生成任务增加锁、超时、取消、重试上限。
- P1: 建立数据库迁移机制。
- P1: 建立最小测试和 CI。
- P1: 收紧上传校验。
- P1: 固定镜像和依赖版本。
- P1: GitHub 分支保护和 PR 模板落地。
- P2: 拆分前端/后端巨型文件。
- P2: 调整 UI 文案和调试信息可见性。
- P2: 增加生产 runbook。
