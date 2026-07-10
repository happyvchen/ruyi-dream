# Prompt 渲染层重构设计

> 状态：P0–P5 已落地；**P6（占位变量化重构）已落地**，见 §10。目标是修正当前 prompt 组装偏离 `image2_input_strategy.md` §5/§6 与 `prompt_context_strategy.md` 的问题，把「快照模型」与「prompt 视图」解耦，并把所有固定 prompt 文本收敛到「Prompt 维护」页。

## 1. 问题

当前所有模型调用共用同一个反模式：**先构造一个「上下文对象」，再 `json.dumps` 整个塞进 prompt。** 这个对象把三种本该分离的关注点混在一层：

| 关注点 | 服务对象 | 例子 |
|---|---|---|
| 真实内容 | 模型需要 | `shots`、`global_directing`、hero 信息、参考图角色 |
| 治理元数据 | 快照审计 / 人 | `schema_version`、`priority_policy`、`cannot_override`、`priority_rules`、`conflict_handling` |
| 快照结构 | 持久化 / 回溯 | 包装层级、内部 id 映射 |

对**生图模型（Image-2）**而言，治理元数据是纯噪音——它不会、也不该去解析 `cannot_override` 数组。

### 1.1 代码定位

噪音在这些函数的**顶层**注入，然后被直接 `dumps()`：

- `build_hero_product_info_context`（`main.py:4045`）→ 顶层 `schema_version` + `priority_policy`
- `build_stage_user_instruction_context`（`main.py:4063`）→ 顶层 `schema_version` + `priority_policy` + `cannot_override`
- `build_user_requirement_context`（`main.py:4085`）→ 同上
- `shoot_script_result_for_prompt`（`main.py:5912`）→ 顶层 `schema_version`
- `image_reference_manifest_for_prompt`（`main.py:6611`）→ 顶层 `schema_version` + `priority_rules`
- `append_user_requirement_block` / `append_runtime_input_block`（`main.py:4113/4125`）、`append_image_reference_manifest`（`main.py:6642`）→ 一律 `dumps(context)` 追加

已存在的 `strip_internal_prompt_fields`（`main.py:5832`）会剥掉**嵌套**的 `schema_version`，但上述函数在**顶层**重新加回来并 `dumps`，等于剥了个寂寞。

### 1.2 这不是审美问题，是 spec 偏离

项目自己的权威文档 `image2_input_strategy.md` §5/§6 **已经规定**了推荐的 Image-2 prompt 结构：分节自然语言、6 个镜头逐格展开、参考图清单一句话说明角色——完全不含 `schema_version` / `priority_policy`。当前实现偏离了这份 spec。修它是**回归 spec**，不是发明新设计。

### 1.3 真实快照证据（本地渲染，未调用任何 provider）

取库内最近一条分镜预览 attempt（`generation_attempts` owner_type=`generation_preview`）的 `request_snapshot_json.prompt`，与按本方案渲染的 prompt 对比：

> 注：下表 1279 字符是 §5 原型（漏了导演字段）的数字，仅用于说明「去 JSON 治理元数据」这一项的量级。补回 §4A 全部「不丢」字段后，实际数字会更高——去噪音是真的，激进删内容不是本方案主张。

| 指标 | 现状 | 本方案（示意原型） |
|---|---:|---:|
| prompt 字符数 | 8395 | 1279（去 JSON 后，未含全部导演字段） |
| `schema_version` 出现 | 4 | 0 |
| `priority_policy` 出现 | 2 | 0 |
| `cannot_override` 出现 | 2 | 0 |
| JSON `{` 出现 | 24 | 0 |

内容零损失：6 格镜头的 `main_focus`/姿态/配饰/不可遮挡区、一致性规则、禁止项全部保留，只是从 JSON 变成导演语言。（注：品牌调性、背景光线、模特导演和商品保真后续已从拍摄剧本 `global_directing` 移除，改由 `Prompt 维护` 模板正文静态承载，renderer 不再输出这些字段。）

## 2. 一个必须尊重的区分：两类调用

修复不能一刀切。生成链路有两类性质不同的模型调用：

| 类别 | 调用 | 输入形态 | 结论 |
|---|---|---|---|
| **策划（Gemini）** | `assembly.ai_tagging`、`assembly.ai_styling`、`assembly.shoot_script` | 结构化 JSON I/O | **保留** JSON 输入输出（模型要返回结构化结果，这是最佳实践）；但去掉每个对象里重复的 `priority_policy`，优先级只在 system instruction / prompt 正文声明一次 |
| **生图（Image-2）** | `assembly.storyboard_preview`、`assembly.final_image` | 自然语言 prompt | **重构**为 prompt view：干净分节文本，去掉全部治理元数据 |

