# backend/ — Agent Guide

## 职责 (Scope)

Lily AI 模特组图生成 MVP 的后端实现目录。当前仅预置文档骨架，后续用于承载 API、生成任务编排、素材索引、任务包生成、结果管理等服务端代码。

设计层面的产品范围、生成职责和技术路线在 `../design/`，本目录只管“代码怎么写”和“后端契约怎么落地”。

## 建议 Tech Stack

当前尚未初始化后端工程。若无额外要求，建议保持轻量：

| Layer | Technology |
|---|---|
| Runtime | Node.js 22+ 或 Python 3.12+ |
| API | Fastify / NestJS 或 FastAPI |
| Storage | 本地文件系统优先，后续可换对象存储 |
| Metadata | SQLite / PostgreSQL，MVP 可先 SQLite |
| AI Provider | 通过 adapter 封装 Image 2 / VLM / 推理模型调用 |

正式选型前先与用户确认，不预先引入复杂 SaaS 权限、多租户、计费、队列集群。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `architecture.md` | 后端架构草案：模块边界、数据对象、服务分层、生成任务编排 | 初始化或实现后端代码前 |
| `api_guideline.md` | API 契约草案：资源、路由、任务状态、错误处理、文件路径规则 | 设计接口或前后端联调前 |

## 核心后端职责

- 读取/索引本地服装货号图包
- 维护数字模特档案和素材引用
- 生成商品事实卡，并支持人工确认/修正
- 调用组图策划器生成 shot plan
- 组装单张 Image 2 任务包
- 管理生成任务状态、单张重生、结果导出
- 保存实验/生产结果的元数据

## 本地规则 (Local Rules)

- 后端实现前先读 `../design/CLAUDE.md`。
- 不把品牌策略、Pose 选择、图位规划写死在 prompt 字符串里，应有结构化 task package。
- 不擅自删除、覆盖、移动用户素材和生成结果。
- MVP 不做多租户、复杂权限、计费、模板市场。
- 新增后端文档或目录时更新本文件索引。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计源头：`../design/CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`

