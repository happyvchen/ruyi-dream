# Frontend Architecture

## 产品形态

前端是一个面向普通业务用户的“拍摄任务工作台”，不是 AI 参数控制台。

默认主流程：

```text
选择服装 -> 选择模特 -> 确认组图方案 -> 生成/确认/导出
```

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `src/App.tsx` | 单页 MVP 主界面：生成组图、服装素材、数字模特、生成记录 |
| `src/api.ts` | API base、请求封装、导出下载 |
| `src/types.ts` | 前端类型定义 |
| `src/styles.css` | 全局样式 |
| `src/main.tsx` | React 入口 |

## 当前页面

| 页面 | 职责 |
|---|---|
| `generate` | 主工作台，承载上传、策划、生成、重生、导出 |
| `garments` | 货号图包列表和素材预览 |
| `models` | 数字模特档案查看 |
| `history` | 事件日志和生成记录 |

`BrandTemplates` 当前没有独立页面，Lily 默认品牌规则由后端 planner 和设计文档承载。

## 状态分工

- Server state：货号、模特、shot plan、generation run、event logs。
- Client state：当前页面、选中货号、选中模特、当前方案、当前生成批次、上传表单、loading/error。
- 当前使用 React local state；后续数据流复杂后再考虑引入 React Query。

## 组件/区域

- 工作台三步卡片：服装、模特、组图方案。
- 生成结果网格：单张状态、重生、设为最终图。
- 服装素材页：上传和列表/详情。
- 数字模特页：默认模特档案。
- 记录页：事件日志。

## API 分层

前端只通过 `src/api.ts` 调用后端，不在组件中散落裸 `fetch`。API 契约以 `../backend/api_guideline.md` 为准，领域字段以 `../backend/domain_model.md` 和 `src/types.ts` 对齐。
