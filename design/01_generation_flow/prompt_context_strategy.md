# Prompt 上下文策略

Prompt 维护页托管稳定业务规则；代码只注入会随任务变化的动态事实。不要把用户训练成 prompt 工程师，也不要让 Gemini 动态生成分镜或成片 Prompt。

## Runtime 占位

| 阶段 | 动态占位 | 说明 |
|---|---|---|
| AI 选搭 | `candidate_retrieval`, `hero_product_info`, `ai_styling_user_instruction`, `user_requirement` | 给 Gemini 的选搭事实与用户要求 |
| 分镜预览 | `category_family`, `styling_outfit`, `resolved_accessories`, `ref_model_identity`, `ref_garment`, `ref_shoot_environment`, `hero_product_info`, `storyboard_preview_user_instruction` | 给三类固定分镜 Prompt 的少量动态事实 |
| 最终成片 | `preview_grid_cell`, `shot_styling`, `shot_global_directing`, `shot_current`, `shot_accessories`, `ref_model_identity`, `ref_preview_grid`, `ref_garment`, `ref_shoot_environment`, `hero_product_info`, `final_image_user_instruction`, `is_shot_1..5` | 给三类固定成片 Prompt 的当前格动态事实 |

## 禁止

- 不传完整候选池给分镜或成片。
- 不传动态剧本 JSON。
- 不传镜头计划 JSON。
- 不把 schema、优先级治理元数据、provider 参数混入面向 Image-2 的自然语言 Prompt。

## 用户要求

用户要求保存为任务级草稿，只影响下一次执行：商品信息、智能选搭要求、分镜预览要求、成片要求。
