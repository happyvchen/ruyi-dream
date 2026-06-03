# Lily 服装视觉生产标签体系

## 1. 设计目标

Lily 标签体系是 Ruyi-Dream 的核心生产资产，不是临时 MVP 辅助字段。它要长期支撑同一件服装在不同任务中承担不同角色：

```text
服装资产 -> 可作为主推款 / 搭配服饰 / 历史成片参考 / 品牌视觉参考
```

因此系统不再把 `主推`、`搭配`、`成片` 设计成三套标签。所有真实服装统一进入 `GarmentAsset` 标签模型，在任务快照里再记录它当时扮演的角色。

核心目标：

- 支撑 Gemini 从全量可用服装中选择真实可见的配角服饰。
- 支撑 ShotPlan 判断穿着关系、图位、遮挡风险和商品展示重点。
- 支撑品牌参考图检索，约束 Image-2 的姿态、构图、鞋包和光影边界。
- 支撑生成记录复盘，能解释为什么选了这些服装。
- 支撑后续人工运营，但不把业务人员训练成 prompt 工程师。

当前实现说明：

- `AI 打标` Prompt 是打标规则和输出结构的唯一维护入口；`segment.tag_rules` 已废弃。
- Gemini 返回的 JSON 结构允许随 `AI 打标` Prompt 变化，后端必须完整保存原始 `ai_tags_json`。
- 后端只对常见字段做最佳努力派生，形成 `effective_tags_json` 供列表摘要和局部展示使用；智能选搭候选卡必须直接携带完整 `ai_tags_json` 标签事实，不能依赖固定字段白名单摘要。派生失败不应丢失原始返回。
- 本文中固定字段和枚举可作为长期标签治理参考，不再要求当前打标结果必须按固定 taxonomy schema 输出。

## 2. 品牌边界

### 2.1 全局品牌基调

Lily 的全局品牌基调来自官方品牌语言、天猫官方店铺和本地真实成片验证：

- 商务时装
- 都市通勤
- 年轻职业女性
- 简约
- 优雅
- 自信
- 清新明快
- 轻松但得体
- 时尚但不夸张
- 上班正合适，聚会亦出彩
- 商品优先
- 现代、利落、干净

这些词进入 `brand_policy_tags`，作为生成链路的长期边界，不需要在每件商品标签里反复写入。

### 2.2 单品标题词

天猫官方商品标题中可出现 `复古`、`老钱风`、`法式`、`小香风`、`浪漫`、`高级感`、`工装风`、`丹宁` 等词。这些词可以记录为标题抽取结果，但不能直接定义 Lily 的全局品牌调性。

规则：

- 来自官方标题的词进入 `official_title_tags`。
- AI 视觉识别可验证的词可映射到局部视觉标签。
- 全局品牌模板不能把这些词当作核心风格。

### 2.3 负向边界

默认生成和选搭应避免：

- 传统严肃职业制服感
- 夸张时装大片感
- 网红辣妹风
- 森系/甜美少女风
- 街头潮牌风
- 宴会礼服感
- 鞋包饰品抢服装主体
- 复杂外景或戏剧化强光

## 3. 标签来源分层

标签必须保存来源，避免 AI 判断、官方标题和人工业务标签混在一起。

```json
{
  "schema_version": "lily-garment-tags-v1",
  "ai_tags": {},
  "official_title_tags": {},
  "manual_business_tags": {},
  "effective_tags": {},
  "tag_meta": {}
}
```

### 3.1 `ai_tags`

AI 从图片判断，用户只读，不允许直接修改。

职责：

- 识别商品视觉事实。
- 识别服装结构、颜色、材质、廓形、细节和可见性。
- 识别穿着关系和搭配风险。
- 给出置信度和需要人工复核的字段。

### 3.2 `official_title_tags`

从 Lily 官方天猫标题、官方商品信息或客户提供的商品文案中抽取。它比普通网页可信，但仍低于图片事实。

职责：

- 保存年份、季节、新款、系列、面料、营销词、官方品类。
- 与 AI 视觉标签冲突时不覆盖图片事实，只进入证据层。

