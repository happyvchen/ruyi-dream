# Backend Architecture

## 职责

后端是 FastAPI + SQLite + S3 兼容对象存储的一体化服务，负责：

- 素材与媒体资产管理；
- DeepSeek（ToAPIs 或官方通道）打标和 AI 智能选搭；
- Prompt 维护槽位读取与 runtime 占位渲染；
- 统一背景、独立姿势选搭、结果集级冻结快照和 GPT Image-2.5 Sunburst edits 最终成片任务包组装；
- provider attempt 快照、进度、错误分类和轮询恢复；
- 生成记录、归档、删除和导出。

文本与视觉理解继续复用 `app/gemini_provider.py` 和 `GEMINI_PROVIDER` 开关：`toapis` 使用 `GEMINI_TOAPIS_MODEL=deepseek-v4.1-flash` 与 `TOAPIS_API_KEY`；`deepseek` 直连官方，使用独立 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL=https://api.deepseek.com`、`DEEPSEEK_MODEL=deepseek-flash`；`direct` 仍为 Gemini。代码缺省仍为 direct，示例配置仍为 toapis；不自动跨供应商回退，不改变 Image-2 配置。新调用记录保存实际模型来源、通道和模型 ID，历史记录不改写。

两个 chat 通道保留现有 Base64 图片、SSE、16 张客户端上限、JSON 解析与重试契约。成片提示词整理按参考图并集计算预算，超过 16 张时按完整镜头分批，每批成功结果立即冻结，失败或取消后继续复用；姿势选搭对两个通道采用相同拼图预算。官方请求体超过 48 MiB 在发送前拒绝；不额外发送思考或 JSON 模式参数，使用模型默认值。DeepSeek 的 HTTP 400/422 按格式/参数错误硬停，401/402 不重试，429/5xx 沿用有限恢复；只有所选通道的密钥可用于请求。

官方通道的连接/发送超时 `DEEPSEEK_CONNECT_TIMEOUT_SECONDS=60`、读取超时 `DEEPSEEK_TIMEOUT_SECONDS=300`、并发 `DEEPSEEK_MAX_CONCURRENCY=5` 独立配置，使用独立请求闸门；两个 chat 通道共用现有文本线程池与 `GEMINI_RETRY_*`、持久化退避和取消边界。ToAPIs 原全局闸门及图片调用保持原行为。

