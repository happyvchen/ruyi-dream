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
上传主推款
-> AI 智能选搭
-> 三类固定 Prompt 生成分镜预览
-> 分镜前 5 格生成成片
```

后端不再包含动态剧本服务。`app/main.py` 中的生成相关代码只围绕 `StylingPlan`、`GenerationPreview` 和 `GenerationResultImage` 编排。

## Image-2 分阶段恢复

- 参考图上传失败只重试失败上传，已成功上传的 provider URL 通过缓存或 attempt 快照复用。
- 每个图片 attempt / 生成重试轮次向 ToAPIs 传稳定 `client_business_id`。创建任务遇到连接中断、`ReadTimeout` 或 TLS EOF 时标记为 `SUBMISSION_UNKNOWN`，默认等待 15 秒后先按该业务 ID 查询原任务；查到即继续，明确 `task_not_found` 才补提，查询仍不确定时停止，避免重复任务和重复计费。
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
| `internal_error` / HTTP 500 | `PROVIDER_INTERNAL_ERROR` | 稍后再试 | 按 `GENERATION_SUBMIT_RETRY_LIMIT` 有限重建 |
| 异步 `generation_failed`，且无更深层不可重试原因 | `PROVIDER_GENERATION_FAILED` | 再次生成 | 按 `GENERATION_SUBMIT_RETRY_LIMIT` 有限重建 |

API 摘要返回稳定 `error={code,title,message,action,retryable}`；普通用户只看可读标题、具体原因和下一步。数据库与 admin 技术详情保留原始 `technicalMessage`、provider 快照、顶层/上游错误码和 providerRequestId。

官方依据：`https://docs.toapis.com/docs/cn/api-reference/tasks/image-status`、`https://docs.toapis.com/docs/cn/api-reference/images/gpt-image-2/official`、`https://docs.toapis.com/docs/cn/api-reference/uploads/images`。

## ToAPIs 并发闸门

ToAPIs 相关调用统一使用 `TOAPIS_MAX_CONCURRENCY` 控制最大并发，默认 `5`。该闸门覆盖 Gemini(ToAPIs) chat、Image-2(ToAPIs) 参考图上传、任务创建和状态查询；批次一键成片不再有独立并发参数，而是按同一 ToAPIs 预算推进排队任务。

旧环境变量 `GENERATION_AUTO_RUN_MAX_CONCURRENCY` 仅作为过渡 fallback：未设置 `TOAPIS_MAX_CONCURRENCY` 时才读取。新部署和文档统一使用 `TOAPIS_MAX_CONCURRENCY`。

## 主要模块

| 文件 | 职责 |
|---|---|
| `app/main.py` | API 路由、AI Stylist、分镜预览、最终图和后台 poller 编排 |
| `app/db.py` | 最终 schema、Prompt 配置槽登记、清理迁移 |
| `app/prompt_render.py` | 动态占位文本和参考图图号区间渲染 |
| `app/image_provider.py` | ToApis / fal 请求快照和图片生成适配 |
| `app/toapis_concurrency.py` | ToAPIs 全局并发闸门 |
| `app/generation_progress.py` | attempt 阶段进度和错误分类 |

## 图片事实源

- 数字模特参考图负责人物身份事实。
- 主推款和搭配服装图片负责商品事实。
- 分镜预览只负责构图、姿态、裁切、背景和组图一致性。
- 最终成片必须回看商品图和模特图，不能只复刻分镜预览。