### 3.3 `manual_business_tags`

人工维护，允许修改。只存放 AI 无法从图片稳定判断的信息。

适合字段：

- `season_year`: `2025` / `2026`
- `season_drop`: `春季新品` / `夏季新品` / `冬新款`
- `launch_batch`: `5月13日上新`
- `retail_channel`: `商场同款`
- `series`: `LILY无界系列` / `ART系列` / `LILY大师系列` / `经典小黑裤系列`
- `collaboration`: `IP合作款`
- `internal_collection`: 客户内部企划系列
- `styling_priority`: 运营指定优先级
- `do_not_use_for_styling`: 运营临时禁用

不适合人工标签：

- 颜色、品类、袖长、领型、裙长、材质视觉、图案、版型等图片可判断事实。
- 这些应由 AI 识别并在必要时通过重打标或后台修正流程处理。

### 3.4 `effective_tags`

由系统合成，不由用户直接编辑。

合成优先级：

```text
ai_tags.image_facts
+ manual_business_tags.business_context
+ official_title_tags.official_context
+ brand_policy_tags.global_boundary
= effective_tags
```

当来源冲突：

- 图片事实优先于标题词。
- 人工业务标签只补充业务上下文，不改写视觉事实。
- 低置信度 AI 字段可标记 `needs_review`，但普通用户不直接编辑 AI 标签。

## 4. 统一服装资产模型

### 4.1 资产角色

服装资产本身不固化角色。角色只存在于任务快照中。

```json
{
  "asset_id": "garment_123",
  "roles_in_task": ["hero", "styling", "reference"]
}
```

可用角色：

- `hero`: 本次主推款。
- `styling`: 本次选中的配角服饰。
- `production_reference`: 历史成片参考图。
- `brand_reference`: 品牌姿态、构图、鞋包、光影参考。

### 4.2 资产状态

```text
DRAFT
TAGGING_RUNNING
TAGGING_READY
TAGGING_FAILED
READY
DISABLED
DELETED
```

只有 `READY` 进入智能选搭候选。`TAGGING_READY` 表示 AI 标签已生成，但存在低置信度、配饰误入、品类不明确、fallback 或其它复核原因；当前界面允许重新打标，暂不将该资产送入选搭。

## 5. AI 标签字段

### 5.1 基础分类

```json
{
  "category": "top | bottom | dress | jumpsuit | outerwear | knitwear | suit_set | other_garment | unsupported_accessory | unknown",
  "sub_category": "shirt | blouse | t_shirt | vest | camisole | cardigan | blazer | jacket | trench | coat | down_jacket | trousers | jeans | shorts | skirt | dress | other",
  "official_category_candidates": ["string"],
  "is_set": false
}
```

说明：

- `category` 是系统稳定大类。
- `sub_category` 是规范子类。
- `official_category_candidates` 保存天猫/标题原始分类，例如 `衬衫/雪纺衫`、`经典小黑裤系列`。

### 5.2 颜色

```json
{
  "colors": {
    "primary": "black | white | ivory | beige | grey | blue | navy | brown | green | red | pink | yellow | purple | metallic | multi | unknown",
    "secondary": ["string"],
    "temperature": "warm | cool | neutral | mixed | unknown",
    "contrast_level": "low | medium | high",
    "color_distribution": "solid | color_block | trim | gradient | print_based"
  }
}
```

### 5.3 材质视觉

```json
{
  "materials_visual": ["cotton_like", "linen_like", "silk_like", "satin_like", "acetate_like", "knit", "wool_like", "denim", "leather_like", "lace", "chiffon", "tweed_like", "down_padded", "technical_fabric", "unknown"],
  "surface_texture": ["smooth", "ribbed", "openwork", "slub", "woven_texture", "sheer", "glossy", "matte", "structured", "soft_drape"]
}
```

说明：AI 只能判断视觉材质，不能把标题里的 `100%桑蚕丝` 当作视觉事实。真实成分来自官方标题或人工业务标签。

### 5.4 版型和轮廓

