# Lily Gemini 打标流程

## 1. 职责边界

Gemini 打标属于预处理层，只负责把一组服装图片转成可追溯的结构化标签。它不负责智能选搭、不负责 ShotPlan、不负责生成图片，也不负责维护人工业务标签。

核心职责：

```text
看图生成只读 AI 事实标签
+ 抽取官方标题证据
+ 标注置信度和风险
-> 后端合成 effective_tags
```

## 2. 入库预处理

用户上传一组服装图片后，后端创建 `GarmentAsset` / 当前实现中的 `styling_garments` 记录，状态为 `DRAFT`。

后端先完成非 AI 处理：

- 保存原图、缩略图、预览图。
- 保留图片顺序，第一张作为主视图候选。
- 记录文件名、尺寸、hash、mime、对象存储 key。
- 保存 code、可选官方标题或外部商品信息。
- 做图片格式、大小、解码校验。

点击保存并打标后，状态进入：

```text
TAGGING_RUNNING
```

## 3. Gemini 输入

每次打标输入包含：

- 当前服装资产的前 3-6 张图片。
- 图片顺序、图片 id、role hint。
- 可选官方标题。
- Lily 全局品牌边界。
- 严格 JSON 输出要求。

示例：

```json
{
  "asset_id": "garment_123",
  "code": "126239C5959601",
  "images": [
    {"image_id": "img_1", "role_hint": "primary"},
    {"image_id": "img_2", "role_hint": "reference"}
  ],
  "official_title": "LILY2026夏新款法式浪漫V领收腰休闲通勤藏青色泡泡袖短袖衬衫女",
  "brand_boundary": {
    "core": ["商务时装", "都市通勤", "简约", "优雅", "清新明快", "商品优先"],
    "avoid": ["传统严肃职业制服感", "网红辣妹风", "街头潮牌风", "宴会礼服感"]
  },
  "schema_version": "lily-garment-tags-v1"
}
```

Gemini 必须按以下优先级判断：

```text
图片事实 > 多图一致性 > 官方标题证据 > Lily 全局边界
```

## 4. Gemini 判断步骤

Gemini 应依次判断：

1. 这组图是否展示同一件服装。
2. 主商品是什么，不把模特、鞋、包、背景识别为服装商品。
3. 大类、子类、是否套装。
4. 颜色、图案、材质视觉、廓形、长度、领型、袖长、闭合方式、细节。
5. 穿着能力：可单穿、可内搭、可外搭、是否会遮挡主推款。
6. Lily 风格语义分数。
7. 场景、季节和搭配约束。
8. 官方标题里有哪些证据词，哪些不能作为图片事实。
9. 哪些字段低置信度或需要复核。

Gemini 不得：

- 用标题覆盖图片事实。
- 把 `老钱风`、`复古` 等标题词升级为 Lily 全局品牌调性。
- 把鞋、包、饰品作为可售服装资产。
- 输出长 prompt 或散文。
- 判定商品最终是否可用。

## 5. Gemini 输出结构

Gemini 返回严格 JSON：

```json
{
  "schema_version": "lily-garment-tags-v1",
  "ai_tags": {},
  "official_title_tags": {},
  "tag_meta": {}
}
```

### 5.1 `ai_tags`

`ai_tags` 是图片事实和图片推断，前台只读。

字段族：

- `category`
- `sub_category`
- `is_set`
- `colors`
- `materials_visual`
- `surface_texture`
- `fit`
- `silhouette`
- `structure`
- `length`
- `neckline`
- `closure`
- `details`
- `patterns`
- `wearability`
- `lily_style`
- `occasion`
- `season`
- `styling_compatibility`

### 5.2 `official_title_tags`

`official_title_tags` 是证据层，不覆盖图片事实。

字段族：

- `raw_title`
- `year`
- `season_text`
- `official_series`
- `official_material_terms`
- `official_style_terms`
- `official_category_terms`
- `official_selling_points`

### 5.3 `tag_meta`

`tag_meta` 保存可追溯信息：

- `tagger_provider`
- `tagger_model`
- `schema_version`
- `overall_confidence`
- `field_confidence`
- `needs_review`
- `review_reasons`
- `source_image_ids`
- `warnings`

## 6. 后端校验

后端收到 Gemini 响应后必须校验：

- JSON 可解析。
- 枚举值合法。
- 必填字段存在。
- 自由文本长度不超过限制。
- 置信度为 0-1。
- `category` 未把配饰当服装。
- 标题和图片冲突时记录 `title_image_conflict`。

失败时：

```text
status = TAGGING_FAILED
tag_error = 错误原因
```

成功时写入：

```text
ai_tags_json
official_title_tags_json
tag_meta_json
tag_schema_version
tag_completed_at
```

## 7. 后端合成 `effective_tags`

`effective_tags` 由后端合成，不由 Gemini 输出，也不由用户直接编辑。

合成规则：

```text
ai_tags
+ official_title_tags
+ manual_business_tags
+ brand_policy_tags
= effective_tags
```

优先级：

- 图片事实优先于标题词。
- 人工标签补充业务上下文，不改写视觉事实。
- 官方标题是证据层。
- Lily 品牌基调作为全局策略，不写成每件商品的散文标签。

## 8. 人工标签维护

前台搭配服饰库展示：

- AI 标签：只读。
- 官方标题信息：只读。
- 人工业务标签：可编辑。
- 技术详情：默认折叠。

人工标签只存 AI 无法从图片稳定判断的业务信息：

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

- 不触发 Gemini 重打标。
- 后端重新合成 `effective_tags`。
- 后续智能选搭读取新的 `effective_tags`。

## 9. 进入智能选搭

智能选搭不读取完整标签 JSON。后端把 `effective_tags` 压缩成候选卡片：

```json
{
  "asset_id": "garment_123",
  "code": "126239C5959601",
  "category": "top",
  "sub_category": "shirt",
  "colors": ["navy"],
  "fit": "regular",
  "silhouette": ["waist_defined"],
  "materials_visual": ["chiffon"],
  "details": ["v_neck", "button"],
  "occasion": ["commute", "office"],
  "season": ["summer"],
  "manual": {
    "season_year": "2026",
    "series": ["ART系列"],
    "do_not_use_for_styling": false
  },
  "pairing_brief": "适合搭配浅色直筒裤、阔腿裤或中长半裙",
  "risk": ["none"]
}
```

这时 Gemini 的职责变成“基于候选卡片选择配角服饰”，不是再次打标。

## 10. 关键结论

```text
Gemini 打标 = 图片事实识别 + 标题证据抽取 + 置信度标注。
后端治理 = 校验 + 来源分层 + effective_tags 合成。
人工运营 = 维护 AI 看不出来的业务标签。
智能选搭 = 使用压缩后的 effective_tags。
```
