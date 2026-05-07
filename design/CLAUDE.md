# design/ — Agent Guide

## 职责 (Scope)

本目录存放 Lily AI 模特组图生成 MVP 的**权威设计文档**：产品范围、生成流程、品牌/素材规则、UX、技术生产路线。

单 SKU 实验、生成结果观察、prompt 试验不放在本目录作为权威依据；这类内容统一在 `../simulations/`。历史复盘和未升格的设计理由放在 `99_drafts/`。

## 文档索引 (Index)

| 路径 | 内容 | 何时需要读 |
|---|---|---|
| `00_product_overview/CLAUDE.md` | 产品定位、MVP 范围、PRD | **任何**产品/功能设计前 |
| `01_generation_flow/CLAUDE.md` | 生成主流程、Image 2 输入策略、生成要素职责拆分 | 设计生成链路、任务包、prompt 策略时 |
| `02_brand_and_assets/CLAUDE.md` | 品类组图规则、素材参数、品牌调性模板、尺寸增强 | 处理素材、品牌模板、图位规则、尺寸比例时 |
| `03_ux_guide/CLAUDE.md` | 普通用户视角 UI/UX、页面信息层级、交互边界 | 设计或实现前端页面时 |
| `04_implementation_guide/CLAUDE.md` | 技术路线、生产实操、模型分工、任务包落地方式 | 工程实现、技术拆分、生产流程设计时 |
| `99_drafts/CLAUDE.md` | 历史复盘、实验结论、未升格设计理由 | 理解历史问题或判断是否需要升格为规范时 |

## 权威边界 (Authority)

- **产品范围源头**：`00_product_overview/product_overview.md`、`00_product_overview/mvp_prd.md`
- **生成主流程源头**：`01_generation_flow/generation_flow.md`
- **Image 2 输入策略源头**：`01_generation_flow/image2_input_strategy.md`
- **生成职责拆分源头**：`01_generation_flow/generation_responsibility_matrix.md`
- **图位规则源头**：`02_brand_and_assets/category_shot_rules.md`
- **素材/参数源头**：`02_brand_and_assets/assets_and_parameters.md`
- **品牌调性源头**：`02_brand_and_assets/lily_brand_style_templates.md`
- **前台 UI/UX 源头**：`03_ux_guide/user_uiux.md`
- **技术生产路线源头**：`04_implementation_guide/technical_route_and_operations.md`

## 本地规则 (Local Rules)

1. **任何设计前先读产品范围和职责拆分**：至少阅读 `00_product_overview/product_overview.md` 与 `01_generation_flow/generation_responsibility_matrix.md`。
2. **先判断职责，再写 prompt**：任何生成问题先判断属于图片事实、参数比例、品牌模板、镜头任务、同组一致性还是导出规格。
3. **Image 2 只做最终生成**：品牌调性、Pose、搭配、镜头和组图结构由预处理层/策划层/模板库负责。
4. **前台 UI 不展示系统复杂性**：普通用户默认只看到服装素材、默认组图方案、生成结果；prompt、任务包、策划器、模板命中等只在内部或调试模式出现。
5. **实验结论要升格后才能成为规范**：`../simulations/` 和 `99_drafts/` 中的新发现，只有同步进对应权威文档后，才可作为正式实现依据。
6. **新增/移动/删除 md 文件时必须更新本文件及对应子目录 `CLAUDE.md` 索引**。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
- 后端实现：`../backend/CLAUDE.md`
- 实验记录：`../simulations/CLAUDE.md`
