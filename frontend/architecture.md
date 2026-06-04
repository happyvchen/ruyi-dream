# Frontend Architecture

## 产品形态

前端默认是一个面向普通业务用户的“拍摄任务工作台”，不是 AI 参数控制台。当前 `user` 和 `admin` 登录后展示同一套工作台；后续 admin 增量模块必须建立在完整 user 工作台之上，保证 admin UI/UX 大于等于 user。

默认主流程：

```text
AI 摄影棚上传主推款 -> AI 智能选搭 -> 生成拍摄剧本 -> 生成分镜预览 -> 生成前 5 个镜头成片 -> 生成记录复盘
```

`一键成片` 是工作台顶部主操作，用于把上述步骤自动串联到最终成片。它不引入新的前端状态模型：页面仍通过 `GenerationTask.status`、active 指针和各阶段 artifact 渲染，自动流程只显示一条轻量进度。

上传主推款时允许在右侧任务摘要填写简短 `商品信息`；其它阶段可在同一个右侧模块填写当前步骤要求后重新执行。界面始终使用服装、拍摄和电商语言，不把该输入称为 prompt。

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `src/App.tsx` | 单页 MVP 主界面、AI 摄影棚、搭配服装库、数字模特、生成记录 |
| `src/api.ts` | API base、请求封装、导出下载 |
| `src/types.ts` | 前端类型定义 |
| `src/styles.css` | 全局样式 |
| `src/main.tsx` | React 入口 |
| `Dockerfile` | 多阶段构建：Node 构建静态资源，Nginx 提供 SPA |
| `nginx.conf` | SPA fallback 与前端健康检查 |

## 当前页面

| 页面 | 职责 |
|---|---|
| `studio` | AI 摄影棚：上传主推款、智能选搭、拍摄剧本、分镜预览、生成成片 |
| `library` | 搭配服装库：导入真实配角服装、自动打标、搜索、完整标签详情 |
| `model` | 数字模特：只读展示当前默认模特 |
| `brand` | 品牌信息：admin-only 单记录维护页面，初始为空，不支持双语字段 |
| `shoot-config` | 拍摄配置：admin-only 维护 AI Director 全局规则、默认 6 镜头规则和标签一级品类的 6 镜头覆盖规则；支持 Prompt 预览、JSON 导入导出 |
| `records` | 生成记录：从草稿开始的历史拍摄任务、输入信息、选搭、预览、结果 |
`BrandTemplates` 当前没有独立页面，默认品牌规则由后端 planner 和设计文档承载。

## 前端路由

前端使用浏览器 History API 做轻量路由，不额外引入路由依赖。URL 用来表达可恢复位置，React state 继续承载临时交互状态。

当前新版路由为 `/studio`、`/studio?task={taskId}`、`/library`、`/model`、admin-only 的 `/brand`、`/records?task={taskId}`。进入 `/studio` 默认显示全新的拍摄任务页面，不自动恢复最近进度；只有新建任务后、或用户点选某个历史任务进入 `/studio?task={taskId}` 时，才按 URL 恢复任务。当前拍摄任务和记录详情写入 URL，可在刷新或分享后恢复；上传中尚未保存的本地文件和折叠状态仍只保存在 React state。

登录后前端读取 `/api/auth/session` 返回的 `role`。`admin` 在保留全部 user 路由和工作流的基础上额外看到 `品牌信息`、`标签规则`、`拍摄配置` 入口；普通 `user` 不展示这些配置入口。`拍摄配置` 排在 `标签规则` 后面，因为品类 6 镜头规则只能从标签一级品类中选择。

搭配服装批量导入使用两阶段前端流程：先逐套上传并创建服务端 `StylingGarment` 记录，再提交 Gemini 标签识别。导入入口支持连续批次：用户点击 `导入` 后，当前非重复项进入进度区，上传入口立即释放，可继续选择下一批文件夹。已存在 code 的重复项不上传、不入库、不打标，保留在导入面板等待用户改 code 后再次导入；修改已有服饰图片必须进入详情页完成。上传阶段刷新会丢失尚未上传的浏览器本地文件；单套保存成功后，标签识别是否完成不再影响数据恢复。

## 状态分工

- Server state：`StylingGarment`、`GenerationTask`、默认模特、生成 attempts。
- Client state：当前页面、主推款上传草稿、搭配服装导入草稿、loading/error、折叠状态。
- 当前使用 React local state；后续数据流复杂后再考虑引入 React Query。

