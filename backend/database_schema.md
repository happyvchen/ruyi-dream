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
| `styling_garments` | 搭配服装及其当前 AI 打标状态、业务标签和本轮安全错误码 |
| `upload_sessions` | 浏览器直传到对象存储的轻量上传任务，用于刷新 / 切页后恢复上传状态 |
| `upload_session_items` | 上传任务内的图片条目，记录对象 key、文件信息、上传确认和业务媒体引用 |
| `upload_object_cleanup_queue` | 上传记录彻底删除后的持久对象清理队列；保存精确 key、来源会话和最近错误，供 RustFS/S3 失败后重试 |
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

## UploadSession 关键字段

- `scope`：`generation-task` 或 `styling-garment`。
- `status`：`PENDING`、`UPLOADING`、`UPLOADED`、`FINALIZING`、`FINALIZED`、`FAILED`、`ABANDONED`。
- `business_code` / `source_title`：上传记录恢复用的业务货号和来源 / 批次名；用于前端显示、搜索和跳转定位。
- `batch_id`：一次批量上传任务的稳定 ID；同批货号按该字段聚合，不再依赖标题和时间窗口猜测。
- `lease_owner` / `lease_expires_at`：短期批次执行租约，防止两个标签页或两台电脑同时续传同一任务。
- `total_count` / `uploaded_count`：用户可见上传进度。浏览器每张 PUT 成功后通过 API 回写条目状态，后端据此计算已上传数量。
- `target_type` / `target_id`：上传完成并保存为业务对象后的引用。
- `archived_at` / `archive_reason`：上传记录自身的归档时间和原因；仅控制记录默认可见性，不改变 `status`，也不级联归档或删除 `target_type` / `target_id` 指向的业务对象。
- `error_message`：业务可读失败摘要。保存为业务对象前的失败（例如货号重复、对象缺失、保存异常）必须写回这里，刷新后仍能在上传记录中提示用户处理。

`upload_session_items.object_key` 是稳定的对象存储直传 key；`relative_path`、`size_bytes`、`last_modified` 和 `checksum_sha256` 用于恢复时拒绝错误目录或已变化的文件。对象存储中的 `sha256` 用户元数据与长度必须同时匹配，条目才会被确认上传。`status` 表示该条目是否已确认上传或已保存为 `MediaAsset`。搭配服装 AI 识别失败不回写为 upload session 失败，因为图片入库和 AI 识别是两个阶段。任意状态的 session 都可单独归档、恢复或彻底删除记录；彻底删除只清理未登记为 `MediaAsset` 的直传对象，已关联业务对象和正式媒体保留。未 `FINALIZED` 的 session 也可以由用户放弃或后台 TTL 清理。

运行中会话收到彻底删除请求后使用内部墓碑 `archive_reason = deletion_pending` 阻断 lease、resume、complete 和 finalize；该状态不进入默认或已归档的用户列表。待直传签名失效后，事务内把安全临时 key 写入 `upload_object_cleanup_queue` 并删除会话；数据库提交后才删除对象，失败时队列保留以便重试。

## StylingGarment 打标错误字段

- `tag_error_code`：只记录当前一轮打标的稳定错误分类；包含 `UPLOAD_REQUIREMENT_NOT_MET` 等可直接驱动用户操作的安全业务码，开始重试和成功完成时清空，避免历史 attempt 的错误码串到新任务。
- `tag_error`：保留当前一轮原始诊断文本，仅供 admin 技术详情使用；普通列表与详情响应不返回该文本。

## 数据清理

彻底删除任务时，后端删除该任务的主推款图引用、输入版本、选搭、分镜、结果集、结果图、attempt 和对应媒体资产。归档只隐藏任务，不删除媒体。
