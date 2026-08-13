# Backend Domain Model

## 当前生成对象

`GenerationTask` 是拍摄任务根节点，冻结 `GenerationTaskInputVersion` 后运行 AI Stylist，得到带商品理解、候选比较和 Aesthetic Critic 结果的 `GenerationStyling / StylingPlan`。

当前后端不创建分镜预览、动态拍摄剧本或镜头计划。旧 `GenerationPreview` 表和序列化字段仅用于历史记录兼容；preview endpoint 固定返回 410。

## 拍摄预设

`ShotPresetSlot` 由 `category_family`（`top` / `bottom` / `one_piece`）与 `shot_index`（1–5）唯一定位，并关联当前生效的拍摄母版和姿势两个 `MediaAsset`。单张替换或删除立即生效，不存在维护草稿、发布或用户可见版本。拍摄母版带原人物、原服装和目标构图，生成时整图替换原人物和原服装；某类型五个槽位不完整时不能开始该类型成片。

`GenerationResultSetShotPreset` 是结果集创建时复制的五条关系快照，保证维护端随后换图不会影响继续生成、单张重生和历史复盘。旧 `ShotPresetVersion` / `ShotPreset` 只用于迁移已有配置和读取旧结果集。

## 最终成片

`GenerationResultSet` 固定包含五个 `GenerationResultImage`，创建时冻结输入版本、StylingPlan 和五条 `GenerationResultSetShotPreset`；`preview_id` 与 `shot_preset_version_id` 对新结果为空。每张成片使用对应冻结预设、模特图、主推款图、搭配款图和同一 StylingPlan 组装 edits 请求。

`GenerationAttempt` 保存 provider 请求快照、状态、结果、错误和恢复信息。新成片 owner type 为 `generation_result_image`；`generation_preview` 只可能出现在历史数据中。

## 媒体

所有业务图片统一登记为 `MediaAsset`，二进制写入 S3 兼容对象存储。当前拍摄预设和结果集冻结快照都参与媒体引用计数；更换当前槽位不会删除仍被历史结果集引用的资产。
