# Lily AI 模特组图生成 MVP

本项目是为 Lily 当前业务阶段设计的内部生产工具，不是标准 SaaS，也不面向多客户、多租户、计费、权限、模板市场等通用商业化场景。

项目目标是：基于 `1 个服装货号图包` 及其默认组图方案，自动生成一组可用于淘宝、天猫等电商场景的 Lily 风格模特服装展示图。

## Agent 入口

如果由 Codex / Claude / 其他 agent 接手本项目，请先读：

- [AGENTS.md](AGENTS.md)：通用 agent 入口
- [CLAUDE.md](CLAUDE.md)：Claude/Codex 入口，与 `AGENTS.md` 同步
- [design/CLAUDE.md](design/CLAUDE.md)：产品设计索引与权威边界
- [frontend/CLAUDE.md](frontend/CLAUDE.md)：前端工程入口
- [backend/CLAUDE.md](backend/CLAUDE.md)：后端工程入口
- [simulations/CLAUDE.md](simulations/CLAUDE.md)：实验记录索引与使用规则
- [brand-analysis/CLAUDE.md](brand-analysis/CLAUDE.md)：品牌分析素材说明

`simulations/` 下的内容是实验记录，不是权威设计。实现产品功能或整理正式方案时，应以 `design/` 下的设计源头文档为准。

## 当前范围

- 服务对象：仅 Lily 一家客户
- 产品阶段：MVP
- 主生产入口：生成组图
- 素材准备入口：服装素材
- 数字模特：产品支持多数字模特，当前阶段先维护 1 个默认数字模特
- 风格方向：Lily 白/浅灰棚拍主图风格
- 输出目标：每个货号生成一套主图
- 暂不包含：自动质量检测、多客户 SaaS、复杂权限、外景大片、视频生成、ZIP 上传、素材图片角色识别

## 当前 MVP 流程

```text
服装素材页上传图包
-> 浏览器本地预览与编辑
-> 保存上传
-> 生成/更新该货号默认组图方案
-> 生成组图页选择货号
-> 直接使用默认方案创建生成任务
-> 提交 GPT Image-2 / fal queue
-> 后台轮询状态并下载结果
-> 单张重生 / 设为最终图 / 导出
```

上传支持：

- 上传 N 张图片，并填写货号，对应一个图包。
- 上传一个文件夹，货号默认使用文件夹名并可修改。
- 上传 N 个文件夹，对应 N 个图包。
- 保存前可编辑货号/标题、删除图片。
- 第一张图片默认作为封面。

## 本地配置

后端会自动读取 `backend/.env`。首次配置可复制示例文件：

```bash
cd backend
cp .env.example .env
```

关键环境变量：

```env
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3-flash-preview

GPT_IMAGE2_PROVIDER=placeholder
GPT_IMAGE2_MODEL_ID=openai/gpt-image-2/edit
GPT_IMAGE2_QUEUE_URL=https://queue.fal.run/openai/gpt-image-2/edit
FAL_KEY=
GPT_IMAGE2_QUALITY=high
GPT_IMAGE2_OUTPUT_FORMAT=png
GENERATION_POLL_INTERVAL_SECONDS=5
```

`GPT_IMAGE2_PROVIDER=placeholder` 会写入本地占位 SVG，不调用真实生图服务。真实调用 fal 时设置：

```env
GPT_IMAGE2_PROVIDER=fal
FAL_KEY=你的_fal_key
```

真实生图走 fal queue。点击生成后后端立即返回任务记录，后台持续查询 fal 状态，前端显示运行计时和每张图状态。

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
│   ├── 04_implementation_guide/
│   └── 99_drafts/
├── frontend/
├── backend/
├── simulations/
└── brand-analysis/
```

当前 `frontend/` 已初始化为 React + Vite + TypeScript 单页 MVP；`backend/` 已初始化为 FastAPI + SQLite + 本地文件系统 MVP。

## 文档目录

- [设计总索引](design/CLAUDE.md)
- [产品总览](design/00_product_overview/product_overview.md)
- [MVP PRD](design/00_product_overview/mvp_prd.md)
- [生成流程](design/01_generation_flow/generation_flow.md)
- [Image 2 输入职责拆分与提示词策略](design/01_generation_flow/image2_input_strategy.md)
- [生成要素职责拆分总表](design/01_generation_flow/generation_responsibility_matrix.md)
- [品类组图规则](design/02_brand_and_assets/category_shot_rules.md)
- [素材与参数规范](design/02_brand_and_assets/assets_and_parameters.md)
- [Lily 品牌调性模板库](design/02_brand_and_assets/lily_brand_style_templates.md)
- [尺寸可选增强模块](design/02_brand_and_assets/optional_measurement_enhancement.md)
- [普通用户视角 UI/UX 设计](design/03_ux_guide/user_uiux.md)
- [技术路线与生产实操](design/04_implementation_guide/technical_route_and_operations.md)
- [后端领域模型](backend/domain_model.md)
- [后端数据库结构](backend/database_schema.md)
- [历史复盘与草稿](design/99_drafts/CLAUDE.md)
- [实验记录索引](simulations/CLAUDE.md)
- [品牌分析素材说明](brand-analysis/CLAUDE.md)

## 本地素材路径

- 模特头部图包：`/Users/fushuai/Desktop/项目/Lily/Face-01`
- 服装图包：`/Users/fushuai/Desktop/项目/Lily/AI单品测试对接`
- 历史真实成片：
  - `/Users/fushuai/Desktop/项目/Lily/2D专供-17款-0410`
  - `/Users/fushuai/Desktop/项目/Lily/3A专供-18款-0410`
  - `/Users/fushuai/Desktop/项目/Lily/3C专供-36款-0417`
  - `/Users/fushuai/Desktop/项目/Lily/3C专供单色款-91款-0420`
  - `/Users/fushuai/Desktop/项目/Lily/3D专供剩余-16款-0427`

## 核心原则

1. 用户不写 prompt，用户确认拍摄信息。
2. 系统不是生成单张图，而是生成一组电商主图。
3. 产品优先复刻 Lily 既有棚拍语言，而不是扩展创意场景。
4. MVP 只保留必要功能，避免 SaaS 化过度设计。
5. 服装保真优先于画面炫技。
6. Image 2 是最终视觉生成器，不承担品牌策略、搭配判断、Pose 策划和镜头规划。
7. 品牌调性、品类规则、Pose、搭配和镜头方案应由前置推理/策划层生成，再交给 Image 2 执行。
8. 前台 UI 面向普通业务用户，默认只展示服装、模特、组图方案、生成结果；AI 任务包、prompt、策划器等复杂细节默认隐藏。
9. 真实生图是持久化异步任务，不在前端 HTTP 请求内长时间等待。
