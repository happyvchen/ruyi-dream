# Backend API Guideline

## 当前生成 API

0.1.0 当前 AI 摄影棚链路固定为：

```text
导入主推款 -> AI 智能选搭 -> 最终成片
```

后端不提供“生成拍摄剧本”API，不保存动态剧本或镜头计划。前端和测试不得调用或伪造旧的剧本步骤。

## 任务接口

- `POST /api/generation-tasks`：兼容旧表单导入路径；新导入体验应优先使用 `upload_sessions -> finalize-generation-task`，以便刷新 / 切页后恢复状态。
- `POST /api/generation-tasks/from-uploads`：已停用（410）；统一使用导入会话创建任务，避免无主对象无法回收。
- `POST /api/generation-tasks/{task_id}/images`：为现有任务追加主推款图片，任务最多维护 8 张；非草稿任务追加后按输入变更规则使既有选搭和成片失效，等待用户重新确认执行。
- `PATCH /api/generation-tasks/{task_id}`：保存任务名和各阶段用户补充要求草稿。
- `POST /api/generation-tasks/{task_id}/styling`：运行 AI 智能选搭，输出当前 `StylingPlan`。
- `POST /api/generation-tasks/{task_id}/preview`：已停用（410）；不再生成新的分镜预览。
- `POST /api/generation-tasks/{task_id}/generate-final`：新任务从 `POSE_SELECTION_READY` 复用冻结的背景、五个姿势、商品/模特输入版本与成片规则，并发生成最终成片；旧任务兼容原预设。
- `POST /api/generation-tasks/{task_id}/auto-run-final`：从当前第一个未完成阶段自动推进到成片。user 的“生成成片/继续生成”使用此接口，自动完成隐藏的姿势选搭；后台拍摄准备在创建调用前因素材/配置失败时，持久化 `SHOOT_PREPARATION_FAILED`，保留造型并允许修复后继续。
- `POST /api/generation-tasks/{task_id}/cancel`：取消当前运行中的选搭或成片。
- `PATCH /api/generation-batches/{batch_id}`：保存批次名；批次任务归属仍由 `GenerationTask.batch_id` 决定。
- `POST /api/generation-batches/{batch_id}/auto-run-final`：批次开始/继续一键成片；整批完成后可传 `restart=true` 重新生成。
- `POST /api/generation-batches/{batch_id}/cancel`：取消批次内所有生成中或排队中的任务，保留已完成成果。

`CANCELLED` 是可恢复的中断态，不是归档或终止态。再次调用单任务 `auto-run-final` 或批次 `auto-run-final` 时，应把任务恢复到最近被取消的阶段并继续；已成功的阶段和成片槽位保持不变。

批次 `auto-run-final` 只负责把批次内任务标记为一键成片队列；实际并发由后端 ToAPIs 全局闸门控制。运行参数统一使用 `TOAPIS_MAX_CONCURRENCY`，默认 `5`，不再提供批次级独立并发参数。

## 导入任务接口

主推款导入和搭配服装导入复用 `upload_sessions` 轻量任务契约。搭配服装批量导入采用文件级断点续传：先持久化整批全部货号和图片清单，再逐文件直传；刷新后只重新导入对象存储中缺失的图片。

