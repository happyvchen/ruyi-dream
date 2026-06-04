# Prompt Context Strategy

## 背景

搭配服装打标结果本身并不长。`styling_garments.ai_tags_json` 是 AI 打标的原始事实账本，应完整保存用于审计和详情展示。但生成链路过去把 `ai_tags_json`、`effective_tags_json`、`manual_tags_json`、`official_title_tags_json`、`tag_meta_json`、旧候选快照和历史 prompt 一起拼进下游请求，导致 prompt 体量被历史上下文和重复字段放大。

0.1.0 原则：每个生成步骤只接收完成当前判断所需的结构化 DTO，不直接透传上游完整对象或历史 prompt。AI Stylist 例外地接收未归档 READY 搭配库的完整 `ai_tags_json` 标签事实，因为这部分本身就是选搭判断所需的业务输入，而不是历史上下文。

## 阶段输入

| 阶段 | Runtime | 输入内容 | 明确不传 |
|---|---|---|---|
| AI Stylist | `candidate_retrieval` | READY 搭配库 `code` + 完整 `ai_tags_json` 标签事实卡 | 内部 id、图片快照、历史 prompt、`effective_tags_json` 派生摘要 |
| AI Stylist | `user_requirement` | 上传阶段补充服装信息、拍摄要求、本次选搭微调要求和冲突处理规则 | 长篇自由 prompt、可覆盖商品事实的模型指令 |
| AI Director | `styling_result` | 主推款理解、选中 outfit、选中服饰 `ai_tags` 标签事实、质量信号 | 全部 outfit 候选池、rejected 全文、AI Stylist prompt、未选中的全库候选 |
| 分镜预览 | `shoot_script_result` | 全局导演、风险、配饰、6 个镜头 | 完整 StylingPlan、候选池、原始标签 |
| 最终成片 | `current_shot_context` | 当前镜头、当前格子、全局导演、选中 outfit、本镜头配饰 | 完整 ShootScript、全部镜头外细节、候选池 |

## 搭配库标签事实卡

AI Stylist 的候选卡以搭配服装 `code` 为唯一模型可见标识，并直接携带 `styling_garments.ai_tags_json` 的解析结果。后端不得把标签字段写死成品类、颜色、材质等白名单摘要；AI 打标 Prompt 新增字段后，应自然进入 `ai_tags` 对象。

```json
{
  "code": "LOOK-A",
  "ai_tags": {
    "一级品类": ["上装"],
    "色彩": ["白色"],
    "面料材质": ["梭织"],
    "穿着能力": ["可塞腰"],
    "风格语义": "通勤、清爽、轻量",
    "风险点": ["可能遮挡腰线"]
  },
  "manual_flags": {},
  "review": {"confidence": 0.86}
}
```

后端内部仍可在快照中保存 code/id 映射用于持久化，但 prompt 输入输出不暴露系统内部 ID。最新实现只认 `ai_tags_json`；如果 READY 服饰缺少 `ai_tags_json`，应视为数据或打标状态问题处理，而不是用派生摘要替代。

## 下游压缩

AI Director 只需要“已选方案如何导演”，不需要重新看到整个搭配库。`styling_result` 保留：

- `hero_understanding`
- `styling_intents`
- `selected_outfit`
- `selected_material_tag_cards`
- `reason_summary`
- `quality_signals`
- `warnings`
- `user_requirement_resolution`

用户要求会作为本次输入版本的一部分冻结，并随 StylingPlan 快照进入下游。AI Director 不重新解释用户要求，只读取 AI Stylist 已解析后的 `user_requirement_resolution` 和选中造型结论；如果用户需要改变造型，应重新选搭，而不是在拍摄剧本阶段临时替换库内搭配服装。

分镜预览只读取 ShootScript 级别的导演信息和 6 个镜头。最终成片只读取当前镜头上下文，避免 5 张最终图重复携带整组剧本和完整选搭历史。

## 规则归属

- AI 选搭业务规则写在 `assembly.ai_styling` Prompt 正文。
- 用户要求优先级写在 AI Stylist 的 system instruction 和 `assembly.ai_styling` Prompt 正文：用户要求高于默认搭配偏好，但低于商品事实、搭配库事实、品牌硬边界、输出 schema 和安全规则。
- 拍摄导演规则写在 `assembly.shoot_script` Prompt 正文和 `shoot_configs`。
- 品牌资料来自 `brand_profiles` 和 Prompt 分段。
- 不再使用独立 `ai_policy_configs` 表。

## 非目标

本轮不实现 prompt 长度阈值、预算监控或自动截断告警。优化目标是通过正确 DTO 切分和删除旧兼容注入，从结构上减少上下文体量。