即：Gemini 阶段的 JSON 是**契约**（合理），Image-2 阶段的 JSON 是**噪音**（要去）。两者都要把「优先级策略」从「每个数据对象重复」改为「正文声明一次」。

## 3. 设计：快照模型 ↔ prompt 视图解耦

核心原则：**一份数据，两种投影。**

```
                 ┌─ to_snapshot()  → 富对象（schema_version + priority_policy + 完整结构）→ 持久化/审计/回溯
业务上下文对象 ──┤
                 ├─ to_gemini_view() → 精简 JSON（去治理元数据）→ Gemini 策划 prompt
                 └─ to_image_view()  → 自然语言文本（分节导演语言）→ Image-2 prompt
```

具体拆分：

1. **保留**现有富上下文对象与其 `schema_version`/`priority_policy`——它们仍写进 `request_snapshot_json` 与业务快照，审计与回溯不受影响。
2. **新增渲染层**（建议 `backend/app/prompt_render.py`，纯函数、无 I/O、无 DB），每个阶段一个 renderer：
   - `render_storyboard_prompt(shoot_script, image_inputs, hero_info, user_instruction) -> str`
   - `render_final_prompt(shot_index, shot_plan, styling_result, shoot_script, image_inputs, hero_info, user_instruction) -> str`
   - Gemini 侧：`gemini_view(context) -> dict`（drop 治理键，保留内容键）
3. **优先级策略集中化**：`cannot_override` / `conflict_handling` 这类规则，从「每个 runtime 对象重复」改为在对应 `assembly.*` prompt 正文（或 Gemini system instruction）声明一次。用户要求为空时整段省略，不再出现 `"info": ""` 这种空壳 JSON。
4. **模板改造**：`assembly.storyboard_preview` / `assembly.final_image` 的占位符不再接收 `dumps(...)`，而是接收 renderer 产出的纯文本段落。

### 3.1 renderer 的 Python 边界（已决策）

**决策：renderer 只渲染数据，规则留在可编辑模板。** 这样既符合 `prompt_context_strategy.md` §规则归属（"导演规则写在 `assembly.shoot_script` 正文、选搭规则写在 `assembly.ai_styling` 正文"），又保留业务用户在 Prompt 维护 UI 里调 prompt 的能力。

| 内容 | 归属 | 谁能改 |
|---|---|---|
| 固定脚手架（`任务：`、`六格镜头：`、`禁止：` 段头） | 可编辑模板 | 业务用户 |
| 优先级/边界规则（事实源以图为准、`cannot_override` 语义、配饰不抢镜） | 可编辑模板正文 / system instruction | 业务用户 |
| 品牌口径固定话术 | 可编辑模板 / `segment.brand_info` | 业务用户 |
| 每次运行的**数据**（6 格 shot 字段、品牌 tone 快照值、参考图角色分组、一致性规则值） | renderer 渲染为文本行，填进模板占位符 | 由上游 ShootScript/StylingPlan 决定 |

renderer 工程约束：

- 只做**数据 → 文本行 / 精简 dict**，不碰 DB、不发请求、不改状态——可纯单测。
- 空值优雅省略：字段为空则不输出该行，杜绝 `""`、`[]`、`{}` 空壳。
- 去掉源数据末尾重复标点（如配饰边界以「。」结尾时不再补「。」）。
- 文本拼接用小的具名 helper（`_shot_line`、`_fact_source_line`），不写巨型 f-string；便于逐段测试。
- renderer 不重新实现事实优先级判断，也不硬编码规则话术——那是内容/规则，来自上游或模板；renderer 只负责把数据摆成可读文本。

## 4. 参考图处理（回应「参考图上传与 prompt 引用对齐」）

现状：`image_reference_manifest_for_prompt` 把带 `schema_version` + `priority_rules` 的清单 `dumps` 进 prompt，而 `build_provider_reference_image_manifest`（`image_provider.py:422`）**已经**把同样的 `reference_index`/`role`/`objectKey` 结构化地随请求发给 provider。prompt 里再塞一遍 JSON 是重复。

方案：

