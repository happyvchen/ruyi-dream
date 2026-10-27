# design/01_generation_flow/ — Agent Guide

## 职责 (Scope)

生成链路、任务包、GPT Image-2.5 Sunburst 输入职责、拍摄预设和生成要素责任边界。这里回答“系统如何从主推款、AI Stylist 造型方案和搭配服装直接生成 5 张最终成片”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `generation_flow.md` | 主推款输入版本 -> AI Stylist / StylingPlan -> 独立姿势与共享背景 -> 五张最终图 | 实现或调整生成主流程时 |
| `current_generation_workflow.md` | 当前四步工作流、三类 Prompt 路由、可选多模态提示词优化、统一背景和恢复 | 设计或实现成片时 |
| `storyboard_preview_business_intent.md` | 历史分镜业务意图；五镜头和商品表达要求可供预设设计参考 | 复盘旧流程时 |
| `final_image_business_intent.md` | 生成成片原始业务意图：逐格兑现分镜导演意图、人物/商品事实源、五张组图与失败判定 | 设计、研究或评审成片 Prompt、参考图策略和成片质量时 |
| `final_image_prompt_research_task_1457.md` | 最终成片 Prompt 研究结论：失败归因、有效/无效规则、黄金链路和系统采纳建议 | 复盘或实现成片 Prompt、单格输入和三类固定成片模板时 |
| `storyboard_prompt_research_task_1440.md` | 分镜预览 Prompt 研究结论：有效/无效规则、镜头 5 修正口径和系统采纳建议 | 复盘或继续分镜 Prompt 实验时 |
| `storyboard_workflow_simplification_proposal.md` | 历史决策记录：删除动态剧本步骤、三类固定分镜 Prompt、代码注入动态事实、直接生图 | 复盘工作流简化决策时 |
| `brand_style_retrieval_workflow.md` | Lily 品牌样本库、全量标签直投、Prompt 选搭质量、受控配饰和0.1.0 技术边界 | 设计或实现搭配库选搭、品牌风格证据、配饰边界时 |
| `styling_quality_signals.md` | AI Stylist 质量信号与客户审核闭环：避免业务质量问题变成用户可见报错 | 设计或排查智能选搭质量问题时 |
| `image2_input_strategy.md` | 10 图输入限制、图片角色、预览/最终 prompt 策略 | 设计任务包、prompt、图片输入优先级时 |
| `prompt_context_strategy.md` | 当前 Prompt runtime 占位策略：AI 选搭和最终成片的动态事实边界 | 评估 prompt 过长、调整 runtime 占位符或生成请求快照时 |
| `prompt_rendering_refactor.md` | Prompt 渲染层重构设计：快照模型与 prompt 视图解耦、生图阶段去 JSON 治理元数据、参考图自然语言化、迁移阶段与本地渲染对比验收 | 修正 prompt 组装偏离 spec、去除 schema_version/priority_policy 噪音、实现 renderer 层时 |
| `generation_responsibility_matrix.md` | 商品、模特、StylingPlan、三类固定 Prompt、动态事实、生成配饰和导出等要素由谁负责 | 分析生成问题归因和修复方案时 |

- 姿势库与独立选搭：`pose_library_workflow.md`，涵盖初筛直接定选、四步状态机和历史兼容。

## 本地规则 (Local Rules)

- AI Stylist 依据主推款、品牌信息和库内标签输出最终服装、穿法、文字配饰、空搭配原因和简短理由；不强制输出多候选、审美诊断或评分。完整穿着无需额外搭配，无合适商品可空搭配。
- 0.1.0 阶段不做后端预筛选/排序/评分/质量判断；Prompt + 大模型读取未归档 READY 搭配服装库的全量 AI 标签事实卡并全权选搭，`ai_tags_json` 是 AI Stylist 的业务输入。
- 选搭质量必须同时满足审美成立、Lily 品牌成立、主推款商品表达成立；只露出主推款但造型难看仍视为失败。
- 背景与光线由同一张共享背景提供，姿势由独立姿势库选搭；一次初筛查看商品图、全库标签与镜头要求，直接定选，不再调用原图复核；冻结商品/模特版本、背景、五个不同姿势与成片规则。景别、构图和商品表达由三类固定成片 Prompt 负责。
- GPT Image-2.5 Sunburst edits 负责最终单图生成，不负责选择商品、维护预设、导演决策或业务状态。
- 最终图固定并发生成 5 个镜头；每张最多 6 个参考图 URL，共享背景排第一，不传遮罩；模特、主推款和搭配款多图按角色合成参考板。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 搭配服装/媒体规则：`../02_brand_and_assets/CLAUDE.md`
