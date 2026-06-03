# Backend Database Schema

当前数据库为 SQLite，初始化入口是 `backend/app/db.py:init_db()`。新版只保留 AI 摄影棚主流程、搭配服饰库、数字模特、品牌资料、Prompt 配置、拍摄配置、媒体资产和审计 attempt。旧版 `garment_skus`、`garment_images`、`shot_plans`、`shot_plan_shots`、`generation_runs`、`generation_images`、`brand_template_versions`、`ai_policy_configs` 已删除；迁移 `027` 会在旧库上 drop 这些表，后续迁移继续为当前新版链路追加字段。

## 表总览

| 表名 | 业务对象 | 说明 |
|---|---|---|
| `model_profiles` | `ModelProfile` | 数字模特档案和头部参考图 |
| `brand_profiles` | `BrandProfile` | 单一品牌资料 |
| `media_assets` | `MediaAsset` | S3/RustFS 对象索引和衍生图 key |
| `styling_garments` | `StylingGarment` | 搭配服饰库商品 |
| `styling_garment_images` | `StylingGarmentImage` | 搭配服饰图片链接 |
| `generation_tasks` | `GenerationTask` | AI 摄影棚主推款任务 |
| `generation_task_images` | `GenerationTaskImage` | 主推款图片链接 |
| `generation_task_input_versions` | `GenerationTaskInputVersion` | 冻结输入版本 |
| `generation_stylings` | `GenerationStyling` | AI Stylist 输出和选搭快照 |
| `generation_shoot_scripts` | `GenerationShootScript` | 拍摄剧本 |
| `generation_shot_plans` | `GenerationShotPlan` | ShootScript 派生 6 个镜头 |
| `generation_previews` | `GenerationPreview` | 3x2 分镜预览 |
| `generation_result_sets` | `GenerationResultSet` | 最终成片集合 |
| `generation_result_images_new` | `GenerationResultImage` | 前 5 张最终图 |
| `generation_attempts` | `GenerationAttempt` | AI/provider 请求响应快照 |
| `shoot_configs` | `ShootConfig` | AI Director 拍摄规则 |
| `prompt_configs` | `PromptConfig` | 可维护 Prompt 模板 |
| `tag_dimensions` / `tag_defs` / `tag_relations` / `tag_applicability_rules` / `garment_tag_assignments` | Tag Taxonomy | 标签体系 |
| `event_logs` | `EventLog` | 业务事件 |
| `schema_migrations` | SchemaVersion | 已执行迁移 |

## 生成主链路

`generation_tasks` 是用户任务根节点。草稿阶段可维护 `generation_task_images` 和 `user_requirement_text`；进入 AI 后创建 `generation_task_input_versions`，冻结主推款图片、任务名、默认数字模特快照和本次用户要求。

一键成片自动推进由 `generation_tasks.auto_run_target` 和 `auto_run_requested_at` 表达。当前唯一目标值为 `FINAL`。该字段只表示“继续自动推进到成片”的意图，不替代 `status`、阶段 artifact 状态或 active 指针；失败、完成、取消后清空。

AI Stylist 写入 `generation_stylings`：

- `candidate_retrieval_snapshot_json` 保存本次 READY 搭配库的全量标签事实卡和 code/id 映射。
- `user_requirement_json` 保存本次 AI Stylist 实际读取的上传补充要求、微调要求和冲突处理策略。
- `styling_plan_json` 保存完整 StylingPlan，包括候选方案、被拒方案、Aesthetic Critic 诊断、质量信号和选中 outfit。
- `selected_material_snapshot_json` 保存被选中搭配服饰的商品快照和图片快照。

AI Director 写入 `generation_shoot_scripts`，并派生 6 条 `generation_shot_plans`。第 6 条只用于分镜预览参考，不进入最终成片。

Image-2 阶段写入：

- `generation_previews`：一张 3x2 分镜预览总图。
- `generation_result_sets`：一次最终图生成批次。
- `generation_result_images_new`：前 5 张最终图状态和当前资产。
- `generation_attempts`：每次 AI/provider 调用的 request/response、provider id、错误和输出资产。

## Prompt 配置

`prompt_configs` 只保存由 Prompt 维护写入的模板正文和分段。后端只负责建表、读写、渲染和保存快照；不会在初始化、迁移或缺配置时存储、seed 或 fallback 任何 Prompt 默认值。运行时占位符由后端提供：

- `runtime.candidate_retrieval`：AI Stylist 使用的未归档 READY 搭配库全量标签事实卡，只暴露业务 `code`、`ai_tags` 和必要复核/人工标记。
- `runtime.user_requirement`：AI Stylist 使用的本次用户要求和冲突处理策略。
- `runtime.styling_result`：AI Director 使用的 StylingPlan 导演上下文。
- `runtime.shoot_script_result`：分镜预览使用的 ShootScript 上下文。
- `runtime.current_shot_context`：单张最终成片上下文。

旧 `ai_policy_configs` 表已删除。AI 选搭规则、质量信号、配饰边界和品牌口径直接维护在 `assembly.ai_styling` Prompt 正文中；拍摄剧本、分镜预览和最终成片文字也必须来自对应 Prompt 配置。

## 迁移

当前迁移链只保留新版 schema 演进：

| 版本 | 名称 | 动作 |
|---|---|---|
| `027` | `drop_legacy_generation_tables` | 删除旧货号、旧 planner、旧 generation run/image、旧品牌模板、旧 AI policy 表 |
| `028` | `add_generation_task_auto_run` | 为 `generation_tasks` 增加一键成片自动推进字段 |
| `029` | `add_generation_user_requirements` | 为 `generation_tasks`、`generation_task_input_versions` 和 `generation_stylings` 增加用户要求字段 |

新库直接创建当前 schema；旧库执行迁移链后只保留新版表并追加当前字段。旧业务数据不做兼容读取。
