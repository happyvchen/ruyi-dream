# Backend Domain Model

## 当前生成对象

`GenerationTask` 是拍摄任务根节点，冻结 `GenerationTaskInputVersion`（新版本包含按页面顺序排列的前四张模特来源）后运行 AI Stylist，得到带商品理解、最终服装、穿法、配饰和空搭配原因的 `GenerationStyling / StylingPlan`。

当前后端不创建分镜预览、动态拍摄剧本或镜头计划。旧 `GenerationPreview` 表和序列化字段仅用于历史记录兼容；preview endpoint 固定返回 410。

## 统一背景与历史预设

`shoot_environment_references` 保存当前共享背景；环境与光线来自它，构图来自三类成片 Prompt。`ShotPresetSlot` 的3×5母版与姿势槽位只用于历史兼容。新流程不要求母版完整。

`GenerationResultSetShotPreset` 是结果集创建时从选定姿势与共享背景复制的五条关系快照（旧任务从原预设复制），保证维护端随后换图不会影响继续生成、单张重生和历史复盘。旧 `ShotPresetVersion` / `ShotPreset` 只用于迁移已有配置和读取旧结果集。

## 最终成片

`GenerationResultSet` 固定包含五个 `GenerationResultImage`，创建时冻结输入版本、StylingPlan 和五条 `GenerationResultSetShotPreset`；`preview_id` 与 `shot_preset_version_id` 对新结果为空。每张成片使用对应冻结预设、模特图、主推款图、搭配款图和同一 StylingPlan 组装 edits 请求。

`GenerationAttempt` 保存 provider 请求快照、状态、结果、错误和恢复信息。新成片 owner type 为 `generation_result_image`；`generation_preview` 只可能出现在历史数据中。

## 媒体

所有业务图片统一登记为 `MediaAsset`，二进制写入 S3 兼容对象存储。当前背景、历史预设和结果集冻结快照都参与媒体引用计数；更换当前槽位不会删除仍被历史结果集引用的资产。

## 成片提示词准备

结果集 `prompt_preparation_json` 存配置快照、direct/optimized 模式、准备状态和按图片 ID 关联的冻结任务包。文本优化 attempt 的 owner_type 为 `generation_result_set`，与任务一并取消、追溯和清理；测试调用通过 invocation.mode 区分。整理请求按最多 16 张视觉附件的并集预算分批，attempt 保存 batchRequests / batchResponses；每批完成即保存对应镜头任务包，继续时不重复整理已完成镜头。单张重生不再刷新整组 input_version_id。

## 姿势资产与选搭

`Pose` 为独立于搭配服装和背景的素材，包含 code、name、唯一一张图片、任意 JSON 标签、识别状态及归档信息。`GenerationPoseSelection` 归属于任务、输入版本和 StylingPlan，包含五个唯一镜头的不同姿势原图、共享背景、理由和动作快照；plan_json 保存 shared_background_v1 标记、成片配置快照和五镜头要求。结果集引用 pose_selection_id 并复制冻结图片；姿势来源删除后 pose_id 置空，历史快照与图片引用保留。初筛定选、职责和失效规则见 `../design/01_generation_flow/pose_library_workflow.md`。

## 精简选搭输出契约

LLM 返回 `selected_files` 与 `styling_plan`；后者版本为 `styling-plan-v2`，只含主推款理解、已选服装及穿法、空搭配原因、文字配饰、整体穿法和简短理由。后端先校验 code 身份、重复项、字段类型与空值一致性，再将最终方案适配为一个 `selected_final` 内部候选，供原有持久化、页面、姿势选搭和成片读取；该内部容器不是要求模型重新输出候选。评分和诊断保持空，不合成评价。原始 v2 响应完整保存，旧 v1 记录继续读取。无需数据库迁移。

空搭配原因 `complete_look`、`no_suitable_match`、`user_requested` 分别表示自身完整、库内无合适商品、用户要求不搭配；有搭配时为 `""`。无合适商品属于有效业务结果，不触发自动重问；不得将缺少必要上衣或下装解释为完整穿着，不通过文字配饰虚构服装。
