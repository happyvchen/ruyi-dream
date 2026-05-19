# design/02_brand_and_assets/ — Agent Guide

## 职责 (Scope)

搭配服饰库、标签 schema、媒体资产、数字模特、Image-2 输入图片预算和 ShotPlan 字段。这里回答“生成依据从哪里来、商品事实如何保存、模型最多 10 张图时怎么选”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `assets_and_parameters.md` | 媒体资产、主推款、搭配服饰、标签、数字模特、ShotPlan 字段 | 处理素材、标签、图片输入、模特和参数时 |
| `category_shot_rules.md` | 旧版各服装品类默认 5 张组图规则，可作为 ShotPlan 参考 | 需要参考品类图位语言时 |
| `brand_style_templates.md` | 目标品牌 背景、光线、Pose、鞋包、配饰、禁区参考 | 设计品牌边界或生成配饰约束时 |
| `optional_measurement_enhancement.md` | 模特尺寸、服装尺寸、品类尺寸字段 | 解决衣长、裙长、裤长、下摆比例等问题时 |

## 本地规则 (Local Rules)

- 搭配服饰库当前只支持真实服装，不支持鞋、包、饰品入库。
- 鞋、包、饰品可作为生成配饰出现，但不作为商品级验收对象。
- 图片决定商品事实，ShotPlan 决定拍摄任务，文本决定边界。
- 尺寸增强是可选模块，MVP 不强制用户填写。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 生成职责：`../01_generation_flow/CLAUDE.md`
