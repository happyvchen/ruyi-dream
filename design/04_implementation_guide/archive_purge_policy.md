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
5. 彻底删除确认后先写入持久删除状态：`archive_reason = deletion_pending`，默认列表立即隐藏；刷新页面后不应回到日常列表。
6. 数据库删除必须先于对象存储清理提交成功。若数据库删除失败，记录保留在已归档视图并写入 `archive_reason = deletion_failed`，媒体文件不得提前丢失。
7. 对象存储清理以数据库登记的 `media_assets.object_key` 为根，枚举原图、normalized、thumb、preview 和 storyboard cells 的确定键逐个删除；业务媒体禁止使用 prefix 清理兜底。
8. 对象存储清理是数据库删除后的最终清理步骤；对象清理失败不恢复已删除业务记录，但必须返回 `objectCleanupErrors` 并允许运维后续重试。
9. 删除或替换单张图片时，只有该 `MediaAsset` 不再被其它关系或历史快照引用，才能删除 DB 元数据和 OSS 文件。
10. 已进入 AI 阶段的主推款输入不能单独改图；只能归档任务或彻底删除整条任务。
11. 搭配服装被历史任务使用后，默认归档不影响历史；彻底删除时必须明确提示会删除依赖它的生成记录。

## 对象策略

| 对象 | 默认操作 | 归档行为 | 彻底删除范围 | OSS 清理 |
|---|---|---|---|---|
| `UploadSession` 上传记录 | 归档 | 任意任务状态均可归档；仅从默认上传记录隐藏，不改变上传状态，不停止任务，不影响已入库业务对象 | 所有状态均接受删除；运行中先进入 `deletion_pending` 并阻断后续写入，直传签名失效后再删除会话及条目元数据；已关联业务对象保留 | 只删除未登记为 `MediaAsset` 的临时直传对象；精确 key 进入持久重试队列 |
| `StylingGarment` 搭配服装 | 归档 | 默认列表隐藏，不进入智能选搭，保留标签和历史快照 | 删除服饰、图片关系、标签、依赖该服饰的生成任务和 attempts | 服饰图、依赖任务输入、预览、结果和 attempt 输出 |
| `StylingGarmentImage` 搭配服装单图 | 移除图片 | 不使用归档；从当前服饰图包移除 | 删除图片关系；若媒体无其它引用则释放 `MediaAsset` | 无引用时删除原图、thumb、preview |
| `GenerationTask` / 生成记录 | 归档 | AI 摄影棚最近任务和生成记录默认隐藏，历史数据保留 | 删除任务、输入版本、主推款图片关系、StylingPlan、预览、结果集、结果图、attempts | 主推款图、预览图、最终图、attempt 输出 |
| `GenerationTaskImage` 草稿主推款单图 | 移除图片 | 不使用归档；仅草稿期允许 | 删除图片关系；若媒体无其它引用则释放 `MediaAsset` | 无引用时删除原图、thumb、preview |
| `ModelProfile` 数字模特 | 不开放普通删除 | 前台只读；如开放应优先归档 | 无历史引用且至少保留一个时才允许删除 | 模特头像目录 |

## API 契约

影响预检接口：

- `POST /api/styling-garments/delete-impact`，body: `{ "ids": [1, 2] }`
- `POST /api/generation-tasks/delete-impact`，body: `{ "ids": [1, 2] }`
- `POST /api/upload-sessions/delete-impact`，body: `{ "ids": ["session-id"] }`

上传记录使用 `POST /api/upload-sessions/archive`、`POST /api/upload-sessions/restore` 和 `DELETE /api/upload-sessions` 管理。所有上传状态均开放这些入口；其归档/删除语义只作用于上传记录层，不能级联改变已入库业务对象。

响应字段使用前端可直接展示的驼峰命名，包括：

- `garmentCount`
- `garmentImageCount`
- `dependentTaskCount`
- `taskCount`
- `resultImageCount`
- `mediaAssetCount`
- `estimatedBytes`

彻底删除接口必须返回 `objectCleanupErrors`。对象清理失败不应伪装成功无事发生；前端或运维应能看到失败对象键。

## 实现备注

当前 0.1.0 不引入独立业务删除 job；上传记录仅使用轻量持久对象清理队列保存精确 key 和重试错误。彻底删除必须遵循持久状态与顺序边界：

1. 校验目标存在性、确认文本和影响范围；运行中上传记录不得拒绝删除请求，而应进入延迟安全清理协议；
2. 写入 `deletion_pending` 并提交，使刷新后的默认列表立即隐藏；
3. 在数据库事务中删除业务记录、解除上传会话等弱引用并删除 `MediaAsset` 元数据；
4. 数据库事务提交后再清理对象存储；
5. 数据库阶段失败时写入 `deletion_failed`，用户可在已归档视图恢复后重试；
6. 对象清理失败时返回 `objectCleanupErrors`，业务记录保持已删除，后续由运维补清理。

业务媒体清理只通过幂等 `delete_object` 容忍对象已不存在。`delete_prefix` 仅保留给明确独占目录的非 `MediaAsset` 聚合对象（当前只有具体 `model-profiles/<model-id>`），对象存储适配层必须拒绝共享根和 `direct-uploads/<scope>` 级前缀。后续如果删除量变大，可把第 3-6 步升级为后台 job，但用户可见语义保持不变。
