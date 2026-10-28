# Prompt 上下文策略

## 1. 维护槽位

当前目录只保留品牌信息、AI 打标、主推款图片挑选、AI 选搭、三类最终成片 Prompt 和可选成片提示词优化规则。三个 `assembly.storyboard_preview_*` 槽位已退出运行链路；迁移会先备份正文，再从维护目录移除。

## 2. 动态事实边界

| 阶段 | 主要动态字段 | 职责 |
|---|---|---|
| AI 选搭 | 主推款事实、可选搭配服装、品牌信息、用户选搭要求 | 输出可追溯 StylingPlan |
| 最终成片 | `category_family`、`shot_index`、`shot_styling`、`ref_shot_background`、`ref_shot_pose`、`ref_model_identity`、`ref_hero_garment`、`ref_styling_garment`、`final_image_user_instruction` | 描述当前镜头事实和参考图身份 |

稳定的五镜头构图、商品展示重点、禁止项以及统一背景与姿势职责写在三类固定成片 Prompt 正文；代码不动态创作镜头。背景来自模特与背景，姿势来自独立姿势库；在选姿势时冻结，并由结果集复用。

## 3. 用户补充要求

`runtime.user_requirement` 保存任务级服装事实；`runtime.ai_styling_user_instruction` 与 `runtime.final_image_user_instruction` 分别只影响对应阶段下一次执行。前台使用摄影和商品语言，不向普通用户暴露 Prompt 工程概念。

## 4. 审计

每次请求快照必须保留渲染后的最终 Prompt、实际使用的动态段、缺失占位符、参考图角色和顺序、冻结背景与姿势资产以及固定画质参数。

## 5. 成片提示词优化边界

新增 `assembly.final_prompt_optimizer` 系统规则槽位，默认关闭。输入为五份已渲染文本与实际背景、姿势、模特及商品参考图；共享图片去重，references.index 保持最终请求局部编号，visionImageIndex 指向优化请求全局图片。成片模板及分段在选姿势时冻结，优化规则与开关在结果集创建时冻结。返回正文直接用于生图，不再拼接锁定段；优化结果与原文均可追溯。多模态整理保留各镜头的摄影要求与允许变化项，不根据镜头编号、角色名或配饰推断景别。`shot_styling` 只提供已选服装、穿法和生成配饰事实，配饰边界由维护中的成片 Prompt 表达，不注入旧分镜规则。详细运行和恢复规则见 [current_generation_workflow.md](current_generation_workflow.md#成片提示词优化)。

## 6. 精简 AI 选搭契约

主推款挑图与服装选搭仍合并为一次调用。挑图 Prompt 仅描述 `selected_files` 及商品图筛选，完整 `styling-plan-v2` 字段契约唯一维护在 AI 选搭 Prompt 尾部。输入顺序为任务与业务约束、品牌与用户资料、全量标签事实、输出契约与短核对项。商品事实及契约优先；用户要求在此边界内优先于品牌默认偏好。

模型只输出主推款理解、图位族、已选商品与穿法、配饰、空搭配原因、整体穿法和简短理由。不要求分析过程、多候选、自评分、落选清单或动态摄影要求。`complete_look` / `no_suitable_match` / `user_requested` 保留空搭配语义。完整穿着不加库内服装；未找到匹配时不强凑，也不把缺失的基础服装伪装成配饰。主观造型质量由用户审核，代码只做契约与身份校验。

参考正文位于 `backend/prompt_templates/hero_image_selection.txt` 与 `ai_styling.txt`；仅显式保存至数据库后生效。更新两个槽位时一起备份、一起应用；不覆盖历史请求快照和生成结果。

主推款与搭配款分别按实际输入角色生成图号，不合并两类商品的引用。无搭配款时，搭配款图号为空，模板用条件段省略对应说明。镜头序号与五镜头条件块保留；全局摄影规则和当前镜头正文只由模板维护。迁移 082 只升级现有配置中的相关占位符，保留管理员其他正文；历史冻结模板读取时应用相同转换，不回写历史记录。
