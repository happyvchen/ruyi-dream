# 素材与参数规范

## 1. 媒体资产

所有图片文件统一进入 `MediaAsset`。业务表只引用媒体资产，不把本地路径或对象 key 当作唯一业务事实。

每个图片资产维护三层衍生：

| 档位 | 建议规格 | 用途 |
|---|---|---|
| `thumb` | WebP，长边约 240px | 列表、历史缩略图、结果小卡片 |
| `preview` | WebP，长边约 1280px | 详情预览、弹窗查看 |
| `original` | 原始文件 | AI 输入、下载、质检、审计 |

规则：

- 原图必须保留。
- 历史快照只引用 asset id、object key、hash、尺寸、文件名，不复制图片二进制。
- 删除素材或图片默认软删除，不影响历史生成记录。
- 对象存储统一使用 S3 兼容接口，私有化默认 RustFS。

## 2. 主推款图片

主推款只属于本次 `GenerationTaskInputVersion`，MVP 不建长期主推款商品库。

上传规则：

- 最多 8 张，推荐 3-5 张。
- 支持散图或单文件夹。
- 不做正面/背面/细节角色识别。
- 上传顺序即优先级；第一张默认主视图。
- 同一任务内文件名重名需提示。

建议用户按以下顺序上传：

1. 正面全貌
2. 背面或侧面
3. 关键细节或面料
4. 其他补充角度

## 3. 搭配服饰

搭配服饰库保存可被真实生成到最终图里的配角服装。当前只支持服装，不支持鞋、包、首饰、配饰入库。

支持品类：

- `top` 上装
- `bottom` 下装
- `dress` 连衣裙/连体类
- `outerwear` 外套
- `set` 套装
- `other_garment` 其他服装

上传规则：

- 散图上传 = 一套搭配服饰。
- 一个文件夹 = 一套搭配服饰。
- 多个一级文件夹 = 多套搭配服饰。
- 单套最多 6 张，推荐 1-3 张。
- 前端可先列出超过 6 张图片用于整理；保存/导入时必须限制在 6 张以内。
- 不做图片角色识别，排序越靠前越重要。
- `code` 全局唯一、可修改；生成快照保存当时 code。
- 图片排序、替换、删除或追加后应提示会影响打标，并允许用户选择仅保存或保存并重新打标。
- 删除搭配服饰默认归档，不影响历史生成引用；级联删除必须二次确认，并删除实际选中过该服饰的生成任务、快照、任务上传素材和生成输出文件。
- 打标中的搭配服饰不可修改图片、标签或归档；选搭完成后，预览与成片使用已冻结的图片快照而不是当前库记录。

状态：

```text
DRAFT
TAGGING_RUNNING
TAGGING_READY
READY
TAGGING_FAILED
DELETED
```

只有 `READY` 状态进入智能选搭候选。`TAGGING_READY` 表示结构化标签已经生成，但因低置信度、识别为非服装配饰、品类不明确、fallback 打标或其它复核原因而暂不进入选搭；打标失败素材保留但不参与选搭。

## 4. 标签 schema

当前权威标签 schema 为 `lily-garment-tags-v1`，完整字段和枚举见 `lily_tagging_system.md`，实际 Gemini 打标规程见 `lily_gemini_tagging_flow.md`。

搭配服饰保留来源分层：

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

原则：

- `ai_tags` 承载图片可判断的视觉事实和搭配推断。
- `official_title_tags` 只保存官方文字证据，不覆盖图片事实。
- `manual_business_tags` 只维护年份、系列、禁用选搭等业务信息。
- `effective_tags` 由后端合成，并压缩后提供给候选召回和 AI Stylist。
- Gemini 打标请求必须提供枚举、评判规则、复核条件和严格输出模板，不能只给空 JSON 结构。
- 后端必须校验和归一化模型输出；需复核或 fallback 的标签不能直接进入 `READY`。

## 5. 品牌样本、候选漏斗与 StylingPlan

搭配服饰标签不是最终审美结论，而是候选漏斗的素材事实输入。生产链路必须区分：

- `effective_tags`：素材事实和搭配约束，用于召回、压缩和解释。
- `visual_caption` / `retrieval_features`：每件搭配服饰的短视觉摘要、衣长体量、遮挡风险、可塞腰/可敞开等检索字段。
- `brand_profile`：admin 维护的单一品牌调性资料，包括品牌名称、标语、品牌定位、视觉风格、搭配原则、拍摄调性和风格禁区；初始为空，不做双语结构。
- `brand_style_samples`：Lily 官方或客户确认的品牌样本，作为品牌风格和配饰边界证据。
- `candidate_retrieval_snapshot`：本次任务进入 AI Stylist 前的候选集合、排序和过滤原因。
- `StylingPlan`：AI Stylist 对主推款和候选服饰做造型判断后的结构化方案。

全量搭配服饰库不直接输入 Gemini。候选漏斗必须先完成硬过滤、召回、排序和 outfit 草案生成，再把少量候选卡片、必要缩略图和品牌样本证据交给 AI Stylist。

### 5.1 品牌信息

