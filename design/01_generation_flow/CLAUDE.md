# design/01_generation_flow/ — Agent Guide

## 职责 (Scope)

生成链路、任务包、Image-2 输入职责、生成要素责任边界。这里回答“系统如何从主推款、AI Stylist 造型方案和搭配服饰走到拍摄剧本、分镜预览与 5 张最终成片”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `generation_flow.md` | 主推款输入版本 -> 全量标签事实 -> AI Stylist / StylingPlan -> ShootScript / ShotPlan -> 分镜预览 -> 前 5 张最终图 | 实现或调整生成主流程时 |
| `shoot_script_workflow.md` | 拍摄剧本工作流最终设计：状态机、ShootScript schema、重生/失效规则、分镜预览和成片一致性策略 | 设计或实现“生成拍摄剧本 / 分镜预览 / 成片重生”时 |
| `brand_style_retrieval_workflow.md` | Lily 品牌样本库、候选漏斗、搭配库充足性诊断、受控配饰和阶段性实施计划 | 设计或实现大规模搭配库选搭、品牌风格检索、配饰边界时 |
| `styling_quality_signals.md` | AI Stylist 质量信号与客户审核闭环：避免业务质量问题变成用户可见报错 | 设计或排查智能选搭质量问题时 |
| `image2_input_strategy.md` | 10 图输入限制、图片角色、预览/最终 prompt 策略 | 设计任务包、prompt、图片输入优先级时 |
| `generation_responsibility_matrix.md` | 商品、模特、ShootScript、ShotPlan、生成配饰、导出等要素由谁负责 | 分析生成问题归因和修复方案时 |

## 本地规则 (Local Rules)

- AI Stylist 负责主推款理解、造型方向探索、多套 outfit 候选、Aesthetic Critic 质量判断和 `StylingPlan` 输出，不是一次性 ID 分类器。
- MVP 阶段不做后端候选漏斗/排序/评分/质量判断；Prompt + 大模型直接读取未归档 READY 搭配服饰库全量标签事实并全权选搭。
- 选搭质量必须同时满足审美成立、Lily 品牌成立、主推款商品表达成立；只露出主推款但造型难看仍视为失败。
- ShootScript 负责把 `StylingPlan` 落到全局导演设定、搭配风险、配饰调整和 6 个 ShotPlan；ShotPlan 负责单镜头图位、穿着关系、姿态、景别、构图、光影和生成配饰边界。
- Image-2 负责生成分镜预览和最终单图，不负责选择商品、导演决策或维护状态。
- 最终图固定生成分镜预览前 5 格对应的 5 个镜头，第 6 格直接忽略。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 搭配服饰/媒体规则：`../02_brand_and_assets/CLAUDE.md`
