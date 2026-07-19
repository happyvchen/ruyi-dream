# Backend API Guideline

## 当前生成 API

0.1.0 当前 AI 摄影棚链路固定为：

```text
上传主推款 -> AI 智能选搭 -> 生成分镜预览 -> 生成成片
```

后端不提供“生成拍摄剧本”API，不保存动态剧本或镜头计划。前端和测试不得调用或伪造旧的剧本步骤。

## 任务接口

- `POST /api/generation-tasks`：兼容旧表单上传路径；新上传体验应优先使用 `upload_sessions -> finalize-generation-task`，以便刷新 / 切页后恢复状态。
- `POST /api/generation-tasks/from-uploads`：基于已上传对象创建任务。
- `PATCH /api/generation-tasks/{task_id}`：保存任务名和各阶段用户补充要求草稿。
- `POST /api/generation-tasks/{task_id}/styling`：运行 AI 智能选搭，输出当前 `StylingPlan`。
- `POST /api/generation-tasks/{task_id}/preview`：按拍摄图位族选择三类固定分镜 Prompt，调用 Image-2 生成 3:2 六格分镜预览。
- `POST /api/generation-tasks/{task_id}/generate-final`：按分镜前 5 格并发生成最终成片。
- `POST /api/generation-tasks/{task_id}/auto-run-final`：从当前第一个未完成阶段自动推进到成片。
- `POST /api/generation-tasks/{task_id}/cancel`：取消当前运行中的选搭、分镜或成片。
- `PATCH /api/generation-batches/{batch_id}`：保存批次名；批次任务归属仍由 `GenerationTask.batch_id` 决定。
- `POST /api/generation-batches/{batch_id}/auto-run-final`：批次开始/继续一键成片；整批完成后可传 `restart=true` 重新生成。
- `POST /api/generation-batches/{batch_id}/cancel`：取消批次内所有生成中或排队中的任务，保留已完成成果。

`CANCELLED` 是可恢复的中断态，不是归档或终止态。再次调用单任务 `auto-run-final` 或批次 `auto-run-final` 时，应把任务恢复到最近被取消的阶段并继续；已成功的阶段和成片槽位保持不变。

批次 `auto-run-final` 只负责把批次内任务标记为一键成片队列；实际并发由后端 ToAPIs 全局闸门控制。运行参数统一使用 `TOAPIS_MAX_CONCURRENCY`，默认 `5`，不再提供批次级独立并发参数。

## 上传任务接口

主推款上传和搭配服装上传复用 `upload_sessions` 轻量任务契约。后端职责是让前端在刷新或切换页面后恢复最近上传状态，而不是提供复杂断点续传队列。

- `POST /api/upload-sessions`：创建上传任务和对象存储直传地址。
- `GET /api/upload-sessions`：按 `scope`、`status`、`limit` 查询最近上传任务；默认返回未完成和最近完成 / 失败的任务，用于页面恢复。
- `GET /api/upload-sessions/{session_id}`：查询单个上传任务和条目。
- `POST /api/upload-sessions/{session_id}/complete-items`：浏览器 PUT 成功后批量确认条目已上传；用于刷新后恢复真实上传进度。
- `POST /api/upload-sessions/{session_id}/finalize-generation-task`：把已上传对象保存为 `GenerationTask` 草稿。
- `POST /api/upload-sessions/{session_id}/finalize-styling-garment`：把已上传对象保存为 `StylingGarment` 草稿。
- `POST /api/upload-sessions/{session_id}/abandon`：用户放弃未完成上传任务；后端标记废弃并尽力清理尚未入库的直传对象。
- `businessCode` / `sourceTitle`：创建上传任务时可选传入业务货号和用户选择的文件夹 / 批次名；如果旧客户端未传，搭配服装保存接口会用最终 `code` 兜底写入，保证刷新恢复后仍能显示真实货号。

上传任务状态：

```text
PENDING
UPLOADING
UPLOADED
FINALIZING
FINALIZED
FAILED
ABANDONED
```