`brand_profile` 是 admin 在前台维护的单一品牌资料，不替代官方样本库，也不是品牌模板版本系统。MVP 当前字段为：

- `name`：品牌名称
- `tagline`：品牌标语
- `positioning`：品牌定位
- `visual_style`：视觉风格
- `styling_principles`：搭配原则
- `shooting_tone`：拍摄调性
- `style_avoidance`：风格禁区

规则：

- 后端不 seed 默认内容，初始所有字段为空。
- 所有字段均为字符串，不保存数组结构或双语 key/value 结构。
- 不支持双语字段，不维护中英文 key/value 成对结构。
- 字段用于约束选搭和拍摄判断，不作为品牌百科或营销话术库。
- 删除后恢复为空状态，不影响已有历史任务快照。
- 未来如果进入生成链路，应以结构化资料作为品牌参考输入，并在任务输入或 planner 快照中记录当时版本。

### 5.2 Lily Brand Style Index

`Lily Brand Style Index` 保存 Lily 官方风格证据，不是商品事实源。样本可来自：

- Lily 中国官网、广告大片、系列发布。
- Lily 官方天猫 / 淘宝店铺公开商品标题、商品图和分类页。
- Lily 海外官方旗舰店公开商品标题和商品图。
- 品牌官方微博、小红书、抖音等可验证官方内容。
- 试点客户提供的 lookbook、历史棚拍图和人工选搭参考。

建议字段：

- `source_type`
- `source_url` 或内部来源说明
- `season`
- `series`
- `category`
- `image_asset_id`
- `official_title`
- `visual_caption`
- `brand_style_tags`
- `accessory_boundary`
- `embedding`
- `confidence`
- `reviewed_by`
- `reviewed_at`

品牌样本只用于召回、排序、品牌解释和配饰边界。最终图的商品级保真仍以主推款图片和被选中搭配服饰图片为准。

### 5.3 生成配饰边界

鞋、包、首饰在 MVP 阶段不入库，因此只能作为受控文字边界：

- 配饰可以为空。
- 首饰默认极简、低存在感；没有明确理由时不生成项链。
- 鞋履优先裸色、米色、浅口、细带、低跟、乐福或简洁通勤鞋。
- 包默认可无；需要包时应小体量、侧拿或手持，不遮挡腰线、裤褶、口袋、门襟。
- 禁止自动生成公文包、商务手提公文包、大体量托特包等把画面拉向传统职业装的配饰。
- 配饰不得成为图位重点，不做特写，不作为商品级验收对象。

`StylingPlan` 至少保存：

- 主推款理解：品类、卖点、不可遮挡区域和展示风险。
- 造型意图：3-5 个可选造型方向。
- outfit 候选：每套候选的真实搭配服饰、受控生成配饰、穿着方式和主推款展示重点。
- Aesthetic Critic 诊断：色彩重量、廓形体量、腰线比例、季节材质、模板化风险和品牌采用可能性。
- 自评分数：审美、Lily 品牌贴合、主推款可见性、商业可用性。
- 选中方案、拒绝方案、品牌证据和原因。
- 最终 `selected_material_ids`，可为空。

商品可见性是底线，不是充分条件。搭配服饰必须服务主推款，也必须让主推款更有吸引力；如果整套 outfit 沉闷、笨重、廉价、比例失衡、色块压迫或不符合 Lily 调性，即使主推款清楚可见，也应判定为选搭失败。

## 6. 数字模特

MVP 只有一个后台维护的默认数字模特，前台只读。

展示字段：

- 名称
- 头像/半身/全身参考图
- 身高、体重、身型
- 胸围、腰围、臀围
- 其他气质和生成边界说明

生成时选择 2 张数字模特图进入 Image-2 输入，并保存模特快照。历史再次生成默认使用当时快照，不读取当前最新模特。

## 7. Image-2 输入图片预算

Image-2 单次最多 10 张输入图。

MVP 选图器：

- 主推款按排序取前 3 张。
- 搭配服饰每套至少取第 1 张，总计最多 4 张。
- 数字模特取 2 张。
- 最终生成额外传六宫格预览图。

如果 10 张预算冲突，MVP 暂用固定截断并记录实际输入资产；更精细的降级优先级记 TODO。

## 8. ShotPlan 字段

每个预览任务生成 6 条 ShotPlan。第 6 条在 MVP 最终生成时直接忽略。ShotPlan 必须读取选中的 `StylingPlan`，把造型意图、穿着方式、主推款卖点和风险落到每个图位。

字段：

- `sort_order`
- `shot_type`
- `framing`
- `pose`
- `camera_angle`
- `main_focus`
- `visible_garments`
- `hero_selling_points`
- `wearing_notes`
- `generated_accessory_notes`
- `background_lighting`
- `composition_notes`
- `negative_constraints`

生成配饰边界：

- 可以生成鞋、包、饰品。
- 不作为真实商品事实源。
- 不做特写。
- 不抢主推款和搭配服饰的视觉中心。

## 9. 输出与下载

MVP 只要求单张下载。批量 ZIP、统一命名、平台规格导出记为后续 TODO。

最终图默认 5 张，对应六宫格前 5 格。