- `POST /api/uploads/presign`：已停用（410）；禁止创建没有 UploadSession 所有权的直传对象。
- `POST /api/upload-sessions`：创建导入任务和对象存储直传地址。
- `POST /api/upload-sessions/styling-import-batch`：原子创建一次搭配服装导入中的全部货号会话；返回共同的 `batchId`，保证用户开始任务后、读取和导入图片前整批记录立即可查。每个货号首次 `resume` 时绑定图片 SHA-256。
- `GET /api/upload-sessions`：按 `scope`、`status`、`batchId`、`limit` 查询导入任务，默认按首次创建时间倒序、同时间按 ID 倒序；默认排除已归档记录，`includeArchived=true` 时一并返回且包含已归档的 `ABANDONED` 记录；`batchId` 用于完整恢复同一次批量任务，单批最多返回 500 个货号。`includeArchived=true` 返回的 `deletion_pending` 仅供客户端对账并移除本地残留任务，不得渲染为用户记录。搭配服装会话同时返回由当前业务对象派生的 `targetSummary`、面向用户的 `importOutcome` 和历史恢复摘要 `historySummary`；前端不得依赖当前服装库分页结果二次拼接这些字段。
- `GET /api/upload-session-batches`：按稳定导入批次聚合导入会话，供导入记录和搭配服装库的 `导入批次` 筛选共同使用。默认排除已归档和内部 `deletion_pending`，不按导入状态或是否产出服装过滤；批次按最早 `createdAt` 倒序，`codeCount` 为当前可见导入会话数。无 `batch_id` 的历史会话以 `session:{session_id}` 作为稳定单条批次键。
- `GET /api/upload-sessions/{session_id}`：查询单个导入任务和条目。
- `POST /api/upload-sessions/{session_id}/resume`：严格校验重新选择的相对路径、大小、修改时间和 SHA-256；认领对象存储中已存在且完整性一致的对象，并只为缺失条目重新签发导入地址。
- `POST /api/upload-sessions/{session_id}/lease`：导入期间续租当前批次的执行权。
- `POST /api/upload-sessions/{session_id}/complete-items`：浏览器 PUT 成功后批量确认条目已导入；用于刷新后恢复真实导入进度。
- `POST /api/upload-sessions/{session_id}/finalize-generation-task`：把已导入对象保存为 `GenerationTask` 草稿。
- `POST /api/upload-sessions/{session_id}/finalize-styling-garment`：把已导入对象保存为 `StylingGarment` 草稿。
- `POST /api/upload-sessions/{session_id}/abandon`：用户放弃未完成导入任务；后端标记废弃并尽力清理尚未入库的直传对象。
- `POST /api/upload-sessions/delete-impact`：预检一个或多个导入记录的会话、条目、临时对象和已关联业务对象数量；不限制导入任务状态。
- `POST /api/upload-sessions/reconcile`：按最多 500 个已知 session ID 只读返回仍存在的 `existingIds` 和其中属于内部删除墓碑的 `deletionPendingIds`；用于浏览器持久导入任务与服务端权威状态对账，不修改业务数据或清理进度。
- `POST /api/upload-sessions/archive`：归档任意状态的导入记录；只写归档字段并从默认列表隐藏，不改变导入状态、不停止导入，也不影响已入库业务对象和媒体。
- `POST /api/upload-sessions/restore`：恢复已归档导入记录的可见性；不改变导入状态。
- `DELETE /api/upload-sessions`：彻底删除任意状态的导入会话元数据；单条输入完整任务名、批量输入 `全部删除`。只清理既未通过条目外键登记、也不存在同 `object_key` 活跃 `MediaAsset` 的临时直传对象，已入库的服装、生成任务和正式媒体不随导入记录删除。未完成会话先返回 `deferredIds` 并进入内部 `deletion_pending` 墓碑，立即阻断续传确认和入库，待已签发直传地址安全失效后再物理删除；该内部状态不返回到任何用户导入记录列表，用户确认删除后记录立即消失。
- `businessCode` / `sourceTitle` / `batchId`：创建导入任务时记录业务货号、用户选择的文件夹 / 批次名和本次批量任务 ID；如果旧客户端未传货号，搭配服装保存接口会用最终 `code` 兜底写入。
- `GET /api/styling-garments` 支持 `uploadBatchKey` 精确筛选服装与当前可见导入会话的关联；普通批次使用 `batch:{batch_id}`，无批次 ID 的历史单条会话使用 `session:{session_id}`。筛选不再把批次货号拼接为关键词；批次没有产出服装时正常返回空分页。
- `executorId`：浏览器标签页执行标识。继续导入和确认条目会续租 90 秒批次执行权；其他页面或电脑只能查看，不能并发接管仍有有效租约的批次。

导入任务状态：

```text
PENDING
UPLOADING
UPLOADED
FINALIZING
FINALIZED
FAILED
ABANDONED
```

`uploaded_count` 表示已确认导入到对象存储的条目数量。相对路径、大小、修改时间和 SHA-256 构成恢复清单；SHA-256 同时写入 S3 用户元数据。S3 PUT 成功但浏览器尚未确认就刷新时，`resume` 会通过对象 HEAD 校验自动补认领。超过 5 分钟仍停在 `FINALIZING` 且没有目标业务对象的会话会恢复为 `UPLOADED`，允许幂等重试保存。`FINALIZED` 表示业务记录已创建，`target_type` 和 `target_id` 指向对应业务对象。搭配服装的 AI 识别是 `StylingGarment` 创建后的独立阶段，不把识别失败写成导入失败。

搭配服装导入记录采用“导入结果 + 当前服装状态”的双状态读模型：

