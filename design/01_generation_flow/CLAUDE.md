# design/01_generation_flow/ — Agent Guide

## 职责 (Scope)

生成链路、任务包、Image-2 输入职责、生成要素责任边界。这里回答“系统如何从主推款、AI Stylist 造型方案和搭配服装直接走到分镜预览与 5 张最终成片”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `generation_flow.md` | 主推款输入版本 -> AI Stylist / StylingPlan -> 三类固定 Prompt 分镜预览 -> 前 5 张最终图 | 实现或调整生成主流程时 |
| `shoot_script_workflow.md` | 当前四步工作流、三类 Prompt 路由、失效规则和成片槽位 | 设计或实现分镜预览 / 成片时 |
| `storyboard_preview_business_intent.md` | 分镜预览原始业务意图：时尚高级姿态、按品类六镜头、镜头 5 中央 2:3 安全裁切和业务验收 | 设计或评审分镜 Prompt、参考图和分镜质量时 |
| `final_image_business_intent.md` | 生成成片原始业务意图：逐格兑现分镜导演意图、人物/商品事实源、五张组图与失败判定 | 设计、研究或评审成片 Prompt、参考图策略和成片质量时 |
| `final_image_prompt_research_task_1457.md` | 任务 1457 成片 Prompt 调研：10 次 A/B 与稳定性验证、有效/无效写法和黄金链路 | 复盘或实现成片 Prompt、单格输入和三类固定成片模板时 |
| `storyboard_prompt_research_task_1440.md` | 任务 1440 分镜 Prompt 调研：验收表、A/B 变量、逐轮 ToApis 结果和有效/无效写法 | 复盘或继续分镜 Prompt 实验时 |
| `storyboard_workflow_simplification_proposal.md` | 删除拍摄剧本步骤、三类固定分镜 Prompt、代码注入动态事实、直接生图的简化方案 | 决定或实现分镜/成片新工作流时 |
| `brand_style_retrieval_workflow.md` | Lily 品牌样本库、全量标签直投、Prompt 选搭质量、受控配饰和0.1.0 技术边界 | 设计或实现搭配库选搭、品牌风格证据、配饰边界时 |
| `styling_quality_signals.md` | AI Stylist 质量信号与客户审核闭环：避免业务质量问题变成用户可见报错 | 设计或排查智能选搭质量问题时 |
| `image2_input_strategy.md` | 10 图输入限制、图片角色、预览/最终 prompt 策略 | 设计任务包、prompt、图片输入优先级时 |
| `prompt_context_strategy.md` | Prompt 上下文 DTO 策略：搭配库 AI 标签事实卡、跨阶段压缩、旧兼容注入删除 | 评估 prompt 过长、调整 runtime 占位符或生成请求快照时 |
| `prompt_rendering_refactor.md` | Prompt 渲染层重构设计：快照模型与 prompt 视图解耦、生图阶段去 JSON 治理元数据、参考图自然语言化、迁移阶段与本地渲染对比验收 | 修正 prompt 组装偏离 spec、去除 schema_version/priority_policy 噪音、实现 renderer 层时 |
| `generation_responsibility_matrix.md` | 商品、模特、StylingPlan、三类固定 Prompt、动态事实、生成配饰和导出等要素由谁负责 | 分析生成问题归因和修复方案时 |

## 本地规则 (Local Rules)

- AI Stylist 负责主推款理解、造型方向探索、多套 outfit 候选、Aesthetic Critic 质量判断和 `StylingPlan` 输出，不是一次性 ID 分类器。
- 0.1.0 阶段不做后端预筛选/排序/评分/质量判断；Prompt + 大模型读取未归档 READY 搭配服装库的全量 AI 标签事实卡并全权选搭，`ai_tags_json` 是 AI Stylist 的业务输入。
- 选搭质量必须同时满足审美成立、Lily 品牌成立、主推款商品表达成立；只露出主推款但造型难看仍视为失败。
- 分镜的六镜头、姿势、裁切与构图规则由三类固定 Prompt 负责；代码只注入商品、选搭、模特和环境等动态事实。
- Image-2 负责生成分镜预览和最终单图，不负责选择商品、导演决策或维护状态。
- 最终图固定生成分镜预览前 5 格对应的 5 个镜头，第 6 格直接忽略。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 搭配服装/媒体规则：`../02_brand_and_assets/CLAUDE.md`
