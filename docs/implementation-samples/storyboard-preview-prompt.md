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

{{#runtime.storyboard_preview_user_instruction}}用户要求：{{runtime.storyboard_preview_user_instruction}}
{{/runtime.storyboard_preview_user_instruction}}

【参考图职责】
{{runtime.ref_model_identity}} 只决定同一位成年女模特的身份、脸、发型和气质
{{runtime.ref_garment}} 只决定主推款与已选搭配服装的真实颜色、长度、廓形、材质和结构
{{runtime.ref_shoot_environment}} 只决定影棚背景、光线、墙面与地面

按 `runtime.category_family` 对应的固定六镜头规则生成 3:2 横版分镜预览（3 列 × 2 行、6 个 1:1 方格）。六格规则、姿态、裁切、背景和镜头 5 安全区写在 Prompt 正文中；代码只注入参考图图号和用户补充要求。

```
