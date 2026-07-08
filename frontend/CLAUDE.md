# frontend/ — Agent Guide

## 职责 (Scope)

ruyi-dream 0.1.0 的前端实现目录。当前前端是 React + Vite + TypeScript 单页应用，用于承载普通用户的“AI 摄影棚”：上传主推款、展示带审美诊断的 AI Stylist 推荐造型方案、从搭配服装库自动选搭或判断无需库内配角、生成只读拍摄剧本、生成前 5 个镜头成片、复盘生成记录。

UX 源头在 `../design/03_ux_guide/user_uiux.md`，本目录只管“前端怎么组织代码”和“界面规则怎么落地”。

## Tech Stack

当前前端实际技术栈：

| Layer | Technology |
|---|---|
| Build tool | Vite 7 |
| UI framework | React 19 |
| Language | TypeScript 5.9 |
| Styling | `src/styles.css` 原生 CSS |
| State | React local state |
| Routing | Browser History API 轻量路由 |
| Icons | lucide-react |

当前未引入 Tailwind、React Query、Zustand。不要为了“规范化”提前引入复杂状态库或后台管理框架。

访问身份当前由固定 `user` / `admin` 双账号区分，两个角色展示同一套普通拍摄工作台。admin 新增模块必须以“admin UI/UX 大于等于 user”为原则追加，不削减 user 已有工作流；不要引入完整后台管理框架、用户管理系统或复杂 RBAC。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `architecture.md` | 当前前端架构：页面结构、状态分工、API 分层、文件位置 | 修改前端代码前 |
| `ui-guideline.md` | UI 规则：工作台布局、用户可见信息、隐藏复杂性、结果状态 | 设计页面和组件时 |
| `src/App.tsx` | 当前单页主实现 | 修改页面和交互时 |
| `src/routes.ts` | 轻量路由解析和 URL 序列化 | 修改深链接或页面路由时 |
| `src/api.ts` | 后端 API 调用封装 | 修改接口联调时 |
| `src/types.ts` | 前端领域类型 | 修改 API 响应或状态结构时 |

## 核心前端职责

- `AI 摄影棚`：创建主推款 `GenerationTask`，推进 AI Stylist 造型方案、拍摄剧本和最终成片。
- `搭配服装库`：导入真实配角服装 `StylingGarment`，系统默认启动打标；列表展示后端从 AI 返回中最佳努力派生的摘要，详情展示完整 AI 打标 JSON，不向普通用户展示底层模型调用快照。
- `数字模特`：只读展示当前默认数字模特。
- `Prompt 维护`：admin-only 页面，维护品牌分段、AI 打标、AI 选搭、拍摄剧本和生成成片等自包含 Prompt。品牌边界、AI 选搭原则、品类策略、质量信号、拍摄导演规则、配饰边界和库充足性口径直接写入对应 Prompt 正文。
- `生成记录`：展示进入过生成阶段的 `GenerationTask`，聚焦入参、搭配和结果。
- 默认不展示 prompt、Image-2 任务包、provider 请求、模型名称、调用快照等复杂细节。

## 本地规则 (Local Rules)

- 前端实现前先读 `../design/03_ux_guide/CLAUDE.md`。
- 接口联调前先读 `../backend/api_guideline.md`、`../backend/domain_model.md`。
- 默认不展示 prompt、Image 2 任务包、StylingPlan JSON、策划器细节、底层模型名称和调用详情。
- 不再提供独立 `AI 策略配置` 入口；开发人员在 `Prompt 维护` 中直接编辑 `AI 选搭` Prompt。
- 不再提供独立 `标签规则` 或 `导演规则` Prompt 配置入口；AI 打标、拍摄剧本和下游生成规则都在 `Prompt 维护` 中维护。
- 后续技术人员配置、provider 状态和调试入口只能作为 admin 的增量模块接入，不混入 user 体验，也不能替代普通拍摄工作台。
- 生成记录主视图展示进入过 AI 阶段的 `GenerationTask`，不要退回事件日志流水账。
- 删除入口默认呈现为“归档”；用户勾选彻底删除时必须展示后端影响预检结果，说明关联记录和文件会被不可恢复地删除。
- 核心业务对象切换要同步 URL；临时草稿、弹窗、折叠状态不要写入 URL。
- 页面语言使用摄影和电商语言：首图、全身、半身、背面、局部、重生、导出。
- 不把 0.1.0 做成复杂 SaaS 后台。
- 不把 `dist/`、`node_modules/`、`*.tsbuildinfo` 当作业务源码或设计依据。
- 新增前端文档或目录时更新本文件索引。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计源头：`../design/CLAUDE.md`
- UX 源头：`../design/03_ux_guide/CLAUDE.md`
- 后端实现：`../backend/CLAUDE.md`
