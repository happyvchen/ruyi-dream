# 分镜预览 Prompt 实施样例

> 本文是 0.1.0 实施阶段可参考的 Prompt 样例，不是系统 seed 默认值。上线实施时应在 `Prompt 维护` 中按客户现场确认后填写。

```text
生成一张 3x2 电商棚拍分镜预览图

【核心原则】
主推款和搭配服装参考图是真实商品事实来源，必须保持商品级保真
画面首先要符合 Lily 品牌调性：轻松高级、现代简洁、自然从容、商品导向的 commercial lookbook
搭配服装和生成配饰服务整体品牌造型，不得成为第二主角
数字模特参考图用于保持同一身份、发型和气质
本次用户要求：若存在，优先满足；若冲突，说明取舍

【本次用户要求】
商品信息：{{runtime.hero_product_info}}
用户要求：{{runtime.storyboard_preview_user_instruction}}

【拍摄剧本结果】
{{runtime.shoot_script_result}}

按拍摄剧本中的 6 个 shots 生成 3x2 分镜预览。每格遵循 shots[].main_focus、accessory_instructions 和 acceptance_criteria
配饰执行优先级：以 resolved_accessories 和 shots[].accessory_instructions 为准；生成配饰只能作为低存在感的风格辅助，不要把商务时装理解为传统职业装或功能性办公室场景

```
