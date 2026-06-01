# 拍摄剧本工作流最终设计

## 1. 设计结论

新版 AI 摄影棚的主流程调整为：

```text
上传主推款
-> AI 智能选搭
-> 生成拍摄剧本
   -> 生成拍摄剧本
   -> 生成分镜预览
-> 生成成片
```

用户侧使用“拍摄剧本”作为产品语言。系统侧使用 `ShootScript` 作为核心结构化产物。

`ShootScript` 是从 AI 选搭到实际拍摄之间的导演层产物：它一次性生成整组拍摄所需的全局导演设定和 6 个镜头 `ShotPlan`，承接主推款、选中搭配服饰、生成配饰边界、品牌规则、品类图位规则、镜头景别、取景范围、模特姿势、身姿走位、背景和光线。用户可以查看拍摄剧本，但 MVP 不允许编辑。

`分镜预览` 是拍摄剧本的可视化结果，即原“六宫格预览”。最终成片阶段每次只生成一个镜头，并传入分镜预览图作为一致性参考。

## 2. 命名规范

| 层级 | 用户侧名称 | 系统对象 / 技术名称 | 说明 |
|---|---|---|---|
| 造型阶段 | AI 智能选搭 | `GenerationStyling` / `StylingPlan` | 决定穿什么、怎么穿、为什么成立 |
| 导演阶段 | 拍摄剧本 | `GenerationShootScript` / `ShootScript` | 决定整组怎么拍 |
| 单镜头分镜 | 镜头方案 | `ShotPlan` | 6 个镜头的结构化导演说明 |
| 可视化预览 | 分镜预览 | `GenerationStoryboardPreview` | 3x2 分镜预览图 |
| 最终输出 | 成片 | `GenerationResultSet` / `GenerationResultImage` | 前 5 个镜头逐张生成 |

旧产品文案“六宫格预览”统一迁移为“分镜预览”。技术字段可以阶段性保留 `preview` 命名，但用户界面、设计文档和新增业务对象应使用“分镜预览 / storyboard preview”。

## 3. 产品原则

1. 用户不写长 prompt，用户确认拍摄信息和生成结果。
2. `AI 智能选搭` 负责造型判断，不负责拍摄导演。
3. `拍摄剧本` 负责导演判断，不重新选择库内搭配服饰。
4. `分镜预览` 是拍摄剧本的视觉样片，不是商品事实来源。
5. `生成成片` 使用当前最新主推款输入版本、AI 选搭、拍摄剧本、分镜预览和对应镜头生成单张图。
6. 每个用户动作都必须由后端持久化记录，刷新浏览器后不丢失状态、结果、失败原因和可恢复操作。
7. 每个阶段都支持重新生成；重新生成创建新版本，旧版本保留但从当前工作流失效。
8. 旧版数据只做一次性迁移，不继续兼容旧版工作流作为标准能力。

## 4. 阶段职责

### 4.1 上传主推款

用户上传本次主推款图片。系统创建 `GenerationTask` 草稿并持久化图片、排序和任务名。

上传阶段负责：

- 保存主推款图片。
- 支持刷新恢复草稿。
- 支持用户调整图片顺序、删除、更换和修改任务名。
- 进入 AI 阶段前冻结 `GenerationTaskInputVersion`。

上传阶段不负责：

- 选搭。
- 拍摄导演。
- 生图。

### 4.2 AI 智能选搭

`AI 智能选搭` 生成 `StylingPlan`。

负责：

- 理解主推款品类、卖点、不可遮挡区域和展示风险。
- 从搭配服饰库候选中选择真实库内服饰，或判断无需库内配角。
- 判断穿着方式：扣合、敞开、塞腰、半塞、自然下摆、叠穿等。
- 判断审美成立、Lily 品牌成立、商品主角成立。
- 输出选中方案、拒绝方案、评分、警告和解释。

不负责：

- 每个镜头怎么拍。
- 背景、光线、走位和镜头脚本细节。
- 生成最终图片。

### 4.3 生成拍摄剧本

用户点击“生成拍摄剧本”后，系统创建新的 `GenerationShootScript` 并调用 AI 导演一次性生成完整 `ShootScript`。

输入：

- 当前最新 `GenerationTaskInputVersion`。
- 当前最新成功 `GenerationStyling` / `StylingPlan`。
- 选中搭配服饰图片快照。
- 默认数字模特快照。
- 品牌信息和 Lily 默认拍摄模板。
- 标签规则中的品类、穿法、遮挡风险、风格风险等结构化标签。
- 基本品类图位规则。
- 生成配饰边界。

输出：

- 1 个全局导演设定。
- 6 个 `ShotPlan`。
- 库内搭配服饰风险与建议。
- 生成配饰调整说明。
- 用户可见摘要。
- 技术详情和 provider attempt 快照。

约束：

