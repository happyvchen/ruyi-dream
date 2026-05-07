# frontend/ — Agent Guide

## 职责 (Scope)

Lily AI 模特组图生成 MVP 的前端实现目录。当前前端已经初始化为 React + Vite + TypeScript 的单页 MVP，用于承载普通用户的“拍摄任务工作台”：上传/选择服装、选择模特、确认组图方案、生成/重生/导出。

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
| Icons | lucide-react |

当前未引入 Tailwind、React Query、Zustand。不要为了“规范化”提前引入复杂状态库或后台管理框架。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `architecture.md` | 当前前端架构：页面结构、状态分工、API 分层、文件位置 | 修改前端代码前 |
| `ui-guideline.md` | UI 规则：工作台布局、用户可见信息、隐藏复杂性、结果状态 | 设计页面和组件时 |
| `src/App.tsx` | 当前单页 MVP 主实现 | 修改页面和交互时 |
| `src/api.ts` | 后端 API 调用封装 | 修改接口联调时 |
| `src/types.ts` | 前端领域类型 | 修改 API 响应或状态结构时 |

## 核心前端职责

- 展示服装货号和素材预览
- 展示/确认数字模特
- 展示商品识别摘要和必要修正入口
- 展示默认 5 张组图方案
- 发起生成、查看进度、单张重生
- 确认最终图片并导出

## 本地规则 (Local Rules)

- 前端实现前先读 `../design/03_ux_guide/CLAUDE.md`。
- 接口联调前先读 `../backend/api_guideline.md`、`../backend/domain_model.md`。
- 默认不展示 prompt、Image 2 任务包、策划器细节。
- 页面语言使用摄影和电商语言：首图、全身、半身、背面、局部、重生、导出。
- 不把 MVP 做成复杂 SaaS 后台。
- 不把 `dist/`、`node_modules/`、`*.tsbuildinfo` 当作业务源码或设计依据。
- 新增前端文档或目录时更新本文件索引。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计源头：`../design/CLAUDE.md`
- UX 源头：`../design/03_ux_guide/CLAUDE.md`
- 后端实现：`../backend/CLAUDE.md`
