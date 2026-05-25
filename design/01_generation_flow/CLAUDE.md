# design/01_generation_flow/ — Agent Guide

## 职责 (Scope)

生成链路、任务包、Image-2 输入职责、生成要素责任边界。这里回答“系统如何从主推款、AI Stylist 造型方案和搭配服饰走到六宫格预览与 5 张最终成片”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `generation_flow.md` | 主推款输入版本 -> 候选召回 -> AI Stylist / StylingPlan -> ShotPlan -> 六宫格预览 -> 前 5 张最终图 | 实现或调整生成主流程时 |
| `image2_input_strategy.md` | 10 图输入限制、图片角色、预览/最终 prompt 策略 | 设计任务包、prompt、图片输入优先级时 |
| `generation_responsibility_matrix.md` | 商品、模特、ShotPlan、生成配饰、导出等要素由谁负责 | 分析生成问题归因和修复方案时 |

## 本地规则 (Local Rules)

- AI Stylist 负责主推款理解、造型方向探索、多套 outfit 候选、自评和 `StylingPlan` 输出，不是一次性 ID 分类器。
- ShotPlan 负责把 `StylingPlan` 落到图位、穿着关系、姿态、景别、构图、光影和生成配饰边界。
- Image-2 负责生成六宫格预览和最终单图，不负责选择商品或维护状态。
- 最终图固定生成六宫格前 5 格，第 6 格直接忽略。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 搭配服饰/媒体规则：`../02_brand_and_assets/CLAUDE.md`
