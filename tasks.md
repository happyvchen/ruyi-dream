# Lily AI 模特组图生成 MVP — Project Tasks

> 项目级任务跟踪。设计源头见 `design/CLAUDE.md`；前后端实现规范见 `frontend/CLAUDE.md` 和 `backend/CLAUDE.md`。

## 整体进度

| 模块 | 设计 | 后端 | 前端 | 联调 |
|---|---|---|---|---|
| Product Scope | ✅ 已整理 | N/A | N/A | N/A |
| Generation Flow | ✅ 已整理 | ✅ MVP 骨架已实现 | ✅ MVP 工作台已实现 | ✅ 本地已跑通 |
| Brand & Assets | ✅ 已整理 | ✅ 默认模特与 Lily 规则占位 | ✅ 页面摘要展示 | ✅ Gemini 策划已跑通 |
| User Workspace UX | ✅ 已整理 | N/A | ✅ 上传预览编辑/默认方案/生成/重生 | ✅ 本地已跑通 |
| Export / History | 🔶 待细化 | ✅ 事件日志 + ZIP 导出 | ✅ 记录区 + 导出入口 | ✅ 本地已跑通 |

## 下一步建议

- [x] 确认前端/后端技术栈
- [x] 初始化前端工程
- [x] 初始化后端工程
- [x] 定义最小 API 契约
- [x] 做 `125309C7948510` 的端到端本地 MVP 验证

## 当前 MVP 实现说明

- 前端：`frontend/`，React + Vite，主页面为服装素材、生成组图、数字模特、生成记录。
- 后端：`backend/`，FastAPI + SQLite + 本地文件系统。
- 上传：服装图片先在浏览器本地预览与编辑，支持散图、单文件夹、多文件夹；保存后复制到 `backend/storage/uploads/{sku}-{timestamp}-{id}/`，不会修改原始素材。
- 默认方案：保存上传后创建货号默认 `ShotPlan`；服装素材页可更新默认方案；生成组图页直接使用默认方案。
- 推理：`Gemini API` 负责根据服装主图生成 `garment + brandStyle + shots` 简单 JSON；失败时回落到本地默认方案。
- 生图：`GPT Image-2` 三方平台 Provider 已占位，当前输出 SVG 占位图，任务包、请求、响应、重生原因完整入库。
- 记录：上传、策划、生成、重生、设为最终图、导出都会写入 SQLite 的 `event_logs` 和 `backend/storage/logs/*.jsonl`。
- 导出：`POST /api/exports/runs/{run_id}` 会打包最终图；如果未标记最终图，则打包该 run 下全部图。
