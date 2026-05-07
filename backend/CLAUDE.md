# backend/ — Agent Guide

## 职责 (Scope)

Lily AI 模特组图生成 MVP 的后端实现目录。当前后端已经初始化为 FastAPI + SQLite + 本地文件系统的 MVP，实现服装图包上传、默认组图方案、Gemini 组图策划、GPT Image-2 Provider 占位生成、单张重生、最终图标记、ZIP 导出和事件留痕。

设计层面的产品范围、生成职责和技术路线在 `../design/`，本目录只管“代码怎么写”和“后端契约怎么落地”。

## Tech Stack

当前后端实际技术栈：

| Layer | Technology |
|---|---|
| Runtime | Python 3.14（本地 `.venv`） |
| API | FastAPI + Uvicorn |
| Storage | 本地文件系统：`backend/storage/` |
| Metadata | SQLite：`backend/storage/lily_mvp.sqlite3` |
| Planner | Gemini API，失败时 fallback 本地默认方案 |
| Image Provider | `app/image_provider.py`，当前 GPT Image-2 三方平台占位输出 SVG |

依赖清单见 `requirements.txt`。不要预先引入复杂 SaaS 权限、多租户、计费、队列集群。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `architecture.md` | 当前后端架构：模块边界、代码位置、服务分层、生成任务编排 | 修改后端代码前 |
| `api_guideline.md` | 当前 API 契约：路由、资源、任务状态、错误处理 | 设计接口或前后端联调前 |
| `domain_model.md` | 当前 MVP 实际业务对象、Domain 边界、对象关系、文件系统对象 | 理解业务建模或新增后端模块前 |
| `database_schema.md` | 当前 SQLite 表结构、字段说明、关系图、后续候选表 | 修改数据库、写迁移或排查数据问题前 |
| `requirements.txt` | Python 依赖清单 | 安装、升级依赖时 |
| `app/` | FastAPI 后端源码 | 修改后端实现时 |

## 核心后端职责

- 保存和管理本地服装货号图包
- 维护数字模特档案和素材引用
- 根据服装主图生成商品摘要
- 调用组图策划器生成货号默认 shot plan
- 组装单张 Image 2 任务包
- 管理生成任务状态、单张重生、结果导出
- 保存实验/生产结果的元数据

## 本地规则 (Local Rules)

- 后端实现前先读 `../design/CLAUDE.md`。
- 修改业务对象前先读 `domain_model.md`；修改表结构前先读 `database_schema.md`。
- 不把品牌策略、Pose 选择、图位规划写死在 prompt 字符串里，应有结构化 task package。
- 不擅自删除、覆盖、移动用户素材和生成结果。
- MVP 不做多租户、复杂权限、计费、模板市场。
- 不把 `.venv/`、`__pycache__/`、`storage/` 运行产物当作业务源码或设计依据。
- 新增后端文档或目录时更新本文件索引。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计源头：`../design/CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
