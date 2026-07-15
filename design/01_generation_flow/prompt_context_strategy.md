# Prompt 上下文策略

Prompt 维护页托管稳定业务规则；代码只注入会随任务变化的动态事实。不要把用户训练成 prompt 工程师，也不要让 Gemini 动态生成分镜或成片 Prompt。

## Runtime 占位

| 阶段 | 动态占位 | 说明 |
|---|---|---|
| 搭配服装图片挑选/排序 | `garment_image_files` | 给 Gemini 的候选图片文件标识与排序约束，运行时必须与 AI 打标合并为一次调用 |
| AI 打标 | `garment_image_files` | 给 Gemini 的打标图片事实，运行时必须与图片挑选/排序合并为一次调用 |
| 主推款图片挑选/排序 | `hero_image_files` | 给 Gemini 的主推款候选图片文件标识与排序约束，运行时必须与 AI 选搭合并为一次调用 |
| AI 选搭 | `hero_image_files`, `candidate_retrieval`, `ai_styling_user_instruction`, `user_requirement` | 给模型的主推款图片、选搭事实与用户要求 |
| 分镜预览 | `category_family`, `user_requirement`, `shot_styling`, `ref_model_identity`, `ref_garment`, `ref_shoot_environment`, `storyboard_preview_user_instruction` | 给三类固定分镜 Prompt 的任务级服装信息、配饰与造型和少量动态事实 |
| 最终成片 | `preview_grid_cell`, `user_requirement`, `shot_styling`, `shot_global_directing`, `shot_current`, `ref_model_identity`, `ref_preview_grid`, `ref_garment`, `ref_shoot_environment`, `final_image_user_instruction`, `is_shot_1..5` | 给三类固定成片 Prompt 的当前格动态事实 |

## 禁止

- 不传完整候选池给分镜或成片。
- 不传动态剧本 JSON。
- 不传镜头计划 JSON。
- 不把 schema、优先级治理元数据、provider 参数混入面向 Image-2 的自然语言 Prompt。

## 用户要求

用户要求分两类保存并渲染：`runtime.user_requirement` 是任务级服装信息，每个生成步骤都可引用；`runtime.ai_styling_user_instruction`、`runtime.storyboard_preview_user_instruction`、`runtime.final_image_user_instruction` 分别是三个执行步骤的本次介入要求，只影响对应步骤的下一次执行。

`runtime.shot_styling` 是面向 Prompt 的“配饰与造型”单行文本，合并选中搭配、穿法和低存在感配饰边界，片段之间使用中文分号 `；` 分隔。不要再使用 `runtime.shot_accessories`、`runtime.styling_outfit` 或 `runtime.resolved_accessories`。
