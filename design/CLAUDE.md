# design/ — Agent Guide

## 职责 (Scope)

本目录存放 ruyi-dream 0.1.0 的权威设计文档：产品范围、生成流程、搭配服装与媒体规则、UX、技术生产路线。

0.1.0 形态是 B2B AI 数字化摄影棚：用户上传主推款，系统通过 AI Stylist 工作流理解主推款、探索多套造型方案、用 Aesthetic Critic 判断搭配是否真正好看、符合品牌并服务主推款，再从搭配服装库选择真实可见的配角服饰或判断无需库内配角；随后按主推款品类选用 Prompt 维护中的三类固定 Prompt 直接生成 3:2 分镜预览，并按前 5 格生成电商棚拍成片。旧“单货号默认组图方案”和动态拍摄剧本不是 0.1.0 当前主流程。

## 文档索引 (Index)

| 路径 | 内容 | 何时需要读 |
|---|---|---|
| `domain.md` | 0.1.0 领域模型设计：模块边界、核心术语、对象定义、状态流转、职责约束和当前实现映射 | 设计领域对象、API、数据库、状态机、Prompt DTO 或跨模块流程前 |
| `00_product_overview/CLAUDE.md` | 产品定位、0.1.0 范围、产品需求 | **任何**产品/功能设计前 |
| `01_generation_flow/CLAUDE.md` | 生成主流程、Image-2 输入策略、生成要素职责拆分 | 设计生成链路、任务包、prompt 策略时 |
| `01_generation_flow/shoot_script_workflow.md` | 当前四步工作流：三类固定分镜 Prompt、成片槽位、状态机和失效规则 | 设计或实现分镜预览、成片重生和状态恢复时 |
| `01_generation_flow/storyboard_preview_business_intent.md` | 分镜预览原始业务意图：时尚高级姿态、按品类六镜头、镜头 5 中央 2:3 安全裁切和业务验收 | 设计或评审分镜 Prompt、姿态、图位和裁切策略时 |
| `01_generation_flow/final_image_business_intent.md` | 生成成片原始业务意图：前 5 格逐格兑现、事实源边界、组图一致性和业务验收 | 设计、研究或评审成片 Prompt、参考图策略和成片质量时 |
| `01_generation_flow/storyboard_prompt_research_task_1440.md` | 任务 1440 分镜 Prompt 调研：方法、逐轮结果、有效/无效写法和最终 Prompt | 复盘分镜 Prompt 实验或决定后续生成手段时 |
| `01_generation_flow/storyboard_workflow_simplification_proposal.md` | 删除拍摄剧本步骤、三类固定分镜 Prompt、代码注入动态事实、直接生图的简化方案 | 评审或实施分镜/成片工作流简化时 |
| `01_generation_flow/brand_style_retrieval_workflow.md` | Lily 品牌风格证据、全量标签直投、Prompt 选搭质量、穿法质量信号和受控配饰 | 设计搭配库选搭、品牌风格证据、穿法/配饰边界时 |
| `02_brand_and_assets/CLAUDE.md` | 搭配服装、标签、媒体资产、数字模特与品类图位规则 | 处理素材、标签、媒体、模特和图位规则时 |
| `02_brand_and_assets/lily_tagging_system.md` | Lily 服装视觉生产标签体系：AI 标签、人工标签、官方标题标签、选搭压缩格式 | 实现或评审 Lily 标签体系时 |
| `02_brand_and_assets/lily_gemini_tagging_flow.md` | Lily Gemini 打标流程：输入、输出、校验、effective_tags 合成和选搭压缩 | 实现或排查 Gemini 打标时 |
| `03_ux_guide/CLAUDE.md` | 普通用户视角 UI/UX、页面信息层级、交互边界 | 设计或实现前端页面时 |
| `04_implementation_guide/CLAUDE.md` | 技术路线、生产实操、模型分工、任务包落地方式 | 工程实现、技术拆分、生产流程设计时 |
| `04_implementation_guide/archive_purge_policy.md` | 归档、彻底删除、媒体引用计数、影响预检和 OSS 清理策略 | 设计或修改删除、归档、批量清理、媒体替换时 |

## 权威边界 (Authority)

- **领域模型源头**：`domain.md`
- **产品范围源头**：`00_product_overview/product_overview.md`、`00_product_overview/product_requirements.md`
- **生成主流程源头**：`01_generation_flow/generation_flow.md`
- **Image-2 输入策略源头**：`01_generation_flow/image2_input_strategy.md`
- **生成职责拆分源头**：`01_generation_flow/generation_responsibility_matrix.md`
- **分镜预览业务意图源头**：`01_generation_flow/storyboard_preview_business_intent.md`
- **生成成片业务意图源头**：`01_generation_flow/final_image_business_intent.md`
- **素材/标签/媒体源头**：`02_brand_and_assets/assets_and_parameters.md`
- **品类图位规则**：`02_brand_and_assets/category_shot_rules.md`
- **品牌调性参考**：`02_brand_and_assets/brand_style_templates.md`
- **前台 UI/UX 源头**：`03_ux_guide/user_uiux.md`
- **技术生产路线源头**：`04_implementation_guide/technical_route_and_operations.md`
- **归档/删除策略源头**：`04_implementation_guide/archive_purge_policy.md`

## 本地规则 (Local Rules)

1. **任何设计前先读领域、产品范围和职责拆分**：至少阅读 `domain.md`、`00_product_overview/product_overview.md` 与 `01_generation_flow/generation_responsibility_matrix.md`。
2. **先判断职责，再写 prompt**：任何生成问题先判断属于主推款事实、候选召回、AI Stylist、StylingPlan、搭配服装事实、数字模特、三类固定分镜 Prompt、生成配饰、媒体输入预算还是导出规格。
3. **Image-2 只做视觉生成**：智能选搭、品类路由、标签体系、历史快照和状态机由前置服务负责；六镜头稳定规则集中在 Prompt 维护。
4. **前台 UI 不展示系统复杂性**：普通用户默认只看到主推款、搭配服装、分镜预览、最终结果和生成记录；prompt、provider 请求、快照 JSON 只在技术详情出现。
5. **实验结论要升格后才能成为规范**：外部文档、临时验证和历史讨论的发现，只有同步进对应权威文档后，才可作为正式实现依据。
6. **新增/移动/删除 md 文件时必须更新本文件及对应子目录 `CLAUDE.md` 索引**。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
- 后端实现：`../backend/CLAUDE.md`
