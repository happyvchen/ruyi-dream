# Backend Database Schema

## 当前原则

数据库以当前四步生成链路为准：

```text
GenerationTask
-> GenerationStyling / StylingPlan
-> GenerationPoseSelection（姿势库 + 共享背景 + 冻结的五镜头要求）
-> GenerationResultSetShotPreset
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
| `generation_previews` | 只读历史兼容表；当前链路不再新增记录 |
| `shot_preset_slots` | 15 个历史品类/镜头槽位，保留旧链路兼容 |
| `generation_result_set_shot_presets` | 结果集复制的五组共享背景/独立姿势关系；旧记录仍为母版 |
| `shot_preset_versions` / `shot_presets` / `shot_preset_family_publications` | 旧维护模型，只用于迁移已有配置与读取历史结果集 |
| `generation_result_sets` | 一组成片结果；新结果 `preview_id`、`shot_preset_version_id` 为空，预设由关系快照冻结 |
| `generation_result_images_new` | 单张最终成片，固定 5 条槽位 |
| `generation_attempts` | provider 调用快照、进度、结果和错误分类 |
| `styling_garments` | 搭配服装及其当前 AI 打标状态、业务标签和本轮安全错误码 |
| `upload_sessions` | 浏览器直传到对象存储的轻量导入任务，用于刷新 / 切页后恢复导入状态 |
| `upload_session_items` | 导入任务内的图片条目，记录对象 key、文件信息、导入确认和业务媒体引用 |
| `upload_object_cleanup_queue` | 导入记录彻底删除后的持久对象清理队列；保存精确 key、来源会话和最近错误，供 RustFS/S3 失败后重试 |
| `prompt_configs` | Prompt 维护配置槽 |
| `retired_prompt_configs` | 退出运行链路的 Prompt 正文备份；不在维护页展示，仅供回滚审计 |

## GenerationTask 关键字段

- `batch_id`
- `ai_styling_user_instruction_text`
- `storyboard_preview_user_instruction_text`（历史兼容）
- `final_image_user_instruction_text`
- `current_input_version_id`
- `active_styling_id`
- `active_preview_id` / `active_storyboard_preview_id`（历史兼容）
- `active_result_set_id`
- `auto_run_target`
- `auto_run_requested_at`
- `auto_run_retry_at`：跨阶段自动恢复的下一次执行时间；后台 poller 到期继续。
- `auto_run_retry_count`：当前一键成片连续自动恢复轮次，成功或人工取消后清零。

不存在 `shoot_script_user_instruction_text` 或 `active_shoot_script_id`。

## Prompt 配置

`prompt_configs` 只保留当前七个槽位：品牌信息、AI 打标、主推款图片挑选、AI 选搭和三类成片。迁移 `066_direct_final_edits_workflow` 先把三个分镜 Prompt 正文备份到 `retired_prompt_configs`，再从运行目录移除。迁移 `072_live_shot_preset_slots` 把旧草稿或已发布配置复制为当前槽位，并建立结果集级冻结关系；旧版本表保留只读兼容。

## UploadSession 关键字段

- `scope`：`generation-task` 或 `styling-garment`。
- `status`：`PENDING`、`UPLOADING`、`UPLOADED`、`FINALIZING`、`FINALIZED`、`FAILED`、`ABANDONED`。
- `business_code` / `source_title`：导入记录恢复用的业务货号和来源 / 批次名；用于前端显示、搜索和跳转定位。
- `batch_id`：一次批量导入任务的稳定 ID；同批货号按该字段聚合，不再依赖标题和时间窗口猜测。
- `lease_owner` / `lease_expires_at`：短期批次执行租约，防止两个标签页或两台电脑同时续传同一任务。
- `total_count` / `uploaded_count`：用户可见导入进度。浏览器每张 PUT 成功后通过 API 回写条目状态，后端据此计算已导入数量。
- `target_type` / `target_id`：导入完成并保存为业务对象后的引用。
- `archived_at` / `archive_reason`：导入记录自身的归档时间和原因；仅控制记录默认可见性，不改变 `status`，也不级联归档或删除 `target_type` / `target_id` 指向的业务对象。
- `error_message`：业务可读失败摘要。保存为业务对象前的失败（例如货号重复、对象缺失、保存异常）必须写回这里，刷新后仍能在导入记录中提示用户处理。

导入记录和搭配服装库的 `导入批次` 筛选统一由 `upload_sessions` 聚合：有 `batch_id` 时使用 `batch:{batch_id}`，历史空 `batch_id` 会话使用 `session:{id}`。默认聚合排除 `archived_at IS NOT NULL` 和 `archive_reason = deletion_pending`，不排除失败、暂停、未入库或 `ABANDONED` 等其它业务状态；批次时间取成员最早 `created_at`，货号数取当前可见会话数。`idx_upload_sessions_batch_visibility` 服务可见批次聚合；`idx_upload_sessions_styling_target(scope, target_type, target_id, archived_at, created_at)` 服务搭配服装列表中的导入批次关联和可见性查询。

导入记录的用户状态不新增持久字段：查询时由 `upload_sessions` 的导入事实、`styling_garments` 的当前状态和 `generation_attempts` 的识别历史派生 `importOutcome`、`targetSummary`、`historySummary`。这样重新识别成功后会立即得到 `已恢复 + 当前可选搭`，同时保留失败审计；不会出现 session 已完成、服装已可用、前端却仍显示失败的第三套状态。

`idx_generation_attempts_owner_status(owner_type, owner_id, status)` 服务导入记录按关联服装批量汇总识别失败与成功历史，避免导入记录分页读取随 attempts 增长退化为全表扫描。

`upload_session_items.object_key` 是稳定的对象存储直传 key；`relative_path`、`size_bytes`、`last_modified` 和 `checksum_sha256` 用于恢复时拒绝错误目录或已变化的文件。对象存储中的 `sha256` 用户元数据与长度必须同时匹配，条目才会被确认导入。`status` 表示该条目是否已确认导入或已保存为 `MediaAsset`。搭配服装 AI 识别失败不回写为 upload session 失败，因为图片入库和 AI 识别是两个阶段。任意状态的 session 都可单独归档、恢复或彻底删除记录；彻底删除只清理未登记为 `MediaAsset` 的直传对象，已关联业务对象和正式媒体保留。未 `FINALIZED` 的 session 也可以由用户放弃或后台 TTL 清理。

运行中会话收到彻底删除请求后使用内部墓碑 `archive_reason = deletion_pending` 阻断 lease、resume、complete 和 finalize；该状态不进入默认或已归档的用户列表。待直传签名失效后，事务内把安全临时 key 写入 `upload_object_cleanup_queue` 并删除会话；数据库提交后才删除对象，失败时队列保留以便重试。

## StylingGarment 打标错误字段

- `tag_error_code`：只记录当前一轮打标的稳定错误分类；包含 `UPLOAD_REQUIREMENT_NOT_MET` 等可直接驱动用户操作的安全业务码，开始重试和成功完成时清空，避免历史 attempt 的错误码串到新任务。
- `tag_error`：保留当前一轮原始诊断文本，仅供 admin 技术详情使用；普通列表与详情响应不返回该文本。
- `tag_retry_at` / `tag_retry_state_json`：打标传输、契约纠错或进程中断的下一次恢复时间、分类与累计次数；由现有 generation poller 到期续跑。终态清空 `tag_retry_at`，但保留安全的累计摘要用于解释已执行的自动恢复。
- `tag_active_attempt_id`：当前逻辑打标 attempt；进程重启恢复时复用，避免把一次自动恢复拆成多条相互覆盖的 attempt。
- `tag_retry_lease_until`：poller 恢复 claim 的短租约；避免多 worker 重复恢复，进程中断后租约过期可再次领取。

## 数据清理

彻底删除任务时，后端删除该任务的主推款图引用、输入版本、选搭、分镜、结果集、结果图、attempt 和对应媒体资产。归档只隐藏任务，不删除媒体。

彻底删除搭配服装时，只删除 `StylingGarment` 聚合、图片关系、标签和 `styling_garment_tagging` attempts。`generation_stylings.selected_material_ids_json` 与历史快照是跨聚合弱引用，不反向级联删除 `GenerationTask`；快照引用的媒体元数据和对象文件继续保留。读取历史选搭时，如果来源服装已不存在，API 返回 `sourceAvailable = false` 的删除占位并脱敏原货号，主推款、分镜和成片保持可读。

## 074：详情修订号

向前兼容新增 `generation_tasks.detail_revision INTEGER NOT NULL DEFAULT 0`。回滚应用保留字段与触发器。

`install_detail_revision_triggers` 集中声明同事务内容失效规则：主任务内容/状态、图片关系、输入版本、选搭、历史分镜、成片结果、冻结预设、媒体元数据及引用的服装变化递增。attempt 的插入、终态/错误/输出变化也递增；终态 response snapshot 变化单独覆盖用户可见错误变化。触发器拦截直接 SQL 写路径，事务回滚时修订号也回滚。

心跳 `updated_at`、锁/租约、运行次数、自动恢复时间/计数及 attempt 的阶段进度不递增，运行进度由 status 直接提供；运行中的 provider 原始请求/响应快照不触发详情刷新，技术详情按需读取。同值 UPDATE 不递增。`detailRevision` 不等于事务数，一笔事务改动多个子对象时可以增加多次，只保证单调变化。

新增详情写表或字段时需在后续迁移中重建对应 revision 触发器并添加契约测试，不得只更新 `updated_at`。`idx_task_cover`、`idx_result_progress` 支持封面和进度查询。

## Migration 075：成片提示词优化

- `prompt_configs.enabled INTEGER NOT NULL DEFAULT 0`：仅 `assembly.final_prompt_optimizer` 使用；新槽位正文仍为空，不注入或回退默认规则。
- `generation_result_sets.prompt_preparation_json TEXT`：冻结模式、configRows、准备状态、attemptId、错误和最终 packages；旧行 NULL 沿用直出兼容路径。
- `generation_attempts.owner_type = generation_result_set`：复用原表保存整组/单镜头优化与测试，删除和取消纳入原生命周期。
- 重建相关 detail_revision 触发器，准备状态变化可被轻量轮询发现。

## 076 姿势库与姿势选搭

- `poses`：唯一 code、name、status、ai_tags_json、tag_error_code、tag_attempt_id、archived_at/archive_reason 和时间戳。
- `pose_images`：pose_id、media_asset_id、sort_order；pose_id 唯一（每个姿势至多一条图片关联），创建及识别接口要求恰好一张；删除姿势级联删除图片关联。迁移 077 无损拆分旧多图姿势，保留历史快照和原图，拆分后需重新识别。
- `generation_pose_selections`：task_id、input_version_id、styling_id、category_family、plan_json、attempt_id、created_at；属于任务历史。
- `generation_pose_selection_shots`：selection_id、shot_index（1..5）、pose_id、pose_snapshot_json、background_asset_id、pose_asset_id；每选择镜头唯一，来源姿势删除只将 pose_id 置空。
- `generation_tasks` 增加 pose_workflow_enabled（旧行默认 0，新建 API 明确写 1）、active_pose_selection_id、pose_selection_user_instruction。
- `generation_result_sets` 增加 pose_selection_id；重新使用既有 generation_result_set_shot_presets 保存成片图片引用。
- detail_revision 覆盖姿势选择、镜头快照、相关媒体及 generation_pose_selection attempt；所有删除按引用计数保留共享媒体。

### 统一背景流程的冻结数据

不增加表：`shoot_environment_references` 保存当前背景，`generation_pose_selection_shots` 的五条记录引用同一个 background_asset_id 和五个不同的 pose_asset_id。`generation_pose_selections.plan_json` 增加 `workflow=shared_background_v1`、`shotRequirements` 与 `promptConfigSnapshot`（本品类成片模板及分段）。结果集复用选择的 input_version_id，不重新拍取模特快照；优化规则与开关仍在结果集创建时冻结。历史记录不回写、不删除旧母版。

## 已发布图片版本迁移（083 / 084）

`generation_image_versions` 保存 image_id、attempt_id、media_asset_id、request_snapshot_json、evidence_source、created_at，以 `(image_id, media_asset_id)` 唯一。关联 attempt 删除时置空、镜头删除时级联，媒体引用保持约束。`generation_result_images_new.execution_snapshot_json` 保存继续执行时的逐镜头事实。版本独立于当前执行状态，失败或取消不移除之前的成功图片。

083/084 将成功 attempt 与当前已发布图片幂等投影到版本表；084 兼容曾运行未完整实现 083 的本地库。只有取消输出的图片不补为已发布版本。补齐已有、可证明的冻结预设，不根据后来改写的组 input_version_id 推断其余镜头的模特或图片。迁移不伪造缺失的背景、姿势或 AI 返回，不删除旧任务、结果组、服装或对象。新增按 image_id、media_asset_id、task_id 的索引以及版本变动的 detail_revision 触发器。


## 页面集合版本（迁移 085）

`collection_revisions(scope TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)` 保存 `poses / garments / records / models` 四个集合版本。相关业务表的 INSERT/UPDATE/DELETE 触发器在原事务内递增计数；UPDATE 排除仅 `updated_at`、lease 心跳及无变化写入。媒体缩略图、预览、状态或元信息改变时使集合失效。只读版本检查为主键单行查询，不扫描素材或生成任务列表；版本不是业务数据历史或事件日志。

## 画面布局规则升级（迁移 086）

无表结构变化。按旧正文 SHA-256 精确匹配升级姿势识别、姿势选搭、三类成片和提示词整理配置，自定义与空正文保留。画面布局、视图组合、参考景别和视图说明仍存于 poses.ai_tags_json 并随选择冻结；不回填无法从旧数据确认的视觉事实，不调用 provider，不更改历史选择或成片。

## 客观姿势标签（迁移087）

无表结构变化。精确匹配086阶段姿势识别和选姿参考正文摘要后，升级为简洁客观标签规则；空配置和自定义正文不变。标签仍完整保存在ai_tags_json，不批量删除旧评价字段、不改历史快照、不触发识别。

## 姿态文字还原要求（迁移088）

无表结构变化。按087阶段识别与选姿参考正文的摘要精确升级，强调可见部位与空间关系的完整覆盖及动作传递保真。自定义配置和历史数据不改写，不触发provider。

## 089：Prompt 实际请求与历史记录瘦身

不新增业务表，不改任务、图片、版本和素材主键。新增六个 Prompt 槽：`assembly.tagging_request`、`assembly.styling_request`、`assembly.final_prompt_optimizer_input`、`assembly.contract_correction`、`assembly.image_retry`、`assembly.reference_transport`。升级时从对应文本文件迁入原先由代码持有的请求结构；仅填充空槽，不替换已保存正文。

- `generation_attempts.request_snapshot_json`：保留实际 system、每批 `batchRequests[].prompt`、批次镜头和图片附件。删除与第一批相同的顶层 prompt；sourcePackages 中与批次 sourcePrompt 相等的正文可省略，由批次事实还原。可执行图片包不保存 `promptConfigSnapshot` / `promptConfigContent`，保留正文、来源图片、runtime 和版本哈希。
- `generation_attempts.response_snapshot_json`：保留 `batchResponses[].raw_text`，删除与末批完全相等的顶层 raw_text，以及与合法批次原始 JSON 完全一致的 shots 解析副本。无法证明相等时保留。姿势响应 plan 中的配置副本移除，姿势业务表中的冻结配置保留。
- `generation_image_versions.request_snapshot_json`、`generation_result_images_new.execution_snapshot_json`：移除可执行包中的重复模板配置，保留重生正文、所有参考图、用户调整基准及恢复信息。
- `generation_result_sets.prompt_preparation_json`：保留共享 configRows、准备状态、已完成镜头的正文及来源；packages 内不再复制共享配置。升级为旧结果集补入原代码协议对应的优化用户消息模板，不重写其摄影规则或优化结果。
- 迁移分批读取，每批最多 100 条；在迁移事务中更新，与 schema_migrations 089 一同提交。可重复运行；非法 JSON 保留并计数。event_logs 的 `call_facts_compacted` 仅记录行数、前后字节数和跳过数量。

历史瘦身不删除原始媒体或生成结果，不改 provider ID、重试预算、任务状态或已发送文本。升级脚本先备份，再迁移和 VACUUM；回滚需恢复升级前数据库及匹配代码。

## 090：姿势选搭仅引用机位与景别

不变更表结构，不改历史 attempts、姿势选择、结果集或执行快照。只调整当前可维护配置：下装旧独立镜头 2 的「当前镜头：腰臀至大腿中段近景」标题改为「机位&景别」；下装已有明确章节但完全缺少镜头 2 条件时，从参考模板迁入该镜头正文，依据现有品类图位规则补齐。已有镜头 2、自定义其他正文、空配置和无法明确定位的章节均不覆盖；后两者在使用时明确报配置错误。姿势选搭说明同步为仅引用该章节。迁移幂等，无 AI 或媒体 I/O，现有离线升级脚本会自动执行。

## 091：优化阶段姿势宫格

不改表结构。新结果集 `prompt_preparation_json.optimizerReferenceLayout` 为 `pose_grid_v1`，已有记录缺省保持旧协议。新调用记录同一版本字段，`batchRequests` 保留单次实际输入和含宫格来源、裁切、格号的附件事实；局部图号仍指向各镜头最终原始图片。迁移仅向非空的当前 `assembly.final_prompt_optimizer.content` 追加明确的参考板职责与图号转换章节，保留原正文和 enabled，幂等且不改历史请求、成片、准备记录或旧快照。
