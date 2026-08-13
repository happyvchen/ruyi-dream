# design/02_brand_and_assets/ — Agent Guide

## 职责

搭配服装库、标签 schema、媒体资产、数字模特、Image-2 输入图片预算和品类图位规则。这里回答“生成依据从哪里来、商品事实如何保存、每套最多 15 张图时怎么选”。

## 文档索引

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `assets_and_parameters.md` | 媒体资产、主推款、搭配服装、标签、数字模特和当前生图输入参数 | 处理素材、标签、图片输入、模特和参数时 |
| `category_shot_rules.md` | Lily 主推品类固定五镜头图位矩阵、取景、姿态和展示重点 | 维护或评审拍摄预设与三类成片 Prompt 时 |
| `brand_style_templates.md` | Lily 品牌视觉语法、造型和棚拍语言 | 维护品牌调性或 Prompt 时 |
| `lily_tagging_system.md` | Lily 服装视觉生产标签体系 | 实现或评审标签体系时 |
| `lily_gemini_tagging_flow.md` | Gemini 打标流程 | 实现或排查打标时 |

## 本地规则

- 图片决定商品事实，数字模特图决定人物事实，15 个拍摄预设决定背景/遮罩/姿势，三类固定 Prompt 决定成片任务。
- 品类图位规则必须进入 Prompt 维护模板；不要依赖大模型动态生成镜头计划。
- 最终成片固定生成五个预设镜头，不生成或依赖分镜预览。
