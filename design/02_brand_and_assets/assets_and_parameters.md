# 素材与参数

## 媒体资产

所有上传图片统一入 `media_assets`，对象存储使用 S3 兼容接口。后端生成 `thumb` 和 `preview` 派生图用于列表和弹窗，AI 输入和下载保留原图。

## 主推款

主推款图片挂在 `generation_task_images`。进入 AI 阶段时创建 `generation_task_input_versions`，冻结当前图片顺序、任务名、商品信息和默认数字模特快照。

## 搭配服装

搭配服装挂在 `styling_garments` / `styling_garment_images`。Gemini 打标只负责把图片转成可追溯标签事实；AI 智能选搭读取 READY 且未归档的搭配服装标签事实。

## 数字模特

数字模特参考图是人物身份事实源。分镜预览和成片必须保持同一人、脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色、气质和身体比例。Prompt 或 provider 默认审美不得覆盖这些事实。

## Image-2 输入角色

- `model_identity`
- `hero_garment`
- `styling_garment`
- `shoot_environment`
- `preview_grid`

参考图图号由代码按输入顺序渲染，职能解释写在 Prompt 维护模板正文。

## 当前生成参数

- 分镜预览：3:2 横版六格图。
- 最终成片：1:1 单图，固定生成分镜前 5 格。
- 成片任务包必须包含当前分镜预览图和当前格号，同时继续包含商品图和模特图。

## 配饰

鞋、包、首饰不进入搭配服装库。它们可以作为低存在感风格辅助出现在 Prompt 和生成图中，但不得遮挡主推款，不做商品级验收对象。
