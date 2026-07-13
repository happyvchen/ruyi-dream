# Backend Domain Model

## 当前生成领域模型

```text
GenerationTask
  -> GenerationTaskInputVersion
  -> GenerationStyling / StylingPlan
  -> GenerationPreview
  -> GenerationResultSet
  -> GenerationResultImage x 5
```

当前后端不创建动态拍摄剧本对象，也不创建镜头计划对象。分镜六镜头和成片五镜头规则由三类固定 Prompt 表达，运行时只保存最终预览图和五张成片。

## StylingPlan

`StylingPlan` 是 AI Stylist 的输出，负责：

- 理解主推款品类、卖点和不可遮挡区域；
- 探索多套 outfit；
- 用 Aesthetic Critic 判断审美、品牌和商品表达是否成立；
- 选择真实可见的搭配服装或判断无需库内配角；
- 输出成片阶段需要的穿着关系、配饰边界和风险提示。

## 分镜预览

`GenerationPreview` 是 Image-2 生成的一张 3:2 六格预览图。它不是商品事实源；商品事实仍来自主推款图和搭配服装图。预览用于固定本组构图、姿态、裁切、背景和组图方向。

## 最终成片

`GenerationResultSet` 固定包含 5 张 `GenerationResultImage`，对应分镜预览前 5 格。每张成片使用当前分镜单格、商品图、模特图、环境图和同一 `StylingPlan` 动态事实。

## Attempt

`generation_attempts.owner_type` 当前包括：

- `styling_garment_tagging`
- `generation_styling`
- `generation_preview`
- `generation_result_image`

历史旧 owner type 由最终迁移清理，不作为当前运行依赖。
