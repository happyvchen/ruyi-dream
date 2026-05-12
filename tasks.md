# ruyi-dream MVP — Project Tasks

> 项目级任务跟踪。设计源头见 `design/CLAUDE.md`；前后端实现规范见 `frontend/CLAUDE.md` 和 `backend/CLAUDE.md`。

## 整体进度

| 模块 | 设计 | 后端 | 前端 | 联调 |
|---|---|---|---|---|
| Product Scope | 已整理 | N/A | N/A | N/A |
| Generation Flow | 已整理 | MVP 已实现 | 工作台已实现 | 已跑通 |
| Brand & Assets | 已整理 | 默认模特与品类规则已接入 | 页面摘要展示 | 已跑通 |
| User Workspace UX | 已整理 | N/A | 上传预览编辑/默认方案/生成/重生 | 已跑通 |
| Export / History | 已整理 | 事件日志 + ZIP 导出 | 记录区 + 导出入口 | 已跑通 |
| Object Storage | 已确定 | S3 兼容对象存储已接入，默认 RustFS | 预签名 URL 展示已支持 | 已验证 |

## 已完成

- [x] 前端工程：React + Vite + TypeScript。
- [x] 后端工程：FastAPI + SQLite。
- [x] 媒体存储：统一 S3 兼容对象存储，私有化默认 RustFS。
- [x] 数据库：默认 `backend/data/app.sqlite3`。
- [x] 旧数据迁移：旧 SQLite 与旧图片已迁移到新数据库和 RustFS bucket。
- [x] 旧目录清理：已删除旧实验资产目录、品牌分析目录和旧媒体目录。
- [x] 文档清理：已移除旧媒体存储方案、实验资产主路径和品牌样本目录说明。
- [x] 构建验证：后端 compileall、前端 `npm run build`、API smoke test 已通过。

## 当前实现说明

- 上传：浏览器本地预览与编辑，保存后后端写入对象存储 `uploads/{sku}-{timestamp}-{id}/`。
- 读取：后端返回对象存储短期预签名 URL，前端可直接显示私有图片；列表和详情按需分页加载。
- 媒体衍生图：上传素材、数字模特头像和生成结果保留原图，并生成 `thumb` / `preview` WebP 衍生图。
- 生成：生成任务的输出写入 `outputs/{sku}/{timestamp}/`，完成后生成对应缩略图和预览图。
- 导出：ZIP 写入 `exports/{sku}/`，同时通过接口流式返回给前端。
- 日志：业务事件写入 SQLite，并双写 `backend/data/logs/*.jsonl`。
- 部署：客户本地化部署默认使用 RustFS Docker；未来 SaaS 可把同一套 S3 配置切到 OSS、COS、AWS S3、GCS 兼容接口等。

## 后续建议

- [ ] 重生 attempt 与当前成功图分离，避免失败重生覆盖已成功结果。
- [ ] 上传安全校验补强：文件大小、数量、扩展名、真实图片解码。
- [ ] 健康检查收敛生产信息：默认不暴露绝对数据库路径。
- [ ] 生产级 Info / Warning / Error 架构：统一后端事件等级、API 错误码与用户可读文案、前端状态提示、任务失败原因展示、运维日志检索和可恢复操作指引，覆盖上传、策划、预览、生图、存储、导出等全链路。
- [ ] 生成失败诊断补强：区分 Gemini、fal 上传、fal queue、对象存储下载等错误来源。
- [ ] 策划层去除默认模特硬编码，让 `plan_json.modelId` 跟随当前选择的数字模特。
