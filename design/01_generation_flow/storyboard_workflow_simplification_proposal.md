# 工作流简化历史决策记录

本文记录 2026-07-13 已采纳的决策：当前 AI 摄影棚不再使用动态剧本中间层，改为三类固定分镜 Prompt 和三类固定成片 Prompt。

## 已采纳结论

- 用户可见流程固定为：上传主推款 -> AI 智能选搭 -> 分镜预览 -> 成片。
- 分镜六镜头规则集中在 `assembly.storyboard_preview_top`、`assembly.storyboard_preview_bottom`、`assembly.storyboard_preview_one_piece`。
- 成片逐格兑现规则集中在 `assembly.final_image_top`、`assembly.final_image_bottom`、`assembly.final_image_one_piece`。
- 代码只注入动态事实：用户补充要求、参考图图号、品类族、选中造型摘要、当前格号和条件段。
- 系统不引入自动评分、自动验收、自动切格或因视觉质量判断触发的自动重生；瞬时技术失败由任务编排有限恢复。

## 当前权威实现

当前权威设计见 [current_generation_workflow.md](current_generation_workflow.md)，Prompt runtime 边界见 [prompt_context_strategy.md](prompt_context_strategy.md)，职责拆分见 [generation_responsibility_matrix.md](generation_responsibility_matrix.md)。
