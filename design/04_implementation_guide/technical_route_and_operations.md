# 技术路线与生产实操

## 当前技术路线

0.1.0 采用 FastAPI + SQLite + React/Vite + S3 兼容对象存储。生成链路固定为：

```text
导入主推款
→ AI 智能选搭
→ 按品类读取已发布的五镜头拍摄母版/姿势预设
→ 三类固定成片 Prompt + Image-2 edits 并发生成 5 张成片
```

后端不做动态拍摄剧本，不保存镜头计划，不调用大模型动态生成分镜或成片 Prompt。Prompt 维护页是稳定业务规则的唯一维护入口；代码只注入少量动态事实。

## 媒体缓存方案

目标是在不引入 CDN、Service Worker、IndexedDB 图片缓存或新的前端缓存系统的前提下，减少重复图片下载、降低后端 `/api/media`、SQLite、对象 HEAD、presign 和 RustFS 下行压力，同时不干扰导入、生成、替换、下载和历史复盘。

### 设计结论

- 主缓存机制使用浏览器原生 HTTP cache。
- `/api/media?key=...&variant=...` 继续作为稳定媒体入口，后端校验媒体登记与对象存在后返回 302 到 RustFS/S3 presigned URL。
- 不使用 CDN；不把图片 Blob 存入 IndexedDB、Cache Storage 或 OPFS；这些浏览器本地数据库只可作为未来业务状态缓存候选，不作为当前图片缓存主方案。
- 图片对象 key 必须按不可变资源处理：导入、生成、替换、重生和衍生图生成都应产生新 key 或保持已存在 key 的内容不变，不允许用同一 key 覆盖不同图片内容。
- 前端必须按展示尺寸使用 `thumbUrl` / `previewUrl` / `originalUrl`，并在 object key 未变化时保持 `<img src>` 稳定。

### 缓存分层

| 场景 | 资源 | 默认策略 | 说明 |
|---|---|---|---|
| 列表、卡片、结果网格 | `variant=thumb` | 私有缓存，中长 TTL + 稳定抖动 | 高频浏览资源，体积小，优先减少重复下载 |
| 详情、弹窗、分镜/成片查看 | `variant=preview` | 私有缓存，中等 TTL + 稳定抖动 | 画质高于缩略图，但仍不应默认加载原图 |
| 普通原图预览 | `variant=original` 且非下载 | 私有短 TTL + 稳定抖动 | 仅必要质检或特殊查看使用，缓存更谨慎 |
| 下载、导出触发 | `download=1` | `private, no-store` | 下载响应不做浏览器持久缓存 |
| 任务、记录、状态 JSON | API JSON | 不做长缓存 | 避免任务状态、生成进度和权限信息变旧 |

推荐默认值：

```text
APP_S3_PRESIGN_EXPIRES_SECONDS=900
APP_MEDIA_PRESIGN_EXPIRES_SECONDS=3600
APP_MEDIA_THUMB_CACHE_SECONDS=2400
APP_MEDIA_PREVIEW_CACHE_SECONDS=1800
APP_MEDIA_ORIGINAL_CACHE_SECONDS=600
APP_MEDIA_CACHE_JITTER_SECONDS=600
APP_MEDIA_PRESIGN_SAFETY_MARGIN_SECONDS=120
```

实际返回的 `max-age` 必须满足：

```text
0 <= max_age <= APP_MEDIA_PRESIGN_EXPIRES_SECONDS - APP_MEDIA_PRESIGN_SAFETY_MARGIN_SECONDS
```

其中 `APP_S3_PRESIGN_EXPIRES_SECONDS` 保持非媒体 presigned URL 的默认有效期，避免影响直传和其它对象访问；`APP_MEDIA_PRESIGN_EXPIRES_SECONDS` 只用于 `/api/media` 的 GET 重定向。实际媒体 `max-age` 按 `APP_MEDIA_PRESIGN_EXPIRES_SECONDS - APP_MEDIA_PRESIGN_SAFETY_MARGIN_SECONDS` 截断。当配置值超过媒体 presign 安全窗口时，后端必须自动截断，而不是返回会指向过期签名 URL 的缓存头。

### TTL 抖动

为避免大量图片在同一时间缓存过期形成小型“缓存雪崩”，非下载图片的 `max-age` 不应全站固定为同一个秒数。后端应基于 `object_key + variant` 计算稳定 hash 抖动：

```text
effective_ttl = base_ttl + stable_hash(object_key, variant) % jitter_seconds
```

同一图片的 TTL 在不同请求中保持稳定，便于调试；不同图片自然错开过期时间，降低一屏图片同时回源的概率。

### 响应头

`/api/media` 的 302 响应：

- `thumb` / `preview` / 非下载 `original`：`Cache-Control: private, max-age=<effective_ttl>`
- `download=1`：`Cache-Control: private, no-store`

