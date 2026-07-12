# 分镜预览 Prompt 实施样例

> 本文是 0.1.0 实施阶段可参考的 Prompt 样例，不是系统 seed 默认值。上线实施时应在 `Prompt 维护` 中按客户现场确认后填写。

```text
生成一张 3:2 横版电商棚拍分镜预览图；等分为 3 列 × 2 行、共 6 个完全等大的方形（1:1）分格，格与格之间留统一细缝。编号从左到右、从上到下：上排 1、2、3，下排 4、5、6。每一格都是一张独立的 1:1 电商棚拍构图，画幅与最终成片一致；所有分格须为同一模特、同一套造型、同一背景光线体系，只在景别、取景、身位、姿势和轻微走位上区分。第 6 格仅备用参考。

【核心原则】
主推款和搭配服装参考图是真实商品事实来源，必须保持商品级保真
画面首先要符合 Lily 品牌调性：轻松高级、现代简洁、自然从容、商品导向的 commercial lookbook
搭配服装和生成配饰服务整体品牌造型，不得成为第二主角
数字模特参考图用于保持同一身份、发型和气质
本次用户要求：若存在，优先满足；若冲突，说明取舍

{{#runtime.hero_product_info}}服装信息：{{runtime.hero_product_info}}
{{/runtime.hero_product_info}}{{#runtime.storyboard_preview_user_instruction}}用户要求：{{runtime.storyboard_preview_user_instruction}}
{{/runtime.storyboard_preview_user_instruction}}

【拍摄剧本结果】
{{runtime.shoot_script_result}}

按拍摄剧本中的 6 个 shots 生成 3:2 横版分镜预览（3 列 × 2 行、6 个 1:1 方格）。每格遵循 shots[].main_focus、accessory_instructions 和 acceptance_criteria
配饰执行优先级：以 resolved_accessories 和 shots[].accessory_instructions 为准；生成配饰只能作为低存在感的风格辅助，不要把商务时装理解为传统职业装或功能性办公室场景

```
