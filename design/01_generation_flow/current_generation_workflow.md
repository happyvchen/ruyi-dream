# 当前直接成片工作流

## 当前主流程

```text
导入主推款
→ AI 智能选搭（Gemini，输出 selected_files + styling_plan）
→ 最终成片（GPT Image-2 edits，按五个固定镜头并发生成）
```

0.1.0 的生产候选链路不再生成分镜预览，也不存在动态拍摄剧本、`ShootScript` 或 `ShotPlan` 运行 artifact。导演约束由“拍摄预设 + 三类固定成片 Prompt 的五个镜头块”共同承担；代码只负责拍摄图位族路由、当前镜头、参考图顺序、选中造型摘要和用户补充要求等动态事实。

## 拍摄预设

拍摄预设按 `shot_category_family × shot_index` 固定为 3 × 5 共 15 个槽位：

- `shot_category_family`：`top`、`bottom`、`one_piece`；
- `shot_index`：1..5；
- 每个槽位包含 `background`（拍摄母版）和 `pose` 两张图片；
- 拍摄母版必须已有原人物、原服装与目标场景、镜头、构图和光线，生产默认 2048 × 2048；
- 上装、下装、套装独立发布；对应类型的 5 个槽位全部有效即可发布，不要求另外两个类型同时完成。任务快照冻结本类型发布版本，后续维护不改变历史任务。

`image_urls[0]` 固定为当前镜头拍摄母版。生成时完整替换其原人物和原服装；姿势参考图独立控制姿态，不能由母版原人物身份或原服装替代。

## Prompt 槽位

最终成片按 `styling_plan.hero_understanding.shot_category_family` 选择三类固定 Prompt：

| 品类族 | Prompt 维护键 |
|---|---|
| 上装、针织、外套、马甲、西装马甲 | `assembly.final_image_top` |
| 裤装、半身裙 | `assembly.final_image_bottom` |
| 连衣裙、连体、套装 | `assembly.final_image_one_piece` |

每类 Prompt 内含 5 个条件镜头块。代码只激活当前镜头块；镜头块必须以可观察边界描述景别、裁切、角度、人物尺度、位置、姿势、留白、商品重点和最可能的坍缩方向，不得依赖已删除的分镜图片。

三个 `assembly.storyboard_preview_*` Prompt 槽位退出运行链路并从 Prompt 维护目录移除。

## 单镜头输入预算

ToAPIs 文档标称 GPT Image-2 edits 的 `image_urls` 最多 16 张，但当前实际 W8X 上游只接受 6 张。生产链路以实际能力为准，把多张事实图确定性合成角色参考板；这只是输入组装，不调用额外 AI。

每个镜头按以下顺序组装，不使用同组成片连续性锚点：

1. 当前镜头拍摄母版图 1 张，固定为 `image_urls[0]`，包含待替换原人物与原服装；
2. 当前镜头姿势参考图 1 张；
3. 数字模特身份参考板 0..1 张，由最多 2 张模特身份图合成；
4. 主推款概览参考板 0..1 张；
5. 主推款补充细节参考板 0..1 张；两个主推款参考板合计承载最多 8 张已排序商品事实图，并按源顺序均衡分组；
6. 搭配服装参考板 0..1 张，合计承载最多 6 张被选中搭配款商品图。

总 `image_urls` 不得超过 6。参考板使用白底等比留白，不拉伸、不写文字，不改变原图颜色；一张来源图时直接使用原图。任务包和 attempt 快照必须记录每个参考板的角色、板类型、全部原始来源、顺序及拍摄预设版本。

## 生成参数

最终五张均固定使用：

```text
model: gpt-image-2
size: 1:1
resolution: 2k
quality: high
n: 1
```

请求使用 `/v1/images/generations`；存在 `image_urls` 时进入多参考图编辑模式，不传 `mask_url`。

## 状态机

```text
DRAFT
→ READY_FOR_STYLING
→ STYLING_RUNNING
→ STYLING_READY
→ FINAL_RUNNING
→ FINAL_PARTIAL / COMPLETED
```

失败统一进入 `FAILED`；取消进入 `CANCELLED`。新任务不再进入 `PREVIEW_RUNNING` 或 `PREVIEW_READY`。历史分镜与历史 attempt 保留只读复盘，不参与新的继续生成。

## 失效关系

- 修改主推款图片：选搭和成片全部失效；
- 重新选搭：成片失效；
- 修改或重新发布拍摄预设：不反向修改已冻结旧版本的任务，新一次成片使用最新已发布版本；
- 重新生成成片：不反向改变选搭。

`继续一键成片` 复用当前选搭和成片结果集，只重新执行失败、取消或尚未创建的成片槽位；成功槽位保持不变。只有用户明确执行“重新一键成片”时，才创建新的结果集并冻结当前拍摄预设版本。

## 明确不做

- 不提供分镜预览步骤或 API；
- 不保存新的分镜预览表记录；
- 不提供动态拍摄剧本或镜头计划；
- 不让 Gemini 动态生成成片 Prompt；
- 不传同组成片作为连续性锚点；
- 不引入自动视觉评分、自动验收或因质量判断触发的自动重生。

## 验收基准

- 商品、人物和背景事实来源边界见 [generation_responsibility_matrix.md](generation_responsibility_matrix.md)；
- 五类镜头的商业职责见 [final_image_business_intent.md](final_image_business_intent.md)；
- 三类五镜头规则见 [../02_brand_and_assets/category_shot_rules.md](../02_brand_and_assets/category_shot_rules.md)。