`uploaded_count` 表示已确认上传到对象存储的条目数量。`business_code` 和 `source_title` 用于上传记录恢复和跳转定位，不参与业务唯一性判断。`FINALIZED` 表示业务记录已创建，`target_type` 和 `target_id` 指向对应 `generation_task` 或 `styling_garment`。保存为业务对象前的失败（例如货号重复、对象缺失、保存异常）会写回 `FAILED` 和 `error_message`，便于刷新后继续显示“需处理”。搭配服装的 AI 识别是 `StylingGarment` 创建后的独立阶段，不把识别失败写成上传失败。

批量上传的失败以 session 或业务对象为单位呈现。已经 `FINALIZED` 的 session 再次 finalize 应幂等返回目标对象；失败 session 可由前端重新上传该货号或删除已创建对象后重来。默认恢复查询不返回 `ABANDONED` session，除非调试场景显式查询。

上传会话只负责“文件是否被系统接住”。搭配服装 AI 识别继续使用服装资源接口：

- `POST /api/styling-garments/{id}/save-and-tag`：保存当前搭配服装并触发 / 重新触发 AI 识别；用于单货号局部重试。
- 批量上传场景中，前端应先等待本批所有可上传项完成 `finalize-styling-garment`，再对成功创建的 `StylingGarment` 并发调用 `save-and-tag`。识别失败只改变 `StylingGarment.status` 和 attempt，不改变 upload session 的 `FINALIZED` 结果。

未 `FINALIZED` 的 upload session 属于临时上传任务。用户显式放弃或后台 TTL 清理时，只能清理未登记为 `MediaAsset` 的直传对象；已经保存为业务对象的图片走对应业务对象的归档 / 删除 / 媒体引用计数规则。

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

## Attempt 错误响应

任务详情和 attempt 分页中的失败摘要统一返回：

```json
{
  "errorCode": "INVALID_REQUEST",
  "errorMessage": "参考图最多支持 6 张",
  "error": {
    "code": "INVALID_REQUEST",
    "title": "生成输入不符合服务要求",
    "message": "参考图最多支持 6 张",
    "action": "调整输入后重试",
    "retryable": false
  }
}
```

`error` 是普通用户契约，前端不得自行解析 provider 原始字符串。admin-only attempt 技术详情额外返回 `technicalMessage`、`requestFacts`、`responseFacts`；其中可包含 ToAPIs 顶层错误、内嵌上游错误和 providerRequestId，但不得进入普通任务列表。

重试必须按错误类别和阶段决策：参数、鉴权、余额、内容策略与业务拒绝不重试；提交结果未知先按 `client_business_id` 对账；状态查询只容忍网络/网关/限流类瞬时错误并受 `GENERATION_STATUS_RETRY_LIMIT` 约束；明确的 provider 内部生成失败按 `GENERATION_SUBMIT_RETRY_LIMIT` 有限重建；回传失败只重新下载，不重新生成。

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

AI 选搭动态事实：

- `runtime.user_requirement`
- `runtime.ai_styling_user_instruction`
- `runtime.hero_image_files`
- `runtime.candidate_retrieval`

分镜预览动态事实：

- `runtime.user_requirement`
- `runtime.shot_styling`
- `runtime.ref_model_identity`
- `runtime.ref_garment`
- `runtime.ref_shoot_environment`
- `runtime.storyboard_preview_user_instruction`
- `runtime.category_family`

成片动态事实：

- `runtime.preview_grid_cell`
- `runtime.user_requirement`
- `runtime.ref_model_identity`
- `runtime.ref_preview_grid`
- `runtime.ref_garment`
- `runtime.ref_shoot_environment`
- `runtime.final_image_user_instruction`
- `runtime.shot_styling`（配饰与造型，单行，使用 `；` 分隔）
- `runtime.shot_global_directing`
- `runtime.shot_current`
- `runtime.is_shot_1` 到 `runtime.is_shot_5`

## 导出

成片批量下载 zip 内结构为 `任务名/序号_图位.扩展名`。图位名称来自最终结果图的 `shot_role`，不是旧镜头计划。