- `importOutcome` 是导入记录的主状态：`IMPORTING`、`ORGANIZING`、`PENDING_RECOGNITION`、`PROCESSING`、`RECOVERING`、`REVIEW_REQUIRED`、`NEEDS_ACTION`、`SUCCEEDED`、`RECOVERED`、`TARGET_MISSING`、`ABANDONED`。它由 upload session、当前 `StylingGarment` 和识别 attempts 共同派生，不新增一套可漂移的持久状态；待识别和待复核不得伪装成后台运行态。
- `targetSummary` 是关联服装的权威当前摘要，包含 `id`、`code`、`exists`、`status`、正式 `coverUrl`、正式 `imageCount`、`updatedAt` 和 `archivedAt`。即使该服装不在当前服装库分页中，导入记录也能显示正确封面、正式图片数和当前状态。
- 重复货号导入不会重复创建服装，但导入会话必须关联已经存在的服装；历史重复记录缺少持久化关联时，读取接口仅依据导入明细保留的失败时间，在同货号服装早于该次失败存在时恢复当前摘要；若缺少可靠失败时间则不做推断，避免误关联后来重建的同货号服装，也不复制媒体文件。
- 失败会话关联已有服装只用于展示和跳转，不代表该批次创建了这件服装；服装库的导入来源只认 `FINALIZED` 会话。
- 重复记录关联的服装被删除后，当前状态优先为 `TARGET_MISSING`，不再提供“查看已有”操作。
- `historySummary` 只保留恢复语义所需的历史事实：是否发生过识别失败、最近失败时间和恢复成功时间。历史失败不覆盖当前事实。
- 识别失败时主状态为 `NEEDS_ACTION`；用户从服装库重新识别成功后，主状态自动变为 `RECOVERED`，副状态显示 `当前可选搭`。失败历史仍可审计，但不继续计入当前失败数量。
- 关联服装被删除时使用 `TARGET_MISSING`，不得继续展示缓存图片、虚构内部记录名或把它误报为成功。

批量导入的失败以 session 或业务对象为单位呈现。已经 `FINALIZED` 的 session 再次 finalize 应幂等返回目标对象；文件未接收或业务对象未创建的失败项提供 `重新导入`，已创建服装但识别失败的项提供 `重新识别`。默认恢复查询不返回 `ABANDONED` session，除非调试场景显式查询。

导入会话只负责“文件是否被系统接住”。搭配服装 AI 识别继续使用服装资源接口：

- `POST /api/styling-garments/{id}/images`：为现有搭配服装追加图片，单件服装最多维护 6 张；识别进行中禁止修改，追加本身不触发外部 AI 识别。
- `POST /api/styling-garments/{id}/save-and-tag`：保存当前搭配服装并触发 / 重新触发 AI 识别；用于单货号局部重试。
- `POST /api/styling-garments/batch-save-and-tag`：批量提交 `DRAFT`、`TAGGING_READY`、`TAGGING_FAILED` 搭配服装进入 AI 识别，也支持对 `READY` 搭配服装重新识别；已归档、正在识别或无需识别的项会跳过并返回 `skipped`。
- `POST /api/styling-garments/delete-impact`：预检待删除服装、服装图片和历史引用数量。`referencingTaskCount` 只表示会保留并显示来源已删除的生成记录，不计入删除的 `taskCount`、结果图或媒体统计。
- `DELETE /api/styling-garments/{id}?purge=true` / `POST /api/styling-garments/batch-delete`：彻底删除只清理搭配服装聚合及其图片关系、标签和打标 attempts；媒体仅在没有其它直接引用或历史快照引用时释放。不得删除引用它的生成任务、分镜、成片或生成 attempts。响应 `deletedTaskIds` 固定为空，`referencingTaskIds` 返回仍保留的历史任务。
- `GET /api/styling-garment-exports/archive?ids={id1,id2}`：下载一个或多个货号的正式原图 ZIP；每个货号一个文件夹，只包含图片，单次最多 50 个货号。下载不受识别状态或归档状态限制，也不触发 AI 识别。
- 批量导入场景中，前端应先等待本批所有可导入项完成 `finalize-styling-garment`，再对成功创建的 `StylingGarment` 调用批量识别接口。识别失败只改变 `StylingGarment` 的当前识别状态、错误码及已创建的 attempt，不改变 upload session 的 `FINALIZED` 结果。