- 整个拍摄剧本包括 6 个 `ShotPlan`，由 AI 一次性生成。
- 用户可查看，不可编辑。
- 不做独立分镜质量质检；当前阶段把灵活度和判断权交给用户。
- 仍需做最小结构校验：JSON 可解析、字段存在、6 个镜头齐全、镜头序号唯一、第 6 格不进入最终成片。

### 4.4 生成分镜预览

用户点击“生成分镜预览”后，系统读取当前最新成功 `ShootScript`，调用 Image-2 生成一张 3x2 分镜预览图。

输入：

- 主推款图片。
- 选中搭配服饰图片。
- 数字模特图。
- 当前最新成功 `ShootScript`。

输出：

- `GenerationStoryboardPreview`。
- 一张 3x2 分镜预览图。
- attempt 请求/响应快照。

规则：

- 前 5 格对应最终 5 张成片。
- 第 6 格仅备用参考，不进入最终生成。
- 预览失败不影响拍摄剧本。
- 重新生成预览时创建新的预览版本，并使旧成片失效。

### 4.5 生成成片

用户点击“生成成片”后，系统创建新的 `GenerationResultSet`，按当前最新成功 `ShootScript` 的前 5 个 `ShotPlan` 并发生成 5 张单图。

每张成片输入：

- 主推款图片。
- 选中搭配服饰图片。
- 数字模特图。
- 当前最新分镜预览图。
- 当前镜头 `ShotPlan`。

规则：

- 每次 Image-2 调用只生成一个镜头。
- 分镜预览图只用于参考对应格的姿势、身位、景别、构图、背景光线和整组风格。
- 分镜预览图不是商品事实来源。
- 商品事实始终来自主推款原图和选中搭配服饰原图。
- 支持整组重新生成。
- 支持单张成片重生，单张重生不影响其他成功成片。

## 5. 状态机设计

### 5.1 总体策略

状态机采用：

```text
任务主状态 + 阶段 artifact 状态 + active_xxx_id 指针
```

不要只用一个 `GenerationTask.status` 承载所有中间状态。每个用户动作都必须创建或更新后端 artifact，确保刷新浏览器后可恢复。

所有长耗时阶段在外部模型或 provider 返回后、写入成功状态前，必须重新读取 `GenerationTask`、当前 artifact 和 attempt 状态。若用户已取消，原请求只能保持 `CANCELLED`，不得写回 ready/succeeded 状态，不得更新 active 指针，也不得 supersede 下游产物。

### 5.2 GenerationTask 主状态

```text
DRAFT
READY_FOR_STYLING
STYLING_RUNNING
STYLING_READY
SCRIPT_RUNNING
SCRIPT_READY
PREVIEW_RUNNING
PREVIEW_READY
FINAL_RUNNING
FINAL_PARTIAL
COMPLETED
FAILED
CANCELLED
ABANDONED
```

含义：

| 状态 | 含义 |
|---|---|
| `DRAFT` | 草稿，主推款可编辑 |
| `READY_FOR_STYLING` | 输入已冻结，可进入 AI 选搭 |
| `STYLING_RUNNING` | AI 智能选搭运行中 |
| `STYLING_READY` | 当前选搭成功，可生成拍摄剧本 |
| `SCRIPT_RUNNING` | 拍摄剧本生成中 |
| `SCRIPT_READY` | 当前拍摄剧本成功，可生成分镜预览 |
| `PREVIEW_RUNNING` | 分镜预览生成中 |
| `PREVIEW_READY` | 当前分镜预览成功，可生成成片 |
| `FINAL_RUNNING` | 成片整组生成中 |
| `FINAL_PARTIAL` | 部分成片成功，部分失败或运行中 |
| `COMPLETED` | 当前结果集前 5 张全部成功 |
| `FAILED` | 当前阶段失败，但历史 artifact 保留 |
| `CANCELLED` | 用户或系统取消 |
| `ABANDONED` | 用户放弃任务 |

`FAILED` 不是终点。页面应根据最新 artifact 判断可重试的动作。

### 5.3 Artifact 状态

所有阶段 artifact 使用统一状态：

```text
QUEUED
RUNNING
SUCCEEDED
FAILED
SUPERSEDED
```

适用对象：

- `GenerationStyling`
- `GenerationShootScript`
- `GenerationStoryboardPreview`
- `GenerationResultSet`
- `GenerationResultImage`

`SUPERSEDED` 表示曾经有效，但被新的上游版本替代。旧数据必须保留，不能删除。

### 5.4 Active 指针

`GenerationTask` 至少维护：

```text
current_input_version_id
active_styling_id
active_shoot_script_id
active_storyboard_preview_id
active_result_set_id
```

页面恢复时，只读取 active 指针作为当前工作流主线，同时允许在生成记录中查看旧版本。

## 6. 重新生成与失效规则

### 6.1 重新选搭

动作：用户点击“重新选搭”。

结果：

- 新建 `GenerationStyling`。
- 新选搭成功后更新 `active_styling_id`。
- 旧 `GenerationShootScript`、旧 `GenerationStoryboardPreview`、旧 `GenerationResultSet` 全部标记 `SUPERSEDED`。
- 数据库和 OSS 文件保留。

