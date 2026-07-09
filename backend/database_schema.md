# Backend Database Schema

当前数据库为 SQLite，初始化入口是 `backend/app/db.py:init_db()`。0.1.0 只保留 AI 摄影棚主流程、搭配服装库、数字模特、Prompt 配置、媒体资产和审计 attempt。旧货号、planner、AI policy、独立品牌资料、独立拍摄配置和固定标签规则表均已删除；迁移 `027` 与 `034` 会在历史库上 drop 这些表，后续迁移继续为当前链路追加字段。

## 表总览

| 表名 | 业务对象 | 说明 |
|---|---|---|
| `model_profiles` | `ModelProfile` | 数字模特档案和头部参考图 |
| `media_assets` | `MediaAsset` | S3/RustFS 对象索引和衍生图 key |
| `styling_garments` | `StylingGarment` | 搭配服装库商品 |
| `styling_garment_images` | `StylingGarmentImage` | 搭配服装图片链接 |
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
| `provider_upload_cache` | - | 参考图 provider 上传缓存（objectKey → provider URL，TTL 复用） |
| `prompt_configs` | `PromptConfig` | 可维护 Prompt 模板 |
| `event_logs` | `EventLog` | 业务事件 |
| `schema_migrations` | SchemaVersion | 已执行迁移 |

## 生成主链路

`generation_tasks` 是用户任务根节点。上传主推款阶段可维护 `generation_task_images` 和当前编辑中的 `hero_product_info_text`；四个阶段用户输入草稿保存为 `*_user_instruction_text`。这些任务级用户输入只影响下一次执行，不改变任务状态、不隐藏当前 active artifact。进入 AI 后创建 `generation_task_input_versions`，冻结主推款图片、任务名、默认数字模特快照和本次实际使用的商品信息。任务进入 AI 阶段后仍允许用户返回上传页调整主推款图片或商品信息；修改图片或排序时，旧阶段 artifact 和 attempt 继续保留，但当前 active 指针清空，任务回到待选搭状态。只修改商品信息时不清空 active artifact；重新执行拍摄剧本、分镜预览、成片或单张重生都会创建新的输入版本，保留可复用的当前前序产物，并让最新商品信息进入接下来执行的阶段。

一键成片自动推进由 `generation_tasks.auto_run_target` 和 `auto_run_requested_at` 表达。当前唯一目标值为 `FINAL`。该字段只表示“继续自动推进到成片”的意图，不替代 `status`、阶段 artifact 状态或 active 指针；失败、完成、取消后清空。自动推进受并发闸门限制（`GENERATION_AUTO_RUN_MAX_CONCURRENCY`，默认 2）：同时推进的任务数达到上限时，其余任务按 `auto_run_requested_at` 排队，"排队中"只是展示态，不是新的任务状态。

批量成片批次由 `generation_tasks.batch_id` 表达：同一批次创建的任务共享一个客户端生成的不透明字符串，用于前端批次条按批次恢复和批量触发一键成片。批次不是独立业务对象，没有批次表和批次状态机。

AI Stylist 写入 `generation_stylings`：

- `candidate_retrieval_snapshot_json` 保存本次 READY 搭配库的全量标签事实卡和 code/id 映射。
- `user_instruction_json` 保存本次 AI Stylist 实际读取的用户介入要求和冲突处理策略。
- `user_requirement_json` 保存历史任务的合成用户要求快照；新执行链路以 `hero_product_info_json` 和各阶段 `user_instruction_json` 为准。
- `styling_plan_json` 保存完整 StylingPlan，包括候选方案、被拒方案、Aesthetic Critic 诊断、质量信号和选中 outfit。
- `selected_material_snapshot_json` 保存被选中搭配服装的商品快照和图片快照。

AI Director 写入 `generation_shoot_scripts`，并派生 6 条 `generation_shot_plans`。第 6 条只用于分镜预览参考，不进入最终成片。

`generation_shoot_scripts.user_instruction_json`、`generation_previews.user_instruction_json`、`generation_result_sets.user_instruction_json` 和 `generation_result_images_new.user_instruction_json` 分别保存对应阶段本次执行的用户介入要求。每次重生会新建或更新本次执行记录，并在 `generation_attempts.request_snapshot_json` 中保留同一份 runtime 上下文。

Image-2 阶段写入：

