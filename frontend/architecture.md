# Frontend Architecture

## 产品形态

前端是一个面向普通业务用户的“拍摄任务工作台”，不是 AI 参数控制台。

默认主流程：

```text
AI 摄影棚上传主推款 -> AI 智能选搭 -> 六宫格预览 -> 生成前 5 张成片 -> 生成记录复盘
```

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `src/App.tsx` | 单页 MVP 主界面、AI 摄影棚、搭配服饰库、数字模特、生成记录 |
| `src/api.ts` | API base、请求封装、导出下载 |
| `src/types.ts` | 前端类型定义 |
| `src/styles.css` | 全局样式 |
| `src/main.tsx` | React 入口 |
| `Dockerfile` | 多阶段构建：Node 构建静态资源，Nginx 提供 SPA |
| `nginx.conf` | SPA fallback 与前端健康检查 |

## 当前页面

| 页面 | 职责 |
|---|---|
| `studio` | AI 摄影棚：上传主推款、智能选搭、六宫格预览、生成成片 |
| `library` | 搭配服饰库：导入真实配角服装、自动打标、搜索和标签摘要 |
| `model` | 数字模特：只读展示当前默认模特 |
| `records` | 生成记录：进入过 AI 阶段的任务、输入快照、选搭、预览、结果；技术 JSON 默认折叠 |

`BrandTemplates` 当前没有独立页面，默认品牌规则由后端 planner 和设计文档承载。

## 前端路由

前端使用浏览器 History API 做轻量路由，不额外引入路由依赖。URL 用来表达可恢复位置，React state 继续承载临时交互状态。

当前 `App.tsx` 内部使用本地页面状态切换 `studio`、`library`、`model`、`records`，尚未把新版页面状态同步到 URL。旧版 `/garment-skus`、`/generation-runs` 路由文件仍在仓库中，不能作为当前页面事实来源。

## 状态分工

- Server state：`StylingGarment`、`GenerationTask`、默认模特、生成 attempts。
- Client state：当前页面、主推款上传草稿、搭配服饰导入草稿、loading/error、折叠状态。
- 当前使用 React local state；后续数据流复杂后再考虑引入 React Query。

## 组件/区域

- AI 摄影棚四步卡片：上传主推款、AI 智能选搭、六宫格预览、生成成片。
- 搭配服饰库：导入服饰、列表搜索、标签摘要、打标状态、归档。
- 数字模特页：默认模特只读展示。
- 生成记录页：任务列表、任务详情、ShotPlan 与 attempts 折叠展示。

## 生成记录语义

前台“生成记录”的主粒度是进入过 AI 阶段的 `GenerationTask`。列表接口使用 `GET /api/generation-records`，详情使用 `GET /api/generation-records/{task_id}`。`event_logs` 仍保留为后端审计/排查信息，但不作为普通用户的生成记录主视图。

## 媒体与轮询

前端按展示尺寸选择图片档位：

- 货号列表、历史列表、素材卡片、结果网格使用 `thumbUrl`。
- 弹窗查看和详情页大图使用 `previewUrl`。
- 下载、导出、AI 输入和必要质检才使用 `originalUrl`。

后端返回的是稳定 `/api/media?...` 路径，组件不应在轮询中主动替换未变化图片的 `src`。如果同一对象 key 没变，应保留现有 URL，避免浏览器重复解码和重绘。

生成中任务的刷新由轻量状态接口驱动。页面不可见时暂停轮询和计时刷新，恢复可见后立即拉取一次状态。切换货号时，`getSku` 和 `getDefaultPlan` 可并发；历史预览恢复使用 `GET /api/garment-skus/{sku_id}/latest-preview-run`，不通过拉取大量 `listRuns` 来查找最新预览。

## API 分层

前端只通过 `src/api.ts` 调用后端，不在组件中散落裸 `fetch`。API 契约以 `../backend/api_guideline.md` 为准，领域字段以 `../backend/domain_model.md` 和 `src/types.ts` 对齐。

Docker Compose 部署时，前端镜像构建阶段会把 `PUBLIC_BACKEND_URL` 写入 `VITE_API_BASE_URL`。如果部署域名、端口或反向代理路径变化，需要重新构建前端镜像：

```bash
docker compose up -d --build frontend
```

前端容器只提供静态资源，不代理 API；浏览器直接访问 `PUBLIC_BACKEND_URL`。
