# 分镜与成片工作流简化方案

## 1. 决策目标

本方案以 `storyboard_preview_business_intent.md` 为唯一目标，不以保留现有 AI Director、ShootScript 或历史步骤为前提。

优先级：

1. 稳定达到原始业务意图。
2. 系统简单、透明、容易维护。
3. Prompt 尽量集中在 `Prompt 维护` 页面。
4. 代码只负责确定性的动态事实与路由。
5. 尽量减少 Gemini 动态生成 Prompt 或镜头方案。
6. 流程只负责生成图片，不增加评分、验收、自动切格、自动重试等环节。

## 2. 核心决策

### 2.1 删除用户可见的“生成拍摄剧本”步骤

目标主流程调整为：

```text
上传主推款
→ AI 智能选搭
→ 生成分镜预览
→ 生成成片
```

不再调用 Gemini AI Director 动态生成 ShootScript，也不再要求模型每次重新决定六个镜头。

历史 ShootScript 数据保留用于旧任务复盘，但新任务不再创建新的 `GenerationShootScript`。第一阶段可先停用而不立刻删除数据库表，降低迁移风险。

### 2.2 只保留三类分镜 Prompt

按主推款归为三个稳定大类：

1. `TOP`：上装、针织、外套类。
2. `BOTTOM`：裤装、半身裙类。
3. `ONE_PIECE`：连衣裙、连体类、套装。

在 `Prompt 维护` 提供三个自包含 Prompt：

```text
assembly.storyboard_preview_top
assembly.storyboard_preview_bottom
assembly.storyboard_preview_one_piece
```

三份 Prompt 各自直接定义本品类的 6 个镜头、姿态边界、镜头 5 黄金写法、环境使用方式和输出结构。它们不依赖 Gemini 动态生成的 ShootScript。

### 2.3 代码只完成确定性动态组装

代码负责：

- 从 StylingPlan 的主推款理解读取已确认品类，并映射到三大类。
- 选择对应 PromptConfig key。
- 注入模特、主推款、选中搭配服装和环境参考图编号。
- 注入商品信息、用户分镜要求、选中 outfit 穿法和低存在感配饰。
- 固定 Image 2 请求参数和参考图顺序。
- 保存 attempt、结果图和历史版本。

代码不负责：

- 动态写自然语言 Prompt。
- 调用 Gemini 规划镜头。
- 对姿势和审美评分。
- 自动分析结果图。
- 自动切格、自动验收或自动重试。

### 2.4 最终成片使用一个公共 Prompt

三类分镜 Prompt 生成固定前 5 格。最终成片继续逐张生成，但只需要一个公共 `assembly.final_image` Prompt：

- 输入对应分镜格作为构图、姿势、身位、背景和光线参考。
- 输入模特图作为身份事实。
- 输入商品图作为商品事实。
- 代码注入当前格号和简单的格子职责标签。

最终成片不再读取 ShootScript，只读取：

```text
category_family
+ shot_index
+ static_shot_role
+ selected outfit facts
+ image references
+ user instruction
```

## 3. 三类固定镜头

### 3.1 TOP

1. 七分身正面中景。
2. 切脸商品细节近景。
3. 侧背上半身中景。
4. 全身穿搭。
5. 通用正背双视图。
6. 镜头 1 姿态候选。

### 3.2 BOTTOM

1. 腰部到脚的正面下装中景。
2. 切脸/无头的腰头细节近景。
3. 侧背下装中景。
4. 全身穿搭。
5. 通用正背双视图。
6. 镜头 1 姿态候选。

### 3.3 ONE_PIECE

1. 正面全身。
2. 无眼鼻的侧正七分身。
3. 无鞋脚的侧背中近景。
4. 侧正动态全身。
5. 通用正背双视图。
6. 镜头 1 姿态候选。

## 4. 镜头 5 固定黄金段落

三个品类 Prompt 均复用同一段经过 P07–P11 五次重复验证的文字：

