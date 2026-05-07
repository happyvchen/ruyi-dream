# design/01_generation_flow/ — Agent Guide

## 职责 (Scope)

生成链路、任务包、Image 2 输入职责、生成要素责任边界。这里回答“系统如何从素材走到一组可用图”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `generation_flow.md` | 预处理层 -> 策划层 -> Image 2 逐张生成 -> 人工确认/导出 | 实现或调整生成主流程时 |
| `image2_input_strategy.md` | 图片/文本职责拆分、短提示词策略、输入角色 | 设计任务包、prompt、图片输入优先级时 |
| `generation_responsibility_matrix.md` | 商品、模特、Pose、品牌、尺寸、导出等要素由谁负责 | 分析生成问题归因和修复方案时 |

## 本地规则 (Local Rules)

- 不让 Image 2 每次重新理解 Lily 调性。
- 不用长 prompt 承担商品事实、品牌策略、Pose 决策和镜头规划。
- 生成任务包应结构化，prompt 只是任务文本的一部分。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 品牌/素材规则：`../02_brand_and_assets/CLAUDE.md`