- 事实源对齐由**结构化 provider manifest** 承载（已存在，保留）。
- prompt 正文只保留**一句人类可读**的顺序说明，例如：`参考图顺序：图1-2=数字模特身份，图3-5=主推款；文字不得覆盖图片事实。` 顺序由选图器（`image2_input_strategy.md` §3 固定优先级）决定，renderer 据 `image_inputs` 的 role 分组自动生成这句话。
- 身份契约（`ModelIdentityContract` 的 `must_preserve`/`forbidden`）作为**一句自然语言约束**进正文，而不是 dump 整个 contract 对象。
- 保持不变：`image_inputs` 的真实上传顺序 = provider `reference_images` 顺序 = 正文描述的图号，三者由选图器统一，renderer 不得自己重排。

## 4A. 字段渲染映射（每阶段必须先审这张表）

**重要更正**：§5 的原型渲染为了展示，**丢掉了若干真实导演字段**（`camera_angle`、`crop_range`、`model_scale`、`movement`、`composition_notes`），所以那 85% 的缩减里**有一部分不是去噪音，是漏内容**。生产实现**不能**照抄原型。落地前必须先审定下面这张「每字段：渲染 / 合并 / 丢弃」映射表——它才是等价性的依据，不是字符数。

分镜预览（`shoot_script_result_for_prompt` 的每个 shot，字段见 `main.py:5925-5937`）：

| 字段 | 处理 | 说明 |
|---|---|---|
| `shot_index` | 渲染 | `格N` |
| `shot_role_label` | 渲染 | 格标签 |
| `framing` | 渲染 | 景别 |
| `crop_range` | 渲染（合并进景别行） | 取景范围，导演信息，**不丢** |
| `model_scale` | 渲染（合并进构图行） | 人物占比，影响 3:2 构图，**不丢** |
| `camera_angle` | 渲染 | 机位，导演信息，**不丢** |
| `body_position` | 渲染（合并进姿态） | |
| `pose` | 渲染 | 姿态 |
| `movement` | 条件渲染 | 非「静态」时才输出动势 |
| `main_focus` | 渲染 | 核心 |
| `visible_garments` | 条件渲染 | 仅当含 hero 以外服饰时输出（0.1.0 多为 `["hero"]`，可省） |
| `forbidden_occlusion_zones` | 渲染 | 不可遮挡 |
| `composition_notes` | 渲染 | 构图，导演信息，**不丢** |
| `accessory_instructions` | 渲染 | 配饰 |
| `negative_constraints` | 渲染（本格） | 本格禁止项 |
| `shot_role` | 丢弃 | 与 `shot_role_label` 重复的机器码 |

治理元数据（`schema_version`、`priority_policy`、`cannot_override`、`priority_rules`、`conflict_handling`）：**一律丢弃**，规则改由模板正文声明一次。

最终成片（`current_shot_context_for_prompt`，`main.py:5946`）：`current_shot`（完整 ShotPlan，同上字段规则）+ `resolved_accessories_for_this_shot` 渲染，`preview_grid_cell` 渲染为「参考分镜第 N 格」，`styling.selected_outfit` 只取 `selected_material_codes` + `wearing_notes`，其余 drop。

结论：真实缩减幅度会**小于 85%**（保留全部导演字段后）。方案的价值是**去治理元数据 + 去 JSON 语法 + 空值省略**，不是激进删内容。审这张表时，任一「丢弃」项都需要产品/导演确认。

## 5. Before / After（真实数据，仅示意去 JSON 效果，非最终字段集）

**Before**（现状，节选，`generation_preview` attempt，8395 字符）：

```text
生成一张 3:2 电商棚拍分镜预览图
【核心原则】...
【本次用户要求】
商品信息：{"schema_version": "hero-product-info-v1", "info": "", "priority_policy": {"fact_role": "...", "cannot_override": ["主推款图片可见商品事实", ...], "conflict_handling": "..."}}
用户要求：{"schema_version": "stage-user-instruction-v1", "stage": "storyboard_preview", "instruction": "", "priority_policy": {...}}
【拍摄剧本结果】
{"schema_version": "shoot-script-storyboard-context-v1", "global_directing": {...}, "shots": [{"shot_index": 1, ...}, ...]}
...
【输入参考图角色清单】
{"schema_version": "image-reference-manifest-v1", "priority_rules": [...], "references": [{"reference_index": 1, "role": "model_identity", ...}, ...]}
```

**After**（本方案渲染，1279 字符，同一份数据）：

