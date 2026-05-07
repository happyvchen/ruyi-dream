# Frontend Architecture

## 产品形态

前端是一个面向普通业务用户的“拍摄任务工作台”，不是 AI 参数控制台。

默认主流程：

```text
服装素材页上传/编辑图包 -> 保存默认组图方案 -> 生成组图页选择货号 -> 直接生成/重生/导出
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
| `generate` | 生成工作台：选择货号，读取默认组图方案，触发生成、重生、最终图、导出 |
| `garments` | 货号图包上传、预览与编辑、素材管理、默认组图方案生成/更新 |
| `models` | 数字模特档案、头图上传、角度维护、参数维护 |
| `history` | 事件日志和生成记录 |

`BrandTemplates` 当前没有独立页面，Lily 默认品牌规则由后端 planner 和设计文档承载。

## 状态分工

- Server state：货号、模特、默认 shot plan、generation run、event logs。
- Client state：当前页面、选中货号、当前默认方案、当前生成批次、上传预览图包、模特编辑草稿、loading/error。
- 当前使用 React local state；后续数据流复杂后再考虑引入 React Query。

## 组件/区域

- 生成工作台两步卡片：选择服装、使用默认方案。
- 生成结果网格：单张状态、重生、设为最终图。
- 服装素材页：导入新图包、待保存图包、货号列表、素材图片卡片、默认组图方案、识别摘要、编辑信息。
- 上传预览卡片和已保存素材卡片使用统一视觉结构：文件名、图片尺寸、文件大小、删除按钮。
- 数字模特页：多模特列表、基础参数、头部参考图上传/替换/排序/角度维护。
- 记录页：事件日志。

## API 分层

前端只通过 `src/api.ts` 调用后端，不在组件中散落裸 `fetch`。API 契约以 `../backend/api_guideline.md` 为准，领域字段以 `../backend/domain_model.md` 和 `src/types.ts` 对齐。
