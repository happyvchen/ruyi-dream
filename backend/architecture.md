# Backend Architecture

## 职责

后端是 FastAPI + SQLite + S3 兼容对象存储的一体化服务，负责：

- 素材与媒体资产管理；
- Gemini 打标和 AI 智能选搭；
- Prompt 维护槽位读取与 runtime 占位渲染；
- Image-2 分镜预览和最终成片任务包组装；
- provider attempt 快照、进度、错误分类和轮询恢复；
- 生成记录、归档、删除和导出。

## 当前生成链路

```text
导入主推款
-> AI 智能选搭
-> 三类固定 Prompt 生成分镜预览
-> 分镜前 5 格生成成片
```

后端不再包含动态剧本服务。`app/main.py` 中的生成相关代码只围绕 `StylingPlan`、`GenerationPreview` 和 `GenerationResultImage` 编排。

## Image-2 分阶段恢复

- 参考图导入失败只重试失败导入，已成功导入的 provider URL 通过缓存或 attempt 快照复用。
- 每个图片 attempt / 生成重试轮次向 ToAPIs 传稳定 `client_business_id`。创建任务遇到连接中断、`ReadTimeout` 或 TLS EOF 时标记为 `SUBMISSION_UNKNOWN`，按统一退避先以该业务 ID 查询原任务；查到即继续，明确 `task_not_found` 才补提，避免重复任务和重复计费。
- 提交结果未知按每张图自己的 `GENERATION_UNKNOWN_SUBMISSION_RETRY_LIMIT` 预算恢复，不再设置同批提交未知熔断器。
- 已取得 provider task ID 后只恢复状态查询；provider 明确瞬时生成失败时仅按 `GENERATION_SUBMIT_RETRY_LIMIT` 重建生成任务。
- provider 成功后的下载失败只重试下载；本地对象存储、数据或代码错误标记为 `SYSTEM_ERROR`，不自动重试。
- 提交未知、生成重试次数和 provider ID 历史均持久化在 attempt 快照中，自动恢复预算耗尽后才向用户呈现部分失败。

## ToAPIs 错误契约

ToAPIs 图片任务是异步协议：创建接口返回任务 ID，状态接口即使 HTTP 200 也可能以 `status=failed` 返回终态错误。后端必须同时解析 HTTP 错误信封和异步 `error.code/error.message`；当顶层 `generation_failed` 的 message 内嵌上游 JSON 时，继续提取最深层 `code/type/message/param`，不能把整个响应字典拼进普通用户文案。

官方基础错误与内部稳定码：

| ToAPIs | 内部码 | 用户动作 | 自动策略 |
|---|---|---|---|
| `invalid_request` / `invalid_request_error` / HTTP 400 | `INVALID_REQUEST` | 修正参考图、格式或参数 | 不重试 |
| `unauthorized` / HTTP 401 | `AUTH_FAILED` | 联系管理员检查密钥/权限 | 不重试 |
| `insufficient_quota` / HTTP 402 | `INSUFFICIENT_BALANCE` | 联系管理员充值 | 不重试，同批硬停 |
| `task_not_found` / HTTP 404 | `TASK_NOT_FOUND` | 重新创建任务 | 不重复查询；仅用于提交未知对账后补提 |
| `content_policy_violation` / HTTP 422 | `CONTENT_POLICY_VIOLATION` | 调整素材或文字要求 | 不重试 |
| `rate_limit_exceeded` / HTTP 429 | `RATE_LIMITED` | 等待 | 尊重 `Retry-After`，有限退避 |
| HTTP 500 + `rate_limit_check_failed` | `RATE_LIMITED` | 等待 | 按零产出瞬时错误指数退避 |
| `internal_error` / HTTP 500 | `PROVIDER_INTERNAL_ERROR` | 稍后再试 | 按 `GENERATION_SUBMIT_RETRY_LIMIT` 有限重建 |
| 异步 `generation_failed`，且无更深层不可重试原因 | `PROVIDER_GENERATION_FAILED` | 再次生成 | 按 `GENERATION_SUBMIT_RETRY_LIMIT` 有限重建 |

API 摘要返回稳定 `error={code,title,message,action,retryable}`；普通用户只看可读标题、具体原因和下一步。数据库与 admin 技术详情保留原始 `technicalMessage`、provider 快照、顶层/上游错误码和 providerRequestId。

官方依据：`https://docs.toapis.com/docs/cn/api-reference/tasks/image-status`、`https://docs.toapis.com/docs/cn/api-reference/images/gpt-image-2/official`、`https://docs.toapis.com/docs/cn/api-reference/uploads/images`。

## ToAPIs 并发闸门

ToAPIs 相关调用统一使用 `TOAPIS_MAX_CONCURRENCY` 控制最大并发，默认 `5`。该闸门覆盖 Gemini(ToAPIs) chat、Image-2(ToAPIs) 参考图导入、任务创建和状态查询；只在真实 HTTP 请求期间占用槽位，provider 生成等待、轮询间隔和重试退避期间释放槽位。批次一键成片不再有独立并发参数，而是按同一 ToAPIs 预算推进排队任务。

Gemini 对明确限流（HTTP 429 或 `rate_limit_check_failed`）、连接失败、读取超时、网关和 provider 瞬时异常在 provider 层自动重发。搭配服装打标把 provider 给出的下一次退避持久化到 `tag_retry_at/tag_retry_state_json`，释放当前 worker 和并发槽位，由现有 generation poller 到期恢复；进程重启后复用 `tag_active_attempt_id` 和累计次数继续，不能重置 10 次预算。JSON 不可解析、缺少 `selected_files` / `ai_tags` / `styling_plan` 等模型契约异常由对应编排层追加协议纠错事实后重问。两类技术恢复默认分别最多重试 `10` 次，采用 `30/60/120/240` 秒封顶退避并受总预算约束；同一种错误只有一个重试所有者，禁止业务层再次处理 provider 已耗尽的传输错误。

