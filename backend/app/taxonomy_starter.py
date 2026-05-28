from __future__ import annotations

from typing import Any


def legacy_lily_taxonomy_starter_payload() -> dict[str, Any]:
    """A user-importable starter kit distilled from the previous Lily tagging prompt."""
    dimensions = [
        dim("category", "品类", "服装商品系统品类；先判断一级品类，再判断子品类。", 10),
        dim("color", "色彩", "商品主色和明确辅色；光影造成的偏色不作为标签。", 20),
        dim("color_attribute", "色彩属性", "冷暖、对比度、色彩分布等颜色判断辅助项。", 30),
        dim("material_visual", "视觉材质", "只判断图片可见的视觉材质，不把标题成分直接当作图片事实。", 40),
        dim("surface_texture", "表面肌理", "面料表面的纹理、光泽、透明度、垂坠或挺括观感。", 50),
        dim("fit", "版型松量", "贴身、合体、宽松、大廓形等穿着松量。", 60),
        dim("silhouette", "廓形", "直筒、A 字、H 型、X 型、阔腿、短款、收腰等整体轮廓。", 70),
        dim("structure", "结构", "剪裁结构、肩线、褶皱、层次、垂坠等结构判断。", 80),
        dim("length", "长度", "上衣长度、袖长、下装/裙装长度。", 90),
        dim("neckline", "领型", "圆领、V 领、衬衫领、翻驳领等可见领型。", 100),
        dim("closure", "闭合方式", "纽扣、拉链、套头、裹身、系带、开襟等。", 110),
        dim("design_detail", "设计细节", "口袋、压褶、开衩、腰带、撞色边等可见设计点。", 120),
        dim("pattern", "图案", "纯色、条纹、格纹、花卉、抽象、肌理图案等。", 130),
        dim("pattern_scale", "图案尺度", "图案大小和密度判断。", 140),
        dim("wearability", "穿着能力", "是否可单穿、做内搭、做外搭、做下装搭配或套装核心。", 150),
        dim("wearing_method", "推荐穿法", "扣合、敞开、塞腰、自然下摆、系腰带、叠穿等真实穿法。", 160),
        dim("coverage_risk", "遮挡风险", "作为搭配服饰时是否可能遮挡主推款。", 170),
        dim("lily_style_score", "Lily 风格分值", "0 到 1 的风格程度分，用于选搭排序和人工复核。", 180),
        dim("formality", "正式度", "休闲到正式的整体场合正式度。", 190),
        dim("occasion", "适用场景", "日常、通勤、办公室、轻社交、约会、旅行、居家等。", 200),
        dim("season", "适用季节", "根据材质厚薄、露肤和视觉重量判断。", 210),
        dim("pairing_logic", "搭配逻辑", "适合搭配或避免搭配的品类方向。", 220),
        dim("style_risk", "风格风险", "用于标记过正式、过休闲、过甜、过街头、过重等风险。", 230),
        dim("review_reason", "复核原因", "触发人工复核的原因。", 240),
        dim("manual_business", "人工业务信息", "季节批次、渠道、系列、内部优先级等非视觉事实。", 250),
    ]

    tags: list[dict[str, Any]] = []
    tags.extend([
        tag("category", "top", "上装", "衬衫、T 恤、背心等上半身主体单品。", sort=10),
        tag("category", "bottom", "下装", "裤装、半身裙、短裤等下半身单品。", sort=20),
        tag("category", "dress", "连衣裙", "一件式裙装，通常覆盖上装和下装位置。", sort=30),
        tag("category", "jumpsuit", "连体裤/连体衣", "一件式连体服装。", sort=40),
        tag("category", "outerwear", "外套", "西装、夹克、风衣、大衣、羽绒外套等外穿外套。", sort=50),
        tag("category", "knitwear", "针织单品", "以针织视觉为主，适合作为针织品类管理的单品。", sort=60),
        tag("category", "suit_set", "套装", "明确成套售卖或图片主体为套装。", sort=70),
        tag("category", "other_garment", "其他服装", "是服装但不能稳定归入其他品类。", sort=80),
        tag("category", "unsupported_accessory", "非服装配饰", "鞋、包、首饰、围巾、帽子等；MVP 不入搭配服饰库。", sort=90),
        tag("category", "unknown", "无法判断", "图片不足或无法判断主商品。", sort=100),
        tag("category", "shirt", "衬衫", "有衬衫结构、门襟或衬衫领的上装。", "top", 2, sort=10),
        tag("category", "blouse", "罩衫/雪纺衫", "偏轻盈、通勤或女性化的非 T 恤类上装。", "top", 2, sort=20),
        tag("category", "t_shirt", "T 恤", "T 恤类基础或休闲上装。", "top", 2, sort=30),
        tag("category", "vest", "马甲/背心", "无袖上装或叠穿马甲。", "top", 2, sort=40),
        tag("category", "camisole", "吊带", "吊带或细肩带上装。", "top", 2, sort=50),
        tag("category", "trousers", "长裤", "西装裤、直筒裤、阔腿裤等长裤。", "bottom", 2, sort=10),
        tag("category", "jeans", "牛仔裤", "牛仔材质裤装。", "bottom", 2, sort=20),
        tag("category", "shorts", "短裤", "短裤类下装。", "bottom", 2, sort=30),
        tag("category", "skirt", "半身裙", "半身裙类下装。", "bottom", 2, sort=40),
        tag("category", "cardigan", "针织开衫", "可开襟穿着的针织单品。", "knitwear", 2, sort=10),
        tag("category", "blazer", "西装外套", "带西装结构、翻驳领或职业剪裁感的外套。", "outerwear", 2, sort=10),
        tag("category", "jacket", "夹克/短外套", "偏夹克结构或短外套的外搭。", "outerwear", 2, sort=20),
        tag("category", "trench", "风衣", "具有风衣结构或轻中长外套特征。", "outerwear", 2, sort=30),
        tag("category", "coat", "大衣", "中长或长款厚外套。", "outerwear", 2, sort=40),
        tag("category", "down_jacket", "羽绒外套", "冬季保暖、体积感较强的外套。", "outerwear", 2, sort=50),
        tag("category", "other", "其他子类", "当前品类下无法稳定细分的子类。", "other_garment", 2, sort=10),
    ])

    tags.extend(list_tags("color", [
        ("black", "黑色"), ("white", "白色"), ("ivory", "象牙白/米白"), ("beige", "米色/浅卡其"),
        ("grey", "灰色"), ("blue", "蓝色"), ("navy", "藏蓝"), ("brown", "棕色"),
        ("green", "绿色"), ("red", "红色"), ("pink", "粉色"), ("yellow", "黄色"),
        ("purple", "紫色"), ("metallic", "金属色"), ("multi", "多色"), ("unknown", "无法判断"),
    ], "商品可见颜色。", 10))
    tags.extend(list_tags("color_attribute", [
        ("warm", "暖调"), ("cool", "冷调"), ("neutral", "中性调"), ("mixed", "混合冷暖"), ("low", "低对比"),
        ("medium", "中对比"), ("high", "高对比"), ("solid", "纯色"), ("color_block", "撞色/色块"),
        ("trim", "边饰异色"), ("gradient", "渐变"), ("print_based", "以图案形成色彩"),
        ("unknown", "无法判断"),
    ], "颜色属性判断。", 10))
    tags.extend(list_tags("material_visual", [
        ("cotton_like", "棉感"), ("linen_like", "亚麻感"), ("silk_like", "真丝感"), ("satin_like", "缎面感"),
        ("acetate_like", "醋酸感"), ("knit", "针织感"), ("wool_like", "羊毛/呢料感"), ("denim", "牛仔"),
        ("leather_like", "皮革感"), ("lace", "蕾丝"), ("chiffon", "雪纺"), ("tweed_like", "粗花呢/香风肌理"),
        ("down_padded", "羽绒/绗缝蓬感"), ("technical_fabric", "功能/科技面料感"), ("unknown", "无法判断"),
    ], "视觉材质。", 10))
    tags.extend(list_tags("surface_texture", [
        ("smooth", "平滑"), ("ribbed", "坑条/罗纹"), ("openwork", "镂空"), ("slub", "竹节/不匀纹理"),
        ("woven_texture", "织纹肌理"), ("sheer", "半透"), ("glossy", "有光泽"), ("matte", "哑光"),
        ("structured", "挺括"), ("soft_drape", "柔软垂坠"),
    ], "表面肌理。", 10))
    tags.extend(list_tags("fit", [
        ("slim", "修身"), ("regular", "合体"), ("relaxed", "略宽松"), ("loose", "宽松"),
        ("oversized", "大廓形"), ("unknown", "无法判断"),
    ], "版型松量。", 10))
    tags.extend(list_tags("silhouette", [
        ("straight", "直筒"), ("a_line", "A 字"), ("h_line", "H 型"), ("x_line", "X 型"),
        ("wide_leg", "阔腿"), ("flare", "微喇/喇叭"), ("tapered", "锥形"), ("cropped", "短款比例"),
        ("waist_defined", "收腰"), ("boxy", "方正轮廓"),
    ], "整体廓形。", 10))
    tags.extend(list_tags("structure", [
        ("tailored", "利落剪裁"), ("soft", "柔软结构"), ("draped", "垂坠结构"), ("layered", "层次结构"),
        ("structured_shoulder", "挺肩"), ("dropped_shoulder", "落肩"), ("pleated", "压褶"), ("gathered", "抽褶/聚拢"),
    ], "结构判断。", 10))
    tags.extend(list_tags("length", [
        ("top_cropped", "短上衣"), ("top_hip", "及胯上衣"), ("top_long", "长上衣"),
        ("sleeve_sleeveless", "无袖"), ("sleeve_short", "短袖"), ("sleeve_elbow", "肘袖"),
        ("sleeve_three_quarter", "七分袖"), ("sleeve_long", "长袖"),
        ("bottom_short", "短下装"), ("bottom_knee", "及膝"), ("bottom_midi", "中长"),
        ("bottom_ankle", "九分/踝长"), ("bottom_full", "全长"), ("unknown", "无法判断"),
    ], "长度判断。", 10))
    tags.extend(list_tags("neckline", [
        ("crew", "圆领"), ("v_neck", "V 领"), ("polo", "Polo 领"), ("shirt_collar", "衬衫领"),
        ("lapel", "翻驳领"), ("boat", "船领"), ("square", "方领"), ("halter", "挂脖"),
        ("cowl", "垂褶领"), ("unknown", "无法判断"),
    ], "领型。", 10))
    tags.extend(list_tags("closure", [
        ("button", "纽扣"), ("zipper", "拉链"), ("pullover", "套头"), ("wrap", "裹身"),
        ("tie", "系带"), ("open_front", "开襟/无闭合"), ("unknown", "无法判断"),
    ], "闭合方式。", 10))
    tags.extend(list_tags("design_detail", [
        ("asymmetric", "不对称"), ("lace", "蕾丝装饰"), ("contrast_trim", "撞色边/包边"), ("embroidery", "刺绣"),
        ("pocket", "口袋"), ("pleat", "褶/压褶"), ("cutout", "镂空/挖空"), ("patchwork", "拼接"),
        ("print", "印花设计"), ("button", "装饰纽扣"), ("ruffle", "荷叶边"), ("bow_tie", "蝴蝶结/飘带"),
        ("belt", "腰带/腰封"), ("slit", "开衩"),
    ], "设计细节。", 10))
    tags.extend(list_tags("pattern", [
        ("solid", "纯色"), ("stripe", "条纹"), ("check", "格纹"), ("polka_dot", "波点"),
        ("floral", "花卉"), ("graphic", "图形"), ("abstract", "抽象图案"), ("texture_pattern", "肌理图案"),
        ("logo", "Logo 图案"), ("animal", "动物纹"), ("unknown", "无法判断"),
    ], "图案。", 10))
    tags.extend(list_tags("pattern_scale", [
        ("none", "无图案"), ("small", "小图案"), ("medium", "中等图案"), ("large", "大图案"),
        ("mixed", "混合尺度"), ("unknown", "无法判断"),
    ], "图案尺度。", 10))
    tags.extend(list_tags("wearability", [
        ("can_be_worn_alone", "可单穿"), ("can_be_inner_layer", "可做内搭"), ("can_be_outer_layer", "可做外搭"),
        ("can_be_bottom_pairing", "可做下装搭配"), ("can_be_set_anchor", "可作为套装核心"),
    ], "穿着能力。", 10, value_kind="boolean"))
    tags.extend(list_tags("wearing_method", [
        ("buttoned", "扣合穿"), ("open", "敞开穿"), ("tucked", "塞腰穿"), ("untucked", "自然下摆"),
        ("belted", "系腰带"), ("layered", "叠穿"),
    ], "推荐穿法。", 10))
    tags.extend(list_tags("coverage_risk", [
        ("none", "无遮挡风险"), ("may_cover_hero", "可能遮挡主推款"), ("likely_cover_hero", "较可能遮挡主推款"),
        ("unknown", "无法判断"),
    ], "遮挡风险。", 10))
    tags.extend(list_tags("lily_style_score", [
        ("business_fashion_score", "商务时装感"), ("commute_score", "都市通勤适配"), ("clean_score", "干净利落感"),
        ("relaxed_score", "轻松得体感"), ("feminine_score", "女性化柔和/优雅"), ("fashion_detail_score", "设计细节感"),
    ], "0 到 1 风格分值。", 10, value_kind="score"))
    tags.extend(list_tags("formality", [
        ("casual", "休闲"), ("smart_casual", "轻商务/得体休闲"), ("office", "办公室/通勤"),
        ("formal", "正式"), ("unknown", "无法判断"),
    ], "正式度。", 10))
    tags.extend(list_tags("occasion", [
        ("daily", "日常"), ("commute", "通勤"), ("office", "办公室"), ("light_social", "轻社交"),
        ("date", "约会"), ("travel", "旅行"), ("home", "居家"),
    ], "适用场景。", 10))
    tags.extend(list_tags("season", [
        ("spring", "春季"), ("summer", "夏季"), ("autumn", "秋季"), ("winter", "冬季"),
    ], "适用季节。", 10))
    tags.extend(list_tags("pairing_logic", [
        ("best_pairing_top", "适合搭配上装"), ("best_pairing_bottom", "适合搭配下装"),
        ("best_pairing_outerwear", "适合搭配外套"), ("best_pairing_dress", "适合搭配连衣裙"),
        ("best_pairing_knitwear", "适合搭配针织"), ("avoid_pairing_top", "避免搭配上装"),
        ("avoid_pairing_bottom", "避免搭配下装"), ("avoid_pairing_outerwear", "避免搭配外套"),
        ("avoid_pairing_dress", "避免搭配连衣裙"), ("avoid_pairing_knitwear", "避免搭配针织"),
    ], "搭配方向。", 10))
    tags.extend(list_tags("style_risk", [
        ("too_formal", "过于正式"), ("too_casual", "过于休闲"), ("too_sweet", "过于甜美"),
        ("too_street", "过于街头"), ("too_party", "过于宴会/派对"), ("too_heavy", "视觉过重"),
        ("too_busy", "信息过满"), ("none", "无明显风险"),
    ], "风格风险。", 10))
    tags.extend(list_tags("review_reason", [
        ("low_confidence", "置信度低"), ("conflicting_images", "多图疑似不同商品"), ("unclear_category", "品类不清"),
        ("title_image_conflict", "标题与图片冲突"), ("insufficient_images", "图片不足"), ("unsupported_accessory", "非服装配饰"),
        ("uncertain_material", "材质不确定"), ("uncertain_color", "颜色不确定"), ("uncertain_fit", "版型不确定"),
        ("unmapped_observation", "有规则外观察"), ("invalid_tag_reference", "返回了非法标签引用"),
        ("missing_required_rule", "缺少必填适用规则"), ("disallowed_tag", "命中禁用适用规则"),
    ], "复核原因。", 10))
    tags.extend(list_tags("manual_business", [
        ("season_year", "季节年份"), ("season_drop", "季节波段"), ("launch_batch", "上新批次"),
        ("retail_channel", "销售渠道"), ("series", "系列"), ("collaboration", "联名"),
        ("internal_collection", "内部集合"), ("styling_priority", "选搭优先级"),
        ("do_not_use_for_styling", "不参与选搭"), ("notes", "业务备注"),
    ], "人工业务信息。", 10, value_kind="text"))

    relations = [
        rel("category", "blazer", "slot_mapping", "wearability", "can_be_outer_layer", 1, "西装外套通常作为外搭层。"),
        rel("category", "cardigan", "slot_mapping", "wearability", "can_be_outer_layer", 0.8, "针织开衫可作为轻外搭。"),
        rel("category", "dress", "slot_mapping", "wearability", "can_be_worn_alone", 1, "连衣裙通常可单穿。"),
        rel("category", "bottom", "slot_mapping", "wearability", "can_be_bottom_pairing", 1, "下装用于与上装搭配。"),
        rel("category", "down_jacket", "style_mapping", "style_risk", "too_heavy", 0.7, "羽绒外套通常视觉量感更强。"),
        rel("material_visual", "tweed_like", "style_mapping", "lily_style_score", "feminine_score", 0.6, "粗花呢/香风肌理通常带精致女性化倾向。"),
    ]

    applicability_rules = []
    applicability_rules.extend(scope("top", [
        ("neckline", "*"), ("length", "top_cropped"), ("length", "top_hip"), ("length", "top_long"),
        ("length", "sleeve_sleeveless"), ("length", "sleeve_short"), ("length", "sleeve_elbow"), ("length", "sleeve_three_quarter"), ("length", "sleeve_long"),
        ("closure", "*"), ("design_detail", "*"), ("fit", "*"), ("silhouette", "*"), ("wearability", "can_be_worn_alone"), ("wearability", "can_be_inner_layer"),
    ]))
    applicability_rules.extend(scope("outerwear", [
        ("neckline", "lapel"), ("neckline", "shirt_collar"), ("closure", "button"), ("closure", "zipper"), ("closure", "open_front"),
        ("length", "top_cropped"), ("length", "top_hip"), ("length", "top_long"), ("length", "sleeve_long"),
        ("structure", "tailored"), ("structure", "structured_shoulder"), ("structure", "dropped_shoulder"), ("structure", "layered"),
        ("design_detail", "pocket"), ("design_detail", "contrast_trim"), ("design_detail", "belt"), ("wearability", "can_be_outer_layer"), ("coverage_risk", "*"),
    ]))
    applicability_rules.extend(scope("bottom", [
        ("length", "bottom_short"), ("length", "bottom_knee"), ("length", "bottom_midi"), ("length", "bottom_ankle"), ("length", "bottom_full"),
        ("closure", "button"), ("closure", "zipper"), ("design_detail", "pocket"), ("design_detail", "pleat"), ("design_detail", "slit"),
        ("silhouette", "straight"), ("silhouette", "a_line"), ("silhouette", "wide_leg"), ("silhouette", "flare"), ("silhouette", "tapered"),
        ("wearability", "can_be_bottom_pairing"),
    ]))
    applicability_rules.extend(scope("dress", [
        ("neckline", "*"), ("closure", "*"), ("length", "bottom_knee"), ("length", "bottom_midi"), ("length", "bottom_ankle"), ("length", "bottom_full"),
        ("length", "sleeve_sleeveless"), ("length", "sleeve_short"), ("length", "sleeve_three_quarter"), ("length", "sleeve_long"),
        ("silhouette", "a_line"), ("silhouette", "h_line"), ("silhouette", "x_line"), ("silhouette", "waist_defined"),
        ("design_detail", "slit"), ("design_detail", "belt"), ("design_detail", "ruffle"), ("design_detail", "lace"), ("wearability", "can_be_worn_alone"),
    ]))
    applicability_rules.extend(scope("knitwear", [
        ("material_visual", "knit"), ("surface_texture", "ribbed"), ("surface_texture", "soft_drape"),
        ("neckline", "*"), ("length", "top_cropped"), ("length", "top_hip"), ("length", "top_long"),
        ("length", "sleeve_short"), ("length", "sleeve_three_quarter"), ("length", "sleeve_long"),
        ("structure", "soft"), ("structure", "dropped_shoulder"), ("wearability", "can_be_inner_layer"), ("wearability", "can_be_outer_layer"),
    ]))

    return {
        "schemaVersion": "ruyi-taxonomy-rules-v1",
        "source": "legacy-lily-prompt-distilled",
        "description": "从旧版 Lily Gemini 打标 Prompt、枚举和复核规则提炼出的新版标签规则 starter kit；不会自动入库，需要管理员手动导入。",
        "dimensions": dimensions,
        "tags": tags,
        "relations": relations,
        "applicabilityRules": applicability_rules,
    }


