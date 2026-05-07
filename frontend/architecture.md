# Frontend Architecture

## 产品形态

前端是一个面向普通业务用户的“拍摄任务工作台”，不是 AI 参数控制台。

默认主流程：

```text
服装素材页上传/编辑图包 -> 保存默认组图方案 -> 生成组图页选择货号 -> 直接生成/重生/导出 -> 生成记录复盘/恢复
```

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `src/App.tsx` | 单页 MVP 主界面、轻量路由、生成组图、服装素材、数字模特、生成记录 |
| `src/api.ts` | API base、请求封装、导出下载 |
| `src/types.ts` | 前端类型定义 |
| `src/styles.css` | 全局样式 |
| `src/main.tsx` | React 入口 |

## 当前页面

| 页面 | 职责 |
|---|---|
| `generate` | 生成工作台：选择货号，读取默认组图方案，触发生成、重生、最终图、导出 |
| `garments` | 货号图包上传、预览与编辑、素材管理、默认组图方案生成/更新 |
| `models` | 数字模特档案、头图上传、角度维护、参数维护 |
| `history` | 任务级生成记录、任务详情、入参快照、图位 attempts、高级调试 JSON |

`BrandTemplates` 当前没有独立页面，Lily 默认品牌规则由后端 planner 和设计文档承载。

## 前端路由

前端使用浏览器 History API 做轻量路由，不额外引入路由依赖。URL 用来表达可恢复位置，React state 继续承载临时交互状态。

当前路由：

| URL | 页面/状态 |
|---|---|
| `/generate` | 生成组图工作台 |
| `/generate?sku={sku}` | 生成组图页，选中指定货号 |
| `/generate?run={runId}` | 生成组图页，恢复指定历史任务 |
| `/garment-skus` | 服装素材列表 |
| `/garment-skus/{sku}` | 服装素材页，打开指定货号图包 |
| `/model-profiles` | 数字模特列表/编辑页 |
| `/model-profiles/{modelId}` | 数字模特页，打开指定模特 |
| `/generation-runs` | 生成记录列表 |
| `/generation-runs/{runId}` | 生成记录页，打开指定任务详情 |

路由规则：

- SKU 使用业务货号。
- 数字模特使用 `modelId`。
- 生成任务使用 `runId`。
- 顶部 Tab、列表选择和恢复历史任务都会同步 URL。
- 刷新页面后通过 URL 重新加载对应对象。
- 弹窗、折叠面板、导入草稿、hover 等临时 UI 状态不进入 URL。

## 状态分工

- Server state：货号、模特、默认 shot plan、generation run、generation image attempts、event logs。
- URL state：当前 Tab、选中 SKU、选中模特、选中生成任务、生成页恢复 run。
- Client state：上传预览图包、模特编辑草稿、loading/error、折叠/弹窗等临时状态。
- 当前使用 React local state；后续数据流复杂后再考虑引入 React Query。

## 组件/区域

- 生成工作台两步卡片：选择服装、使用默认方案。
- 生成结果网格：单张状态、重生、设为最终图。
- 服装素材页：导入新图包、待保存图包、货号列表、素材图片卡片、默认组图方案、识别摘要、编辑信息。
- 上传预览卡片和已保存素材卡片使用统一视觉结构：文件名、图片尺寸、文件大小、删除按钮。
- 数字模特页：多模特列表、基础参数、头部参考图上传/替换/排序/角度维护。
- 生成组图页右侧记录概览：最近任务缩略图、货号、状态、完成张数、记录号、重生次数；点击后在生成组图页恢复历史任务。
- 生成记录页：任务列表、任务详情、入参快照、输出与重生过程、attempts、高级调试 JSON。

## 生成记录语义

前台“生成记录”的主粒度是 `GenerationRun`：

- 每次整组生成一条记录。
- 同素材包重复生成一次，产生另一条记录。
- 单张重生不新建任务记录，追加到对应 `GenerationImage.attempts`。
- `event_logs` 仍保留为后端审计/排查信息，但不作为普通用户的生成记录主视图。

生成记录列表接口使用 `GET /api/generation-runs`，详情使用 `GET /api/generation-runs/{run_id}`。

## API 分层

前端只通过 `src/api.ts` 调用后端，不在组件中散落裸 `fetch`。API 契约以 `../backend/api_guideline.md` 为准，领域字段以 `../backend/domain_model.md` 和 `src/types.ts` 对齐。
