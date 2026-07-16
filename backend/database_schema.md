# Backend Database Schema

## 当前原则

数据库以当前四步生成链路为准：

```text
GenerationTask
-> GenerationStyling / StylingPlan
-> GenerationPreview
-> GenerationResultSet / GenerationResultImage
```

当前 schema 不包含动态拍摄剧本表、镜头计划表或相关 active 指针。迁移 `050_finalize_direct_prompt_generation_workflow` 会清理旧本地库中的遗留表、遗留列、旧 Prompt 键和旧 attempt。

## 核心表

| 表 | 语义 |
|---|---|
| `generation_batches` | 批次元数据，保存批次 ID、用户可见批次名和更新时间；批次内任务仍通过 `generation_tasks.batch_id` 聚合 |
| `generation_tasks` | 拍摄任务根节点，保存任务名、阶段用户补充要求草稿、active 指针和自动推进意图 |
| `generation_task_images` | 当前任务主推款图片 |
| `generation_task_input_versions` | 进入 AI 阶段时冻结的输入版本：任务名、主推款图、数字模特快照 |
| `generation_stylings` | AI 智能选搭结果，保存 `StylingPlan`、候选、选中搭配和 attempt |
| `generation_previews` | 分镜预览记录，保存预览图、环境参考图、用户要求和 attempt |
| `generation_result_sets` | 一组成片结果，固定关联一个分镜预览 |
| `generation_result_images_new` | 单张最终成片，固定 5 条槽位 |
| `generation_attempts` | provider 调用快照、进度、结果和错误分类 |
| `prompt_configs` | Prompt 维护配置槽 |

## GenerationTask 关键字段

- `batch_id`
- `ai_styling_user_instruction_text`
- `storyboard_preview_user_instruction_text`
- `final_image_user_instruction_text`
- `current_input_version_id`
- `active_styling_id`
- `active_preview_id` / `active_storyboard_preview_id`
- `active_result_set_id`
- `auto_run_target`
- `auto_run_requested_at`

不存在 `shoot_script_user_instruction_text` 或 `active_shoot_script_id`。

## Prompt 配置

`prompt_configs` 只保留当前九个槽位：品牌信息、AI 打标、AI 选搭、三类分镜预览、三类成片。旧的动态剧本 Prompt 与旧通用分镜/成片 Prompt 会被迁移删除。

## 数据清理

彻底删除任务时，后端删除该任务的主推款图引用、输入版本、选搭、分镜、结果集、结果图、attempt 和对应媒体资产。归档只隐藏任务，不删除媒体。