```text
镜头 5 是一个 1:1 方形正背双视图。
左侧为同一模特正面全身，右侧为同一模特背面微侧转全身，
中央保留极细白色竖缝。

两个人物必须同时满足“大、靠中、可裁切”：
每个人从头到鞋完整，高度约为方格的 76%–80%，
头部接近方格上方约 10%，鞋底接近下方约 10%。
两人的内侧肩膀靠近中央竖缝，中央不得出现大块空白；
两人的外侧手臂、衣摆/裤脚和鞋不得越出中央 2/3，
方格最左 1/6 和最右 1/6 只显示空白背景。
人物不能缩成小缩略图，也不能分散到左右边缘。
裁掉左右各 1/6 后，两个人仍从头到鞋完整、商品清晰可读。
```

该段放在三份 Prompt 正文中，允许管理员集中维护；代码不再动态改写。

## 5. 简化后的领域状态

新任务主状态可简化为：

```text
DRAFT
READY_FOR_STYLING
STYLING_RUNNING
STYLING_READY
PREVIEW_RUNNING
PREVIEW_READY
FINAL_RUNNING
FINAL_PARTIAL
COMPLETED
FAILED
CANCELLED
```

删除新流程中的：

```text
SCRIPT_RUNNING
SCRIPT_READY
active_shoot_script_id 运行依赖
```

历史字段可暂留数据库，仅新流程不再读取和写入。

## 6. API 与 UI 调整

### 6.1 UI

五步改为四步：

```text
1 上传主推款
2 AI 智能选搭
3 生成分镜预览
4 生成成片
```

删除“拍摄剧本要求”和拍摄剧本展示卡。用户继续可以填写简短“分镜预览要求”。

### 6.2 API

- 新流程不再调用 `POST /generation-tasks/{id}/shoot-script`。
- `POST /generation-tasks/{id}/preview` 从 `STYLING_READY` 直接进入。
- preview artifact 直接关联 `styling_id` 和 `input_version_id`，不要求 `shoot_script_id`。
- `auto-run-final` 在选搭完成后直接生成分镜。

## 7. Prompt 维护页

建议显示：

- 分镜预览 · 上装/针织/外套。
- 分镜预览 · 裤装/半身裙。
- 分镜预览 · 连衣裙/连体/套装。
- 生成成片 · 公共 Prompt。

每份分镜 Prompt 是完整、可直接阅读的摄影说明，不把业务关键规则拆散到代码常量。

代码只提供少量稳定占位符：

```text
runtime.ref_model_identity
runtime.ref_garment
runtime.ref_shoot_environment
runtime.hero_product_info
runtime.styling_outfit
runtime.resolved_accessories
runtime.storyboard_preview_user_instruction
```

## 8. 不引入的能力

按本方案明确不增加：

- 分镜评分模型。
- Aesthetic Critic 二次审核。
- 自动切分六格。
- 人体或商品检测。
- 镜头 5 自动裁切检测。
- 自动重试或自动抽卡。
- Gemini 动态生成镜头 Prompt。
- 独立 Prompt 编译 DSL。

生成结果由用户在现有分镜页面直接查看，并决定使用或点击重新生成。

## 9. 实施顺序

1. 新增三类分镜 PromptConfig，并把调研后的有效 Prompt 分别整理进去。
2. 增加三类品类映射函数和静态 `shot_role` 列表。
3. 让 preview 从 `STYLING_READY` 直接生成。
4. 修改最终成片任务包，去掉 ShootScript 依赖，改读静态 `shot_role`。
5. 删除前端拍摄剧本步骤和用户输入。
6. 更新一键成片状态推进。
7. 为旧任务保留只读兼容，确认迁移稳定后再决定是否删除旧表和 API。

## 10. 最终判断

该方案比“保留 AI Director 再增加质检”更符合当前目标：

- 六镜头业务意图已经足够稳定，不需要 Gemini 每次重新规划。
- Prompt 由业务人员在一个页面集中维护，行为更透明。
- 三类模板覆盖当前主要商品图位，代码路由简单。
- 保留 AI Stylist 只解决选搭问题，不让它扩张到摄影 Prompt。
- 不增加生成后的复杂链路，用户仍保有重新生成的直接控制权。

