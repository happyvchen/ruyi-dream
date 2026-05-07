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
| Production Hardening | 🔶 已 Review | 🔴 有必须修项 | 🟡 构建已恢复 | 🔴 待修复后复测 |

## 下一步建议

- [x] 确认前端/后端技术栈
- [x] 初始化前端工程
- [x] 初始化后端工程
- [x] 定义最小 API 契约
- [x] 做 `125309C7948510` 的端到端本地 MVP 验证

## 生产级 Review 修复队列

本节来自最近一轮生产级 review。目标不是把 MVP 做成复杂 SaaS，而是把当前内部生产工具从“演示可跑”推进到“内部使用不容易出事故”。

### P0 / MVP 必须解决

- [ ] **限制 `/media` 静态暴露范围**
  - 当前问题：`backend/app/main.py` 将整个 `backend/storage` 挂载到 `/media`，SQLite、JSONL 日志、导出 ZIP、上传原图都在同一目录树下，存在直接下载内部数据的风险。
  - 解决方案：不要 mount 整个 `storage`。短期可只 mount `storage/public`，并只把允许公开访问的上传/输出文件放入 public；更稳方案是改为受控 media handler，只允许访问白名单目录和文件。
  - 验收标准：`/media/lily_mvp.sqlite3`、`/media/logs/...`、`/media/exports/...` 不可访问；前端图片预览仍正常。

- [ ] **移除生产路径中的实验资产和 SKU 特例**
  - 当前问题：`build_task_package` 对 `125309C7948510` 写死比例约束，`build_image_inputs` 优先读取 `simulations/<sku>-v4/assets`。这与“`simulations/` 不是权威设计”的文档规则冲突，也会污染真实货号生成。
  - 解决方案：默认生成输入只来自用户上传素材、当前数字模特头像、正式品牌模板/规则。SKU 特例应升格为通用尺寸/品类/品牌模板规则，或放到显式 dev/debug flag 下且默认关闭。
  - 验收标准：主生成链路不读取 `simulations/`；搜索 `backend/app` 不应出现 `125309C7948510` 特例逻辑。

- [ ] **修复重生失败覆盖已有成功图的问题**
  - 当前问题：单张重生开始时会把原 `GenerationImage` 改为 `QUEUED`，provider 失败后改成 `FAILED`，可能覆盖原本成功可用的图片状态。
  - 解决方案：attempt 状态和当前输出分离。发起重生时追加 pending attempt，但不改当前成功图的 `output_path/status`；只有新 attempt 成功后才切换 current attempt。失败时记录 failed attempt，原成功图继续可用。
  - 验收标准：对成功图发起失败重生后，原图仍显示为成功且可导出；attempt 列表能看到失败记录。

- [ ] **收紧最终图和导出约束**
  - 当前问题：任意图片记录都可设为最终图；导出时会跳过无 URL 文件，但可能生成空 ZIP 并记录成功。
  - 解决方案：只有 `status == SUCCEEDED` 且本地输出文件存在的图片可以设为最终图。导出只打包有效成功图片；如果实际写入 0 张，返回 `422`，不记录成功导出。
  - 验收标准：失败/排队/缺文件图片不能设为最终图；空导出返回错误；成功导出的 ZIP 至少包含 1 张有效图片。

### P1 / MVP 最小安全集

- [ ] **补齐上传安全校验**
  - 当前问题：上传接口只信任 `content_type.startswith("image/")`，随后无限制写盘。Content-Type 可伪造，且缺少大小、数量、扩展名和真实图片校验。
  - MVP 必修范围：限制单文件大小、单次上传数量、扩展名 allowlist（jpg/jpeg/png/webp）、真实图片解码/文件头校验、失败时清理已写文件和半成品 DB。
  - 可后置范围：杀毒、CDR、异步扫描、内容安全审核。
  - 验收标准：伪造 Content-Type 的非图片被拒绝；超大文件/超量上传被拒绝；失败上传不留下孤儿文件或半成品记录。

