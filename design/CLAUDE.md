# design/ — Agent Guide

## 职责 (Scope)

本目录存放 ruyi-dream 0.1.0 的权威设计文档：产品范围、生成流程、搭配服装与媒体规则、UX、技术生产路线。

0.1.0 形态是 B2B AI 数字化摄影棚：用户导入主推款，系统通过 AI Stylist 工作流理解主推款、探索并批判多套造型方案，再从搭配服装库选择真实可见的配角服饰或判断无需库内配角；随后按拍摄图位族和五个固定镜头选择当前生效的拍摄母版/姿势预设，通过三类固定成片 Prompt 与 GPT Image-2 多参考图编辑并发生成五张电商棚拍成片。分镜预览、旧“单货号默认组图方案”和动态拍摄剧本不属于当前主流程。

## 文档索引 (Index)

| 路径 | 内容 | 何时需要读 |
|---|---|---|
| `domain.md` | 0.1.0 领域模型设计：模块边界、核心术语、对象定义、状态流转、职责约束和当前实现映射 | 设计领域对象、API、数据库、状态机、Prompt DTO 或跨模块流程前 |
| `00_product_overview/CLAUDE.md` | 产品定位、0.1.0 范围、产品需求 | **任何**产品/功能设计前 |
| `01_generation_flow/CLAUDE.md` | 生成主流程、Image-2 输入策略、生成要素职责拆分 | 设计生成链路、任务包、prompt 策略时 |
| `01_generation_flow/current_generation_workflow.md` | 当前三步直出工作流：15 个拍摄预设、三类固定成片 Prompt、状态机和版本冻结 | 设计或实现成片、重生和状态恢复时 |
| `01_generation_flow/storyboard_preview_business_intent.md` | 历史分镜业务意图；其中五镜头商品表达要求可供拍摄预设设计参考 | 复盘旧流程或设计五镜头预设时 |
| `01_generation_flow/final_image_business_intent.md` | 历史成片业务意图；商品保真、组图一致性和业务验收可参考，镜头 5 安全区以现行品类图位规则为准 | 评审成片质量时 |
| `01_generation_flow/final_image_prompt_research_task_1457.md` | 最终成片 Prompt 研究结论：失败归因、有效/无效规则、黄金链路和系统采纳建议 | 复盘成片调研或决定成片系统改造时 |
| `01_generation_flow/storyboard_prompt_research_task_1440.md` | 分镜预览 Prompt 研究结论：有效/无效规则、镜头 5 修正口径和系统采纳建议 | 复盘分镜 Prompt 实验或决定后续生成手段时 |
| `01_generation_flow/storyboard_workflow_simplification_proposal.md` | 历史决策记录：删除动态剧本步骤、三类固定分镜 Prompt、代码注入动态事实、直接生图 | 复盘工作流简化决策时 |
| `01_generation_flow/brand_style_retrieval_workflow.md` | Lily 品牌风格证据、全量标签直投、Prompt 选搭质量、穿法质量信号和受控配饰 | 设计搭配库选搭、品牌风格证据、穿法/配饰边界时 |
| `02_brand_and_assets/CLAUDE.md` | 搭配服装、标签、媒体资产、数字模特与品类图位规则 | 处理素材、标签、媒体、模特和图位规则时 |
| `02_brand_and_assets/lily_tagging_system.md` | Lily 服装视觉生产标签体系：AI 标签、人工标签、官方标题标签、选搭压缩格式 | 实现或评审 Lily 标签体系时 |
| `02_brand_and_assets/lily_gemini_tagging_flow.md` | Lily Gemini 打标流程：输入、输出、校验、effective_tags 合成和选搭压缩 | 实现或排查 Gemini 打标时 |
| `03_ux_guide/CLAUDE.md` | 普通用户视角 UI/UX、页面信息层级、交互边界 | 设计或实现前端页面时 |
| `03_ux_guide/ui_ux_unification_plan.md` | UI/UX 统一优化方案：简约克制科技感、视觉系统、控件层级、PC 桌面页面改造和验收清单 | 做全局 UI/UX 统一、视觉系统或生产级体验优化时 |
| `04_implementation_guide/CLAUDE.md` | 技术路线、生产实操、模型分工、任务包落地方式 | 工程实现、技术拆分、生产流程设计时 |
| `04_implementation_guide/archive_purge_policy.md` | 归档、彻底删除、媒体引用计数、影响预检和 OSS 清理策略 | 设计或修改删除、归档、批量清理、媒体替换时 |

## 权威边界 (Authority)

- **领域模型源头**：`domain.md`
- **产品范围源头**：`00_product_overview/product_overview.md`、`00_product_overview/product_requirements.md`
- **生成主流程源头**：`01_generation_flow/generation_flow.md`
- **Image-2 输入策略源头**：`01_generation_flow/image2_input_strategy.md`
- **生成职责拆分源头**：`01_generation_flow/generation_responsibility_matrix.md`
- **历史分镜业务意图**：`01_generation_flow/storyboard_preview_business_intent.md`（不再描述当前运行链路）
- **成片商品保真与组图验收参考**：`01_generation_flow/final_image_business_intent.md`；镜头 5 当前安全区以 `02_brand_and_assets/category_shot_rules.md` 为准
- **素材/标签/媒体源头**：`02_brand_and_assets/assets_and_parameters.md`
- **品类图位规则**：`02_brand_and_assets/category_shot_rules.md`
- **品牌调性参考**：`02_brand_and_assets/brand_style_templates.md`
- **前台 UI/UX 源头**：`03_ux_guide/user_uiux.md`
- **技术生产路线源头**：`04_implementation_guide/technical_route_and_operations.md`
- **归档/删除策略源头**：`04_implementation_guide/archive_purge_policy.md`

## 本地规则 (Local Rules)

1. **任何设计前先读领域、产品范围和职责拆分**：至少阅读 `domain.md`、`00_product_overview/product_overview.md` 与 `01_generation_flow/generation_responsibility_matrix.md`。
2. **先判断职责，再写 prompt**：任何生成问题先判断属于主推款事实、候选召回、AI Stylist、StylingPlan、搭配服装事实、数字模特、三类固定分镜 Prompt、生成配饰、媒体输入预算还是导出规格。
3. **Image-2 只做视觉生成**：智能选搭、拍摄图位族路由、五镜头预设、标签体系、历史快照和状态机由产品服务负责。
4. **前台 UI 不展示系统复杂性**：普通用户默认只看到主推款、搭配服装、最终结果和生成记录；prompt、provider 请求和快照 JSON 只在管理或技术视图出现。
5. **实验结论要升格后才能成为规范**：外部文档、临时验证和历史讨论的发现，只有同步进对应权威文档后，才可作为正式实现依据。
6. **新增/移动/删除 md 文件时必须更新本文件及对应子目录 `CLAUDE.md` 索引**。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
- 后端实现：`../backend/CLAUDE.md`