未 `FINALIZED` 的 upload session 属于临时导入任务。用户显式放弃、彻底删除导入记录或后台 TTL 清理时，只能清理未登记为 `MediaAsset` 的直传对象；已经保存为业务对象的图片走对应业务对象的归档 / 删除 / 媒体引用计数规则。导入记录归档与业务对象归档相互独立。彻底删除的对象 key 在删除会话前写入持久清理队列；对象存储失败时保留错误和 key，由后台轮询幂等重试。

搭配服装列表与详情在 `TAGGING_FAILED` 时返回本轮任务的安全稳定字段 `taggingErrorCode`：`RATE_LIMITED` 表示限流恢复耗尽，`UPLOAD_REQUIREMENT_NOT_MET` 表示模型未选出可入库商品图，其余错误使用对应技术分类；新一轮开始及识别成功时会清空该字段。普通响应中的 `tagError` 为 `null`，且不返回 attempt、provider 原文或请求/响应快照；admin 技术详情仍通过 `tagError` 和 `tagAttempts` 查看完整事实。

## 状态

新任务只使用四步链路；`PREVIEW_RUNNING` / `PREVIEW_READY` 仅为历史记录兼容状态，不再由当前接口创建：

```text
DRAFT
READY_FOR_STYLING
STYLING_RUNNING
STYLING_READY
POSE_SELECTION_RUNNING
POSE_SELECTION_READY
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

重试必须按错误类别和阶段决策：参数、鉴权、余额、内容策略、输入不合格与业务拒绝不重试；Gemini 的明确限流、连接失败、读取超时、网关、精确 `unknown provider for model` 和安全判定为零输出的未知 provider 异常在 provider 层自动重发，已有正文、reasoning、非零 usage 或 finish reason 的响应不允许盲目重发。JSON/必需字段契约异常由打标或 AI Stylist 编排层追加协议纠错事实后重问。Image-2 提交结果未知先按 `client_business_id` 对账；状态查询只容忍网络/网关/限流类瞬时错误；明确的 provider 内部生成失败有限重建；回传失败只重新下载，不重新生成。各类默认最多重试 10 次，按 30/60/120/240 秒封顶退避并受阶段总预算约束；同一种错误只有一个重试所有者。

ToAPIs 图片参考输入在上传前生成只读传输副本：官方限制为长边不超过 8192、总像素不超过 4000 万，系统使用 8000、3600 万和 9.5 MiB 安全目标。副本等比缩放、透明底转白底，原始 `MediaAsset` / S3 对象不变；`input-v2` 缓存版本与 `providerReferenceImageManifest` 用于避免旧 URL 复用并支持 admin 追溯。无法解码或转换后仍不合规则返回 `INVALID_REQUEST`，不消耗 10 次自动重试。

任务响应中的 `autoRunRetryAt`、`autoRunRetryCount` 与 `autoRunRetryLimit` 表示已持久化的下一次跨阶段自动恢复时间、当前轮次和配置上限；`autoRunRetryAt` 非空且为未来时间时前端显示 `自动恢复中`，不得提前呈现终态失败。

## Prompt 维护槽位

当前后端只登记并读取这些 Prompt 配置槽：

- `segment.brand_info`
- `assembly.ai_tagging`
- `assembly.hero_image_selection`
- `assembly.ai_styling`
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

成片动态事实：

- `runtime.category_family`
- `runtime.shot_index`
- `runtime.user_requirement`
- `runtime.ref_model_identity`
- `runtime.ref_shot_background`
- `runtime.ref_shot_pose`
- `runtime.ref_hero_garment`（主推款参考图号）
- `runtime.ref_styling_garment`（搭配款参考图号；无搭配款时为空）
- `runtime.final_image_user_instruction`
- `runtime.shot_styling`（配饰与造型，单行，使用 `；` 分隔）
- `runtime.is_shot_1` 到 `runtime.is_shot_5`

## 统一拍摄背景

- `GET /api/shoot-environment`：登录用户读取当前共享背景，返回 `{asset}`。
- `POST /api/shoot-environment`：admin 使用 multipart `file` 替换背景。
- `DELETE /api/shoot-environment`：admin 清除当前背景；历史选姿势和结果仍引用的图片保留。

新姿势选择返回 `poseSelection.workflow = shared_background_v1`，五个 shots 共享同一 background，pose 各不相同。初筛和复核均传入商品图片；复核额外传入候选姿势原图。无背景、当前品类成片 Prompt 未配置或可用姿势不足五个时，选姿势在调用 provider 前失败。历史无 workflow 的选择返回 legacy_masters。

## 历史拍摄预设接口

- `GET /api/shot-presets`：仅 admin 读取 15 个当前槽位，以及三个服装类型各自的完整槽位数和校验错误。
- `POST /api/shot-presets/{category_family}/{shot_index}/{asset_role}`：仅 admin 单张替换 `background` 或 `pose`，成功后立即成为当前生效图片。
- `DELETE /api/shot-presets/{category_family}/{shot_index}/{asset_role}`：仅 admin 删除当前单张图片并立即生效；该类型不完整期间不能开始新成片。
- 旧整槽保存和发布接口返回 `410 Gone`。

最终请求固定 `image_urls <= 6`，共享背景排第一，不传 `mask_url`，使用 `1:1 / 2k / high`；模特、主推款、搭配款每个角色只送一张事实图，姿势图按镜头景别预裁切后作为运行时传输副本上传；结果集通过 `generation_result_set_shot_presets` 保存共享背景与五个姿势；姿势选择阶段已冻结这些引用。

## 导出

成片批量下载 zip 内结构为 `任务名/序号_图位.扩展名`。图位名称来自最终结果图的 `shot_role`，不是旧镜头计划。

## 网络与媒体 P0 契约

`GET /api/generation-tasks/{task_id}/status` 返回轻量对象（不套 `item`）：`taskId/status/stage/progress/operations/activeOperation/detailRevision/updatedAt/autoRunTarget/autoRunRetryAt/autoRunRetryCount/retryAt/error`。`operations` 只有当前 attempt 的 ID、状态、当前阶段和阶段起点，不含请求/响应快照；用于合并已有详情里的进度。原始响应预算 <2 KB。旧详情 GET 保留。

任务、记录、批次和工作区列表通过批量 SQL 读取摘要，不调用完整任务装配。摘要保留名称、状态、创建/更新时间、批次、归档/自动运行操作状态、`detailRevision`、`progress` 和 `summary.cover`（仅 ID、文件名、thumbUrl、thumbStatus）。不再返回 summary 的 finalImages/selectedGarments/preview，不返回 input version、StylingPlan、attempt 和用户要求。详情仍按原接口读取。

`MediaAsset` 和服装封面返回 `thumbStatus/previewStatus = AVAILABLE | MISSING`，以派生元数据为准；MISSING 的 URL 为 null。`/api/media?variant=thumb|preview` 会检查实际对象，缺失返回 404 和 `private, no-store`，不在 GET 中生成派生图，也不回退原图。对象在 302 之后变为缺失时由浏览器显示占位，不声称后端能够观测 RustFS 的最终响应。原图下载与导出保持 `private, no-store`。

仅 FastAPI 的 `HttpPerformanceMiddleware` 压缩 JSON/Text：阈值 1024 字节、gzip level 5、`Vary: Accept-Encoding`；图片、归档和 SSE 不压缩。网关不再重复 gzip API。访问日志只记录路由模板、方法、状态码、耗时、实际响应 body 字节、媒体档位与下载布尔值，不记录 query、Cookie、Authorization 或业务正文。302 字节仅是控制响应，RustFS 的媒体字节需通过 HAR/对象存储侧统计。

## 成片提示词优化接口

- Prompt 配置响应新增 `enabled`；优化规则槽位提供 `suggestedContent`，仅供管理员显式填入编辑器，运行时不作 fallback。
- `PATCH /api/prompt-configs/{key}`：优化规则可同时保存 `content`、`enabled`；保存不调用模型。
- `POST /api/prompt-configs/{key}/test-optimization`：仅 admin，输入 `content`、`attemptId`；支持优化规则或三类成片模板的历史上下文测试。返回测试 attemptId 与原始、优化、最终文本；失败保留审计记录。不改成片、不调用图像 provider。
- 任务结果集新增 `preparation={status,error,attemptId}`，不返回配置和提示词正文；admin 使用现有 ai-invocations 接口查看详情。attempts 的 ownerType 支持 `generation_result_set`。

## 姿势库与姿势选搭

所有姿势库接口、独立 select-poses、姿势要求 PATCH、attempts 分页与历史 shot-presets 读取仅 admin 可访问，user 返回 403；参考 Prompt 草稿也仅 admin 可读。user 使用 auto-run-final 自动编排内部姿势选搭，内部函数不受管理接口权限限制。

| 方法与路径 | 契约 |
|---|---|
| GET `/api/poses` | q 多关键词、status、includeArchived、limit（1–96，默认 24）、offset；返回 items/total/limit/offset |
| POST `/api/poses` | multipart name、files（恰好 1 张）；保存 DRAFT，不调用模型 |
| GET / PATCH `/api/poses/{id}` | 详情 / 更新名称；图片改动后需重新识别 |
| PUT `/api/poses/{id}/image` | multipart files，恰好 1 张；替换后待识别，不改动历史冻结图片 |
| POST `/api/poses/{id}/images` | 兼容旧入口，仅允许为无图条目补 1 张；已有图片返回 409 |
| DELETE `/api/poses/{id}/images/{imageId}` | 禁止删除唯一图片（409），应替换图片或管理整个姿势 |
| POST `/api/poses/{id}/recognize` | 显式启动后台识别；返回 TAGGING_RUNNING，重复或已归档返回 409。模型返回 name（1–80 字）与非空 tags 对象，成功后同时回填条目 name 和 aiTags；失败不覆盖原名称与标签 |
| POST `/api/poses/delete-impact` | `{ids}`；返回条目、可释放媒体和引用任务数量 |
| POST `/api/poses/manage` | `{ids,action,confirmPhrase?}`，action 为 archive/restore/purge；purge 需单条名称或“全部删除”，返回清理错误列表 |
| POST `/api/generation-tasks/{id}/select-poses` | 可选 `{userInstruction}`，先持久化状态和 attempt，再后台执行；重复请求 409 |
| GET `/api/pose-prompt-drafts/{tagging\|selection}` | admin 获取参考正文，仅供填入编辑器，不写数据库或调用模型 |

user 的 generation 系列 JSON 响应不含 poseSelection 参考图、姿势要求、原始调用快照与 provider 标识；保留后台状态和 active 指针用于恢复。配置错误转为业务提示，admin 与数据库快照保留原始事实。

任务增加 `poseWorkflowEnabled`、`activePoseSelectionId`、`poseSelection` 与 `stageUserInputs.poseSelection`；PATCH 支持 `poseSelectionUserInstruction`。状态增加 POSE_SELECTION_RUNNING / POSE_SELECTION_READY。结果集返回 `poseSelectionId`，成片始终读取该选择及冻结的五组图片。新任务不得跳过姿势选搭，历史任务默认兼容原流程；选搭失败、取消和中断沿用现有失败协议，晚到响应不覆盖取消。

## 完整记录与成片版本

`GET /api/generation-records/{id}` 的 `resultSets` 返回该任务全部结果组（新到旧），不依赖 active_result_set_id。每个镜头的 `versions` 包含 id、asset、createdAt、isCurrent、canRegenerate；不向普通用户暴露执行快照。记录展示和下载不以当前执行状态为过滤条件。

- `POST /api/generation-result-images-new/{id}/retry` 可传 `{versionId,userInstruction}`。versionId 必须属于该镜头；使用该版本真实输入，成功后增加版本，失败或取消保留之前的成功图片。
- `POST /api/generation-result-sets/{id}/regenerate` 从指定组的单张冻结输入创建新组，保留原组、选搭和拍摄上下文。
- `POST /api/generation-result-sets/{id}/continue` 激活指定组，仅恢复失败、取消或缺失镜头，成功镜头不变。
- 导出预检与 archive 支持可选 resultSetId；默认导出任务所有组和已发布版本，多组/多版本使用独立子目录。归档任务可查看、下载，恢复后才能继续生成。
- 缺失真实拍摄输入返回 409，并保留原图查看/下载；重新一键成片可从当前工作流重新拍摄，不能把当前配置冒充原图事实。


## 可见页面静默刷新版本检查

`GET /api/collection-revisions/{scope}` 返回 `{revision: number}`，scope 为 `poses / garments / records / models`。遵循会话认证，`poses` 仍要求 admin，未知 scope 返回 404。接口只按主键读取一行计数，不装配列表、不查询对象存储、不调用 AI/provider。

迁移 085 建立 `collection_revisions` 和 SQLite 触发器：相关业务表新增、修改、删除时，在同一事务递增对应计数；仅更新时间/lease 心跳或字段无变化不递增。媒体派生可用性等字段变化使相关视图重新读取。计数持久化，多进程共享，事务回滚也回滚计数；客户端取得版本后才读取列表，读取成功后记住版本，以免漏掉并发写入或失败重试。

前端空闲每 30–36 秒检查；有运行条目时按 5–18 秒自适应检查。只有版本变化才读取当前分页/详情，页面离开、标签页隐藏或离线时停止后台请求。首次版本检查会建立基线并读取一次列表，之后无变化只返回版本号。更新前后端需配套部署，后端启动执行迁移后提供此接口。
