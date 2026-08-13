# Backend Domain Model

## 当前生成对象

`GenerationTask` 是拍摄任务根节点，冻结 `GenerationTaskInputVersion` 后运行 AI Stylist，得到带商品理解、候选比较和 Aesthetic Critic 结果的 `GenerationStyling / StylingPlan`。

当前后端不创建分镜预览、动态拍摄剧本或镜头计划。旧 `GenerationPreview` 表和序列化字段仅用于历史记录兼容；preview endpoint 固定返回 410。

## 拍摄预设

`ShotPresetVersion` 表示一套版本化预设，状态为 `DRAFT`、`PUBLISHED` 或 `SUPERSEDED`。同一时刻最多一个草稿和一个已发布版本。

`ShotPreset` 由 `category_family`（`top` / `bottom` / `one_piece`）与 `shot_index`（1–5）唯一定位，并关联拍摄母版和姿势两个 `MediaAsset`。拍摄母版带原人物、原服装和目标构图，生成时整图替换原人物和原服装。上装、下装、套装分别发布：对应类型的 5 个槽位完整即可发布，不要求另外两个类型同时完成。数据库保留可空的 `mask_asset_id` 仅用于历史版本复盘，不参与新预设或新请求。

## 最终成片

`GenerationResultSet` 固定包含五个 `GenerationResultImage`，创建时冻结输入版本、StylingPlan 和 `shot_preset_version_id`；`preview_id` 对新结果为空。每张成片使用对应预设、模特图、主推款图、搭配款图和同一 StylingPlan 组装 edits 请求。

`GenerationAttempt` 保存 provider 请求快照、状态、结果、错误和恢复信息。新成片 owner type 为 `generation_result_image`；`generation_preview` 只可能出现在历史数据中。

## 媒体

所有业务图片统一登记为 `MediaAsset`，二进制写入 S3 兼容对象存储。拍摄预设引用参与媒体引用计数；更新草稿槽位不会删除仍被其他版本或历史任务引用的资产。
