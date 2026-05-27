# design/04_implementation_guide/ — Agent Guide

## 职责 (Scope)

技术路线、生产实操、建议数据模型、服务模块、模型调用和 API 草案。这里回答“新版 MVP 如何落地实现”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `technical_route_and_operations.md` | MediaAsset、StylingGarment、GenerationTask、ShotPlan、Preview、ResultSet、Attempt 等实现路线 | 工程实现、接口设计、数据库拆分时 |
| `archive_purge_policy.md` | 归档、彻底删除、媒体引用计数、影响预检和 OSS 清理策略 | 设计或修改删除、归档、批量清理、媒体替换时 |

## 本地规则 (Local Rules)

- 复用 RustFS/S3、三层衍生图、后台轮询和 provider 快照等旧 demo 有效工程手段。
- 不让旧版货号默认组图方案主导新版 schema。
- 状态推进由后端服务统一完成。
- 每个模型调用必须有 attempt 和快照。
- 默认删除语义是归档；彻底删除必须先做影响预检，并按 `archive_purge_policy.md` 清理关联数据和对象文件。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 后端实现：`../../backend/CLAUDE.md`
