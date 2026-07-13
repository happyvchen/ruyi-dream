# design/03_ux_guide/ — Agent Guide

> **当前第一阶段设计（2026-07-13）**：运行链路已统一为“上传主推款 → AI 智能选搭 → 三类固定 Prompt 生成分镜预览 → 分镜前 5 格生成成片”。不再提供“生成拍摄剧本”步骤或对外 API；下文若仍出现 ShootScript/ShotPlan，属于待第二阶段删除的旧实现背景，不得作为当前产品或实现依据。当前权威设计见 `design/01_generation_flow/shoot_script_workflow.md` 与 `generation_responsibility_matrix.md`。

## 职责 (Scope)

普通用户视角 UI/UX、页面信息层级、主流程交互、默认隐藏的 AI 复杂性。这里回答“用户应该看到什么、确认什么、怎么完成一次 AI 摄影棚生产”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `user_uiux.md` | AI 摄影棚、搭配服装库、数字模特、生成记录的页面结构、状态和文案 | 设计页面、原型、交互或前端实现时 |

## 本地规则 (Local Rules)

- 前台像“B2B 摄影工作台”，不是“AI 参数控制台”。
- 默认流程为：上传主推款 -> AI 智能选搭 -> 生成拍摄剧本 -> 生成分镜预览 -> 生成成片。
- prompt、provider 请求、ShootScript / ShotPlan JSON、快照 JSON 只在技术详情出现。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 前端实现：`../../frontend/CLAUDE.md`
