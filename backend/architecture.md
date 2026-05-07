# Backend Architecture

## 目标

后端负责把 Lily 本地素材、数字模特、品牌模板和生成任务组织成可追踪的生产链路。MVP 阶段优先保证流程清晰、可调试、可复跑，不追求 SaaS 化基础设施。

当前实现为 FastAPI 单体应用，代码集中在 `backend/app/`，持久化使用 SQLite 和本地文件系统。

## 当前代码结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | FastAPI app、API 路由、生成/重生/导出编排 |
| `app/db.py` | SQLite 初始化、默认模特 seed、事件日志写入 |
| `app/planner.py` | Gemini 组图策划、fallback 默认方案 |
| `app/image_provider.py` | GPT Image-2 Provider adapter，占位 SVG 输出 |
| `app/config.py` | 环境变量、storage、数据库、模型配置 |
| `storage/` | 上传素材、生成结果、导出 ZIP、SQLite、JSONL 日志 |

## 业务模块

| 模块 | 职责 |
|---|---|
| `assets` | 本地服装图包、模特图包、品牌参考图索引 |
| `model-profile` | 数字模特档案、头部角度、身体参数 |
| `garment-facts` | 商品事实卡、图片角色分类、人工修正 |
| `brand-template` | Lily 品牌模板、品类/图位参考图检索；当前内嵌在 planner 输出和 task package |
| `planner` | 根据品类规则生成 shot plan 和单张任务包 |
| `generation` | 调用 Image 2，管理生成状态、单张重生 |
| `export` | 结果命名、打包、导出 |

## 数据对象

- `GarmentSku`：货号、素材目录、图片列表、识别状态
- `GarmentImage`：货号下的单张素材图和图片角色
- `GarmentAnalysis`：保存在 `garment_skus.analysis_json` 的服装摘要
- `ModelProfile`：模特身份图、头部角度、身体参数、禁止项
- `BrandTemplate`：Lily 背景、光线、Pose、鞋包、构图边界；当前没有独立表
- `ShotPlan`：一组图的图位、镜头、角度、展示重点
- `GenerationRun`：一次整组生成或一次单张重生批次
- `GenerationImage`：单张生成结果、任务包、provider 请求/响应、最终图标记
- `TaskPackage`：保存在 `generation_images.task_package_json` 的单张生成契约
- `EventLog`：SQLite + JSONL 双写的事件留痕

实际字段和关系以 `domain_model.md` 与 `database_schema.md` 为准。

## 分层

```text
FastAPI Route (app/main.py)
  -> Generation / Upload / Export orchestration
  -> Planner adapter (app/planner.py)
  -> Image provider adapter (app/image_provider.py)
  -> SQLite repository helpers (app/db.py + SQL in app/main.py)
  -> Local file store (backend/storage/)
```

## MVP 约束

- 单客户 Lily，不做 tenant。
- 本地文件系统优先，不要求上传平台化。
- 任务状态要可恢复、可复查。
- AI provider 通过 adapter 包装，避免业务代码绑定单一模型 SDK。
- 当前没有删除接口；破坏性操作需要单独设计确认流程。
