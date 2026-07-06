# Backend API Guideline

API 面向前台 AI 摄影棚工作台，不暴露 prompt 工程细节。`/api/garment-skus`、`/api/shot-plans`、`/api/generation-runs`、`/api/generation-images` 和 `/api/ai-policy-config` 不属于 0.1.0 API。

## 认证与基础

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/auth/login` | POST | 登录 |
| `/api/auth/logout` | POST | 登出 |
| `/api/auth/session` | GET | 当前会话 |
| `/api/media` | GET | 稳定媒体访问入口 |
| `/api/uploads/presign` | POST | 为浏览器直传对象存储创建短期 PUT 签名 |
| `/api/logs` | GET | 事件日志 |

## 搭配服装库

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/styling-garments` | GET | 列表 |
| `/api/styling-garments` | POST | 创建搭配服装并上传图片 |
| `/api/styling-garments/{id}` | GET | 详情 |
| `/api/styling-garments/{id}` | PATCH | 编辑状态、人工标签和归档信息 |
| `/api/styling-garments/{id}/images` | POST | 追加图片 |
| `/api/styling-garment-images/{image_id}` | PATCH | 调整图片 |
| `/api/styling-garment-images/{image_id}` | DELETE | 删除图片链接 |
| `/api/styling-garments/{id}/save-and-tag` | POST | 保存并发起 AI 打标 |
| `/api/styling-garments/batch-save-and-tag` | POST | 批量保存并发起 AI 打标 |

打标会保存完整 `ai_tags_json` 和 attempt 快照；选搭阶段把 READY 搭配服装的完整 `ai_tags` 事实卡提供给 AI Stylist。

## AI 摄影棚任务

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/generation-tasks` | GET | 任务列表 |
| `/api/generation-tasks/from-uploads` | POST | 使用已直传到对象存储的主推款图片创建草稿任务 |
| `/api/generation-tasks/{id}` | GET | 任务详情 |
| `/api/generation-tasks/{id}` | PATCH | 编辑任务名、商品信息和各阶段用户输入草稿；用户输入只影响下一次阶段执行 |
| `/api/generation-tasks/{id}` | DELETE | 归档或删除任务 |
| `/api/generation-tasks/{id}/images/from-uploads` | POST | 使用已直传到对象存储的图片追加主推款图片 |
| `/api/generation-task-images/{image_id}` | PATCH | 调整主推款图片 |
| `/api/generation-task-images/{image_id}` | DELETE | 删除主推款图片 |
| `/api/generation-tasks/{id}/ready` | POST | 冻结草稿，进入选搭 |
| `/api/generation-tasks/{id}/select-styling` | POST | 调用 AI Stylist，可携带本次用户介入要求 |
| `/api/generation-tasks/{id}/shoot-script` | POST | 调用 AI Director，可携带本次用户介入要求 |
| `/api/generation-tasks/{id}/preview` | POST | 生成 3x2 分镜预览，可携带本次用户介入要求 |
| `/api/generation-tasks/{id}/generate-final` | POST | 并发生成前 5 张成片，可携带本次用户介入要求 |
| `/api/generation-tasks/{id}/auto-run-final` | POST | 一键成片：复用已有有效中间结果并自动完成后续步骤 |
| `/api/generation-result-images-new/{image_id}/retry` | POST | 重生单张最终图，可携带本次用户介入要求 |
| `/api/generation-result-images-new/{image_id}/redeliver` | POST | 重新回传单张最终图：provider 已成功、仅回传失败时零成本重新拉取结果，不创建新的 provider 任务 |
| `/api/generation-records` | GET | 生成记录列表 |

所有会调用外部 AI/provider 的端点都必须由用户动作触发；本地验证和测试不得调用外部 AI/provider。`auto-run-final` 由一次用户动作触发，但内部仍按现有阶段创建独立 attempt，并在 request snapshot 中记录 `invocation.mode = auto_run`。

主推款图片上传契约：

- 前端必须先调用 `/api/uploads/presign` 获取短期 PUT URL，再由浏览器直接 PUT 到 S3 兼容对象存储，最后调用 `/api/generation-tasks/from-uploads` 或 `/api/generation-tasks/{id}/images/from-uploads` 登记已上传对象。
- 主推款图片不得通过前端上传到后端再由后端中转到对象存储；这会把大文件流量压回应用服务器，并掩盖 bucket CORS 或签名配置问题。
- 后端启动和签名前会确保对象存储 bucket 存在直传 CORS 规则；默认使用 `BACKEND_CORS_ORIGINS`，也可用 `APP_DIRECT_UPLOAD_CORS_ORIGINS` 单独配置。

一键成片规则：

- 不新增独立业务状态；失败、取消和部分完成沿用原阶段状态。
- 已有当前有效 `StylingPlan`、`ShootScript` 或分镜预览时复用。
- `FINAL_PARTIAL` 断点续跑只补失败或缺失的最终图。
- 任一运行中状态下，所有生成入口都应返回 409 或在前端禁用；只允许取消生成。
- 任务详情中的 `aiOperations` 只返回当前 active 链路的运行摘要。被新结果替代的旧结果集、旧分镜、旧剧本和旧选搭 attempt 不进入普通任务摘要；完整历史仅用于技术详情和审计。
- 新最终图结果集替代旧结果集时，旧结果集中仍处于 `QUEUED` / `RUNNING` 的单图和 attempt 必须收口为 `CANCELLED`。
- 已被替代的旧最终图后台任务即使晚于新结果集完成，也不得通过单图回写或 result set reconcile 恢复为当前 active 结果集。

## 配置

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/model-profiles` | GET/POST | 数字模特列表和创建 |
| `/api/model-profiles/{id}` | PATCH/DELETE | 编辑或删除数字模特 |
| `/api/model-profiles/{id}/head-references` | POST | 上传头部参考图 |
| `/api/prompt-configs` | GET | Prompt 配置列表 |
| `/api/prompt-configs/{key}/preview` | GET | 本地渲染预览，不调用模型 |
| `/api/prompt-configs/{key}` | PATCH | 保存 Prompt |

