# design/02_brand_and_assets/ — Agent Guide

## 职责 (Scope)

搭配服饰库、标签 schema、媒体资产、数字模特、Image-2 输入图片预算、ShootScript 和 ShotPlan 字段。这里回答“生成依据从哪里来、商品事实如何保存、模型最多 10 张图时怎么选”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `assets_and_parameters.md` | 媒体资产、主推款、搭配服饰、标签、数字模特、ShootScript / ShotPlan 字段 | 处理素材、标签、图片输入、模特和参数时 |
| `lily_tagging_research_raw.md` | Lily 标签体系设计前的公开资料、天猫店铺采集、本地素材盘点和甄别规则备份 | 设计或调整 Lily 标签体系前 |
| `lily_tagging_system.md` | Lily 服装视觉生产标签体系：来源分层、AI 标签、人工标签、选搭压缩格式、前台展示规则 | 实现或评审标签体系、智能选搭输入、标签展示/治理时 |
| `lily_gemini_tagging_flow.md` | Gemini 实际打标流水线：输入、判断步骤、输出、后端校验、effective_tags 合成 | 实现打标服务、排查打标质量或联调标签展示时 |
| `category_shot_rules.md` | Lily 主推品类 ShotPlan 图位矩阵、取景、姿态和展示重点 | 生成或评审拍摄剧本和 ShotPlan 时 |
| `brand_style_templates.md` | Lily 官方样本驱动的背景、光线、Pose、鞋包、配饰和穿法边界 | 设计品牌边界、生成配饰或官方样本检索时 |
| `optional_measurement_enhancement.md` | 模特尺寸、服装尺寸、品类尺寸字段 | 解决衣长、裙长、裤长、下摆比例等问题时 |

## 本地规则 (Local Rules)

- 搭配服饰库当前只支持真实服装，不支持鞋、包、饰品入库。
- 鞋、包、饰品可作为生成配饰出现，但不作为商品级验收对象。
- 图片决定商品事实，ShootScript / ShotPlan 决定拍摄任务，文本决定边界。
- ShotPlan 必须按主推款品类生成，不能把通用全身图模板套给所有品类。
- 尺寸增强是可选模块，MVP 不强制用户填写。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 生成职责：`../01_generation_flow/CLAUDE.md`
