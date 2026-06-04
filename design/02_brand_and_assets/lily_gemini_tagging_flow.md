# Lily Gemini 打标流程

## 1. 职责边界

Gemini 打标属于预处理层，只负责把一组服装图片转成可追溯的 AI 打标返回。它不负责智能选搭、不负责 ShootScript / ShotPlan、不负责生成图片，也不维护人工业务标签。

当前实现采用 `AI 打标` Prompt 自包含模式：

```text
服装图片
+ AI 打标 Prompt 中维护的全部规则
-> Gemini 返回严格 JSON
-> 后端原样保存 ai_tags_json
-> 后端最佳努力派生 effective_tags_json
-> 智能选搭读取 ai_tags_json 标签事实卡
```

`segment.tag_rules` 已废弃；`AI 打标` Prompt 是唯一的打标规则入口。分类口径、字段名、标签值、语义描述方式、置信度和风险点都以当前 `AI 打标` Prompt 为准。

## 2. 入库预处理

用户上传一组服装图片后，后端创建 `styling_garments` 记录，状态为 `DRAFT`。

后端先完成非 AI 处理：

- 保存原图、缩略图、预览图。
- 保留图片顺序，第一张作为主视图候选。
- 记录文件名、尺寸、hash、mime、对象存储 key。
- 保存 code。
- 做图片格式、大小、解码校验。

点击保存并打标后，状态进入：

```text
TAGGING_RUNNING
```

## 3. Gemini 输入

每次打标输入包含：

- 当前服装资产的前 3-6 张图片，作为 Gemini 多模态 image parts 传入。
- `AI 打标` Prompt 配置项中的完整规则、字段口径、输出结构和示例。
- provider attempt 中保存的图片顺序、图片 id、role hint、文件名、mime、尺寸、object key 和完整 prompt。

`AI 打标` 不需要 `{{segment.brand_info}}`，也不需要 `{{runtime.tagging_input}}`。图片本身通过 provider 的多模态输入传入；Prompt 只负责说明如何看图、如何打标、如何输出 JSON。

如果未来需要 code、文件名、官方标题或其它文字元数据参与判断，必须明确升级运行时输入契约，而不是隐式依赖旧的标签规则分段。

## 4. 判断原则

Gemini 应依次判断：

1. 这组图是否展示同一件服装。
2. 主商品是什么，不把模特、鞋、包、背景识别为服装商品。
3. 按当前 `AI 打标` Prompt 的分类口径判断品类和子类。
4. 判断颜色、图案、材质视觉、表面肌理、版型、廓形、结构、长度、领型、闭合方式、设计细节。
5. 判断穿法、穿着能力、场景、季节、搭配逻辑和风险点。
6. 按当前 Prompt 要求输出语义描述、置信度和其它字段。

Gemini 不得：

- 输出非 JSON 内容。
- 输出长 prompt 或散文替代结构化 JSON。
- 把鞋、包、饰品作为可售服装资产。
- 用图片外的想象覆盖图片事实。
- 判定商品最终是否可用。

## 5. 输出结构

Gemini 必须返回严格 JSON，但 JSON 字段结构由 `AI 打标` Prompt 决定，不再固定为 `lily-garment-tags-v1` 或 `ruyi-taxonomy-tagging-v1`。

允许的返回形态：

```json
{
  "一级品类": ["上装"],
  "二级品类": ["衬衫"],
  "色彩": ["白色"],
  "整体视觉语义": "白色衬衫，视觉干净利落。",
  "置信度": 88,
  "风险点": ""
}
```

也允许嵌套对象：

```json
{
  "服装标签卡": {
    "基础信息": {
      "一级品类": ["外套类"],
      "二级品类": ["西装"],
      "色彩": ["藏青色"]
    },
    "语义描述": {
      "整体视觉语义": "藏青色西装外套，结构清楚，适合通勤搭配。"
    }
  },
  "质量": {
    "置信度": 93
  }
}
```

如果 provider 返回数组或字符串等非对象 JSON，后端包装为：

```json
{
  "raw_result": []
}
```

## 6. 后端处理

后端收到 Gemini 响应后执行：