## 组件/区域

- AI 摄影棚五步卡片：创建草稿并编辑主推款图片、确认输入、AI 智能选搭、生成拍摄剧本、生成分镜预览、生成成片。
- AI 摄影棚右侧任务摘要在 `最终图` 下方固定一个用户输入模块：上传主推款时编辑商品信息；其它步骤时编辑当前步骤要求。所有输入都有保存按钮，保存后只作为下一次执行草稿，不改变任务状态。
- 搭配服装库：导入服饰、列表搜索、标签摘要、打标状态、详情抽屉、归档。
- 搭配服装详情抽屉：完整展示 AI/生效/人工/官方标题/元信息标签；不向普通用户展示底层模型请求与返回快照。
- 数字模特页：默认模特只读展示。
- 品牌信息页：admin 维护单一品牌资料，保存后供后续选搭与 ShotPlan 参考；普通用户不可见。
- 生成记录页：任务列表、任务详情、分镜预览与最终成片展示。

## 生成记录语义

前台“生成记录”和 AI 摄影棚“最近任务”使用同一套 `GenerationTask` 列表口径：任务从草稿创建开始进入列表。列表接口使用 `GET /api/generation-records`，详情使用 `GET /api/generation-records/{task_id}`。`event_logs` 仍保留为后端审计/排查信息，但不作为普通用户的生成记录主视图。

AI 摄影棚“最近任务”、生成记录和搭配服装库默认按创建时间倒序展示；状态推进、打标完成、修改名称或追加图片不改变列表位置。

AI 摄影棚轮询当前 `GenerationTask` 时，右侧“最近任务”中对应任务必须同步更新状态、缩略图摘要和完成数量，但不能因为状态变化重新排序。当前任务详情和最近任务行使用同一次任务更新结果做 upsert，避免主内容区显示已完成而最近任务仍停留在旧状态。

AI 摄影棚右侧“最近任务”使用 `/api/generation-records` 分页加载，滚动到列表底部时继续取下一页，并在底部显示已显示数量、总数、加载中或已触底提示。最近任务行点击直接进入 `/studio?task={taskId}` 恢复工作台状态。

生成记录列表的行主体负责查看详情；行尾恢复按钮按任务状态显示 `查看进度`、`再次生成` 或 `继续生成`，点击后只跳转到 AI 摄影棚并恢复任务，不直接触发任何生成接口。

## 媒体与轮询

前端按展示尺寸选择图片档位：

- 货号列表、历史列表、素材卡片、结果网格使用 `thumbUrl`。
- 弹窗查看和详情页大图使用 `previewUrl`。
- 下载、导出、AI 输入和必要质检才使用 `originalUrl`。

后端返回的是稳定 `/api/media?...` 路径，组件不应在轮询中主动替换未变化图片的 `src`。如果同一对象 key 没变，应保留现有 URL，避免浏览器重复解码和重绘。

AI 摄影棚在 `STYLING_RUNNING`、`SCRIPT_RUNNING`、`PREVIEW_RUNNING`、`FINAL_RUNNING` 状态定时刷新当前 `GenerationTask`，从而展示阶段推进和逐张结果。若任务带有 `autoRunTarget`，即使短暂处于非运行中状态也继续刷新，等待后端自动推进下一步。

前端可用本地临时 `AIOperation` 补足用户刚点击后的即时反馈，但本地 operation 必须带当前 `taskId`，只能渲染到所属任务。任务从后端刷新到非运行中且无 `autoRunTarget` 后，应清理该任务的本地 operation。右侧任务摘要的运行进度以当前任务 active 链路为准，不展示其它任务或旧结果集的历史 attempt。

## API 分层

前端只通过 `src/api.ts` 调用后端，不在组件中散落裸 `fetch`。API 契约以 `../backend/api_guideline.md` 为准，领域字段以 `../backend/domain_model.md` 和 `src/types.ts` 对齐。

Docker Compose 部署时，前端镜像构建阶段会把 `PUBLIC_BACKEND_URL` 写入 `VITE_API_BASE_URL`。如果部署域名、端口或反向代理路径变化，需要重新构建前端镜像：

```bash
docker compose up -d --build frontend
```

前端容器只提供静态资源，不代理 API；浏览器直接访问 `PUBLIC_BACKEND_URL`。