```json
{
  "fit": "slim | regular | relaxed | loose | oversized | unknown",
  "silhouette": ["straight", "a_line", "h_line", "x_line", "wide_leg", "flare", "tapered", "cropped", "waist_defined", "boxy"],
  "structure": ["tailored", "soft", "draped", "layered", "structured_shoulder", "dropped_shoulder", "pleated", "gathered"]
}
```

映射 Lily 天猫筛选项：

- `常规款` / `标准` -> `regular`
- `宽松型` -> `relaxed` 或 `loose`
- `直筒型` -> `straight`
- `修身型` -> `slim`

### 5.5 品类细节

```json
{
  "length": {
    "top_length": "cropped | hip | long | unknown",
    "sleeve_length": "sleeveless | short | elbow | three_quarter | long | unknown",
    "bottom_length": "short | knee | midi | ankle | full | unknown"
  },
  "neckline": ["crew", "v_neck", "polo", "shirt_collar", "lapel", "boat", "square", "halter", "cowl", "unknown"],
  "closure": ["button", "zipper", "pullover", "wrap", "tie", "open_front", "unknown"],
  "details": ["asymmetric", "lace", "contrast_trim", "embroidery", "pocket", "pleat", "cutout", "patchwork", "print", "button", "ruffle", "bow_tie", "belt", "slit"]
}
```

映射 Lily 天猫筛选项：

- `不对称` -> `asymmetric`
- `蕾丝` -> `lace`
- `撞色` -> `contrast_trim`
- `绣花` -> `embroidery`
- `口袋` -> `pocket`
- `褶皱` -> `pleat`
- `镂空` -> `cutout`
- `拼接` -> `patchwork`
- `印花` -> `print`
- `纽扣` -> `button`

### 5.6 图案

```json
{
  "patterns": ["solid", "stripe", "check", "polka_dot", "floral", "graphic", "abstract", "texture_pattern", "logo", "animal", "unknown"],
  "pattern_scale": "none | small | medium | large | mixed"
}
```

### 5.7 穿着能力

```json
{
  "wearability": {
    "can_be_worn_alone": true,
    "can_be_inner_layer": false,
    "can_be_outer_layer": false,
    "can_be_bottom_pairing": false,
    "can_be_set_anchor": false,
    "recommended_wearing_methods": ["buttoned", "open", "tucked", "untucked", "belted", "layered"],
    "coverage_risk": "none | may_cover_hero | likely_cover_hero | unknown"
  }
}
```

用途：

- 避免外套、马甲、衬衫错误遮挡主推款核心卖点。
- 判断连衣裙是否需要下装搭配。
- 判断上装是否适合作为内搭或外搭。

### 5.8 Lily 风格语义

```json
{
  "lily_style": {
    "business_fashion_score": 0.0,
    "commute_score": 0.0,
    "clean_score": 0.0,
    "relaxed_score": 0.0,
    "feminine_score": 0.0,
    "fashion_detail_score": 0.0,
    "formality": "casual | smart_casual | office | formal | unknown",
    "style_notes": ["string"]
  }
}
```

说明：

- 使用分数而不是大量散标签，避免 `优雅/气质/高级/通勤` 在每件商品上失去区分度。
- `style_notes` 必须短，最多 3 条。

### 5.9 场景和季节

```json
{
  "occasion": ["daily", "commute", "office", "light_social", "date", "travel", "home"],
  "season": ["spring", "summer", "autumn", "winter"],
  "season_confidence": 0.0
}
```

映射 Lily 天猫筛选项：

- `日常` -> `daily`
- `旅游` -> `travel`
- `通勤` -> `commute`
- `聚会` -> `light_social`
- `居家` -> `home`
- `职场` -> `office`
- `约会` -> `date`

### 5.10 搭配约束

```json
{
  "styling_compatibility": {
    "best_pairing_categories": ["top", "bottom", "outerwear", "dress", "knitwear"],
    "avoid_pairing_categories": ["string"],
    "color_pairing": {
      "works_with": ["black", "white", "ivory", "beige", "grey", "navy", "brown"],
      "avoid_with": ["string"],
      "notes": "short string"
    },
    "silhouette_pairing": {
      "works_with": ["slim_top", "relaxed_top", "wide_leg_bottom", "straight_bottom", "a_line_skirt"],
      "avoid_with": ["string"],
      "notes": "short string"
    },
    "style_risk": ["too_formal", "too_casual", "too_sweet", "too_street", "too_party", "too_heavy", "too_busy", "none"],
    "pairing_brief": "short string"
  }
}
```

