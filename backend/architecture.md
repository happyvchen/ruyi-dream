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
- 创建任务遇到连接中断、`ReadTimeout` 或 TLS EOF 时，内部标记为 `SUBMISSION_UNKNOWN`；默认等待 15 秒后仅重提当前镜头 1 次。
- 同批成片累计 2 次提交未知后触发熔断，后续镜头不再放大自动重提；阈值可通过环境变量调整。
- 已取得 provider task ID 后只恢复状态查询；provider 明确瞬时生成失败时仅重试生成阶段。
- provider 成功后的下载失败只重试下载；本地对象存储、数据或代码错误标记为 `SYSTEM_ERROR`，不自动重试。
- 提交未知、生成重试次数和 provider ID 历史均持久化在 attempt 快照中，自动恢复预算耗尽后才向用户呈现部分失败。

## 主要模块

| 文件 | 职责 |
|---|---|
| `app/main.py` | API 路由、AI Stylist、分镜预览、最终图和后台 poller 编排 |
| `app/db.py` | 最终 schema、Prompt 配置槽登记、清理迁移 |
| `app/prompt_render.py` | 动态占位文本和参考图图号区间渲染 |
| `app/image_provider.py` | ToApis / fal 请求快照和图片生成适配 |
| `app/generation_progress.py` | attempt 阶段进度和错误分类 |

## 图片事实源

- 数字模特参考图负责人物身份事实。
- 主推款和搭配服装图片负责商品事实。
- 分镜预览只负责构图、姿态、裁切、背景和组图一致性。
- 最终成片必须回看商品图和模特图，不能只复刻分镜预览。
