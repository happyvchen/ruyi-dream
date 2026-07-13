# Prompt 渲染层最终边界

本文取代早期“把动态剧本 JSON 渲染为自然语言”的草案。当前系统已经删除动态剧本和镜头计划中间层，渲染层只保留很窄的职责。

## 当前职责

`backend/app/prompt_render.py` 只负责：

- 商品信息一句话清洗；
- 用户补充要求一句话清洗；
- AI Stylist 用户要求合并文本；
- 参考图按输入顺序渲染为 `图1` / `图1-3` 这样的图号区间。

## 不做

- 不渲染六镜头正文；
- 不渲染单镜头计划；
- 不输出固定摄影规则；
- 不输出事实源约束；
- 不输出 schema、priority、provider 参数。

## 规则归属

固定规则进入 Prompt 维护模板正文：

- AI 选搭规则：`assembly.ai_styling`
- 分镜规则：`assembly.storyboard_preview_top` / `_bottom` / `_one_piece`
- 成片规则：`assembly.final_image_top` / `_bottom` / `_one_piece`

代码只提供动态事实占位，避免隐藏规则与 Prompt 维护页出现两套口径。
