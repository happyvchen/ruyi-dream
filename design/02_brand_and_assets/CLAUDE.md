# design/02_brand_and_assets/ — Agent Guide

## 职责 (Scope)

服装品类组图规则、素材参数、数字模特档案、品牌调性模板、尺寸可选增强。这里回答“生成依据从哪里来、品牌边界是什么、每类衣服默认拍哪些图”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `category_shot_rules.md` | 各服装品类默认 5 张组图规则 | 生成或调整组图方案时 |
| `assets_and_parameters.md` | 模特素材、服装素材、识别字段、尺寸参数、输出规格 | 处理素材、参数、导出时 |
| `brand_style_templates.md` | 目标品牌 背景、光线、Pose、鞋包、配饰、禁区 | 设计品牌模板库或参考图检索时 |
| `optional_measurement_enhancement.md` | 模特尺寸、服装尺寸、品类尺寸字段 | 解决衣长、裙长、裤长、下摆比例等问题时 |

## 本地规则 (Local Rules)

- 品牌调性模板来自 品牌历史真实图，不来自临时审美词。
- 图片决定商品事实，尺寸参数辅助比例，模板决定品牌边界。
- 尺寸增强是可选模块，MVP 不强制用户填写。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 生成职责：`../01_generation_flow/CLAUDE.md`