```text
任务：生成一张 3:2 电商棚拍分镜预览图，六格对应下方 6 个镜头。

事实源（以参考图为准，不可改款/换色/换脸）：主推款与搭配服装参考图=真实商品事实源，保持品类/颜色/面料/版型/结构/比例与可见细节；数字模特参考图=唯一身份事实源，保持同一人脸型/五官/头型/发长/发型轮廓/刘海碎发/肤色/气质。
参考图顺序：图1-2=数字模特身份，图3-5=主推款；文字不得覆盖图片事实。

品牌与画面：Lily商务时装，轻松高级、智性优雅，都市女性从容不费力的现代通勤状态。背景：极简暖灰色棚拍，弱化地面交界线，保留极轻微自然投影。光线：柔和均匀无影罩面光，低反差，真实还原肤色与针织/棉麻材质细节。
配饰：饰品体量小巧精致不夺眼球；鞋包色彩与整体呼应，携带姿势不可遮挡主推款核心渐变设计。
一致性：Face-01 短发刘海与面部特征所有镜头绝对一致；针织衫竖向坑条间距与横向米褐-乳白-深咖渐变比例各焦段一致；搭配下装暖白色相、棉麻肌理及细腰带形态一致。

六格镜头：
格1（全身主视觉·近全身景）：上紧下松通勤廓形，针织衫渐变与暖白阔腿裤清爽衔接。姿态：双腿前后错落，重心微偏后脚。配饰：露出米白法式单鞋鞋尖；手提袋右侧偏后自然下垂。不可遮挡：前襟纽扣区；高腰裤细皮带与褶裥结合处。
格2（背面中景·中景）：背面落肩过渡与背部横向渐变色条完整性。姿态：背部挺拔，双肩放松下沉。配饰：不出现手提袋。不可遮挡：后背及肩线过渡带。
格3（切脸半身近景·近景）：圆领罗纹工艺、开衫纽扣及胸前色彩过渡。姿态：放松正面通勤站姿。配饰：香槟金极细项链露于领口中央。不可遮挡：圆领下方 5cm 前襟区。
格4（非对称局部特写·特写）：竖向坑条精细质感、下摆开叉与裤装细腰带材质对比。姿态：微动势停顿瞬间。配饰：无饰品干扰。不可遮挡：下摆纽扣与开叉细节。
格5（全身正反双拼图·全身景）：渐变针织衫配暖白阔腿裤的正反版型对比。姿态：双脚微张，站姿端正自然。配饰：两侧皆米白粗跟法式单鞋，不带包袋。不可遮挡：拼图中央垂直对齐线；下摆前后水平高度线。
格6（走动备用全身姿态·全身景）：阔腿裤行进垂坠感，针织衫与高腰裤动态衔接。姿态：一脚前迈，裤腿自然摆动。配饰：单鞋与白色包袋动态中清晰可见。不可遮挡：前襟纽扣与渐变带核心区。

禁止：不用办公桌/文件夹等传统道具；不摆僵硬军姿或过度张腿扭腰等网红姿势；不遮挡领口至前襟关键渐变与纽扣；不生成文字/水印/logo/边框；生成配饰只作低存在感辅助，不特写、不抢主推款视觉中心。
```

## 6. 迁移阶段

分阶段落地，每阶段独立可验、可回滚。**当前状态：P0–P5 全部落地并通过第一道验收（本地渲染对比 + 单测，共 25 个 renderer 单测全绿）；第二道受控出图 A/B 待用户授权后执行。**

- **P0（本文档）✅**：评审设计与 before/after，确认范围。
- **P1｜生图阶段 ✅**：新增 `prompt_render.py`，实现 `render_shoot_script_body` / `render_shot_context_body` + hero/instruction 行渲染；改 `build_new_preview_prompt` / `build_new_final_prompt` 用 renderer 填充占位符。（注：P1 当时把段头 `【全局导演】` 等留在 renderer 代码里、未改 DB 模板；此点**已被 P6 取代**——段头与固定话术已移入模板正文，`render_shoot_script_body` / `render_shot_context_body` 被拆成粒度渲染函数并删除，`available_prompt_placeholders` / `sample_prompt_runtime` 同步更新。）快照写入不变。
- **P2｜参考图 ✅**：`append_image_reference_manifest` 改为「参考图顺序：图N=角色」自然语言 + 事实源约束；删除废弃的 `image_reference_manifest_for_prompt`；provider 侧 `build_provider_reference_image_manifest` 保持结构化不变。
- **P3｜Gemini 阶段 ✅（等价性依据已确认）**：`ai_styling`/`shoot_script` 的 hero/用户要求/介入要求自然语言化，去掉重复的 `priority_policy`/`cannot_override`。**关键**：该优先级与冲突处理规则**已完整写在对应组装 Prompt 正文中**，删除运行时重复不丢规则——这是行为等价的依据。`candidate_retrieval`（标签事实）和 `styling_result`（导演上下文）保持结构化 JSON。仍建议受控 A/B 复核。
- **P4｜清理 ✅**：删除因上游改造而失效的死代码 `append_user_requirement_block` / `has_user_requirement` / `append_runtime_input_block` / `has_user_input`；prompt 出口统一走 renderer，空值由 `render_*_line` 返回「（无）」、`_block` 省略空行，杜绝空壳 JSON。