### P2 / 文档与接手体验

- [ ] **补充 `backend/.env.example` 或修正 README**
  - 当前问题：README 指导 `cp .env.example .env`，但仓库中没有 `backend/.env.example`。
  - 解决方案：新增不含真实密钥的 `backend/.env.example`，或修改 README 为直接创建 `.env`。
  - 验收标准：新 agent / 新开发者按 README 可以完成本地配置。

- [ ] **减少健康检查中的本地路径泄露**
  - 当前问题：`/api/health` 返回 `storageDir` 和 `databasePath`。
  - 解决方案：默认只返回 `ok`、provider 是否配置、版本/环境；本地路径放入 debug-only 接口或日志。
  - 验收标准：生产/默认健康检查不暴露绝对路径。

- [ ] **补齐 fallback 品类规则一致性**
  - 当前问题：设计文档覆盖 `上衣 / 外套 / 马甲 / 裤子 / 半裙长裙 / 连衣裙 / 套装`，但 `planner.py` 的 fallback 只实现部分品类，未命中会退回连衣裙。
  - 解决方案：将 `design/02_brand_and_assets/category_shot_rules.md` 中的 7 类默认 5 张规则同步到 fallback 规则。
  - 验收标准：Gemini 不可用时，7 类品类都能生成对应默认图位，不错误退回连衣裙。

- [ ] **消除策划层 `Face-01` 硬编码**
  - 当前问题：产品已支持多数字模特，但 planner prompt / normalize 仍硬编码 `Face-01`。
  - 解决方案：`plan_with_gemini` 接收当前 `ModelProfile` 的 `model_id` 和必要摘要，prompt 和 fallback 都使用所选模特。
  - 验收标准：选择非默认模特生成方案时，`plan_json.modelId` 与所选模特一致。

### 已验证 / 已解决

- [x] **前端生产构建已恢复**
  - 之前问题：`parseRoute`、`routeToUrl`、`routeKey` 缺失导致 TypeScript 构建失败。
  - 当前状态：已运行 `npm run build`，TypeScript + Vite 构建通过。
  - 后续要求：提交前继续把 `npm run build` 作为前端必跑检查。

## 当前 MVP 实现说明

- 前端：`frontend/`，React + Vite，主页面为服装素材、生成组图、数字模特、生成记录。
- 后端：`backend/`，FastAPI + SQLite + 本地文件系统。
- 上传：服装图片先在浏览器本地预览与编辑，支持散图、单文件夹、多文件夹；保存后复制到 `backend/storage/uploads/{sku}-{timestamp}-{id}/`，不会修改原始素材。
- 默认方案：保存上传后创建货号默认 `ShotPlan`；服装素材页可更新默认方案；生成组图页直接使用默认方案。
- 推理：`Gemini API` 负责根据服装主图生成 `garment + brandStyle + shots` 简单 JSON；失败时回落到本地默认方案。
- 生图：`GPT Image-2` 三方平台 Provider 已占位，当前输出 SVG 占位图，任务包、请求、响应、重生原因完整入库。
- 记录：上传、策划、生成、重生、设为最终图、导出都会写入 SQLite 的 `event_logs` 和 `backend/storage/logs/*.jsonl`。
- 导出：`POST /api/exports/runs/{run_id}` 会打包最终图；如果未标记最终图，则打包该 run 下全部图。

## 当前生产化判断

- 当前整体产品蓝图没有偏离：仍是 Lily 内部电商图片生产工具，不是泛 SaaS，也没有把普通用户训练成 prompt 工程师。
- 当前最大风险不是功能方向，而是演示验证代码仍混入生产主路径，尤其是 `simulations/` 资产、样例 SKU 特例和 placeholder provider。
- MVP 继续开发前，优先修复 P0 项；否则后续新增功能会建立在不可靠的数据/文件/生成状态边界上。