默认不在 302 上使用 `stale-while-revalidate`，避免浏览器在签名 URL 接近过期时继续使用陈旧重定向。presigned GET URL 应尽量携带对象响应覆盖参数，使 RustFS/S3 最终图片响应也返回相同或更保守的 `Cache-Control`。如果对象存储或代理不支持该覆盖，系统仍可通过缓存 302 减少后端查询和 presign 压力，但图片内容下载收益会打折，需在压测和浏览器 Network 面板中验证。

### 风险与约束

- 旧图风险：同 key 覆盖不同内容会导致浏览器显示旧图。解决方式是把 object key 视为不可变，替换即新 key。
- 签名过期风险：302 缓存时间大于 presigned URL 有效期会导致图片加载失败。后端必须强制 TTL 小于 presign 有效期并保留安全余量。
- 本机隐私残留：`private` 表示仅浏览器本地缓存，不允许共享代理缓存；但公共电脑仍可能残留 thumb/preview。高保密部署可以下调 TTL，下载和导出继续 `no-store`。
- 解码与渲染压力：缓存只减少网络和后端压力，不能替代图片规格分层、懒加载和同屏图片数量控制。列表不得默认使用原图。
- 调试误判：DevTools `Disable cache`、强制刷新、代理覆盖响应头都会绕过或削弱缓存；排查时必须同时看 `/api/media` 302 和最终 RustFS/S3 图片响应头。

### 验收标准

- 短时间内重复进入同一列表或详情页，已加载图片应命中 memory/disk cache 或至少不全量穿透 `/api/media`。
- `thumb`、`preview`、非下载 `original` 和 `download=1` 的 `Cache-Control` 符合上表。
- 同一个 object key + variant 的 TTL 稳定，不同 key 的 TTL 有抖动。
- 所有非下载 TTL 均小于 presign 有效期减安全余量。
- 前端轮询任务状态时，未变化图片不闪烁、不重新解码、不重复下载。
- 下载成片、批量导出、AI 输入和 provider 调试路径不因缓存策略读取过期或错误资源。

## 核心表

- `media_assets`
- `styling_garments`
- `styling_garment_images`
- `generation_tasks`
- `generation_task_images`
- `generation_task_input_versions`
- `generation_stylings`
- `generation_previews`
- `generation_result_sets`
- `generation_result_images_new`
- `generation_attempts`
- `prompt_configs`

## 任务状态

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
ABANDONED
```

## Prompt 工程边界

Prompt 配置槽：

- 品牌信息；
- 搭配服装图片挑选/排序；
- AI 打标；
- 主推款图片挑选/排序；
- AI 选搭；
- 三类成片。

代码负责：

- 对搭配服装和主推款图片执行 Gemini 候选池最多 15 张的确定性预处理；导入暂存阶段允许超过 15 张，前台标红提示，正式保存前先保留当前排序前 15 张进入候选池；Gemini 挑选/排序后，搭配服装正式保留最多 6 张纯服装图，主推款输入版本保留最多 8 张参考图；
- 校验 Gemini 返回的 `selected_files` 是否存在且可唯一映射；
- 从 `styling_plan.hero_understanding.shot_category_family` 路由拍摄图位族；缺失、非法或无法映射时直接失败，不由商品 `category` / `sub_category` fallback；
- 按“拍摄母版、姿势、模特身份参考板、主推款概览板、主推款细节板、搭配款参考板”顺序组装最多 6 个参考图 URL，不传遮罩；
- 在 ToAPIs 传输边界对每张输入生成只读 `input-v2` 副本：以 8000 长边、3600 万像素、9.5 MiB 为安全目标等比缩放并将透明底转白；不写回原始对象，无法解码或仍不合规则在本地硬停；
- 渲染图号区间；
- 注入用户本次补充要求、选中造型摘要、当前镜头号和五个条件段标记；
- 记录 request / response / provider request id / output asset。

代码不负责：

- 维护隐藏摄影规则；
- 动态生成五镜头文本；
- 评分、验收、切格或因视觉质量判断自动重生；限流、连接/读取超时、网关、精确 `unknown provider for model` 与安全确认的零输出未知错误由 provider 适配层重发，已有任何输出证据时禁止盲目重发；JSON/必需字段契约异常由对应任务编排层纠错重问，默认分别最多 10 次并使用 `30/60/120/240` 秒封顶退避；
- 替用户编辑 Prompt。

## 最终成片

最终成片固定创建 5 个槽位。每格单独调用 Image-2 多参考图编辑，固定 `1:1 / 2k / high`。拍摄母版是 `image_urls[0]`，其原人物和原服装会被整图替换；姿势、商品图和模特图作为后续参考。成片 Prompt 负责当前镜头构图与商品表达，不能改变商品款式或人物身份。结果集冻结拍摄预设版本，重生沿用冻结版本，不加入同组连续性锚点。

## 验证

默认验证使用：

- Python 编译；
- 后端单元测试；
- 前端构建；
- 数据库 schema 检查；
- Prompt 配置槽检查；
- 本地 mock/provider-free 路径。

未经用户明确要求，不触发 Gemini、Image-2、ToApis、fal 等外部额度。