- `generation_previews`：一张 3x2 分镜预览总图。
- `generation_result_sets`：一次最终图生成批次。
- `generation_result_images_new`：前 5 张最终图状态和当前资产。
- `generation_attempts`：每次 AI/provider 调用的 request/response、provider id、错误和输出资产。`progress_json` 保存阶段进度（准备/上传/排队/生成/回传及分段起止时间），仅作展示元数据，不是业务状态；`error_code` 保存失败分类（`UPLOAD_FAILED`/`PROVIDER_FAILED`/`PROVIDER_TIMEOUT`/`DELIVERY_FAILED`）。`DELIVERY_FAILED` 表示 provider 已生成成功、仅结果回传失败，可通过 redeliver 零成本重新拉取。
- `provider_upload_cache`：同一 objectKey 的参考图在 TTL 内复用 provider 上传 URL，避免同组成片并发时重复上传。

## Prompt 配置

`prompt_configs` 保存可维护 Prompt 槽位和由 Prompt 维护写入的模板正文。后端会初始化空正文的固定槽位，保证 UI 可维护；但不会存储、seed 或 fallback 任何 Prompt 默认正文。缺配置或空正文在真实生成时都作为配置错误处理。运行时占位符由后端提供：

- `runtime.styling_garment`：AI 打标使用的当前搭配服装 code 和图片输入摘要。
- `runtime.candidate_retrieval`：AI Stylist 使用的未归档 READY 搭配库全量标签事实卡，只暴露业务 `code`、`ai_tags` 和必要复核/人工标记。
- `runtime.hero_product_info`：跟随当前执行实际使用的主推款输入版本冻结的商品信息和业务事实补充；重新执行拍摄剧本、分镜预览、成片或单张重生时会冻结最新任务商品信息。
- `runtime.ai_styling_user_instruction`：AI 智能选搭本次执行的用户介入要求。
- `runtime.shoot_script_user_instruction`：拍摄剧本本次执行的用户介入要求。
- `runtime.storyboard_preview_user_instruction`：分镜预览本次执行的用户介入要求。
- `runtime.final_image_user_instruction`：最终成片或单张重生本次执行的用户介入要求。
- `runtime.styling_result`：AI Director 使用的 StylingPlan 导演上下文。
- `runtime.shoot_script_result`：分镜预览使用的 ShootScript 上下文。
- `runtime.current_shot_context`：单张最终成片上下文。

历史 `ai_policy_configs`、`brand_profiles`、`shoot_configs` 和固定标签规则表已删除。AI 选搭规则、质量信号、配饰边界、品牌口径和拍摄导演规则直接维护在对应 Prompt 正文中。

## 迁移

当前迁移链只保留 0.1.0 schema 演进：

| 版本 | 名称 | 动作 |
|---|---|---|
| `027` | `drop_legacy_generation_tables` | 删除 0.1.0 之前的货号、planner、generation run/image、品牌模板和 AI policy 表 |
| `028` | `add_generation_task_auto_run` | 为 `generation_tasks` 增加一键成片自动推进字段 |
| `029` | `add_generation_user_requirements` | 为 `generation_tasks`、`generation_task_input_versions` 和 `generation_stylings` 增加用户要求字段 |
| `030` | `split_generation_user_inputs` | 增加商品信息和各阶段用户介入要求字段，并从旧用户要求字段做一次性回填 |
| `031` | `add_generation_pending_stage_inputs` | 为 `generation_tasks` 增加各阶段待执行用户输入草稿字段 |
| `032` | `add_shoot_script_runtime_columns` | 为旧库补齐拍摄剧本、分镜预览和成片复盘所需的运行时列，并将 `generation_shot_plans` 唯一约束迁到 `(shoot_script_id, sort_order)` |
| `033` | `cleanup_legacy_tables_and_attempt_provider_request_ids` | 确保 attempt provider 请求 ID 列存在，并再次清理 0.1.0 之前的空表残留 |
| `034` | `drop_prompt_replaced_config_tables` | 删除已由 Prompt 维护替代的品牌资料、拍摄配置和固定标签规则表 |
| `035` | `recover_legacy_shoot_scripts` | 从已有成功分镜预览/旧版 preview 恢复历史任务缺失的 ShootScript、ShotPlan 关联和 active 指针 |
| `036` | `add_attempt_progress_and_upload_cache` | 为 `generation_attempts` 增加阶段进度列，并创建 `provider_upload_cache` 表 |
| `037` | `add_generation_task_batch_id` | 为 `generation_tasks` 增加批量成片批次字段 `batch_id` 及索引 |

新库直接创建当前 schema；已有库执行迁移链后只保留 0.1.0 表并追加当前字段。0.1.0 之前的业务表不作为运行时读取路径。
