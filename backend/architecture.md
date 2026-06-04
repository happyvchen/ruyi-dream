# Backend Architecture

后端负责把主推款、搭配服装库、数字模特、Prompt 配置和生成任务组织成可追踪的 AI 摄影棚生产链路。当前实现为 FastAPI 单体应用，SQLite 存储元数据，媒体统一进入 S3 兼容对象存储。

## 代码结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | API 路由、AI Stylist、ShootScript、分镜预览、最终图和后台 poller 编排 |
| `app/db.py` | SQLite 初始化、默认模特 seed、迁移、事件日志 |
| `app/image_provider.py` | Image-2 provider adapter；默认 toapis，保留 fal adapter |
| `app/image_derivatives.py` | thumb / preview WebP 衍生图 |
| `app/object_storage.py` | S3/RustFS 对象访问、预签名 URL、删除和读取 |
| `app/config.py` | 环境变量和运行配置 |

历史 `app/planner.py` 已删除。后端不再提供旧货号组图规划器、历史 `garment-skus`、历史 `shot-plans` 或历史 `generation-runs` 主流程。

## 业务模块

| 模块 | 职责 |
|---|---|
| `styling-garment` | 搭配服装库、图片、Gemini 打标、生效标签和 READY 状态 |
| `generation-task` | 主推款任务、输入版本、StylingPlan、ShootScript、ShotPlan、预览和成片 |
| `model-profile` | 数字模特身份图、身体参数和默认模特 |
| `prompt-config` | 可维护 Prompt 模板、品牌分段、生成规则和运行时占位符 |
| `generation` | Image-2 任务提交、状态恢复、单张重试和结果下载 |

## 主流程

```text
POST /api/generation-tasks
  -> 创建草稿任务
  -> 上传主推款图片到 media_assets
  -> 保存可选的补充服装信息或拍摄要求

POST /api/generation-tasks/{id}/ready
  -> 冻结草稿，进入 READY_FOR_STYLING

POST /api/generation-tasks/{id}/select-styling
  -> 创建 GenerationTaskInputVersion
  -> 冻结上传补充要求和本次微调要求
  -> 读取 READY 搭配服装库
  -> 构造全量标签事实 candidate_retrieval prompt DTO
  -> 构造 user_requirement prompt DTO
  -> 调用 AI Stylist 生成 StylingPlan
  -> 保存完整 StylingPlan、选中服饰快照和 attempt

POST /api/generation-tasks/{id}/shoot-script
  -> 构造 StylingPlan 导演上下文
  -> 调用 AI Director 生成 ShootScript
  -> 派生 6 条 GenerationShotPlan

POST /api/generation-tasks/{id}/preview
  -> 读取 ShootScript 上下文
  -> Image-2 生成 3x2 分镜预览

POST /api/generation-tasks/{id}/generate-final
  -> 固定读取前 5 个 ShotPlan
  -> 每张图只传当前单镜头上下文
  -> 并发调用 Image-2

POST /api/generation-tasks/{id}/auto-run-final
  -> 记录 auto_run_target = FINAL
  -> 从当前第一个未完成阶段自动推进到成片
  -> 复用当前 active StylingPlan / ShootScript / 分镜预览
  -> FINAL_PARTIAL 时只补失败或缺失的最终图
```

`一键成片` 只是一层自动编排壳。手动按钮和自动推进共用相同阶段实现、artifact 表、attempt 表、active 指针和失败语义。任一阶段失败或用户取消时，自动推进标记会清除；再次点击 `一键成片` 会根据当前 active 产物断点续跑。

## Prompt Context

后端区分“审计快照”和“进入模型的 prompt DTO”：

- `generation_attempts.request_snapshot_json` 可以保存技术排查需要的 request/response 快照。
- 传给模型的 runtime 只给当前步骤必需信息，不透传完整历史阶段、完整候选池或历史 prompt。
- AI Stylist 输入是未归档 READY 搭配库全量标签事实卡和 `user_requirement`。用户要求优先于默认搭配偏好，但不能覆盖商品事实、搭配库事实、品牌硬边界、输出 schema 和安全规则。
- AI Director 输入是 `styling_result_for_prompt` + `model_identity_context_for_prompt`，只包含主推款理解、选中 outfit、选中服饰紧凑标签、理由、质量信号、warnings 和数字模特身份契约。
- 分镜预览输入是 `shoot_script_result_for_prompt` + `image_reference_manifest_for_prompt`，只包含全局导演、配饰、风险、6 个镜头、输入参考图序号和事实源。
- 最终成片输入是 `current_shot_context_for_prompt` + `image_reference_manifest_for_prompt`，只包含当前格子、当前镜头、全局导演、选中 outfit、本镜头配饰、输入参考图序号和事实源。

数字模特参考图是人物身份事实源。后端必须把 `model_identity` 参考图前置进 Image-2 输入，并在 prompt 中声明它们覆盖 ShootScript、分镜预览和品牌妆发文字；分镜预览只允许作为构图、姿势和光影参考。

不实现独立 prompt 长度预算监控；0.1.0 通过结构化 DTO 和删除历史注入来控制上下文体量。

Prompt 正文只来自 `prompt_configs.content`。后端只初始化空正文的可维护 Prompt 槽位，不在代码、迁移或初始化中保存 Prompt 默认正文；缺少 Prompt 配置或正文为空时应暴露配置错误，而不是使用代码字符串 fallback。

## 媒体与恢复

对象存储保存原图，应用层登记 `thumb`、`preview`、`original` 三层媒体结构。前端访问稳定 `/api/media?key=...&variant=...`，后端校验 key 后跳转对象存储短期 URL。

`generation_previews` 和 `generation_result_images_new` 使用 `locked_by` / `locked_until` / `run_attempts` 做单实例恢复。后端重启后，poller 扫描锁过期任务，标记历史 attempt 失败并重建 attempt 继续执行。带有 `auto_run_target = FINAL` 的任务会在非运行中状态下重新调度自动推进。

## 约束

- Image-2 只负责视觉生成，不承担标签、选搭、历史状态或业务策略。
- 用户不写长 prompt；用户可填写简短业务要求，并确认拍摄信息、造型方案、分镜预览和结果。
- 0.1.0 之前的逻辑、库表和数据不作为运行时读取路径。
