# 领域模型

## 当前边界

0.1.0 主流程是“导入主推款 → AI 智能选搭 → 姿势选搭 → 最终成片”。系统不创建分镜预览、动态拍摄剧本或镜头计划。五镜头规则由三类固定成片 Prompt 承载，共用一张背景与光线参考。

## 核心对象

- `GenerationTask`：一次主推款拍摄任务及状态。
- `GenerationTaskInputVersion`：冻结的主推款、模特、服装事实和用户要求。
- `GenerationStyling` / `StylingPlan`：AI Stylist 的商品理解与最终选搭、穿法、配饰和空搭配原因；旧候选与审美诊断只读兼容。
- `Pose`：独立姿势库条目，保存原图、标签与状态。
- `GenerationPoseSelection`：基于已选造型的五镜头姿势选择，冻结共享背景、五个不同姿势、标签与理由、成片规则及商品与模特输入版本。
- `ShotPresetSlot`：历史任务的母版/姿势资产对；新流程不读取该表。
- `GenerationResultSetShotPreset`：结果集创建时从姿势选择复制的五组共享背景/独立姿势资产；旧任务从原槽位冻结。
- `GenerationResultSet`：冻结输入版本、StylingPlan 和背景和五个姿势图片的一组五张成片。
- `GenerationResultImage`：单个镜头的状态、输出和 active attempt。
- `GenerationAttempt`：provider 请求、响应、错误和恢复审计记录。

## 责任边界

| 要素 | 权威来源 |
|---|---|
| 主推款和搭配款商品事实 | 商品原图与 StylingPlan |
| 人物身份 | 数字模特参考图 |
| 场景与光线 | 统一背景图，选姿势时冻结并由结果集引用 |
| 构图、景别、裁切、人物尺度 | 三类成片 Prompt 五镜头要求 |
| 人物姿势、动作与表情 | GenerationPoseSelection 冻结的姿势原图；标签仅用于选姿，历史动作说明只读保留 |
| 五镜头构图与禁止项 | 三类固定成片 Prompt |
| 参考图顺序、数量和请求参数 | 产品组装代码 |
| 视觉生成 | GPT Image-2.5 Sunburst edits |

旧 `GenerationPreview` 数据只用于历史记录兼容，不再创建；旧 preview endpoint 固定返回 `410 Gone`。

## 成片准备快照

GenerationResultSet 的 `prompt_preparation_json` 冻结配置、优化模式、准备状态及可复用的五镜头提示词；不增加新领域对象或前台步骤。文本优化调用复用 GenerationAttempt，owner_type 为 `generation_result_set`。普通用户只读准备状态，原始文本和模型调用详情仅管理员可见。