官方协议核对（2026-10-29）：[首次调用与模型名](https://api-docs.deepseek.com/)、[图片输入与限制](https://api-docs.deepseek.com/guides/vision/)、[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[错误码](https://api-docs.deepseek.com/quick_start/error_codes/)。`deepseek-flash` 当前对应 DeepSeek-V4.1-Flash-1017，支持图片输入；此处保留客户端 16 张预算，未扩大到官方上限。

AI 选搭的模型输出只含 `selected_files` 和最终方案业务字段，不要求 `schema_version` 或旧候选编号。后端按业务结构校验并补内部历史兼容标记，原始返回保留。选搭每次业务重问前及返回后检查任务/attempt 取消状态，取消后不再发起下一轮契约重问。

摄影棚与生成记录共用任务取消接口，批次取消复用该接口并清除排队目标。选搭、姿势选搭和成片提示词优化将当前 attempt 的取消检查传到文本请求层；每次请求前及取得并发槽位后重新检查，覆盖退避期间取消。图片生成在上传进度、实际提交、HTTP 重试、失败重生成和提交结果未知后的补提边界检查当前 attempt。已提交的图片仍允许查询和回传；旧 attempt 的晚到结果只进入历史，不覆盖取消后重启的当前成片。选搭异常处理也检查原 attempt，避免把新一轮任务改为失败。

取消排队只清除一键成片目标，工作协程启动时重新读取该目标。搭配服装“停止识别”仅支持持久化重试等待阶段，停止与恢复领取均使用条件更新；已经领取执行时返回 409。页面切换只中止 GET 读取，不代替业务取消，也不中断取消请求本身。上述取消为本地调度边界，不承诺撤回已经发出的 provider 请求；阻塞请求、并发等待或退避在到达下一检查点时退出。

## 当前生成链路

```text
导入主推款
-> AI 智能选搭
-> 结合商品图、全库标签与冻结成片规则，一次初筛直接选择五个不同姿势并冻结共享背景
-> 三类固定 Prompt + GPT Image-2.5 Sunburst edits 并发生成五张成片
```

后端不再包含动态剧本或当前分镜服务。`app/main.py` 中的新生成链路围绕 `StylingPlan`、`GenerationPoseSelection`、`GenerationResultSetShotPreset` 和 `GenerationResultImage` 编排；旧预览与旧拍摄预设版本数据只读兼容。

## Image-2 分阶段恢复

- 参考图导入失败只重试失败导入，已成功且标记为当前输入策略版本的 provider URL 通过缓存或 attempt 快照复用。
- ToAPIs 只接收运行时传输副本：官方长边/总像素上限按 `8192 / 4000 万` 校验，生产目标收紧为 `8000 / 3600 万`，同时压到 9.5 MiB 以内；始终等比缩放、透明底转白底，不写回或覆盖 S3 原始素材。缓存键携带 `input-v2`，升级策略后旧 URL 不会误复用。
- 新结果集在 `prompt_preparation_json.referenceCanvas` 冻结 `square_white_v1`，递归写入任务包每张参考的 `providerCanvas`。统一字节渲染路径供提示词整理与生图上传复用：EXIF方向校正、原有镜头裁切、居中补白到1:1、拼图，再对拼图补方；竖图补左右、横图补上下，不拉伸或增加裁切。ToAPIs继续执行上述体积/尺寸压缩，fal也执行补方。缓存与URL复用区分带画布策略的版本，整理附件去重也包含画布策略；历史结果集无该字段时保留原行为，不修改原素材或既有结果。
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
| 精确 `unknown provider for model` 或真正零输出且未分类的 provider 错误 | `PROVIDER_TRANSIENT_UNKNOWN` | 等待自动恢复 | 安全边界内有限重试；已有正文、reasoning、usage 或 finish reason 时不盲目重发 |
| 异步 `generation_failed`，且无更深层不可重试原因 | `PROVIDER_GENERATION_FAILED` | 再次生成 | 按 `GENERATION_SUBMIT_RETRY_LIMIT` 有限重建 |

API 摘要返回稳定 `error={code,title,message,action,retryable}`；普通用户只看可读标题、具体原因和下一步。数据库与 admin 技术详情保留原始 `technicalMessage`、provider 快照、顶层/上游错误码、providerRequestId 及 `providerReferenceImageManifest.providerInputPolicyVersion`。

官方依据：`https://docs.toapis.cn/docs/cn/api-reference/tasks/image-status`、`https://docs.toapis.cn/docs/cn/api-reference/images/gpt-image-2.5-sunburst/generation`、`https://docs.toapis.cn/docs/cn/api-reference/uploads/images`。

## 文本调用与上传资源隔离

阻塞的 ToAPIs 文本调用使用独立、有界的线程池，worker 容量沿用 Python 原默认 executor 的有界容量，不将较小的 HTTP 并发额度用作线程数，以免退避中的请求占满所有文本 worker；超出名额的工作留在 executor 队列，不创建等待线程。该池独立于上传、媒体处理及图片回传使用的 asyncio 默认 executor。HTTP 请求仍使用下述共享 ToAPIs 闸门，保持重试、取消检查和 HTTP 并发总量语义。

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
| `app/main.py` | API 路由、AI Stylist、背景与姿势、最终图和后台 poller 编排 |
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
- 统一背景负责场景与照明，独立姿势图负责动作与表现；三类固定成片 Prompt 负责五镜头景别、构图与禁止项。
- 最终成片必须同时参考商品图、模特图和当前镜头预设；不使用同组连续性锚点。

## 媒体与查询性能

列表走 `generation_task_summaries` / `generation_list_context`，批次列表统一复用摘要和批次元数据，查询次数不随任务数逐条增加。status 不装配媒体、选搭或历史快照；详情变化通过数据库 revision 触发器统一通知。`app/http_performance.py` 提供单层 gzip 和脱敏访问计量。

维护脚本 `scripts/repair_media_metadata.py` 默认按 ID 有界处理 50 条，优先未归档任务、常用搭配服装和当前预设；`--after-id` 续跑，`--verify` 同时核对已有元数据的对象，`--include-archived` 渐进补齐长尾。每条对象处理结束才短事务写回，不在网络/图片处理期间持有 SQLite 写锁。不会调用外部 AI/provider。

## 成片提示词优化

`app/prompt_optimization.py` 只组装 LLM 文本输入、按镜头读取返回正文，不审核内容或拼接锁定段落；`main.py` 复用现有 Gemini 通道与后台任务，先准备整组文本再并发生图。配置和结果保存在结果集 JSON，调用审计复用 attempts。默认关闭；不增加服务、队列、LLM 识图或自动视觉验收。约束以 [当前工作流](../design/01_generation_flow/current_generation_workflow.md#成片提示词优化) 为准。

## 姿势工作流扩展

`main.py` 管理姿势库、显式后台识别、初筛直接定选姿势与一键成片编排；`pose_selection.py` 只校验模型输出的五镜头、候选身份及图片归属。数据库迁移 076 保存素材与选择快照。运行 Prompt 来自数据库；`prompt_templates/pose_*.txt` 仅通过 admin 参考草稿接口供编辑器填入并显式保存。新任务顺序为服装选搭 → 姿势选搭 → 最终成片；取消、恢复、失效和引用释放契约详见姿势工作流设计。

## 已发布版本兼容

`app/generation_records.py` 负责从真实调用证据补齐已发布版本、逐镜头重放输入与旧参考图传输适配。数据库迁移只投影事实，不调用模型或对象存储；main 的发布事务同时更新 attempt、当前图片和版本。`scripts/audit_upgrade.py` 独立于应用配置与生命周期，提供升级前后只读盘点和清理预检。

## 可维护请求与紧凑过程事实（迁移 089）

模型指令只来自 Prompt 配置。`assembly.tagging_request`、`assembly.styling_request` 通过显式 `{{assembly.*}}` 引用原挑图／打标／选搭正文；渲染器处理占位符、条件和模板引用，不再在调用代码里拼接正文。原配置继续独立维护，新增请求模板展示完整发送结构。格式纠错、单张调整和旧参考板编号映射分别由 `assembly.contract_correction`、`assembly.image_retry`、`assembly.reference_transport` 托管。迁移一次性把旧代码中的结构和原有指令转入这些槽位，已有自定义正文不覆盖，运行时缺配置仍报错，不提供内置 fallback。

成片优化的 system 是 `assembly.final_prompt_optimizer` 原文；user 由 `assembly.final_prompt_optimizer_input` 渲染，变量仅含镜头完整正文、图号映射和附件角色。存储键、资产 ID、URL、文件名和裁切记录留在过程事实，不进入新调用的 user 文本。图片附件按冻结的传输版本组装；新结果集使用姿势宫格整组调用，历史版本保持去重、裁切和分批规则；优化正文直接用于生图，provider 不再隐式 trim 正文。

`app/call_facts.py` 对新写入及历史记录应用相同的保守瘦身：可执行镜头包不再复制配置集合和原始模板；结果集仅冻结所选模板及显式依赖，供未完成的渲染继续使用。优化批次只记录一份请求和原始响应，不再额外复制第一批 prompt、最后一批 raw_text 和可完全还原的解析结果。sourcePackages 的正文仅在与批次 sourcePrompt 完全相等时省略，读取历史测试上下文时按批次事实还原。图片来源、运行变量、未知字段、不同或无法解析的原始响应、provider 恢复状态和已发布版本保留。姿势选搭 audit 中的整库配置副本移除，实际姿势选择 plan 中供后续执行的冻结规则保留。

迁移 089 自动处理 attempts、已发布版本、镜头执行记录和结果集准备记录，不操作对象存储或调用 AI。`scripts/upgrade_database.py` 在停写维护窗口执行 SQLite 一致性备份、现有迁移、完整性检查和 VACUUM，释放已腾空页面；备份不自动删除。直接启动应用同样自动执行逻辑迁移，但不在在线启动中执行 VACUUM。

## 姿势选搭章节引用（迁移 090）

`runtime.pose_shots` 使用 `render_pose_shot_requirements`，只读取对应品类成片配置中的「## 机位&景别」。先按当前镜头展开模板条件，再定位唯一标题及其正文（到同级或更高标题结束），然后解析正文占位符。五个镜头都必须有有效条件正文；只有公共引导语、章节缺失、重复或正文占位符缺失均在发起模型调用前失败。完整成片仍使用同一冻结配置，摄影正文不会复制到选姿势输入。

Prompt 列表、保存结果和草稿预览都通过相同章节校验；姿势选搭预览按当前配置与样例/历史事实重建五条要求，历史实际发送独立展示、不回写。历史尝试没有旧运行时字段时，从原选搭及输入版本恢复事实。新调用仅在已有 runtime 中保留所需动态事实，不增设重复配置副本。

## 优化姿势宫格（迁移 091）

新结果集冻结 `optimizerReferenceLayout=pose_grid_v1`。`optimizer_reference_plan` 同时生成实际附件和纯事实映射，预览、审计及发送复用该函数。五个姿势保留各自裁切、补方图策略后合入固定 3×2 板；每镜头最终 `references.index` 不变，`visionImageIndex` 指向姿势板，`cell` 指定镜头号。`imageManifest` 只增加格号、镜头号、布局和空格事实，不注入业务指令。来源元数据留在过程事实。

背景、姿势板、身份单图、身份宫格、主推款、搭配款按首次出现顺序去重。当前满额14张；新版本超16张报错，不拆镜头。优化返回原样交生图，任务包的原始独立姿势输入不替换为参考板。恢复时只整理缺少成功输出的镜头，板中保留原格号，缺席镜头和第六格标 EMPTY。无版本标记的历史准备记录继续旧分批协议；已有成功包、单张重生和已提交 provider 请求均沿用冻结事实。

迁移091仅为非空的当前优化系统规则追加可维护的「优化阶段的姿势宫格与最终图号」章节，保留自定义正文与启用状态，不改历史配置或调用。不创建额外调用、不在运行代码中拼接指令。当前草稿预览/显式测试采用新协议，历史实际发送保留原记录。
