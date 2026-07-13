# 当前分镜预览与成片工作流

## 当前主流程

```text
上传主推款
→ AI 智能选搭（Gemini，输出 StylingPlan）
→ 分镜预览（Image-2，按主推款品类选择固定 Prompt）
→ 最终成片（Image-2，并发生成分镜前 5 格）
```

0.1.0 当前主流程不再生成动态拍摄剧本，也不存在 `ShootScript` / `ShotPlan` 运行 artifact。分镜与成片的稳定规则集中维护在 Prompt 维护页；代码只负责品类路由、参考图顺序、当前镜头标记、商品信息、选中造型摘要和用户补充要求等动态事实。

## Prompt 槽位

分镜预览按主推款品类族选择三类固定 Prompt：

| 品类族 | Prompt 维护键 |
|---|---|
| 上装、针织、外套 | `assembly.storyboard_preview_top` |
| 裤装、半身裙 | `assembly.storyboard_preview_bottom` |
| 连衣裙、连体、套装 | `assembly.storyboard_preview_one_piece` |

最终成片按相同品类族选择三类固定 Prompt：

| 品类族 | Prompt 维护键 |
|---|---|
| 上装、针织、外套 | `assembly.final_image_top` |
| 裤装、半身裙 | `assembly.final_image_bottom` |
| 连衣裙、连体、套装 | `assembly.final_image_one_piece` |

## 动态事实输入

分镜预览只注入：

- 数字模特参考图；
- 主推款与选中搭配服装参考图；
- 拍摄环境参考图；
- `StylingPlan` 的自然语言摘要；
- 商品信息；
- 本次分镜预览用户补充要求。

最终成片只注入：

- 数字模特参考图；
- 当前分镜预览图和当前格号；
- 主推款与选中搭配服装参考图；
- 同一拍摄环境参考图；
- `StylingPlan` 的自然语言摘要；
- 商品信息；
- 本次成片用户补充要求。

## 状态机

```text
DRAFT
→ READY_FOR_STYLING
→ STYLING_RUNNING
→ STYLING_READY
→ PREVIEW_RUNNING
→ PREVIEW_READY
→ FINAL_RUNNING
→ FINAL_PARTIAL / COMPLETED
```

失败统一进入 `FAILED`；取消进入 `CANCELLED`。当前状态机没有 `SCRIPT_RUNNING` 或 `SCRIPT_READY`。

## 失效关系

- 修改主推款图片或商品信息：选搭、分镜预览、成片全部失效。
- 重新选搭：分镜预览和成片失效。
- 重新生成分镜预览：成片失效。
- 重新生成成片：不反向改变选搭或分镜预览。

## 明确不做

- 不提供“生成拍摄剧本”步骤或 API。
- 不保存动态拍摄剧本表、镜头计划表或相关 active 指针。
- 不让 Gemini 动态生成分镜/成片提示词。
- 不引入自动评分、自动验收、自动切格或自动重试作为默认链路。

## 验收基准

- 分镜预览原始业务意图见 [storyboard_preview_business_intent.md](storyboard_preview_business_intent.md)。
- 成片原始业务意图见 [final_image_business_intent.md](final_image_business_intent.md)。
- 分镜 Prompt 调研结论见 [storyboard_prompt_research_task_1440.md](storyboard_prompt_research_task_1440.md)。
- 成片 Prompt 调研结论见 [final_image_prompt_research_task_1457.md](final_image_prompt_research_task_1457.md)。
