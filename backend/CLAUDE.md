# backend/ — Agent Guide

## 职责 (Scope)

ruyi-dream MVP 的后端实现目录。当前后端是 FastAPI + SQLite + S3 兼容对象存储的新版 AI 摄影棚实现，覆盖搭配服饰打标、主推款任务流、AI Stylist / StylingPlan、拍摄剧本 / ShootScript、Image-2 分镜预览/成片生成、结果复盘。旧版货号组图、旧 planner、旧生成批次和旧表不再兼容。

设计层面的产品范围、生成职责和技术路线在 `../design/`，本目录只管“代码怎么写”和“后端契约怎么落地”。

## Tech Stack

当前后端实际技术栈：

| Layer | Technology |
|---|---|
| Runtime | Python 3.14（本地 `.venv`） |
| API | FastAPI + Uvicorn |
| Storage | S3 兼容对象存储，私有化默认 RustFS |
| Metadata | SQLite：`backend/data/app.sqlite3` |
| AI Stylist / Director | Gemini API；失败直接返回错误并记录事件日志 |
| Image Provider | `app/image_provider.py`，GPT Image-2 provider 适配；默认 toapis，保留 fal queue 提交、状态查询、结果下载 |

直接依赖见 `requirements.txt`，传递依赖约束见 `requirements.constraints.txt`。不要预先引入复杂 SaaS 权限、多租户、计费、队列集群。

访问控制当前使用固定 `user` / `admin` 双账号框架，不建立用户表、权限组或租户模型。本阶段两个角色访问同一套普通拍摄工作台；新增 admin 能力必须以“admin UI/UX 大于等于 user”为前提，并在后端按 session role 做权限判断。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `architecture.md` | 当前后端架构：模块边界、代码位置、服务分层、生成任务编排 | 修改后端代码前 |
| `api_guideline.md` | 当前 API 契约：路由、资源、任务状态、错误处理 | 设计接口或前后端联调前 |
| `domain_model.md` | 当前 MVP 实际业务对象、Domain 边界、对象关系、对象存储布局 | 理解业务建模或新增后端模块前 |
| `database_schema.md` | 当前 SQLite 表结构、字段说明、关系图、后续候选表 | 修改数据库、写迁移或排查数据问题前 |
| `../design/04_implementation_guide/archive_purge_policy.md` | 归档、彻底删除、媒体引用计数和 OSS 清理策略 | 修改删除、归档、批量清理、媒体替换时 |
| `requirements.txt` / `requirements.constraints.txt` | Python 直接依赖与传递依赖约束 | 安装、升级依赖时 |
| `app/` | FastAPI 后端源码 | 修改后端实现时 |
| `scripts/` | 一次性维护脚本，如媒体衍生图元数据补齐 | 修复历史数据或运维补数时 |
| `tests/` | 后端 smoke tests | 修改鉴权、迁移、任务状态时 |

## 核心后端职责

- 保存和管理主推款任务图片、搭配服饰库图片和媒体资产
- 维护数字模特档案和素材引用
- 维护 admin-only 单一品牌资料，供后续选搭、拍摄剧本和 ShotPlan 作为结构化品牌参考
- 维护 admin-only Prompt 配置；AI 选搭原则、品类策略、质量信号、配饰边界和空库内搭配取舍直接写入 `AI 选搭` Prompt 正文，后端不解释其中业务规则，也不内置、seed 或 fallback 任何 Prompt 默认值
- 管理 AI 摄影棚主推款任务、输入版本和状态机
- 对未归档 READY 搭配服饰生成全量标签事实卡；Prompt 只接收业务 `code` 和 `ai_tags` 等必要事实，不接收内部 ID、图片快照或后端排序/评分结果
- 调用 AI Stylist 生成带 Aesthetic Critic 诊断的 `StylingPlan`，保存完整 request/response 快照
- 基于 `StylingPlan` 生成 ShootScript / ShotPlan 和 Image-2 任务包
- 管理生成任务状态、单张重试、结果下载和记录复盘
- 保存实验/生产结果、紧凑候选快照、完整 StylingPlan 和 provider attempts 元数据

## 本地规则 (Local Rules)

- 后端实现前先读 `../design/CLAUDE.md`。
- 修改业务对象前先读 `domain_model.md`；修改表结构前先读 `database_schema.md`。
- AI 打标的规则、字段和输出结构由 `AI 打标` Prompt 自包含维护；后端必须完整保存任意可解析 JSON 返回，并最佳努力派生 `effective_tags_json`，不要重新假设固定 taxonomy 输出。
- AI 智能选搭的业务内容全部进入 Prompt 维护：主推款理解、搭配选择、品类语义、质量信号、空库内搭配比较、配饰边界和品牌判断都由 Prompt + 大模型完成；Prompt 输入输出统一使用搭配服饰 `code`，后端只做 READY/归档/删除数据范围、code 到内部 ID 的持久化映射、schema、状态机、快照和 provider 调用。
- 后端不得存储 Prompt 默认值，不得在迁移或初始化时 seed Prompt 配置，也不得在缺少 Prompt 配置时使用代码字符串 fallback；缺配置必须作为配置错误暴露，Prompt 正文只能来自 `prompt_configs`。
- 不把品牌策略、选搭权重、品类互斥/降权、质量信号阈值、配饰边界、Pose 选择、图位规划写死在不可维护的代码字符串里；这些业务口径进入 Prompt 维护，主要由对应 Prompt 正文表达，不作为 Python 执行逻辑。
- 不擅自删除、覆盖、移动用户素材和生成结果。
- 删除/归档实现必须遵守 `../design/04_implementation_guide/archive_purge_policy.md`：默认归档，彻底删除先做影响预检，媒体文件按引用计数释放。
- MVP 不做多租户、复杂权限、计费、模板市场。
- 不把固定双账号框架扩展成注册、邀请、成员管理或复杂 RBAC，除非产品范围明确升级。
- 不把 `.venv/`、`__pycache__/`、`data/`、`storage/` 运行产物当作业务源码或设计依据。
- 新增后端文档或目录时更新本文件索引。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计源头：`../design/CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