def dim(code: str, label: str, description: str, sort: int) -> dict[str, Any]:
    return {"code": code, "labelZh": label, "descriptionZh": description, "sortOrder": sort, "isActive": True}


def tag(
    dimension: str,
    code: str,
    label: str,
    description: str,
    parent: str | None = None,
    level: int = 1,
    *,
    sort: int = 0,
    value_kind: str = "tag",
    selectable: bool = True,
) -> dict[str, Any]:
    return {
        "dimensionCode": dimension,
        "code": code,
        "labelZh": label,
        "descriptionZh": description,
        "parentCode": parent,
        "level": level,
        "valueKind": value_kind,
        "isSelectable": selectable,
        "isActive": True,
        "sortOrder": sort,
    }


def list_tags(dimension: str, items: list[tuple[str, str]], description: str, start_sort: int, *, value_kind: str = "tag") -> list[dict[str, Any]]:
    return [
        tag(dimension, code, label, description, sort=start_sort + index * 10, value_kind=value_kind)
        for index, (code, label) in enumerate(items)
    ]


def rel(from_dimension: str, from_tag: str, relation_type: str, to_dimension: str, to_tag: str, weight: float, description: str) -> dict[str, Any]:
    return {
        "fromDimensionCode": from_dimension,
        "fromTagCode": from_tag,
        "relationType": relation_type,
        "toDimensionCode": to_dimension,
        "toTagCode": to_tag,
        "weight": weight,
        "descriptionZh": description,
        "isActive": True,
    }


def scope(category_tag: str, targets: list[tuple[str, str]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for dimension, code in targets:
        if code == "*":
            continue
        expanded.append({
            "categoryDimensionCode": "category",
            "categoryTagCode": category_tag,
            "targetDimensionCode": dimension,
            "targetTagCode": code,
            "mode": "allowed",
            "appliesToDescendants": True,
            "note": "按品类允许的部件或设计细节。",
            "isActive": True,
        })
    return expanded