原因：

选搭改变后，下游拍摄剧本、预览和成片都不再代表当前造型。

### 6.2 重新生成拍摄剧本

动作：用户点击“重新生成剧本”。

结果：

- 新建 `GenerationShootScript`。
- 成功后更新 `active_shoot_script_id`。
- 旧分镜预览和旧成片标记 `SUPERSEDED`。
- 旧拍摄剧本保留在历史中。

失败：

- 保留当前已成功的旧拍摄剧本。
- 新失败 attempt 可查看错误。
- 用户可再次重试。

### 6.3 重新生成分镜预览

动作：用户点击“重新生成预览”。

结果：

- 新建 `GenerationStoryboardPreview`。
- 成功后更新 `active_storyboard_preview_id`。
- 旧成片标记 `SUPERSEDED`。
- 当前拍摄剧本不变。

失败：

- 保留当前拍摄剧本。
- 若存在旧成功预览，可继续显示但标记不是当前最新尝试。

### 6.4 重新生成成片

动作：用户点击“重新生成成片”。

结果：

- 新建 `GenerationResultSet`。
- 使用当前最新主推款输入版本、AI 选搭、拍摄剧本和分镜预览。
- 前 5 个镜头并发生成。
- 成功后更新 `active_result_set_id`。
- 旧结果集标记 `SUPERSEDED`。

### 6.5 单张成片重生

动作：用户在某张成片上点击“重生”。

结果：

- 对当前 `GenerationResultImage` 新增 attempt，或创建该单图的新版本记录。
- 复用当前最新主推款输入版本、AI 选搭、拍摄剧本、分镜预览和对应 `ShotPlan`。
- 不影响同组其他已成功成片。

## 7. ShootScript Schema

### 7.1 顶层结构

