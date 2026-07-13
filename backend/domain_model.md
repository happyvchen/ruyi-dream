# Backend Domain Model

> **当前第一阶段设计（2026-07-13）**：运行链路已统一为“上传主推款 → AI 智能选搭 → 三类固定 Prompt 生成分镜预览 → 分镜前 5 格生成成片”。不再提供“生成拍摄剧本”步骤或对外 API；下文若仍出现 ShootScript/ShotPlan，属于待第二阶段删除的旧实现背景，不得作为当前产品或实现依据。当前权威设计见 `design/01_generation_flow/shoot_script_workflow.md` 与 `generation_responsibility_matrix.md`。

## 核心链路

```text
GenerationTask
  -> GenerationTaskInputVersion
  -> GenerationStyling / StylingPlan
  -> GenerationShootScript / 6 x GenerationShotPlan
  -> GenerationPreview
  -> GenerationResultSet / 5 x GenerationResultImage
```

0.1.0 之前的货号、planner、生成批次和单图记录已经从领域模型中删除。当前系统只有 AI 摄影棚任务链路。

## StylingGarment

搭配服装库中的真实商品。只有 `status = READY` 且未归档、未删除的服饰进入 AI Stylist 候选范围。

关键事实：

- `ai_tags_json`：Gemini 打标原始返回，用于审计和详情展示。
- `official_title_tags_json`：官方标题解析结果。
- `manual_tags_json`：人工修正和业务标记。
- `effective_tags_json`：最新生效标签。
- `tag_meta_json`：置信度、复核原因和 schema 信息。

AI Stylist 接收未归档 READY 搭配库的全量标签事实卡。后端只做状态范围过滤和结构化组装，使用搭配服装 `code` 作为 prompt 输入输出标识，不做预筛选、排序、评分或审美判断。

## GenerationTask

AI 摄影棚主推款任务。草稿阶段可上传和排序主推款图片，也可填写简短 `user_requirement_text` 作为服装事实补充或拍摄要求；进入 AI 后通过 `GenerationTaskInputVersion` 冻结输入。

主要状态：

- `DRAFT`
- `READY_FOR_STYLING`
- `STYLING_RUNNING`
- `STYLING_READY`
- `SCRIPT_RUNNING`
- `SCRIPT_READY`
- `PREVIEW_RUNNING`
- `PREVIEW_READY`
- `FINAL_RUNNING`
- `FINAL_PARTIAL`
- `COMPLETED`
- `FAILED`
- `CANCELLED`

`auto_run_target = FINAL` 表示用户触发了 `一键成片` 自动推进。它不是独立流程状态；系统仍依赖当前 active 指针和阶段 artifact 判断下一步。自动推进失败或取消时清除该字段，任务停留在原阶段失败或取消语义。

## GenerationStyling / StylingPlan

AI Stylist 的职责是理解主推款、探索多套 outfit、批判审美与品牌成立性，最后选择一套能服务主推款的造型。

保存内容：

- `candidate_retrieval_snapshot_json`：本次 READY 搭配库全量标签事实卡、source ids、candidate ids 和 code/id 映射。
- `user_requirement_json`：上传补充要求、本次选搭微调要求和冲突处理策略。
- `styling_plan_json`：完整 StylingPlan，包含候选方案、选中方案、被拒方案、诊断、评分、质量信号和 warnings。
- `selected_material_snapshot_json`：选中搭配服装的商品与图片快照。

面向 prompt 的 `runtime.candidate_retrieval` 只包含业务 `code`、`ai_tags` 和必要人工/复核事实，不包含内部 id、图片快照、历史 prompt、完整历史快照或后端排序/评分结果。

## GenerationShootScript / ShotPlan

AI Director 基于当前 StylingPlan 生成 ShootScript。ShootScript 负责把造型意图落到导演层面：

- 全局品牌、背景、光线、模特、妆发和一致性规则。
- 搭配风险与导演处理策略。
- 生成类配饰的最终执行方案。
- 6 个镜头的图位、景别、姿势、走位、取景、可见商品和验收标准。

`generation_shot_plans` 从 ShootScript 派生，前 5 个镜头进入最终成片，第 6 个只在分镜预览中参考。

## GenerationPreview / GenerationResult

`GenerationPreview` 保存 3:2 分镜预览总图。它是导演和构图参考，不是商品事实来源。

`GenerationResultSet` 保存一次最终图生成批次。`GenerationResultImageNew` 保存前 5 张单图状态、当前资产、锁和重试 attempt。

最终单图 prompt 只读取 `current_shot_context`，不读取完整 StylingPlan、完整 ShootScript、候选池或 rejected candidates。

`一键成片` 在最终阶段复用当前 active result set。若当前结果为 `FINAL_PARTIAL`，自动续跑只把失败或缺失的单图重新排队，不覆盖已经成功的最终图；手动 `生成成片 / 重新生成成片` 仍创建新的结果集。

创建新的最终图结果集、重新选搭、重新生成拍摄剧本或重新生成分镜预览时，历史 `GenerationResultSet` 会进入 `SUPERSEDED`。旧结果集中仍处于 `QUEUED` / `RUNNING` 的单图和 attempt 必须同步收口为 `CANCELLED`，避免被当前任务摘要误认为仍在生成。若旧后台 job 晚于新结果集完成，旧结果集的单图落库和 reconcile 必须视为 no-op 或取消收口，不得回写 `GenerationTask.active_result_set_id`。

## GenerationAttempt

所有 AI/provider 调用都以 attempt 记录：

- `styling_garment_tagging`
- `generation_styling`
- `generation_shoot_script`
- `generation_preview`
- `generation_result_image`

attempt 保存 provider、状态、实际请求过程事实、实际响应过程事实、错误、provider request id 和输出资产。它不是业务对象快照，也不承担未来重放；历史列名 `request_snapshot_json` / `response_snapshot_json` 仅作为兼容存储载体。技术详情默认不返回给普通前台，只在 debug 场景查看。

普通任务详情中的 `aiOperations` 是当前 active 链路的轻量运行摘要，不是完整历史流水。它只包含当前 active StylingPlan、ShootScript、分镜预览和 active result set 的 attempts；历史版本、旧结果集和 superseded artifacts 的 attempts 只在技术详情或审计读取中展示。

## Prompt DTO

模型输入按阶段收敛：

| 阶段 | Runtime | 内容 |
|---|---|---|
| AI Stylist | `candidate_retrieval` | READY 搭配库全量标签事实卡 |
| AI Stylist | `user_requirement` | 上传补充要求、本次微调要求和冲突处理策略 |
| AI Director | `styling_result` | 主推款理解、选中 outfit、选中服饰紧凑标签、质量信号 |
| 分镜预览 | `shoot_script_result` | 全局导演、配饰、风险和 6 个镜头 |
| 最终成片 | `current_shot_context` | 当前镜头、当前格子、选中 outfit、本镜头配饰和禁止项 |

这套模型只在 AI Stylist 阶段提供搭配库 `ai_tags` 标签事实卡，避免把内部 id、历史 candidate snapshots、历史 AI Styling prompt 或完整上游对象重复塞入下游 prompt。

Prompt 正文只来自 `prompt_configs.content`。后端只初始化空正文的可维护 Prompt 槽位，不内置 Prompt 默认正文，也不在缺配置或空正文时使用代码字符串 fallback；缺失配置或空正文应作为配置错误处理。
