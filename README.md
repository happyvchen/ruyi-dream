# Lily AI 模特组图生成 MVP

本项目是为 Lily 当前业务阶段设计的内部生产工具，不是标准 SaaS，也不面向多客户、多租户、计费、权限、模板市场等通用商业化场景。

项目目标是：基于 `数字模特` 和 `1 个服装货号图包`，自动生成一组可用于淘宝、天猫等电商场景的 Lily 风格模特服装展示图。

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
- 主入口：生成组图
- 数字模特：产品支持多数字模特，当前阶段先维护 1 个默认数字模特
- 风格方向：Lily 白/浅灰棚拍主图风格
- 输出目标：每个货号生成一套主图
- 暂不包含：自动质量检测、多客户 SaaS、复杂权限、外景大片、视频生成

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