## 6. 官方标题标签

```json
{
  "official_title_tags": {
    "raw_title": "LILY2026夏新款...",
    "year": "2026",
    "season_text": "夏新款",
    "official_series": ["商场同款", "ART系列"],
    "official_material_terms": ["莱赛尔", "亚麻", "醋酸", "桑蚕丝", "莫代尔"],
    "official_style_terms": ["法式", "复古", "老钱风", "小香风"],
    "official_category_terms": ["衬衫", "雪纺衫", "西装外套"],
    "official_selling_points": ["收腰", "显瘦", "高腰", "凉感", "防晒"]
  }
}
```

规则：

- `official_style_terms` 不直接进入全局风格。
- `official_material_terms` 不直接覆盖 AI 视觉材质。
- 年份、系列、上新批次等可同步到人工业务标签或由运营确认。

## 7. 人工业务标签

```json
{
  "manual_business_tags": {
    "season_year": "2026",
    "season_drop": "夏季新品",
    "launch_batch": "5月13日上新",
    "retail_channel": ["商场同款"],
    "series": ["ART系列"],
    "collaboration": ["IP合作款"],
    "internal_collection": [],
    "styling_priority": "normal | preferred | deprioritized",
    "do_not_use_for_styling": false,
    "notes": "short internal note"
  }
}
```

前台规则：

- 搭配服饰库展示 AI 标签和人工标签。
- AI 标签只读，不提供编辑入口。
- 人工标签可编辑。
- 修改人工标签不触发 AI 重打标。
- 修改人工标签会更新 `effective_tags` 和后续选搭候选数据。

## 8. 生产参考标签

历史成片和商品模特图还需要服务品牌参考图检索。

```json
{
  "production_reference": {
    "image_role": "flat_lay | model_full_body | model_half_body | model_detail | color_swatch | back_view | side_view | unknown",
    "shot_type": "hero | full_body | half_body | side | back | detail | color | reference",
    "pose": ["natural_standing", "walking", "hand_in_pocket", "hand_on_waist", "slight_turn", "back_view"],
    "framing": "full_body | knee_up | waist_up | close_up | detail",
    "background": "white | light_grey | studio | minimal_indoor | other",
    "lighting": "soft | clean_shadow | high_key | unknown",
    "product_visibility": "high | medium | low",
    "reference_use": ["pose", "composition", "styling", "footwear", "bag", "lighting", "detail"]
  }
}
```

用途：

- 从历史成片中检索同品类姿态和构图参考。
- 给 ShotPlan 选择品牌真实图位语言。
- 控制鞋包配饰不偏离 Lily 电商棚拍边界。

## 9. 置信度与复核

```json
{
  "tag_meta": {
    "tagger_provider": "gemini",
    "tagger_model": "string",
    "tagged_at": "ISO-8601",
    "schema_version": "lily-garment-tags-v1",
    "overall_confidence": 0.0,
    "field_confidence": {
      "category": 0.0,
      "colors.primary": 0.0,
      "materials_visual": 0.0
    },
    "needs_review": false,
    "review_reasons": ["low_confidence", "conflicting_images", "unclear_category", "title_image_conflict"],
    "source_image_ids": ["asset_image_id"],
    "source_title_id": "optional"
  }
}
```

自动进入复核的情况：

- `category = unknown`
- `unsupported_accessory`
- `overall_confidence < 0.72`
- 标题和图片冲突，例如标题是连衣裙但图片是上衣。
- 多图之间显示不同商品且无法判断主商品。
- 商品图过少或只有细节图。

## 10. 选搭标签事实卡

