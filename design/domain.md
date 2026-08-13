# 领域模型

## 当前边界

0.1.0 主流程是“导入主推款 → AI 智能选搭 → 最终成片”。系统不创建分镜预览、动态拍摄剧本或镜头计划。五镜头规则由版本化拍摄预设和三类固定成片 Prompt 承载。

## 核心对象

- `GenerationTask`：一次主推款拍摄任务及状态。
- `GenerationTaskInputVersion`：冻结的主推款、模特、服装事实和用户要求。
- `GenerationStyling` / `StylingPlan`：AI Stylist 的商品理解、候选比较、审美批判和最终选搭。
- `ShotPresetVersion`：一套可发布、可冻结的拍摄预设版本。
- `ShotPreset`：一个品类图位族、一个镜头的拍摄母版/姿势资产对；拍摄母版中的原人物和原服装在生成时被替换。
- `GenerationResultSet`：冻结输入版本和拍摄预设版本的一组五张成片。
- `GenerationResultImage`：单个镜头的状态、输出和 active attempt。
- `GenerationAttempt`：provider 请求、响应、错误和恢复审计记录。

## 责任边界

| 要素 | 权威来源 |
|---|---|
| 主推款和搭配款商品事实 | 商品原图与 StylingPlan |
| 人物身份 | 数字模特参考图 |
| 场景、构图、人物尺度、原人物替换位置、姿势 | 已发布 ShotPreset |
| 五镜头构图与禁止项 | 三类固定成片 Prompt |
| 参考图顺序、数量和请求参数 | 产品组装代码 |
| 视觉生成 | GPT Image-2 edits |

旧 `GenerationPreview` 数据只用于历史记录兼容，不再创建；旧 preview endpoint 固定返回 `410 Gone`。
