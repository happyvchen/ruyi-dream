# ruyi-dream 0.1.0 领域模型

## 当前主链路

```text
GenerationTask
→ GenerationTaskInputVersion
→ GenerationStyling / StylingPlan
→ GenerationStoryboardPreview
→ GenerationResultSet / GenerationResultImage x 5
```

0.1.0 当前主流程不包含动态拍摄剧本或镜头计划中间层。分镜六镜头和最终五镜头的稳定规则由 Prompt 维护中的三类固定分镜 Prompt 与三类固定成片 Prompt 承载；代码只负责拍摄图位族路由、参考图顺序、当前格号、选中造型摘要和用户补充要求等动态事实注入。

## 核心术语

- **主推款**：用户本次上传并希望重点表现的真实商品。
- **搭配服装**：搭配库中真实可见的配角服装，必须服务主推款。
- **AI 智能选搭 / StylingPlan**：AI Stylist 输出的造型方案，包含主推款理解、候选搭配、审美批判、选中搭配、穿法和风险。
- **分镜预览**：Image-2 生成的 3:2 六格预览图，用于固定整组构图、姿态、裁切、背景和组图语言。
- **成片**：Image-2 按分镜前 5 格逐张生成的最终电商棚拍图。

## 职责边界

| 要素 | 负责方 | 不负责 |
|---|---|---|
| 商品事实 | 主推款图、搭配服装图 | 分镜图凭空决定商品款式 |
| 人物身份 | 数字模特参考图 | Prompt 或 provider 默认审美改脸/改发型 |
| 造型判断 | AI Stylist / StylingPlan | Image-2 临场选搭 |
| 六镜头规则 | 三类固定分镜 Prompt | Gemini 动态生成镜头剧本 |
| 五张成片规则 | 三类固定成片 Prompt | 自动评分、自动切格、自动重试 |
| 状态与历史 | GenerationTask / attempts | provider 自己维护业务状态 |

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

失败进入 `FAILED`，取消进入 `CANCELLED`，归档进入 `ABANDONED` 或设置归档字段。当前状态机没有剧本相关状态。

## 失效规则

- 修改主推款图片或排序：当前选搭、分镜、成片失效，任务回到待选搭。
- 修改主推款图片：不立即清空当前结果；下一次执行选搭、分镜或成片时冻结最新图片顺序。
- 重新选搭：旧分镜和旧成片失效但保留历史。
- 重新生成分镜：旧成片失效但保留历史。
- 重新生成成片：只影响当前结果集，不反向改变选搭或分镜。