智能选搭应直接读取 `styling_garments.ai_tags_json`。传给 Gemini 的候选项只需要隐藏系统内部 ID 和图片快照，不应把标签字段压缩成固定白名单摘要；AI 打标 Prompt 新增的字段必须自然保留在 `ai_tags` 对象里。

```json
{
  "code": "126239C5959601",
  "ai_tags": {
    "一级品类": ["上装"],
    "二级品类": ["衬衫"],
    "色彩": ["白色", "藏青色"],
    "面料材质": ["雪纺感", "蕾丝"],
    "穿着能力": ["可单穿", "可作内搭"],
    "风格语义": "轻通勤、清爽、精致",
    "风险点": []
  },
  "manual_flags": {},
  "review": {"confidence": 0.86}
}
```

## 11. Gemini 打标输出要求

Gemini 输出必须：

- 严格 JSON。
- 字段缺失时返回 `unknown` 或空数组，不编造。
- 不把官方标题当作图片事实。
- 不把鞋、包、饰品打成可售服装资产。
- 不输出长散文。
- 每个自由文本字段最多 80 个中文字符。
- 给出低置信度原因。

Gemini 不负责：

- 决定商品是否最终可用。
- 修改人工业务标签。
- 定义 Lily 全局品牌调性。
- 代替 Image-2 做视觉生成。

## 12. 前台展示规则

搭配服饰库详情抽屉建议分区：

1. `AI 识别标签`
   - 只读。
   - 展示品类、颜色、材质视觉、版型、细节、场景、搭配摘要、置信度。
   - 提供“重新打标”操作，但不允许逐字段编辑。

2. `人工业务标签`
   - 可编辑。
   - 展示年份、上新批次、系列、商场同款、IP 合作、内部分组、是否禁用选搭。

3. `官方标题信息`
   - 只读或后台导入。
   - 展示原始标题和抽取词。

4. `技术详情`
   - 默认折叠。
   - 展示完整 JSON、schema version、provider、时间、source image ids。
   - 展示最近的 Gemini 打标 attempts：请求 prompt、图片输入元数据、原始响应、归一化响应和失败信息。

展示要求：

- 列表卡片仍只展示少量摘要和状态，避免普通工作流变成标签控制台。
- 详情抽屉必须允许查看完整 `ai_tags`、`effective_tags`、`manual_business_tags`、`official_title_tags` 和 `tag_meta`，不能只展示压缩摘要。
- `TAGGING_READY` 对用户显示为 `待复核`；它不是可选搭状态，可提供重新打标入口。

## 13. 数据库落地建议

概念字段：

```text
garment_assets.ai_tags_json
garment_assets.official_title_tags_json
garment_assets.manual_business_tags_json
garment_assets.effective_tags_json
garment_assets.tag_status
garment_assets.tag_schema_version
garment_assets.tagged_at
garment_assets.review_status
```

如果当前实现仍沿用旧 `garment_skus`，可先通过新增 JSON 字段或新增标签表过渡，但领域命名应逐步收敛到 `GarmentAsset`。

每次 Gemini 打标调用还应保存 attempt 快照；当前实现可复用通用 `generation_attempts` 表，以 `owner_type = styling_garment_tagging`、`owner_id = styling_garment_id` 关联搭配服饰。

## 14. 版本演进

`lily-garment-tags-v1` 一旦用于生产，应避免破坏性修改。后续新增字段遵守：

- 新字段必须可为空或有默认值。
- 枚举新增可以兼容；枚举重命名必须保留别名映射。
- 删除字段必须至少经历一个版本的 deprecated。
- 大规模重打标前必须保存旧版本标签快照。
- 历史生成记录永远引用当时的标签快照。

## 15. 设计结论

最终版标签体系的关键不是字段尽可能多，而是来源分层和职责清楚：

```text
图片事实由 AI 标签承载；
业务年份/系列由人工标签承载；
官方标题作为证据层；
品牌基调作为全局策略；
选搭使用压缩后的 effective tags；
历史生成保存当时快照。
```

这样可以避免后续为了 `2026款`、`商场同款`、`ART系列` 这类 AI 无法看出的信息反复重打标，也能避免把标题营销词误写成 Lily 全局品牌风格。
