# 分镜预览与成片工作流

> 本文保留原文件路径以避免文档入口断裂，但 0.1.0 当前设计已经删除“生成拍摄剧本”、`ShootScript` 与 `ShotPlan` 运行步骤。

## 当前主流程

```text
上传主推款
→ AI 智能选搭（Gemini，输出 StylingPlan）
→ 分镜预览（Image-2，按主推款品类选择固定 Prompt）
→ 最终成片（Image-2，并发生成分镜前 5 格）
```

分镜预览只接受以下动态事实：数字模特参考图、主推款与选中搭配服装参考图、拍摄环境参考图、`StylingPlan` 的自然语言摘要、商品信息和用户本次补充要求。系统不再调用 Gemini 动态生成镜头剧本，也不把结构化拍摄 JSON 传给 Image-2。

## 三类固定 Prompt

代码只负责根据 `StylingPlan.hero_understanding.category` 路由：

| 品类族 | Prompt 维护键 |
|---|---|
| 上装、针织、外套 | `assembly.storyboard_preview_top` |
| 裤装、半身裙 | `assembly.storyboard_preview_bottom` |
| 连衣裙、连体、套装 | `assembly.storyboard_preview_one_piece` |

六镜头、姿势、裁切、镜头 5 中央 2:3 安全区、背景墙地缝等稳定规则全部集中在以上 Prompt 中维护。代码只注入少量动态事实，不拼装另一套隐藏规则。

## 成片

成片阶段固定创建 5 个简单槽位，只保存 `sort_order` 与 `shot_role`。每张图使用同一分镜预览、同一 `StylingPlan`、同一商品/模特参考图，并明确参考分镜第 N 格。第 6 格不生成成片。

第一阶段不引入自动评分、自动验收、自动切格或自动重试。用户验证生成质量后，第二阶段再完成剩余清理与体验细节。

## 失效关系

- 重新上传或修改主推款：选搭、分镜与成片失效。
- 重新选搭：分镜与成片失效。
- 重新生成分镜：成片失效。
- 重新生成成片：不反向改变选搭与分镜。

## 验收基准

分镜原始业务意图与逐镜头标准以 [storyboard_preview_business_intent.md](storyboard_preview_business_intent.md) 为准；Prompt 实验结论以 [storyboard_prompt_research_task_1440.md](storyboard_prompt_research_task_1440.md) 为准；系统简化决策以 [storyboard_workflow_simplification_proposal.md](storyboard_workflow_simplification_proposal.md) 为准。
