# 当前分镜预览与成片工作流

## 当前主流程

```text
上传主推款
→ AI 智能选搭（Gemini，输出 selected_files + styling_plan）
→ 分镜预览（Image-2，按拍摄图位族选择固定 Prompt）
→ 最终成片（Image-2，并发生成分镜前 5 格）
```

0.1.0 当前主流程不再生成动态拍摄剧本，也不存在 `ShootScript` / `ShotPlan` 运行 artifact。分镜与成片的稳定规则集中维护在 Prompt 维护页；代码只负责拍摄图位族路由、参考图顺序、当前镜头标记、选中造型摘要和用户补充要求等动态事实。

## Prompt 槽位

分镜预览按 `styling_plan.hero_understanding.shot_category_family` 选择三类固定 Prompt；该字段缺失、非法或无法映射时，当前 AI 智能选搭结果视为契约失败，不做本地 fallback。商品品类不等于拍摄图位族，例如 `西装马甲` 的商品品类仍是西装马甲，但拍摄图位族必须由 `shot_category_family = top` 明确给出。

| 品类族 | Prompt 维护键 |
|---|---|
| 上装、针织、外套、马甲、西装马甲 | `assembly.storyboard_preview_top` |
| 裤装、半身裙 | `assembly.storyboard_preview_bottom` |
| 连衣裙、连体、套装 | `assembly.storyboard_preview_one_piece` |

最终成片按相同拍摄图位族选择三类固定 Prompt：

| 品类族 | Prompt 维护键 |
|---|---|
| 上装、针织、外套、马甲、西装马甲 | `assembly.final_image_top` |
| 裤装、半身裙 | `assembly.final_image_bottom` |
| 连衣裙、连体、套装 | `assembly.final_image_one_piece` |

## 动态事实输入

AI 智能选搭输入包括：

- 主推款候选图片，最多 15 张；
- 主推款图片文件标识；
- READY 且未归档的搭配服装标签事实；
- AI 选搭用户补充要求。

Gemini 返回 `selected_files` 与 `styling_plan`。`selected_files` 仅用于冻结本次主推款参考图顺序，不替代 `styling_plan.hero_understanding`。

分镜预览只注入：

- 数字模特参考拼图；
- 主推款与选中搭配服装参考拼图；
- 拍摄环境参考图；
- `StylingPlan` 的自然语言摘要；
- 本次分镜预览用户补充要求。

最终成片只注入：

- 数字模特参考拼图；
- 当前分镜预览图和当前格号；
- 主推款与选中搭配服装参考拼图；
- `StylingPlan` 的自然语言摘要；
- 本次成片用户补充要求。

ToApis Image-2 参考图上限为 6 张。分镜预览使用 `1 模特拼图 + 最多 2 主推款拼图 + 最多 2 搭配服装拼图 + 1 环境图`；最终成片使用 `1 模特拼图 + 最多 2 主推款拼图 + 最多 2 搭配服装拼图 + 1 当前分镜单格`，成片阶段不额外传环境图。

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

- 修改主推款图片：选搭、分镜预览、成片全部失效。
- 重新选搭：分镜预览和成片失效。
- 重新生成分镜预览：成片失效。
- 重新生成成片：不反向改变选搭或分镜预览。

`继续一键成片` 复用当前选搭、分镜和成片结果集，只重新执行失败、取消或尚未创建的成片槽位；状态为 `SUCCEEDED` 的步骤、槽位、图片和 Attempt 保持不变。只有用户明确执行“重新一键成片”时，才从头生成一套新方案。

## 自动技术恢复

`一键成片` 的自动推进状态和下一次重试时间持久化在 `GenerationTask`。进程重启或用户离开页面后，后台 poller 仍会从当前第一个未完成阶段继续；已经成功的选搭、分镜和成片槽位一律复用，不重复生成。

- Gemini 的连接失败、读取超时和明确限流会自动重发请求；Image-2 的参考图上传、提交对账、状态查询、明确瞬时生成失败和结果回传分别在本阶段内恢复。
- 每类默认最多自动重试 10 次，等待采用 `30 秒、60 秒、120 秒、240 秒`，之后保持 240 秒封顶；同时受阶段总超时和总预算约束。
- 每类错误只有一个重试所有者，不把阶段内 10 次与跨 attempt 10 次相乘；查询和回传失败保持原 provider 现场，不重新提交生成。
- 参数、鉴权、余额、内容策略、上传要求和业务契约错误不重试，避免掩盖需要用户或管理员处理的问题。
- 自动恢复预算耗尽后才落为 `失败` 或 `部分完成`；失败成片槽位可继续生成，已成功槽位不受影响。

## 明确不做

- 不提供“生成拍摄剧本”步骤或 API。
- 不保存动态拍摄剧本表、镜头计划表或相关 active 指针。
- 不让 Gemini 动态生成分镜/成片提示词。
- 不引入自动评分、自动验收、自动切格或因视觉质量判断触发的重新生成。上述自动技术恢复不是视觉质量重生，并完整保留 attempt 审计事实。

## 验收基准

- 分镜预览原始业务意图见 [storyboard_preview_business_intent.md](storyboard_preview_business_intent.md)。
- 成片原始业务意图见 [final_image_business_intent.md](final_image_business_intent.md)。
- 分镜 Prompt 调研结论见 [storyboard_prompt_research_task_1440.md](storyboard_prompt_research_task_1440.md)。
- 成片 Prompt 调研结论见 [final_image_prompt_research_task_1457.md](final_image_prompt_research_task_1457.md)。