## Prompt Runtime

Prompt 配置可使用以下 runtime：

- `runtime.styling_garment`：AI 打标使用的当前搭配服装 code 和图片输入摘要。
- `runtime.candidate_retrieval`：AI Stylist 候选服饰全量标签事实卡。
- `runtime.hero_product_info`：跟随当前执行实际使用的主推款输入版本冻结的商品信息和业务事实补充；重新执行拍摄剧本、分镜预览、成片或单张重生时会冻结最新任务商品信息。
- `runtime.ai_styling_user_instruction`：AI 智能选搭本次执行的用户介入要求。
- `runtime.shoot_script_user_instruction`：拍摄剧本本次执行的用户介入要求。
- `runtime.storyboard_preview_user_instruction`：分镜预览本次执行的用户介入要求。
- `runtime.final_image_user_instruction`：最终成片或单张重生本次执行的用户介入要求。
- `runtime.styling_result`：AI Director 使用的选搭导演上下文。
- `runtime.model_identity_context`：AI Director 使用的数字模特身份契约和参考图上下文。
- `runtime.shoot_script_result`：分镜预览使用的拍摄剧本上下文。
- `runtime.current_shot_context`：最终成片单镜头上下文。
- `runtime.image_reference_manifest`：Image-2 输入参考图序号、角色和事实源优先级。

不再支持 `runtime.ai_policy`、`runtime.ai_policy_card`、历史 `runtime.styling_plan`、历史 `runtime.shot_plans` 等兼容占位符。相关规则应直接写入当前 Prompt 正文。

## 选搭输入 DTO

`runtime.candidate_retrieval` 中每个候选使用搭配服装 `code` 作为模型可见标识，并携带完整 `ai_tags` 事实卡：

```json
{
  "code": "LOOK-A",
  "ai_tags": {
    "一级品类": ["上装"],
    "色彩": ["白色"],
    "面料材质": ["梭织"],
    "穿着能力": ["可塞腰", "可外搭"],
    "风格语义": "通勤、清爽、轻量",
    "风险点": ["可能遮挡腰线"]
  },
  "manual_flags": {},
  "review": {"confidence": 0.86}
}
```

不要把内部 id、图片快照、后端排序/评分、历史 prompt 或历史 response 作为 AI Stylist 的业务输入。

## 生成输入原则

- 主推款和选中搭配服装图片是商品事实来源。
- 数字模特参考图是人物身份事实来源，必须作为 `model_identity` 前置输入并写入 `image_reference_manifest`。
- 分镜预览是构图参考，不是商品事实来源，也不是模特身份事实来源。
- ShootScript、分镜预览、品牌妆发和 provider 默认审美不得覆盖数字模特的脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色和气质。
- Image-2 prompt 只给当前步骤必要结构；最终单张图只给当前镜头上下文。
- 第 6 个 ShotPlan 只用于分镜预览，不进入最终成片。

## 用户输入原则

- 用户输入不是自由长 prompt，而是业务语言中的服装事实补充、拍摄要求或选搭微调。
- 用户输入分为两类：跟随主推款输入版本的 `heroProductInfo`，以及每个阶段本次执行的 `userInstruction`。
- `heroProductInfo` 在创建/更新任务时保存到 `generation_tasks.hero_product_info_text`；进入 AI Stylist 前会冻结到 `generation_task_input_versions.hero_product_info_json`，重新执行拍摄剧本、分镜预览、成片或单张重生时也会冻结最新商品信息，允许用户跳过前序阶段，直接让新商品信息影响接下来执行的阶段。
- 每个阶段的用户输入草稿保存到 `generation_tasks.*_user_instruction_text`，保存动作不改变任务状态、不清空 active artifact。
- `userInstruction` 在每次阶段执行时分别保存到阶段 artifact 的 `user_instruction_json`，并同步写入 `generation_attempts.request_snapshot_json`，用于追溯多次重生过程。
- `auto-run-final` 自动推进各阶段时必须读取同一份任务级用户输入草稿，不能静默忽略已保存的阶段要求。
- 用户要求优先于默认搭配偏好和导演偏好；不得覆盖商品图片事实、搭配库 READY 范围、数字模特身份契约、品牌硬边界、输出 schema 和安全规则。
- AI Stylist 输出必须包含 `user_requirement_resolution`，说明已满足项、未满足项、冲突项和替代处理。
- AI Director 不重新选择搭配服装，只承接 StylingPlan 中已解析的用户要求和选搭结论。
- 上传页允许在非运行中状态继续 CRUD 主推款图片；修改主推款图片或排序会保留旧过程记录，但清空当前 active artifact 并让任务回到待选搭。单独保存商品信息或阶段用户输入不会隐藏已生成结果，只影响下一次阶段执行。
