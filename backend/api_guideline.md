# Backend API Guideline

## 当前生成 API

0.1.0 当前 AI 摄影棚链路固定为：

```text
上传主推款 -> AI 智能选搭 -> 生成分镜预览 -> 生成成片
```

后端不提供“生成拍摄剧本”API，不保存动态剧本或镜头计划。前端和测试不得调用或伪造旧的剧本步骤。

## 任务接口

- `POST /api/generation-tasks`：上传主推款并创建草稿任务。
- `POST /api/generation-tasks/from-uploads`：基于已上传对象创建任务。
- `PATCH /api/generation-tasks/{task_id}`：保存任务名、商品信息和各阶段用户补充要求草稿。
- `POST /api/generation-tasks/{task_id}/styling`：运行 AI 智能选搭，输出当前 `StylingPlan`。
- `POST /api/generation-tasks/{task_id}/preview`：按拍摄图位族选择三类固定分镜 Prompt，调用 Image-2 生成 3:2 六格分镜预览。
- `POST /api/generation-tasks/{task_id}/generate-final`：按分镜前 5 格并发生成最终成片。
- `POST /api/generation-tasks/{task_id}/auto-run-final`：从当前第一个未完成阶段自动推进到成片。
- `POST /api/generation-tasks/{task_id}/cancel`：取消当前运行中的选搭、分镜或成片。

`CANCELLED` 是可恢复的中断态，不是归档或终止态。再次调用单任务 `auto-run-final` 或批次 `auto-run-final` 时，应把任务恢复到最近被取消的阶段并继续；已成功的阶段和成片槽位保持不变。

## 状态

任务状态只包含当前四步链路：

```text
DRAFT
READY_FOR_STYLING
STYLING_RUNNING
STYLING_READY
PREVIEW_RUNNING
PREVIEW_READY
FINAL_RUNNING
FINAL_PARTIAL
COMPLETED
FAILED
CANCELLED
ABANDONED
```

不存在 `SCRIPT_RUNNING` / `SCRIPT_READY`。

## Prompt 维护槽位

当前后端只登记并读取这些 Prompt 配置槽：

- `segment.brand_info`
- `assembly.ai_tagging`
- `assembly.ai_styling`
- `assembly.storyboard_preview_top`
- `assembly.storyboard_preview_bottom`
- `assembly.storyboard_preview_one_piece`
- `assembly.final_image_top`
- `assembly.final_image_bottom`
- `assembly.final_image_one_piece`

运行时 Prompt 正文完全来自 `prompt_configs.content`。代码只注入动态事实，不写死黄金 Prompt 正文。

## Runtime 占位职责

分镜预览动态事实：

- `runtime.ref_model_identity`
- `runtime.ref_garment`
- `runtime.ref_shoot_environment`
- `runtime.hero_product_info`
- `runtime.storyboard_preview_user_instruction`
- `runtime.category_family`
- `runtime.styling_outfit`
- `runtime.resolved_accessories`

成片动态事实：

- `runtime.preview_grid_cell`
- `runtime.ref_model_identity`
- `runtime.ref_preview_grid`
- `runtime.ref_garment`
- `runtime.ref_shoot_environment`
- `runtime.hero_product_info`
- `runtime.final_image_user_instruction`
- `runtime.shot_styling`
- `runtime.shot_global_directing`
- `runtime.shot_current`
- `runtime.shot_accessories`
- `runtime.is_shot_1` 到 `runtime.is_shot_5`

## 导出

成片批量下载 zip 内结构为 `任务名/序号_图位.扩展名`。图位名称来自最终结果图的 `shot_role`，不是旧镜头计划。
