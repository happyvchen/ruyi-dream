# 生成职责矩阵

## 当前原则

系统采用四步链路：上传主推款 → AI 智能选搭 → 分镜预览 → 最终成片。没有动态拍摄剧本、`ShootScript` 或 `ShotPlan` 中间层。

| 要素 | 唯一职责来源 | 不负责 |
|---|---|---|
| 商品颜色、结构、长度、面料 | 主推款与搭配服装参考图 | Prompt 猜测、分镜像素反推 |
| 商品参考图挑选/排序 | Prompt 维护中的图片挑选/排序 Prompt + Gemini 返回的 `selected_files` | 人工逐张编辑、分镜或成片阶段临时挑图 |
| 选搭与穿着关系 | `styling_plan` | Image-2 临场换搭配 |
| 模特身份 | 数字模特参考图 | 品牌文字或分镜改变脸与发型 |
| 六镜头规则 | Prompt 维护中的三类固定分镜 Prompt | Gemini 动态生成镜头 JSON |
| 拍摄图位族路由 | `styling_plan.hero_understanding.shot_category_family` + 后端确定性校验 | 大模型自由选择 Prompt、把商品品类直接当 Prompt 键、缺失时本地 fallback |
| 动态业务事实 | 后端渲染为短自然语言 | 复制完整 StylingPlan JSON |
| 背景与光线 | 拍摄环境参考图 + 固定 Prompt 约束 | 后处理补背景 |
| 分镜预览 | Image-2 一次直出 3:2 六宫格 | 自动切格、自动评分、自动验收 |
| 最终成片 | Image-2 参考分镜前 5 格逐张生成 | 使用第 6 格、读取旧 ShotPlan |
| 业务状态与追溯 | 数据库与任务状态机 | Image-2 |

## Prompt 与代码边界

Prompt 维护页集中保存：品牌信息、搭配服装图片挑选/排序、AI 打标、主推款图片挑选/排序、AI 选搭、三类分镜预览 Prompt、三类最终成片 Prompt。稳定的摄影规则和挑图规则必须写在 Prompt 维护正文；代码只做图片上限、文件标识映射、确定性的拍摄图位族路由、参考图编号、当前镜头标记和少量动态事实注入。

AI 选搭输出的 `category` 是商品真实品类，`sub_category` 是规范子类，`shot_category_family` 才是分镜/成片 Prompt 路由族。`shot_category_family` 只允许 `top`、`bottom`、`one_piece`。后端只校验并使用该字段；缺失、非法或无法映射时，本次 AI 智能选搭结果失败，不根据商品品类和子类做 fallback。例如 `西装马甲` / `suit_vest` 必须由模型明确输出 `shot_category_family = top`。

分镜 Prompt 允许的动态占位包括：模特参考图范围、服装参考图范围、环境参考图范围、商品信息、用户本次补充要求。选中搭配与穿法由服装参考图承担，不再单独注入分镜自然语言占位。禁止重新引入动态六镜头 JSON。

## 最终成片槽位

最终结果表只保存前五格的 `sort_order` 与 `shot_role`。成片任务包包含当前分镜单格、商品图、模特图、拍摄环境图、StylingPlan 的自然语言摘要和当前格号；不连接拍摄剧本或镜头方案表。

## 第一阶段明确不做

- 自动评分或自动验收；
- 自动切格；
- 因评分自动重试；
- 额外后处理；
- Gemini 动态生成分镜镜头描述。

详细分镜验收见 [storyboard_preview_business_intent.md](storyboard_preview_business_intent.md)。
