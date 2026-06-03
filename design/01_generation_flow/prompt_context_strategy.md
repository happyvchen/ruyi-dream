# Prompt Context Strategy

## 背景

搭配服饰打标结果本身并不长。`styling_garments.ai_tags_json` 是 AI 打标的原始事实账本，应完整保存用于审计和详情展示。但生成链路过去把 `ai_tags_json`、`effective_tags_json`、`manual_tags_json`、`official_title_tags_json`、`tag_meta_json`、旧候选快照和旧 prompt 一起拼进下游请求，导致 prompt 体量被历史上下文和重复字段放大。

新版原则：每个阶段只接收完成本阶段判断所需的结构化 DTO，不直接透传上游完整对象或历史 prompt。

## 阶段输入

| 阶段 | Runtime | 输入内容 | 明确不传 |
|---|---|---|---|
| AI Stylist | `candidate_retrieval` | READY 搭配库紧凑候选卡 | 内部 id、完整标签 JSON、图片快照、旧 prompt |
| AI Director | `styling_result` | 主推款理解、选中 outfit、选中服饰紧凑标签、质量信号 | 全部 outfit 候选池、rejected 全文、AI Stylist prompt |
| 分镜预览 | `shoot_script_result` | 全局导演、风险、配饰、6 个镜头 | 完整 StylingPlan、候选池、原始标签 |
| 最终成片 | `current_shot_context` | 当前镜头、当前格子、全局导演、选中 outfit、本镜头配饰 | 完整 ShootScript、全部镜头外细节、候选池 |

## 搭配库候选卡

AI Stylist 的候选卡以搭配服饰 `code` 为唯一模型可见标识：

```json
{
  "code": "LOOK-A",
  "category": "上装",
  "colors": ["白色"],
  "materials": ["梭织"],
  "fit": "合体",
  "silhouette": "短款利落",
  "length": "及腰",
  "wearing_capabilities": ["可塞腰"],
  "style_summary": "通勤、清爽、轻量",
  "pairing_risks": ["可能遮挡腰线"],
  "manual_flags": {},
  "review": {"confidence": 0.86}
}
```

后端内部仍可在快照中保存 code/id 映射用于持久化，但 prompt 输入输出不暴露系统内部 ID。

## 下游压缩

AI Director 只需要“已选方案如何导演”，不需要重新看到整个搭配库。`styling_result` 保留：

- `hero_understanding`
- `styling_intents`
- `selected_outfit`
- `selected_material_tag_cards`
- `reason_summary`
- `quality_signals`
- `warnings`

分镜预览只读取 ShootScript 级别的导演信息和 6 个镜头。最终成片只读取当前镜头上下文，避免 5 张最终图重复携带整组剧本和完整选搭历史。

## 规则归属

- AI 选搭业务规则写在 `assembly.ai_styling` Prompt 正文。
- 拍摄导演规则写在 `assembly.shoot_script` Prompt 正文和 `shoot_configs`。
- 品牌资料来自 `brand_profiles` 和 Prompt 分段。
- 不再使用独立 `ai_policy_configs` 表。

## 非目标

本轮不实现 prompt 长度阈值、预算监控或自动截断告警。优化目标是通过正确 DTO 切分和删除旧兼容注入，从结构上减少上下文体量。
