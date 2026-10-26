# Prompt 上下文策略

## 1. 维护槽位

当前目录只保留品牌信息、AI 打标、主推款图片挑选、AI 选搭、三类最终成片 Prompt 和可选成片提示词优化规则。三个 `assembly.storyboard_preview_*` 槽位已退出运行链路；迁移会先备份正文，再从维护目录移除。

## 2. 动态事实边界

| 阶段 | 主要动态字段 | 职责 |
|---|---|---|
| AI 选搭 | 主推款事实、可选搭配服装、品牌信息、用户选搭要求 | 输出可追溯 StylingPlan |
| 最终成片 | `category_family`、`shot_index`、`shot_current`、`shot_styling`、`ref_shot_background`、`ref_shot_pose`、`ref_model_identity`、`ref_garment`、`final_image_user_instruction` | 描述当前镜头事实和参考图身份 |

稳定的五镜头构图、商品展示重点、禁止项以及母版替换规则写在三类固定成片 Prompt 正文；代码不动态创作镜头。拍摄母版和姿势本身由当前生效拍摄预设提供，并在结果集创建时冻结。

## 3. 用户补充要求

`runtime.user_requirement` 保存任务级服装事实；`runtime.ai_styling_user_instruction` 与 `runtime.final_image_user_instruction` 分别只影响对应阶段下一次执行。前台使用摄影和商品语言，不向普通用户暴露 Prompt 工程概念。

## 4. 审计

每次请求快照必须保留渲染后的最终 Prompt、实际使用的动态段、缺失占位符、参考图角色和顺序、冻结拍摄预设资产以及固定画质参数。

## 5. 成片提示词优化边界

新增 `assembly.final_prompt_optimizer` 系统规则槽位，默认关闭。输入为五份已渲染文本及实际参考图角色，不传完整业务 JSON 或重新识图；规则和模板在结果集创建时冻结。锁定段原样附加，优化结果与原文均可追溯。详细运行和恢复规则见 [current_generation_workflow.md](current_generation_workflow.md#成片提示词优化)。
