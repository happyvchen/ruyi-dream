# AI 摄影棚生成主流程

## 1. 四步链路

```text
上传主推款
→ AI 智能选搭 / StylingPlan
→ Image-2 分镜预览（按拍摄图位族选择固定 Prompt）
→ Image-2 最终成片（分镜前 5 格）
```

0.1.0 不生成动态拍摄剧本，不存在用户侧“生成拍摄剧本”步骤。分镜摄影规则集中在 Prompt 维护页，避免 Gemini 每次改写镜头造成漂移。

## 2. AI 智能选搭

Gemini 只负责理解主推款、探索候选 outfit、判断品牌与商品成立性并输出 `StylingPlan`。选中的搭配服装必须来自搭配服装库，鞋、包和首饰只作为低存在感文字边界。

`StylingPlan.hero_understanding` 必须区分商品真实品类与拍摄图位族：

- `category`：商品真实品类，例如 `西装马甲`、`半身裙`、`连衣裙`。
- `sub_category`：规范子类，例如 `suit_vest`、`skirt`、`dress`。
- `shot_category_family`：拍摄图位族，只允许 `top`、`bottom`、`one_piece`，直接决定分镜/成片 Prompt 路由。

后端优先读取 `shot_category_family`，缺失时根据 `category`、`sub_category` 和官方品类候选做确定性 fallback 映射；不再调用大模型生成镜头描述。`西装马甲`、`马甲`、`vest` 等归入 `top`。

## 3. 分镜预览

三类 Prompt：

- `assembly.storyboard_preview_top`
- `assembly.storyboard_preview_bottom`
- `assembly.storyboard_preview_one_piece`

输入包括数字模特参考图、主推款与选中搭配服装参考图、拍摄环境参考图，以及代码渲染的短自然语言商品/造型事实。Image-2 一次直出 3:2、三列两行六宫格；无前处理和后处理。

六格镜头与姿势标准以 [storyboard_preview_business_intent.md](storyboard_preview_business_intent.md) 为准。镜头 5 必须同时满足 1:1 使用和中央 2:3 安全裁切。

## 4. 最终成片

系统为分镜前 5 格创建五个简单结果槽位，保存格号、`shot_role`、状态和资产。五张图可并发生成，每张明确参考对应的分镜格，并继续携带商品、模特和搭配参考图。第 6 格不生成成片。

## 5. 状态与失效

- `READY_FOR_STYLING` → `STYLING_RUNNING` → `STYLING_READY`
- `PREVIEW_RUNNING` → `PREVIEW_READY`
- `FINAL_RUNNING` → `COMPLETED` / `FINAL_PARTIAL`

重新上传主推款会使选搭、分镜和成片失效；重新选搭使分镜和成片失效；重新生成分镜使成片失效。旧的 `SCRIPT_RUNNING` / `SCRIPT_READY` 不属于当前流程。

## 6. 第一阶段范围

第一阶段只验证稳定达到原始业务意图的最短链路。明确不加入自动评分、自动验收、自动切格、基于评分的自动重试或额外后处理。Prompt 的有效规则与实验记录见 [storyboard_prompt_research_task_1440.md](storyboard_prompt_research_task_1440.md)。
