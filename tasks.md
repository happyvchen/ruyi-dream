# ruyi-dream MVP — Project Tasks

> 项目级任务跟踪。设计源头见 `design/CLAUDE.md`；前后端实现规范见 `frontend/CLAUDE.md` 和 `backend/CLAUDE.md`。

## 整体进度

| 模块 | 设计 | 后端 | 前端 | 联调 |
|---|---|---|---|---|
| Product Scope | 已整理 | N/A | N/A | N/A |
| Generation Flow | 已整理；阶段 1 方案已补充 | 阶段 1 第一版已实现；阶段 2-4 未开始 | 工作台已实现；StylingPlan 复盘信号未完整展示 | 基础链路已跑通；335-338 新代码回归未跑 |
| Brand & Assets | 已整理；品类 ShotPlan 已补强 | 默认模特与品类规则已接入；官方样本库未实现 | 页面摘要展示 | 已跑通 |
| User Workspace UX | 已整理 | N/A | 上传预览编辑/默认方案/生成/重生 | 已跑通 |
| Export / History | 已整理 | 事件日志 + ZIP 导出 | 记录区 + 导出入口 | 已跑通 |
| Object Storage | 已确定 | S3 兼容对象存储已接入，默认 RustFS | 稳定媒体 URL 与三档图片展示已支持 | 已验证 |

## 已完成

- [x] 前端工程：React + Vite + TypeScript。
- [x] 后端工程：FastAPI + SQLite。
- [x] 媒体存储：统一 S3 兼容对象存储，私有化默认 RustFS。
- [x] 数据库：默认 `backend/data/app.sqlite3`。
- [x] 旧数据迁移：旧 SQLite 与旧图片已迁移到新数据库和 RustFS bucket。
- [x] 旧目录清理：已删除旧实验资产目录、品牌分析目录和旧媒体目录。
- [x] 文档清理：已移除旧媒体存储方案、实验资产主路径和品牌样本目录说明。
- [x] 媒体缓存优化：前台使用稳定 `/api/media` 路径，不再直接暴露每次变化的预签名 URL。
- [x] 三档图片策略：`thumb` 长边约 240px、`preview` 长边约 1280px、`original` 仅用于下载/导出/AI 输入。
- [x] 媒体元数据落库：上传素材和生成结果记录衍生图路径与三档文件大小，历史数据可用修复脚本补齐。
- [x] 生成轮询减重：新增轻量状态接口，普通详情默认不返回任务包、provider 响应和 attempts 调试数据。
- [x] 新版摄影棚草稿闭环：主推款任务先保存为草稿，AI 前支持名称与图片增删/替换/排序，确认后进入智能选搭。
- [x] 新版摄影棚历史一致性：输入版本冻结主推款和默认数字模特快照，预览/成片均读取冻结输入。
- [x] 新版摄影棚任务与记录支持 URL 恢复；失败最终图走单张重试。
- [x] 阶段 1 第一版选搭质量信号：同 `code` 主推款识别、主推款自己排除、空库内搭配比较、StylingPlan 候选/拒绝/质量信号快照、不支持塞腰记录为审核信号、品类化 ShotPlan。
- [x] 构建验证：后端 compileall、前端 `npm run build`、API smoke test 已通过。

## 当前实现说明

- 上传：浏览器本地预览与编辑，保存后后端写入对象存储 `uploads/{sku}-{timestamp}-{id}/`。
- 读取：后端返回稳定 `/api/media?key=...&variant=...` 路径，由媒体接口跳转到对象存储短期预签名 URL；列表和详情按需分页加载。
- 媒体衍生图：上传素材、数字模特头像和生成结果保留原图，并生成 `thumb` / `preview` WebP 衍生图；路径和文件大小落库。
- 历史媒体修复：`backend/scripts/repair_media_metadata.py` 可补齐旧素材和旧生成结果的衍生图与文件大小。
- 生成：生成任务的输出写入 `outputs/{sku}/{timestamp}/`，完成后生成对应缩略图和预览图；生成中轮询走轻量状态接口。
- AI 摄影棚：`GenerationTask` 草稿确认后进入选搭；进入 AI 后使用冻结主推款/搭配服装/数字模特快照推进六宫格和前 5 张成片。
- AI Stylist 阶段 1 第一版：未归档 READY 搭配服装库以全量标签事实卡进入 `assembly.ai_styling` Prompt；后端不做候选漏斗、库充足性判断、排序、评分或 Prompt 默认值 fallback。空库内搭配、库内配角取舍、被拒方案和补充素材建议由 StylingPlan 表达，前台尚未完整展示。
- 导出：ZIP 写入 `exports/{sku}/`，同时通过接口流式返回给前端。
- 日志：业务事件写入 SQLite，并双写 `backend/data/logs/*.jsonl`。
- 部署：客户本地化部署默认使用 RustFS Docker；未来 SaaS 可把同一套 S3 配置切到 OSS、COS、AWS S3、GCS 兼容接口等。

## 后续建议

- [ ] 独立视觉 `HeroUnderstanding` provider 调用，替代任务名启发式 fallback。
- [ ] 主推品类为 `unknown` 时进入复核或安全 fallback，不进入正常选搭通过状态。
- [ ] 用新代码重新跑 335-338，验证全量标签事实、空库内搭配比较、StylingPlan 质量信号、ShotPlan 和预览图是否达到预期。
- [ ] 前台展示 StylingPlan 候选比较、被拒原因、质量信号和补充素材建议。
- [ ] Lily 官方样本库：表结构、样本导入、样本检索、ShotPlan 姿势/取景参考。
- [ ] 多模态 embedding、向量检索、compatibility ranker 和反馈闭环。
- [ ] 成功阶段整阶段重跑策略待产品拍板：当前代码仍允许成功后重新选搭、重新预览和重新生成成片；若决定禁止，需要同步前后端状态机。
- [ ] 重生 attempt 与当前成功图分离，避免失败重生覆盖已成功结果。
- [ ] 上传安全校验补强：文件大小、数量、扩展名、真实图片解码。
- [ ] 健康检查收敛生产信息：默认不暴露绝对数据库路径。
- [ ] 生产级 Info / Warning / Error 架构：统一后端事件等级、API 错误码与用户可读文案、前端状态提示、任务失败原因展示、运维日志检索和可恢复操作指引，覆盖上传、策划、预览、生图、存储、导出等全链路。
- [ ] 生成失败诊断补强：区分 Gemini、fal 上传、fal queue、对象存储下载等错误来源。
- [ ] 策划层去除默认模特硬编码，让 `plan_json.modelId` 跟随当前选择的数字模特。
