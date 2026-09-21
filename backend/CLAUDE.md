# backend/ — Agent Guide

## 职责 (Scope)

ruyi-dream 0.1.0 的后端实现目录。当前后端是 FastAPI + SQLite + S3 兼容对象存储的 AI 摄影棚实现，覆盖搭配服装打标、主推款任务流、AI Stylist / StylingPlan、当前生效拍摄预设与结果集冻结快照、三类固定成片 Prompt 路由、GPT Image-2.5 Sunburst edits 成片生成和结果复盘。

设计层面的产品范围、生成职责和技术路线在 `../design/`，本目录只管“代码怎么写”和“后端契约怎么落地”。

## Tech Stack

当前后端实际技术栈：

| Layer | Technology |
|---|---|
| Runtime | Python 3.14（本地 `.venv`） |
| API | FastAPI + Uvicorn |
| Storage | S3 兼容对象存储，私有化默认 RustFS |
| Metadata | SQLite：`backend/data/app.sqlite3` |
| AI Stylist | Gemini（`app/gemini_provider.py`，`GEMINI_PROVIDER` 切换：`toapis` 走 ToAPIs OpenAI 兼容中转，`direct` 走 google-genai 直连；代码缺省 `direct` 兼容存量部署，`.env.example` 和本项目部署配置为 `toapis`）；瞬时技术失败有限自动恢复，终态错误记录事件日志 |
| Image Provider | `app/image_provider.py`，默认 ToAPIs GPT Image-2.5 Sunburst；保留 fal Image-2 queue 提交、状态查询、结果下载 |

生产直接依赖见 `requirements.txt`，测试依赖入口见 `requirements-dev.txt`，传递依赖约束见 `requirements.constraints.txt`。不要预先引入复杂 SaaS 权限、多租户、计费、队列集群。

访问控制当前使用固定 `user` / `admin` 双账号框架，不建立用户表、权限组或租户模型。两个角色访问同一套普通拍摄工作台；admin 增量能力必须以“admin UI/UX 大于等于 user”为前提，并在后端按 session role 做权限判断。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `architecture.md` | 当前后端架构：模块边界、代码位置、服务分层、生成任务编排 | 修改后端代码前 |
| `api_guideline.md` | 当前 API 契约：路由、资源、任务状态、错误处理 | 设计接口或前后端联调前 |
| `domain_model.md` | 当前 0.1.0 实际业务对象、Domain 边界、对象关系、对象存储布局 | 理解业务建模或新增后端模块前 |
| `database_schema.md` | 当前 SQLite 表结构、字段说明、关系图、后续候选表 | 修改数据库、写迁移或排查数据问题前 |
| `../design/04_implementation_guide/archive_purge_policy.md` | 归档、彻底删除、媒体引用计数和 OSS 清理策略 | 修改删除、归档、批量清理、媒体替换时 |
| `requirements.txt` / `requirements-dev.txt` / `requirements.constraints.txt` | Python 生产直接依赖、测试依赖入口与传递依赖约束 | 安装、升级依赖时 |
| `app/` | FastAPI 后端源码 | 修改后端实现时 |
| `scripts/` | 一次性维护脚本，如媒体衍生图元数据补齐 | 修复历史数据或运维补数时 |
| `tests/` | 后端 smoke tests | 修改鉴权、迁移、任务状态时 |

## 核心后端职责

- 保存和管理主推款任务图片、搭配服装库图片和媒体资产
- 维护数字模特档案，以及上装/下装/套装各五个当前生效的拍摄母版、姿势拍摄预设；单图更换立即生效，结果集创建时冻结当次五组资产
- 维护 admin-only Prompt 配置；系统初始化可创建空正文的可维护 Prompt 槽位，品牌边界通过 `segment.brand_info` Prompt 分段承载，AI 选搭原则、品类策略、质量信号、配饰边界和空库内搭配取舍直接写入 `AI 选搭` Prompt 正文，后端不解释其中业务规则，也不内置或 fallback 任何 Prompt 默认正文
- 管理 AI 摄影棚主推款任务、输入版本和状态机
- 对未归档 READY 搭配服装生成全量标签事实卡；Prompt 只接收业务 `code` 和 `ai_tags` 等必要事实，不接收内部 ID、图片快照或后端排序/评分结果
- 调用 AI Stylist 生成带 Aesthetic Critic 诊断的 `StylingPlan`，保存完整 request/response 快照
- 基于 `StylingPlan.hero_understanding.shot_category_family` 确定性选择三类成片 Prompt 和当前生效拍摄预设；当前类型不完整时拒绝开始成片，并组装 Image-2 多参考图编辑任务包
- 管理生成任务状态、单张重试、结果下载和记录复盘
- 保存实验/生产结果、紧凑候选快照、完整 StylingPlan 和 provider attempts 元数据

## 本地规则 (Local Rules)

- 后端实现前先读 `../design/CLAUDE.md`。
- 修改业务对象前先读 `domain_model.md`；修改表结构前先读 `database_schema.md`。
- AI 打标的规则、字段和输出结构由 `AI 打标` Prompt 自包含维护；后端必须完整保存任意可解析 JSON 返回，并最佳努力派生 `effective_tags_json`，不要重新假设固定标签枚举输出。
- AI 智能选搭的业务内容全部进入 Prompt 维护：主推款理解、搭配选择、品类语义、质量信号、空库内搭配比较、配饰边界和品牌判断都由 Prompt + 大模型完成；Prompt 输入输出统一使用搭配服装 `code`，后端只做 READY/归档/删除数据范围、code 到内部 ID 的持久化映射、schema、状态机、快照和 provider 调用。
- 后端只允许初始化空正文的 Prompt 槽位元数据，不得存储 Prompt 默认正文，也不得在缺少或空正文时使用代码字符串 fallback；缺配置或空正文必须作为配置错误暴露，Prompt 正文只能来自 `prompt_configs.content`。
- 不把品牌策略、选搭权重、品类互斥/降权、质量信号阈值、配饰边界、Pose 选择、图位规划写死在不可维护的代码字符串里；这些业务口径进入 Prompt 维护，主要由对应 Prompt 正文表达，不作为 Python 执行逻辑。
- 不擅自删除、覆盖、移动用户素材和生成结果。
- 删除/归档实现必须遵守 `../design/04_implementation_guide/archive_purge_policy.md`：默认归档，彻底删除先做影响预检，媒体文件按引用计数释放。
- 0.1.0 不做多租户、复杂权限、计费、模板市场。
- 不把固定双账号框架扩展成注册、邀请、成员管理或复杂 RBAC，除非产品范围明确升级。
- 不把 `.venv/`、`__pycache__/`、`data/`、`storage/` 运行产物当作业务源码或设计依据。
- 新增后端文档或目录时更新本文件索引。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计源头：`../design/CLAUDE.md`
- 前端实现：`../frontend/CLAUDE.md`
