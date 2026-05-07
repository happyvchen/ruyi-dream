# Backend API Guideline

## 设计原则

- API 面向前台“拍摄任务工作台”，不暴露 prompt 工程细节。
- task package 可以作为调试字段存在，但默认不展示给普通用户。
- 所有会覆盖素材、结果或实验记录的操作必须显式确认。

## 当前资源

| Resource | Method | 说明 |
|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/garment-skus` | GET | 货号图包列表 |
| `/api/garment-skus` | POST | 上传一个货号图包和多张素材图 |
| `/api/garment-skus/{sku_id}` | GET | 货号详情、素材图片、识别摘要 |
| `/api/model-profiles` | GET | 数字模特档案列表 |
| `/api/shot-plans` | POST | 根据货号和模特生成组图方案 |
| `/api/shot-plans/{shot_plan_id}` | GET | 查看组图方案 |
| `/api/generation-runs` | POST | 根据组图方案生成整组图 |
| `/api/generation-runs` | GET | 生成批次列表 |
| `/api/generation-runs/{run_id}` | GET | 生成批次详情和单张结果 |
| `/api/generation-images/{image_id}/regenerate` | POST | 单张重生 |
| `/api/generation-images/{image_id}/final` | PATCH | 设置/取消最终图 |
| `/api/logs` | GET | 事件日志 |
| `/api/exports/runs/{run_id}` | POST | 打包导出该 run 的最终图或全部图 |

当前没有独立 `/api/brand-templates`，Lily 品牌模板暂时内嵌在 planner 输出和 task package 中。

## 状态

| 对象 | 状态 |
|---|---|
| `GarmentSku` | `NEW` / `ANALYZED` / `CONFIRMED` |
| `ShotPlan` | `DRAFT` / `CONFIRMED` |
| `GenerationRun` | `CREATED` / `RUNNING` / `SUCCEEDED` / `FAILED` |
| `GenerationImage` | `QUEUED` / `RUNNING` / `SUCCEEDED` / `FAILED` |

单张重生通过新的 `GenerationRun.kind = regeneration` 和 `GenerationImage.regenerate_from_image_id` 表达，不使用独立 `REGENERATED` 状态。

## 错误处理

- `400`：参数错误或缺少必要素材
- `404`：货号、模特、任务不存在
- `409`：状态不允许当前操作，例如未确认 shot plan 就开始生成
- `422`：素材可读但无法识别或不满足生成条件
- `500`：模型调用、文件系统或未知错误

## 数据契约

- Domain 对象以 `domain_model.md` 为准。
- SQLite 表结构以 `database_schema.md` 为准。
- 前端类型位于 `../frontend/src/types.ts`，修改 API 响应时需要同步。