- **P5｜残留清理 ✅**：
  - 模特身份事实仍以模特参考图为准；AI Director 不再接收额外的模特身份 runtime。
  - 删除 `styling_garment_prompt_context`（ai_tagging）顶层 `schema_version`（实为未被模板引用的死运行时键，不影响任何 prompt 输出，顺手清理）。

至此生成链路上所有会进入 prompt 的上下文均已按「快照留结构化、prompt 走自然语言/去治理元数据」完成改造。

**重生一致性**：重生某格或某张成片时，用**当次输入快照数据 + 新 renderer** 重新渲染 prompt（renderer 是纯函数、输入是被冻结的快照，故同组内一致）；不回放历史 prompt 文本。历史 attempt 的旧式 prompt 仍原样保存在 `request_snapshot_json` 供回溯，不被改写（承 `shoot_script_workflow.md` 重生/失效与"同组一致性"）。

## 7. 验收（两道关，已决策）

**第一道：本地渲染对比（默认关，不消耗额度）** —— 遵守 `CLAUDE.md` 全局规则 7：

1. **渲染对比脚本**：对每个已改阶段，取库内真实快照数据（`generation_attempts.request_snapshot_json`）作为输入，本地跑新 renderer，打印 before/after prompt 文本 diff、字符数、噪音关键词计数（`schema_version`/`priority_policy`/`cannot_override`/`{`），断言 after 治理关键词为 0，且 **§4A 表中所有「渲染/合并」字段的值都出现在输出里**（防止再次误删导演字段）。
2. **单元测试**（`backend/tests/`）：renderer 纯函数单测——给定构造输入，断言输出不含治理键、空值被省略、6 格齐全、§4A「不丢」字段齐全、参考图顺序句正确。测试用临时数据，不触碰 `data/app.sqlite3`（遵守全局规则 12）。
3. 本道**不调用**任何 provider。

**第二道：小规模受控出图 A/B（上线前，需显式授权）** —— 本地渲染只能证明 prompt 更干净，**证明不了 Image-2 出图不退化**，故上线前必须：

4. 选 3-5 个真实 case，用**旧 prompt vs 新 renderer prompt**、同一组参考图、同一 seed（若 provider 支持），各出一版，人工对比：主推款保真、模特身份一致、构图/景别是否因删字段而丢失、品牌调性。
5. 该步**会消耗 provider 额度**，属 `CLAUDE.md` 规则 7 的例外，**必须经用户明确授权后触发**；未授权不得跑。A/B 结论记录进本文件或迁移 PR。
6. 若 A/B 显示某字段删除导致构图/景别退化，回 §4A 把该字段从「丢弃」改「渲染」，重跑第一道。

## 8. 非目标

- 不引入 prompt 长度阈值 / 预算监控 / 自动截断（承 `prompt_context_strategy.md` 非目标）。
- 不改选图器规则、不改 provider 请求结构、不改快照字段。
- 不改 Gemini 策划阶段的结构化 JSON 输出契约本身，只去重复治理元数据。
- 不重构 `main.py` 无关部分（不顺手拆巨型文件）。

## 9. 相关

- `image2_input_strategy.md` §5/§6：Image-2 推荐 prompt 结构（本方案回归的 spec）
- `prompt_context_strategy.md`：阶段输入 DTO 切分与旧兼容注入删除
- `generation_responsibility_matrix.md`：Image-2 只做视觉生成，不承担业务语义

## 10. P6：占位变量化重构（会变 / 不变 彻底分离）✅

P1–P5 把 prompt 从「dump JSON」改成了「自然语言」，但仍有两类**固定文本残留在代码里**：段头（`【全局导演】`/`【六格镜头】`/`【分镜参考】`…）由 renderer 拼接；参考图职能标签（`数字模特身份`/`主推款`…）写死在 `render_image_reference_line` 里。这些都绕过了「Prompt 维护」页，业务用户改不到。