`一键成片` 的跨阶段恢复由 `auto_run_target`、`auto_run_retry_at` 和 `auto_run_retry_count` 持久化；后台 poller 到期后继续推进，成功产物保持不变。参数、鉴权、余额、内容策略、输入不合格和业务判断拒绝不进入该恢复队列；模型输出格式/必需字段契约异常由当前 Gemini 编排层纠错，不交给跨阶段 auto-run 重复处理。

每类错误只有一个重试所有者，禁止 `10×10` 叠加：Gemini 连接、Image-2 提交/状态查询/明确生成失败和回传由各自阶段完成最多 10 次；跨 attempt 的持久 auto-run 只接管单次导入失败与进程中断。状态查询或回传耗尽后保留原 attempt 与 providerRequestId，不通过 auto-run 重新提交生成。

批量 AI 打标在一个后台任务内并发调度，并受 `TAGGING_MAX_CONCURRENCY` 与 ToAPIs 全局请求闸门双重约束。退避等待不占用这两个并发槽位；仍在当前进程排队、执行或持久等待恢复的服装不会被 stale 清理误判。进程重启后，有 `tag_retry_at` 的任务由 poller 续跑；首次调用或成功落库前中断而尚无 `tag_retry_at` 的无主运行态，按 `PROCESS_INTERRUPTED` 纳入同一 10 次进程恢复预算并复用当前 attempt，不能直接转为失败。预算耗尽后保留安全的累计次数、最后错误分类和 attempt 快照，供用户提示与管理员追溯。

旧环境变量 `GENERATION_AUTO_RUN_MAX_CONCURRENCY` 仅作为过渡 fallback：未设置 `TOAPIS_MAX_CONCURRENCY` 时才读取。新部署和文档统一使用 `TOAPIS_MAX_CONCURRENCY`。

## 主要模块

| 文件 | 职责 |
|---|---|
| `app/main.py` | API 路由、AI Stylist、分镜预览、最终图和后台 poller 编排 |
| `app/db.py` | 最终 schema、Prompt 配置槽登记、清理迁移 |
| `app/prompt_render.py` | 动态占位文本和参考图图号区间渲染 |
| `app/image_provider.py` | ToApis / fal 请求快照和图片生成适配 |
| `app/media_storage.py` | 媒体对象键、兼容读取边界和单资产精确清理清单 |
| `app/toapis_concurrency.py` | ToAPIs 全局并发闸门 |
| `app/generation_progress.py` | attempt 阶段进度和错误分类 |

## RustFS 对象布局与所有权

运行时只有一个媒体事实源：SQLite `media_assets` 保存元数据和引用关系，RustFS 保存二进制。所有新建的搭配服装图、主推款图、拍摄环境图、分镜预览和最终成片统一使用独立媒体命名空间：

```text
media/<media-uuid>/original.<ext>
media/<media-uuid>/_derived/normalized/original.{jpg|png}
media/<media-uuid>/_derived/thumb/original.webp
media/<media-uuid>/_derived/preview/original.webp
media/<media-uuid>/_derived/final-cells/original-shot<1..6>.png
```

- 一张 `MediaAsset` 独占一个 `<media-uuid>`，业务归属只保存在数据库关系中，不编码进对象键。
- 直传申请直接写入上述 durable key；导入会话只是传输与恢复状态，不再成为永久目录。
- provider 生成对象按 `generation_attempt` 派生稳定的 opaque UUID：同一 attempt 崩溃恢复写回同一键，`MediaAsset` 按 `object_key + hash` 幂等登记；新的重试 attempt 使用新键，保证历史媒体不可变。
- `direct-uploads/...`、`styling-garments/...`、`generation-tasks/...`、`shoot-environment/...` 是历史键，只保持兼容读取和精确删除，不再产生新对象。
- 数字模特目前仍是聚合对象，使用 `model-profiles/<model-id>/...`，文件名清单由 model profile JSON 管理；删除只允许落到具体 model ID 目录。这是 0.1.0 唯一运行时例外，后续若模型素材需要跨对象复用，再迁为 `MediaAsset`，不能新增第三套媒体机制。
- 研究脚本只允许写入 `experiments/research/...`，不得作为生产业务素材来源。

未完成导入会话默认保留 168 小时（`APP_UPLOAD_SESSION_TTL_HOURS`），过期且无有效 lease 时标记 `ABANDONED`，并按该会话登记的每个对象精确清理；每小时最多处理 20 个会话（`APP_UPLOAD_SESSION_CLEANUP_BATCH_SIZE`），避免历史积压阻塞 generation poller。任何已绑定 `media_asset_id` 或已登记到 `media_assets.object_key` 的对象均排除，RustFS 暂时不可用时保留待重试状态。`GET /api/admin/storage-diagnostics` 使用 `limit + offset` 分页完成全量巡检；未确认导入的 `PENDING` 项不是“丢失文件”，只有 `UPLOADED/READY` 对象不可读才告警。

## 图片事实源

- 数字模特参考图负责人物身份事实。
- 主推款和搭配服装图片负责商品事实。
- 分镜预览只负责构图、姿态、裁切、背景和组图一致性。
- 最终成片必须回看商品图和模特图，不能只复刻分镜预览。
