# 归档与彻底删除策略

## 目标

本策略统一前台页面、后端 API、数据库记录和 S3/RustFS 文件的删除语义。

- 归档：页面默认不可见，业务数据和对象存储文件不删除，可通过归档视图找回。
- 恢复：清空归档时间和原因，业务对象重新进入默认列表；不修改其业务状态或历史快照。
- 彻底删除：删除表单数据、直接关联数据、明确依赖数据和已登记的对象存储文件，不可恢复。
- 单张图片移除/替换：先解除当前业务关系，再按引用计数释放无人引用的 `MediaAsset` 和三档对象文件。

## 通用规则

1. 默认用户操作必须是归档，不是物理删除。
2. 归档只写 `archived_at` / `archive_reason` 并从默认列表过滤，不清理 OSS。
3. 彻底删除必须有影响预检，展示将删除的业务记录、生成记录、媒体数量和估算大小。
4. 彻底删除必须二次确认：单条输入完整名称，批量输入 `全部删除`。
5. 对象存储清理以数据库登记的 `media_assets.object_key`、`thumb_key`、`preview_key` 为主，prefix 清理只作为兜底。
6. 删除或替换单张图片时，只有该 `MediaAsset` 不再被其它关系或历史快照引用，才能删除 DB 元数据和 OSS 文件。
7. 已进入 AI 阶段的主推款输入不能单独改图；只能归档任务或彻底删除整条任务。
8. 搭配服装被历史任务使用后，默认归档不影响历史；彻底删除时必须明确提示会删除依赖它的生成记录。

## 对象策略

| 对象 | 默认操作 | 归档行为 | 彻底删除范围 | OSS 清理 |
|---|---|---|---|---|
| `StylingGarment` 搭配服装 | 归档 | 默认列表隐藏，不进入智能选搭，保留标签和历史快照 | 删除服饰、图片关系、标签、依赖该服饰的生成任务和 attempts | 服饰图、依赖任务输入、预览、结果和 attempt 输出 |
| `StylingGarmentImage` 搭配服装单图 | 移除图片 | 不使用归档；从当前服饰图包移除 | 删除图片关系；若媒体无其它引用则释放 `MediaAsset` | 无引用时删除原图、thumb、preview |
| `GenerationTask` / 生成记录 | 归档 | AI 摄影棚最近任务和生成记录默认隐藏，历史数据保留 | 删除任务、输入版本、主推款图片关系、StylingPlan、ShotPlan、预览、结果集、结果图、attempts | 主推款图、预览图、最终图、attempt 输出 |
| `GenerationTaskImage` 草稿主推款单图 | 移除图片 | 不使用归档；仅草稿期允许 | 删除图片关系；若媒体无其它引用则释放 `MediaAsset` | 无引用时删除原图、thumb、preview |
| `ModelProfile` 数字模特 | 不开放普通删除 | 前台只读；如开放应优先归档 | 无历史引用且至少保留一个时才允许删除 | 模特头像目录 |

## API 契约

影响预检接口：

- `POST /api/styling-garments/delete-impact`，body: `{ "ids": [1, 2] }`
- `POST /api/generation-tasks/delete-impact`，body: `{ "ids": [1, 2] }`

响应字段使用前端可直接展示的驼峰命名，包括：

- `garmentCount`
- `garmentImageCount`
- `dependentTaskCount`
- `taskCount`
- `resultImageCount`
- `mediaAssetCount`
- `estimatedBytes`

彻底删除接口必须返回 `objectCleanupErrors`。对象清理失败不应伪装成功无事发生；前端或运维应能看到失败 prefix。

## 实现备注

当前 0.1.0 同步执行对象清理，并通过幂等 `delete_object` / `delete_prefix` 容忍对象已不存在。后续如果删除量变大，应升级为 `deletion_pending` 状态和后台清理 job，但用户可见语义保持不变。