P6 的判定边界收敛为一条：**「是否会变」**。

| 类型 | 例子 | 归属 |
|---|---|---|
| **会变** | 图号区间（随图片数量变的 `图N-M`）、镜头格号、6 格镜头字段值、各分段正文、模型输出 | **代码**：以变量渲染，暴露为 `{{runtime.*}}` 占位 |
| **不变** | 段头、职能描述、事实源约束、身份优先级话术、背景/光线/品牌口径 | **Prompt 维护**：写在 `assembly.*` 模板正文 |

### 10.1 渲染引擎：条件段

`render_prompt_config` 新增 Mustache 风格条件段，让模板在「有内容才显示表头/话术」的前提下完全托管固定文本：

- `{{#runtime.x}}…{{/runtime.x}}`：占位 `x` 非空才渲染内段（含表头）。
- `{{^runtime.x}}…{{/runtime.x}}`：占位为空才渲染（如「本次无环境图」兜底）。
- 空 runtime 占位（`""`）合法、不再算缺失；渲染末尾折叠 3+ 连续空行为 2，避免省略段留下空洞。
- 真正**未提供**的 runtime key 仍报缺失（防拼写错误）。

### 10.2 renderer 拆分：只出「可变数据正文」（无表头）

删除带表头的合成函数 `render_shoot_script_body` / `render_shot_context_body`，拆为粒度函数：

- 拍摄剧本 → `render_shoot_script_global_directing` / `_styling_risks` / `_resolved_accessories` / `_shots`
- 成片 → `render_preview_grid_cell`（纯格号数字）/ `render_shot_styling` / `render_shot_global_directing` / `render_shot_current` / `render_shot_accessories`

每个函数**无表头、无话术**，空数据返回空串，模板用条件段决定是否显示该段的表头。写死的 `【分镜参考】"只借用姿势…分镜预览不是事实来源"` 整句删除，改由 `assembly.final_image` 模板正文承载（`{{runtime.preview_grid_cell}}` 只提供格号）。

> **边界**：单条镜头/配饰内部的字段标签（`核心`/`构图`/`机位`…）属于「变长记录的序列化格式」，简单占位引擎无法用循环表达，保留在 renderer；它们是数据格式，不是可调话术。

### 10.3 参考图：按职能输出图号区间

`render_image_reference_line`（把图号 + 职能标签捆在一起）→ `render_image_reference_spans`：**只按职能输出会变的图号区间**（`model_identity`/`preview_grid`/`hero_garment`/`styling_garment`/`shoot_environment`，外加 `subject`=模特+主推款+搭配的合并区间，供「模特与服装以图X为准」的动态交叉引用）。职能描述与事实源约束全部移入模板正文，例如：

```
{{runtime.ref_model_identity}} 为数字模特身份参考图，是唯一人物身份事实源…身份特征不得改变。
{{runtime.ref_shoot_environment}} 为拍摄环境参考图，只参考环境、背景、光线；模特与服装以 {{runtime.ref_subject}} 为准。
```

### 10.4 死元数据清理

`build_hero_product_info_context` / `build_stage_user_instruction_context` / `build_user_requirement_context` 顶层的 `priority_policy`（`user_*_priority` / `cannot_override` / `conflict_handling`）删除——它们从不进模型（`render_*_line` 只取 `info`/`instruction`/`combined_requirement`），优先级规则由各 `assembly.*` 模板正文声明。`build_accessory_boundary` 的 `bag_rule`/`jewelry_rule` 经核实未进任何模型 prompt（业务默认值），保留。

### 10.5 新占位与模板

`available_prompt_placeholders` / `sample_prompt_runtime` 同步登记 §10.2/§10.3 的新 `{{runtime.*}}` 占位（前端 Prompt 维护页数据驱动，自动展示）。开发库 `assembly.shoot_script` / `storyboard_preview` / `final_image` 三个模板正文迁移：把搬出的段头/规则/职能描述并入，改用新粒度占位与条件段，保留原有措辞、仅重定位（迁移前整库备份）。

### 10.6 验收

沿用 §7 第一道（本地渲染 + 单测，不消耗额度）：引擎条件段/空省略单测、粒度 renderer 单测、图号区间单测、「渲染层不得含表头/话术」回归断言，及三阶段用样例 runtime 渲染无缺失占位、无残留旧占位。第二道受控出图 A/B 仍需用户显式授权。
