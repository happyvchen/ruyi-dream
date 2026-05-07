# Backend API Guideline

## 设计原则

- API 面向前台“拍摄任务工作台”，不暴露 prompt 工程细节。
- task package 可以作为调试字段存在，但默认不展示给普通用户。
- 所有会覆盖素材、结果或实验记录的操作必须显式确认。

## 资源草案

| Resource | 说明 |
|---|---|
| `/api/garment-skus` | 货号图包列表、素材图片、商品事实卡 |
| `/api/model-profiles` | 数字模特档案 |
| `/api/brand-templates` | Lily 品牌模板和参考图索引 |
| `/api/shot-plans` | 根据货号、模特、品类规则生成组图方案 |
| `/api/generation-tasks` | 单张/整组生成任务、状态、重生 |
| `/api/exports` | 导出最终组图 |

## 状态草案

| 对象 | 状态 |
|---|---|
| `GarmentSku` | `NEW` / `ANALYZED` / `CONFIRMED` |
| `ShotPlan` | `DRAFT` / `CONFIRMED` |
| `GenerationTask` | `QUEUED` / `RUNNING` / `SUCCEEDED` / `FAILED` / `REGENERATED` |
| `ExportJob` | `CREATED` / `READY` / `FAILED` |

## 错误处理

- `400`：参数错误或缺少必要素材
- `404`：货号、模特、任务不存在
- `409`：状态不允许当前操作，例如未确认 shot plan 就开始生成
- `422`：素材可读但无法识别或不满足生成条件
- `500`：模型调用、文件系统或未知错误

