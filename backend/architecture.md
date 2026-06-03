# Backend Architecture

后端负责把主推款、搭配服饰库、数字模特、品牌资料、Prompt 配置和生成任务组织成可追踪的 AI 摄影棚生产链路。当前实现为 FastAPI 单体应用，SQLite 存储元数据，媒体统一进入 S3 兼容对象存储。

## 代码结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | API 路由、AI Stylist、ShootScript、分镜预览、最终图和后台 poller 编排 |
| `app/db.py` | SQLite 初始化、默认模特 seed、迁移、事件日志 |
| `app/image_provider.py` | Image-2 provider adapter；默认 toapis，保留 fal adapter |
| `app/image_derivatives.py` | thumb / preview WebP 衍生图 |
| `app/object_storage.py` | S3/RustFS 对象访问、预签名 URL、删除和读取 |
| `app/config.py` | 环境变量和运行配置 |

旧 `app/planner.py` 已删除。后端不再提供旧货号组图规划器、旧 `garment-skus`、旧 `shot-plans` 或旧 `generation-runs` 主流程。

## 业务模块

| 模块 | 职责 |
|---|---|
| `styling-garment` | 搭配服饰库、图片、Gemini 打标、生效标签和 READY 状态 |
| `generation-task` | 主推款任务、输入版本、StylingPlan、ShootScript、ShotPlan、预览和成片 |
| `model-profile` | 数字模特身份图、身体参数和默认模特 |
| `brand-profile` | 单一品牌资料 |
| `shoot-config` | AI Director 全局规则和 6 镜头规则 |
| `prompt-config` | 可维护 Prompt 模板和运行时占位符 |
| `generation` | Image-2 任务提交、状态恢复、单张重试和结果下载 |

## 主流程

```text
POST /api/generation-tasks
  -> 创建草稿任务
  -> 上传主推款图片到 media_assets

POST /api/generation-tasks/{id}/ready
  -> 冻结草稿，进入 READY_FOR_STYLING

POST /api/generation-tasks/{id}/select-styling
  -> 创建 GenerationTaskInputVersion
  -> 读取 READY 搭配服饰库
  -> 构造紧凑 candidate_retrieval prompt DTO
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
```

## Prompt Context

后端区分“审计快照”和“进入模型的 prompt DTO”：

- `generation_attempts.request_snapshot_json` 可以保存技术排查需要的 request/response 快照。
- 传给模型的 runtime 只给当前阶段必需信息，不透传完整历史阶段、完整候选池或旧 prompt。
- AI Stylist 输入是搭配库紧凑候选卡：`code/category/colors/materials/fit/silhouette/length/wearing_capabilities/style_summary/pairing_risks/manual_flags/review`。
- AI Director 输入是 `styling_result_for_prompt`，只包含主推款理解、选中 outfit、选中服饰紧凑标签、理由、质量信号和 warnings。
- 分镜预览输入是 `shoot_script_result_for_prompt`，只包含全局导演、配饰、风险和 6 个镜头。
- 最终成片输入是 `current_shot_context_for_prompt`，只包含当前格子、当前镜头、全局导演、选中 outfit 和本镜头配饰。

不实现独立 prompt 长度预算监控；本轮优化通过结构化 DTO 和删除旧兼容注入来控制上下文体量。

## 媒体与恢复

对象存储保存原图，应用层登记 `thumb`、`preview`、`original` 三层媒体结构。前端访问稳定 `/api/media?key=...&variant=...`，后端校验 key 后跳转对象存储短期 URL。

`generation_previews` 和 `generation_result_images_new` 使用 `locked_by` / `locked_until` / `run_attempts` 做单实例恢复。后端重启后，poller 扫描锁过期任务，标记旧 attempt 失败并重建 attempt 继续执行。

## 约束

- Image-2 只负责视觉生成，不承担标签、选搭、历史状态或业务策略。
- 用户不写长 prompt；用户只确认拍摄信息、造型方案、分镜预览和结果。
- 旧逻辑、旧表和旧数据不再兼容读取。
