# design/ — Agent Guide

## 职责 (Scope)

本目录存放 ruyi-dream MVP 的权威设计文档：产品范围、生成流程、搭配服饰与媒体规则、UX、技术生产路线。

新版形态是 B2B AI 数字化摄影棚：用户上传主推款，系统通过 AI Stylist 工作流理解主推款、探索多套造型方案、用 Aesthetic Critic 判断搭配是否真正好看、符合品牌并服务主推款，再从搭配服饰库选择真实可见的配角服饰或判断无需库内配角，生成 3x2 六宫格预览，并按前 5 格生成电商棚拍成片。旧版 demo 已封存为 `demo-20260518`，只复用其中有效工程手段，不延续旧版产品形态作为主流程。

## 文档索引 (Index)

| 路径 | 内容 | 何时需要读 |
|---|---|---|
| `00_product_overview/CLAUDE.md` | 产品定位、MVP 范围、PRD | **任何**产品/功能设计前 |
| `01_generation_flow/CLAUDE.md` | 生成主流程、Image-2 输入策略、生成要素职责拆分 | 设计生成链路、任务包、prompt 策略时 |
| `01_generation_flow/brand_style_retrieval_workflow.md` | Lily 官方样本库、候选漏斗、搭配库充足性诊断、穿法质量门、受控配饰和阶段性实施计划 | 设计大规模搭配库选搭、品牌风格检索、穿法/配饰边界时 |
| `02_brand_and_assets/CLAUDE.md` | 搭配服饰、标签、媒体资产、数字模特、ShotPlan 字段 | 处理素材、标签、媒体、图位规则时 |
| `02_brand_and_assets/lily_tagging_research_raw.md` | Lily 标签体系资料备份：官方公开信息、天猫店铺采集、本地素材盘点、甄别规则 | 追溯 Lily 标签体系设计依据时 |
| `02_brand_and_assets/lily_tagging_system.md` | Lily 服装视觉生产标签体系：AI 标签、人工标签、官方标题标签、选搭压缩格式 | 实现或评审 Lily 标签体系时 |
| `02_brand_and_assets/lily_gemini_tagging_flow.md` | Lily Gemini 打标流程：输入、输出、校验、effective_tags 合成和选搭压缩 | 实现或排查 Gemini 打标时 |
| `03_ux_guide/CLAUDE.md` | 普通用户视角 UI/UX、页面信息层级、交互边界 | 设计或实现前端页面时 |
| `04_implementation_guide/CLAUDE.md` | 技术路线、生产实操、模型分工、任务包落地方式 | 工程实现、技术拆分、生产流程设计时 |

## 权威边界 (Authority)

- **产品范围源头**：`00_product_overview/product_overview.md`、`00_product_overview/mvp_prd.md`
- **生成主流程源头**：`01_generation_flow/generation_flow.md`
- **Image-2 输入策略源头**：`01_generation_flow/image2_input_strategy.md`
- **生成职责拆分源头**：`01_generation_flow/generation_responsibility_matrix.md`
- **素材/标签/媒体源头**：`02_brand_and_assets/assets_and_parameters.md`
- **ShotPlan 品类图位规则**：`02_brand_and_assets/category_shot_rules.md`
- **品牌调性参考**：`02_brand_and_assets/brand_style_templates.md`
- **前台 UI/UX 源头**：`03_ux_guide/user_uiux.md`
- **技术生产路线源头**：`04_implementation_guide/technical_route_and_operations.md`

## 本地规则 (Local Rules)

1. **任何设计前先读产品范围和职责拆分**：至少阅读 `00_product_overview/product_overview.md` 与 `01_generation_flow/generation_responsibility_matrix.md`。
2. **先判断职责，再写 prompt**：任何生成问题先判断属于主推款事实、候选召回、AI Stylist、StylingPlan、搭配服饰事实、数字模特、ShotPlan、生成配饰、媒体输入预算还是导出规格。
3. **Image-2 只做视觉生成**：智能选搭、ShotPlan、标签体系、历史快照和状态机由前置服务负责。
4. **前台 UI 不展示系统复杂性**：普通用户默认只看到主推款、搭配服饰、六宫格、最终结果和生成记录；prompt、provider 请求、快照 JSON 只在技术详情出现。
5. **实验结论要升格后才能成为规范**：外部文档、临时验证和历史讨论的发现，只有同步进对应权威文档后，才可作为正式实现依据。
6. **新增/移动/删除 md 文件时必须更新本文件及对应子目录 `CLAUDE.md` 索引**。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
- 后端实现：`../backend/CLAUDE.md`