- 解析 JSON；不可解析属于不可恢复失败，进入 `TAGGING_FAILED`。
- 原样保存可解析 JSON 到 `ai_tags_json`；前端详情完整展示。
- 生成系统 `tag_meta_json`，记录 provider、model、result_format、overall_confidence、needs_review、review_reasons、source_image_ids 和 warnings。
- 从任意层级最佳努力读取常见字段别名，例如 `一级品类/category`、`色彩/color/colors`、`面料材质/material_visual`、`整体视觉语义/overall_summary`、`置信度/confidence`。
- 置信度支持 0-1 或 0-100；低于阈值进入复核。
- 后端不强制固定 taxonomy 枚举；分类口径以当前 `AI 打标` Prompt 为准。

provider 调用失败或返回内容无法解析为 JSON 时：

```text
status = TAGGING_FAILED
tag_error = 错误原因
```

JSON 可解析但存在系统可识别风险时：

```text
status = TAGGING_READY
```

包括：

- 低置信度。
- 空 JSON 或无有效内容。
- 使用 fallback 标签，未获得真实 Gemini 识别结果。
- 当前 Prompt 要求输出风险或复核原因，且后端能够从返回中识别。

正常可解析且无系统复核原因时：

```text
status = READY
```

成功写入：

```text
ai_tags_json
official_title_tags_json
effective_tags_json
tag_meta_json
tag_schema_version
tag_completed_at
```

## 7. 调用快照

每次打标都保存一次 provider attempt，包含：

- provider 与 model。
- 发送给模型的完整 prompt 和 result format。
- 图片输入的 id、排序、role hint、object key、文件名、mime 和尺寸元数据。
- provider 原始返回、归一化后的保存结果，或失败错误信息。
- 开始、完成或失败时间。

这些信息用于技术排查和模型质量回溯，前台默认折叠展示，不作为普通业务操作的主信息。

## 8. `effective_tags`

`effective_tags` 是后端从原始 AI 返回中最佳努力派生的压缩摘要，不由 Gemini 直接输出，也不由用户直接编辑。

合成规则：

```text
ai_tags_json 原始返回
+ official_title_tags_json
+ manual_business_tags
+ 后端字段别名派生
= effective_tags_json
```

关键原则：

- `ai_tags_json` 是事实来源和回溯来源，必须完整保存。
- `effective_tags_json` 服务列表摘要和局部展示，允许不完整；AI Stylist 只读取完整 `ai_tags_json` 标签事实卡。
- 人工标签补充业务上下文，不改写 AI 原始返回。
- 如果当前 `AI 打标` Prompt 改了字段名或结构，后端应继续保存完整返回，并尽量通过别名派生可用摘要；无法派生的字段不应导致打标失败。

## 9. 人工标签维护

前台搭配服装库展示：

- AI 打标返回：只读，完整展示原始 JSON。
- 人工业务标签：可编辑。
- 技术详情：默认折叠。

人工标签只存 AI 无法从图片稳定判断的业务信息，例如：

- `season_year`
- `season_drop`
- `launch_batch`
- `retail_channel`
- `series`
- `collaboration`
- `internal_collection`
- `styling_priority`
- `do_not_use_for_styling`

修改人工标签后：

- 不重新调用 Gemini。
- 后端重新合成 `effective_tags_json`。
- 后续智能选搭读取新的 `ai_tags_json` 标签事实卡片。

## 10. 选搭标签事实卡

智能选搭直接读取完整 `ai_tags_json`。后端只负责把未归档 READY 搭配服装按 `code` 组装成标签事实卡，并附带必要的人工业务标记和复核信息；进入 prompt 的卡片不暴露内部 id，也不把标签字段写死成固定摘要：

```json
{
  "code": "126239C5959601",
  "ai_tags": {
    "一级品类": ["外套类"],
    "色彩": ["藏青色"],
    "面料材质": ["羊毛感"],
    "版型": ["合体"],
    "穿着能力": ["可敞开外搭"],
    "风格语义": "藏青色西装外套，结构清楚，适合通勤搭配。",
    "风险点": []
  },
  "manual_flags": {},
  "review": {"confidence": 0.86}
}
```

标签事实卡是 AI Stylist 的选搭输入。`ai_tags_json` 的字段允许随 AI 打标 Prompt 演进，后端不得因为字段名不认识而丢弃。
