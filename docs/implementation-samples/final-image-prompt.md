# 生成成片 Prompt 实施样例

> 本文是 0.1.0 实施阶段可参考的 Prompt 样例，不是系统 seed 默认值。上线实施时应在 `Prompt 维护` 中按客户现场确认后填写。

```text
生成一张电商棚拍成片

参考分镜预览图中对应的格子，只参考姿势、身位、景别、构图、背景和光影方向
分镜预览不是模特和商品的事实来源。主推款参考图是主角商品事实来源，搭配服装参考图是配角商品事实来源，模特参考图是模特事实来源

【要求】
- 模特一致性：与输入的模特参考图保持一致
- 环境一致性：同一组镜头保持同一位模特、同一套造型、同一背景光线体系
- 品牌调性：轻松高级、现代简洁、自然从容、商品导向；避免传统办公室、网红写真和夸张大片感
- 商品一致性：保持主推款和搭配服装的品类、颜色、面料、版型、结构、比例和真实质感
- 当前单张不必展示所有细节，但必须准确执行本镜头 main_focus 和 acceptance_criteria
- 鞋、包、饰品只作为辅助搭配，不作为商品事实源，不做特写
- 不要改款、换色、替换商品；不要添加文字、水印、logo
- 本次用户要求：若存在，优先满足；若冲突，说明取舍

{{#runtime.hero_product_info}}服装信息：{{runtime.hero_product_info}}
{{/runtime.hero_product_info}}{{#runtime.final_image_user_instruction}}用户要求：{{runtime.final_image_user_instruction}}
{{/runtime.final_image_user_instruction}}

【选搭、剧本和分镜信息】
{{runtime.current_shot_context}}

```