```json
{
  "schema_version": "shoot-script-v1",
  "script_source_mode": "production",
  "input_refs": {
    "task_id": 0,
    "input_version_id": 0,
    "styling_id": 0,
    "styling_plan_version": "styling-plan-v1",
    "model_snapshot_id": "Face-01"
  },
  "global_directing": {},
  "styling_risks": [],
  "accessory_adjustments": [],
  "resolved_accessories": [],
  "shots": [],
  "user_visible_summary": {},
  "warnings": []
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `schema_version` | 是 | 当前固定 `shoot-script-v1` |
| `script_source_mode` | 是 | `production` / `fallback` / `manual_import` |
| `input_refs` | 是 | 生成剧本时使用的输入引用 |
| `global_directing` | 是 | 整组拍摄导演设定 |
| `styling_risks` | 是 | 库内搭配服饰风险和建议 |
| `accessory_adjustments` | 是 | 生成类鞋包饰品调整说明 |
| `resolved_accessories` | 是 | 最终进入分镜预览和成片的配饰方案 |
| `shots` | 是 | 6 个镜头 |
| `user_visible_summary` | 是 | 前端可展示摘要 |
| `warnings` | 是 | 系统警告 |

### 7.2 global_directing

```json
{
  "brand_tone": "Lily 商务时装，现代、干净、轻熟、自信、克制，商品优先。",
  "background": "白色或浅灰棚拍背景，少量极简都市空间感，背景不抢服装主体。",
  "lighting": "柔和均匀、低反差、真实肤色，轻微落地阴影，面料边缘和结构清楚。",
  "model_direction": "同一位数字模特，自然、自信、从容、克制，姿态有轻微生活感。",
  "hair_makeup_boundary": "保持数字模特原有脸型、五官、头型、发型、刘海和气质；妆容干净自然。",
  "accessory_boundary": "鞋包首饰低存在感，可省略；不得遮挡主推款卖点，不做配饰特写。",
  "consistency_rules": [
    "6 个镜头必须是同一位模特、同一套造型、同一背景光线体系。",
    "只允许改变景别、取景范围、身位、姿势和轻微走位。"
  ],
  "product_fidelity_rules": [
    "主推款图片是主角商品事实来源。",
    "选中搭配服饰图片是配角商品事实来源。",
    "不得改款、换色、改变长度、比例、结构和关键细节。"
  ],
  "negative_constraints": [
    "不要文字、水印、logo、编号和边框说明。",
    "不要传统职业制服感、网红写真感、街头潮流感、夸张时装大片感。",
    "不要复杂背景、戏剧强光、浓妆、攻击性姿态或过度配饰。"
  ]
}
```

### 7.3 styling_risks

库内搭配服饰不合适时，AI 导演不能替换库内服饰，仍需正常输出拍摄剧本，并披露风险和建议。

```json
[
  {
    "risk_id": "outerwear_may_cover_waistline",
    "severity": "medium",
    "material_id": 123,
    "material_code": "LILY-STYLE-001",
    "risk": "外套在裤装腰脚图中可能遮挡主推款腰头和门襟。",
    "affected_shots": [2, 3],
    "director_strategy": "保持外套敞开并让手部避开腰头，图 2 和图 3 强制露出裤腰。",
    "user_visible_recommendation": "当前搭配可以继续拍摄，但腰头展示镜头需要特别避免外套遮挡。若预览仍遮挡，建议回到 AI 智能选搭重新选择更轻量上装。"
  }
]
```

`severity` 可选：

```text
low
medium
high
blocking
```

当前阶段即使 `blocking` 也不自动打回，只披露给用户。

### 7.4 accessory_adjustments

鞋、帽、包、首饰等生成类配饰不是库内商品事实。AI 导演可以在生成拍摄剧本时灵活调整，但必须披露。

```json
[
  {
    "item_type": "bag",
    "original_direction": "小号肩包",
    "adjusted_direction": "取消包袋，仅保留低存在感鞋履",
    "reason": "裤装主推款需要展示腰头、门襟和口袋，包袋容易遮挡核心卖点。",
    "affected_shots": [1, 2, 3]
  }
]
```

### 7.5 resolved_accessories

`resolved_accessories` 是最终配饰执行方案。`StylingPlan.generated_accessories` 只是 AI Stylist 的初始建议；如果 `ShootScript.accessory_adjustments` 对配饰做了保留、弱化、替换、取消或新增，分镜预览和最终成片必须以 `resolved_accessories` 为准。

```json
[
  {
    "item_type": "footwear",
    "final_direction": "极简深棕色尖头鞋，低存在感，只辅助腰带色彩闭环",
    "source": "director_adjusted",
    "visibility": "low",
    "applies_to_shots": [1, 2, 5],
    "reason": "呼应主推款腰带，不遮挡裙摆和腰部卖点。"
  },
  {
    "item_type": "bag",
    "final_direction": "取消包袋",
    "source": "removed",
    "visibility": "none",
    "applies_to_shots": [2, 3],
    "reason": "避免遮挡腰部结构和口袋细节。"
  }
]
```

每个 `shots[]` 还应包含 `accessory_instructions`，把最终配饰方案落到单镜头：

```json
{
  "shot_index": 1,
  "accessory_instructions": [
    "footwear: 极简深棕色尖头鞋，低存在感，不做特写。",
    "bag: 取消包袋。"
  ]
}
```

允许调整：

- 取消包。
- 更换为小体量低存在感包。
- 更换鞋型为低跟凉鞋、浅口鞋、乐福鞋、轻量短靴。
- 减少或取消首饰。

禁止：

- 把库内真实搭配服饰当作可随意调整对象。
- 生成抢主体的大 logo 包、大体量托特、公文包、厚底鞋、运动鞋、宴会感高跟或夸张首饰。

### 7.5 shots

固定 6 个镜头。前 5 个进入最终成片，第 6 个仅备用参考。

```json
{
  "shot_index": 1,
  "shot_role": "hero_full_body",
  "shot_role_label": "全身主视觉",
  "framing": "全身或近全身",
  "crop_range": "头顶到脚尖完整入画，裤脚和鞋完整。",
  "model_scale": "模特占画面高度约 85%-92%，保留干净边距。",
  "camera_angle": "正面或轻斜正，镜头平视。",
  "body_position": "轻斜正",
  "pose": "自然站立，重心轻微偏移，一腿微前。",
  "movement": "静态中带轻微走动感。",
  "hand_position": "手自然垂放或单手轻插袋，不遮挡主推款关键结构。",
  "main_focus": "完整比例、主推款轮廓、整体 look。",
  "visible_garments": ["hero", "styling:123"],
  "hero_selling_points": ["腰头", "裤褶", "裤腿垂坠"],
  "forbidden_occlusion_zones": ["腰头", "门襟", "口袋", "裤脚"],
  "composition_notes": "商品居中但不僵硬，浅灰白棚拍，留白干净。",
  "acceptance_criteria": [
    "主推款品类、颜色、版型和长度清楚。",
    "搭配服饰服务主推款，不抢视觉中心。",
    "姿势符合 Lily 克制通勤棚拍。"
  ],
  "negative_constraints": [
    "不要切掉裤脚。",
    "不要让包遮挡腰头或口袋。",
    "不要站军姿。"
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `shot_index` | 是 | 1-6 |
| `shot_role` | 是 | 机器可读图位 |
| `shot_role_label` | 是 | 用户可读图位 |
| `framing` | 是 | 景别 |
| `crop_range` | 是 | 取景范围 |
| `model_scale` | 是 | 模特占画幅比例 |
| `camera_angle` | 是 | 镜头角度 |
| `body_position` | 是 | 身位 |
| `pose` | 是 | 姿势 |
| `movement` | 是 | 身姿走位 / 动态 |
| `hand_position` | 是 | 手部位置 |
| `main_focus` | 是 | 当前镜头展示重点 |
| `visible_garments` | 是 | 必须可见服饰 |
| `hero_selling_points` | 是 | 主推款卖点 |
| `forbidden_occlusion_zones` | 是 | 不可遮挡区域 |
| `composition_notes` | 是 | 构图说明 |
| `acceptance_criteria` | 是 | 用户判断预览和成片是否可用的验收点 |
| `negative_constraints` | 是 | 当前镜头禁止项 |

### 7.6 user_visible_summary

```json
{
  "title": "浅灰棚拍通勤裤装拍摄剧本",
  "summary": "本组拍摄以完整裤型、腰头门襟和裤腿垂坠为核心，采用轻斜正、腰脚图、腰膝近景、侧身动态和正面标准图组成前 5 张成片。",
  "key_points": [
    "前 5 张覆盖完整比例、品类核心、细节、动态和标准正面。",
    "包袋不作为重点，避免遮挡腰头和口袋。",
    "姿态保持 Lily 克制通勤风格。"
  ],
  "risk_summary": "当前搭配可继续拍摄，但腰头展示镜头需要避免配角上衣或包袋遮挡。"
}
```

## 8. 品类图位规则

当前阶段支持标签规则中的所有品类，但不为每个子类写死完整镜头。采用：

```text
基础/默认 6 图结构 + 标签一级品类覆盖规则 + AI 导演发挥
```

`拍摄配置` 不维护独立品类字典。品类 6 镜头基础规则必须从 `标签规则` 的 `category` 一级品类中选择，继承其 `id`、`code`、`label`、`enabled` 和 `sortOrder` 等通用信息；拍摄配置只维护该品类独有的 `shots`。当某个一级品类未维护覆盖规则、被停用或被删除时，拍摄剧本生成使用基础/默认 6 镜头规则，并让 AI Director 根据主推款商品事实发挥。

`全局导演规则` 是可 CRUD 的条目列表，而不是固定字段表单。每条规则至少包含 `id`、`label`、`value`、`enabled`、`sortOrder`。最终组装 prompt 时，只按排序读取启用且有值的规则，因此管理员可以删除“品牌调性”、新增“品牌风格”、把“模特导演”改为“模特”，系统仍按当前配置动态组装。

### 8.1 通用 6 图结构

| 图位 | 用途 | 说明 |
|---|---|---|
| 1 | 全身主视觉 | 建立整体比例和第一视觉 |
| 2 | 品类核心展示 | 展示该品类最重要结构 |
| 3 | 设计细节近景 | 展示卖点、结构、面料或工艺 |
| 4 | 身位 / 动态补充 | 展示侧面、背面、走动或动态 |
| 5 | 标准正面颜色图 | 平台友好、颜色和版型稳定展示 |
| 6 | 备用姿态 | 只作预览参考，不进入最终成片 |

### 8.2 支持品类

根据标签规则，主推款品类至少覆盖：

```text
top
shirt
blouse
t_shirt
vest
camisole
knitwear
cardigan
bottom
trousers
jeans
shorts
skirt
dress
jumpsuit
suit_set
outerwear
blazer
jacket
trench
coat
down_jacket
other_garment
unknown
```

规则：

- `unknown` 不应进入正常生产链路；当前阶段如果必须继续，应使用安全通用图位，并在剧本 warnings 中披露。
- `unsupported_accessory` 不作为主推款正常品类。
- 子类可继承父类基础规则，例如 `shirt` 继承 `top`，`blazer` 继承 `outerwear`，`trousers` 和 `jeans` 继承 `bottom` 中的裤装规则。

### 8.3 基础品类方向

裤装 / 牛仔裤：

- 核心：腰头、门襟、裤褶、口袋、裤腿、裤脚、裤长和鞋的关系。
- 强制避免：切掉裤脚、包遮挡腰头、上衣完全遮住门襟。

半裙 / 短裤：

- 核心：腰头、臀胯、裙摆/裤口、长度、开衩或褶量。
- 强制避免：上装或包遮挡腰头和裙摆判断。

上衣 / 衬衫 / T 恤 / 针织：

- 核心：领口、肩线、袖型、胸腰松量、下摆、面料。
- 强制避免：下装、腰带或外套抢走上衣主体。

外套 / 西装 / 夹克 / 风衣 / 大衣：

- 核心：肩线、领型、门襟、扣位、衣长、口袋、后片结构。
- 强制避免：手臂贴死导致门襟不清、内搭关系混乱。

马甲 / 背心：

- 核心：V 领、袖笼、纽扣、腰线、下摆、层次关系。
- 强制避免：内搭或手部遮挡门襟和腰线。

连衣裙 / 连体类：

- 核心：整体裙型或连体比例、领口、袖型、腰线、裙长/裤长、后片和摆量。
- 强制避免：外套遮挡腰线和裙摆；包袋成为画面主体。

套装：

- 核心：上下装关系、肩线、门襟、腰线、下装版型和整体比例。
- 强制避免：只拍上半身导致下装关系缺失，或只拍下装导致套装关系缺失。

其他服装：

- 使用通用 6 图结构，让 AI 导演根据主推款理解选择核心展示点，并披露不确定性。

## 9. Lily 生产级默认配置

### 9.1 品牌资料

推荐生产填写：

```text
品牌名称：
Lily

品牌标语：
商务新风尚，成就你的每一步

品牌核心：
Lily 是面向现代职场女性的商务时装品牌，以 Professional + Fashion 为核心，不是传统职业制服，而是可通勤、可生活延展的都市女装。

风格 DNA：
Business Fashion、Modern Corporate Chic、Contemporary Women’s Workwear、Professional Elegance、Empowered Minimalism、Clean Urban Elegance、Soft Power Dressing、Structured yet Feminine。整体表达应现代、干净、轻熟、自信、利落，强调结构感与女性化平衡。

选搭准则：
搭配必须服务主推款。优先清晰腰线、干净廓形、轻量层次、低饱和都市色、挺括与垂感平衡的材质、低存在感精致配饰。避免厚重、沉闷、廉价职业感、配角服饰或配饰喧宾夺主。

拍摄准则：
采用商品导向的 commercial lookbook / e-commerce fashion photography。背景使用白色、浅灰、浅冷灰棚拍或极简都市空间。光线柔和均匀、低反差、真实肤色，服装结构、面料纹理和真实颜色清楚。模特姿态自然、自信、从容、克制，有轻微生活感。

负向约束：
避免传统职业制服感、银行柜员式职业装、过度性感、甜美少女感、网红写真感、街头潮流感、夸张时装大片感、强戏剧光、高冷奢侈品大片、复杂背景、浓妆、攻击性姿态、过度配饰、服装细节失真、面料质感不真实。
```

### 9.2 拍摄配置

推荐生产填写：

```json
{
  "schemaVersion": "shoot-config-v2",
  "globalRules": [
    { "label": "品牌调性", "value": "Lily 商务时装，现代、干净、轻熟、自信、克制，商品优先。", "enabled": true, "sortOrder": 10 },
    { "label": "背景", "value": "白色或浅灰棚拍背景，背景不抢服装主体。", "enabled": true, "sortOrder": 20 },
    { "label": "光线", "value": "柔和均匀、低反差、真实肤色，服装边缘和结构清楚。", "enabled": true, "sortOrder": 30 },
    { "label": "模特", "value": "同一位数字模特，自然、自信、从容、克制。", "enabled": true, "sortOrder": 40 },
    { "label": "妆发边界", "value": "保持数字模特原有脸型、五官、头型、发型和气质。", "enabled": true, "sortOrder": 50 },
    { "label": "配饰边界", "value": "鞋包首饰低存在感，可省略；不得遮挡主推款卖点。", "enabled": true, "sortOrder": 60 },
    { "label": "一致性规则", "value": "6 个镜头必须是同一位模特、同一套造型、同一背景光线体系。", "enabled": true, "sortOrder": 70 },
    { "label": "商品保真规则", "value": "主推款图片是主角商品事实来源。选中搭配服饰图片是配角商品事实来源。", "enabled": true, "sortOrder": 80 },
    { "label": "负向约束", "value": "不要文字、水印、logo、编号和边框说明。不要改款换色。不要让生成配饰成为视觉中心。", "enabled": true, "sortOrder": 90 }
  ],
  "defaultShotRule": { "shots": "固定 6 条，作为未命中品类规则时的基础图位" },
  "categoryShotRules": "从标签规则 category 一级品类中选择，只维护每类 6 条 shots"
}
```

品类规则 UI 必须展示标签规则来源信息，但不允许在拍摄配置中自由输入品类 code。新增品类规则时，用户只能从启用的一级品类中选择；已停用品类的历史规则可以保留和展示，但生成拍摄剧本时不再作为命中规则。

后台 `拍摄配置` 页面必须支持：

- Prompt 预览：渲染当前配置最终进入 AI Director 的拍摄配置规则段；预览底层与拍摄剧本生成调用使用同一个后端组装方法，不再提供多版本切换。
- 导出：导出当前配置 JSON 和标签一级品类快照。
- 导入：导入并覆盖当前配置；后端按当前标签规则过滤不可用、已删除或不应拍摄配置化的品类引用。

Prompt 预览只展示规则组装结果，不触发 Gemini、Image-2 或任何外部 provider 调用。

### 9.3 拍摄模板

```text
背景：
白色、浅灰或浅冷灰棚拍背景，可有极简都市室内感，但不得复杂外景化。

光线：
柔和均匀、低反差、真实肤色，保留轻微落地阴影，服装边缘和面料质感清楚。

镜头：
商业电商棚拍语言，比例自然，避免夸张广角、强拉腿、杂志大片或街拍感。

构图：
商品优先，留白干净。前 5 张组成商品说明，而不是 5 张重复全身姿势。
```

### 9.4 姿势走位边界

允许：

- 轻斜正。
- 3/4 斜正。
- 轻侧身。
- 重心偏移。
- 错腿。
- 自然垂手。
- 单手插袋。
- 轻扶腰、轻扶门襟。
- 轻走动。
- 侧身轻转。
- 回身展示后片。

避免：

- 站军姿。
- 双手僵直贴腿。
- 夸张扭腰。
- 过度张腿。
- 情绪化表演姿势。
- 网红摆拍。
- 时装秀式 pose。
- 手部遮挡主推款核心卖点。

### 9.5 生成配饰边界

允许：

- 无包。
- 小号手提包、手拿包、小肩包，低位出现。
- 低存在感低跟鞋、浅口鞋、细带凉鞋、乐福鞋、轻量短靴。
- 极简耳饰、手表、细手链。

避免：

- 大 logo 包。
- 大体量托特包。
- 商务公文包。
- 厚底鞋。
- 运动鞋。
- 宴会感高跟。
- 复杂绑带鞋。
- 抢主体的项链和夸张首饰。

## 10. 前端体验

### 10.1 主工作台步骤

前端步骤展示：

```text
1. 上传主推款
2. AI 智能选搭
3. 生成拍摄剧本
4. 生成分镜预览
5. 生成成片
```

当前测试验证阶段，`生成拍摄剧本` 和 `生成分镜预览` 都暴露为独立按钮。

### 10.2 按钮规则

| 当前状态 | 主按钮 | 重新生成按钮 |
|---|---|---|
| `READY_FOR_STYLING` | AI 智能选搭 | 无 |
| `STYLING_READY` | 生成拍摄剧本 | 重新选搭 |
| `SCRIPT_READY` | 生成分镜预览 | 重新生成剧本 |
| `PREVIEW_READY` | 生成成片 | 重新生成预览 |
| `COMPLETED` / `FINAL_PARTIAL` | 无或继续查看 | 重新生成成片、单张重生 |

运行中按钮应展示运行状态并禁用重复点击。刷新后继续显示后端记录的运行状态。

### 10.3 拍摄剧本展示

用户可见：

- 全局拍摄设定摘要。
- 6 个镜头卡片。
- 每个镜头的图位、景别、取景、姿势、走位、展示重点和不可遮挡区域。
- 库内搭配风险。
- 生成配饰调整。
- 用户可读建议。

用户不可编辑：

- 全局导演设定。
- 单镜头字段。
- JSON 原文。
- prompt。

技术详情可以展示完整 `ShootScript`、provider attempt 和请求快照，仅限需要排查时使用。

## 11. 后端数据模型建议

### 11.1 GenerationTask

新增或调整字段：

```text
active_shoot_script_id
active_storyboard_preview_id
```

保留：

```text
current_input_version_id
active_styling_id
active_result_set_id
```

### 11.2 GenerationShootScript

建议新表：

```text
generation_shoot_scripts
```

字段：

```text
id
task_id
input_version_id
styling_id
status
script_json
summary_json
styling_risks_json
accessory_adjustments_json
active_attempt_id
locked_by
locked_until
run_attempts
created_at
updated_at
superseded_at
```

### 11.3 GenerationShotPlan

可以继续使用 `generation_shot_plans`，但应从属于 `shoot_script_id`，而不是只从属于 `styling_id`。

建议字段：

```text
id
task_id
shoot_script_id
sort_order
shot_type
plan_json
created_at
updated_at
```

迁移期可保留 `styling_id` 作为冗余查询字段，但新增业务应以 `shoot_script_id` 为权威归属。

### 11.4 GenerationStoryboardPreview

现有 `generation_previews` 可改名或语义迁移为 `generation_storyboard_previews`。

字段：

```text
id
task_id
input_version_id
styling_id
shoot_script_id
preview_asset_id
status
active_attempt_id
locked_by
locked_until
run_attempts
created_at
updated_at
superseded_at
```

如果短期不改表名，API 和前端文案仍应使用“分镜预览”。

### 11.5 GenerationResultSet / GenerationResultImage

结果集必须引用：

```text
input_version_id
styling_id
shoot_script_id
storyboard_preview_id
```

单张结果必须引用：

```text
shot_plan_id
sort_order
status
current_asset_id
active_attempt_id
run_attempts
```

## 12. API 设计建议

用户动作 API：

```text
POST /api/generation-tasks/{id}/style
POST /api/generation-tasks/{id}/shoot-script
POST /api/generation-tasks/{id}/storyboard-preview
POST /api/generation-tasks/{id}/final-results
POST /api/generation-result-images/{id}/regenerate
```

恢复 API：

```text
GET /api/generation-tasks/{id}
GET /api/generation-tasks/{id}/status
```

要求：

- 所有 POST 必须先创建或更新后端记录，再进入异步任务。
- API 返回当前 `GenerationTask` 快照。
- 失败写入 artifact 和 attempt，不能只返回前端 toast。
- 状态接口必须足以让前端刷新后恢复页面。

## 13. Image-2 输入策略

### 13.1 分镜预览

输入：

- 主推款原图，按输入排序选前 3 张。
- 选中搭配服饰原图，每套至少 1 张，总计按预算截断。
- 数字模特参考图。
- `ShootScript` 文本化任务包。

不输入：

- 旧分镜预览。
- 旧成片。

### 13.2 成片单图

输入：

- 当前分镜预览图。
- 主推款原图。
- 选中搭配服饰原图。
- 数字模特参考图。
- 当前镜头 `ShotPlan`。

优先级：

```text
1. 主推款原图：主角商品事实。
2. 选中搭配服饰原图：配角商品事实。
3. 数字模特图：人物身份事实。
4. 当前 ShotPlan：景别、取景、姿势、走位、构图。
5. 分镜预览图：一致性参考，不覆盖商品事实。
```

## 14. 分镜质检策略

当前阶段不做独立业务质检，把判断权交给用户。

仍必须保留系统级最小校验：

- JSON 可解析。
- `schema_version` 正确。
- `shots` 恰好 6 条。
- `shot_index` 为 1-6 且不重复。
- 每条镜头有必填字段。
- 第 6 条不会进入最终成片。
- Image-2 任务包能被稳定组装。

不在当前阶段做：

- AI 审片。
- 自动判断姿势好不好看。
- 自动判断每个镜头是否挡住主推款。
- 自动打回选搭。
- 自动替换库内搭配服饰。

## 15. 旧数据迁移计划

原则：

- 新系统不兼容旧版工作流。
- 旧数据做一次性迁移。
- 迁移脚本不是长期标准功能。
- 迁移后数据进入新版对象模型或归档记录。

一次性迁移建议：

1. 旧 `generation_previews` 语义迁移为 `GenerationStoryboardPreview`。
2. 旧 `generation_shot_plans` 如果只有 `styling_id`，为其补建 `GenerationShootScript`：
   - `script_source_mode = "manual_import"` 或 `"legacy_migration"`。
   - 将旧 6 条 `ShotPlan` 放入 `shots`。
   - 补齐默认 `global_directing`。
3. 旧成片结果集补充 `shoot_script_id` 和 `storyboard_preview_id`。
4. 旧文案“六宫格预览”在 UI 统一显示为“分镜预览”。
5. 保留旧 OSS 文件，不移动大文件；只迁移数据库引用和语义字段。
6. 迁移完成后，不在运行时代码中保留旧版分支逻辑。

## 16. 开发顺序建议

1. 更新设计文档和 API/domain 文档。
2. 新增数据库迁移：`GenerationShootScript`、active 指针、预览/结果引用关系。
3. 后端实现拍摄剧本生成 artifact 和状态机。
4. 后端改造分镜预览读取 `ShootScript`。
5. 后端改造成片读取 `ShootScript` + `StoryboardPreview`。
6. 前端工作台新增“拍摄剧本”步骤和按钮。
7. 前端展示拍摄剧本摘要、6 个镜头、风险和配饰调整。
8. 实现重生与失效规则。
9. 一次性迁移旧数据。
10. 回归验证刷新恢复、失败重试、重新生成和单张重生。

## 17. 验收标准

### 17.1 状态恢复

- 用户在任意阶段刷新浏览器，页面能恢复当前任务、当前阶段、运行状态、成功结果和失败原因。
- 前端不依赖本地内存判断是否可以继续下一步。

### 17.2 拍摄剧本

- 用户点击“生成拍摄剧本”后，后端创建可追溯 artifact。
- 剧本成功后展示全局导演设定和 6 个镜头。
- 用户不可编辑剧本。
- 剧本失败后可重新生成，选搭结果保留。

### 17.3 风险披露

- 库内搭配服饰风险以用户可读形式展示。
- AI 导演不得替换库内服饰。
- 生成配饰调整必须展示调整前、调整后和原因。

### 17.4 分镜预览

- 分镜预览读取当前最新成功拍摄剧本。
- 预览失败不影响拍摄剧本。
- 重新生成预览会使旧成片失效但保留数据。

### 17.5 成片

- 成片按前 5 个镜头逐张生成。
- 每张成片传入当前分镜预览图作为一致性参考。
- 分镜预览不作为商品事实来源。
- 支持整组重新生成。
- 支持单张重生，不影响其他成片。

### 17.6 迁移

- 旧“六宫格预览”在用户界面显示为“分镜预览”。
- 旧数据迁移后能在新版生成记录中复盘。
- 运行时代码不把旧版工作流作为长期兼容路径。

## 18. 当前阶段明确不做

- 不做品牌样本库 / Brand Style Index。
- 不做用户编辑拍摄剧本。
- 不做自动分镜业务质检。
- 不做 AI 自动打回选搭。
- 不做库内搭配服饰自动替换。
- 不做多品牌、多租户、计费和复杂权限。
- 不把用户训练成 prompt 工程师。

这些能力可以在试点质量验证后进入后续版本设计。
