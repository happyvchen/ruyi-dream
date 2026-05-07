# Backend Architecture

## 目标

后端负责把 Lily 本地素材、数字模特、品牌模板和生成任务组织成可追踪的生产链路。MVP 阶段优先保证流程清晰、可调试、可复跑，不追求 SaaS 化基础设施。

## 建议模块

| 模块 | 职责 |
|---|---|
| `assets` | 本地服装图包、模特图包、品牌参考图索引 |
| `model-profile` | 数字模特档案、头部角度、身体参数 |
| `garment-facts` | 商品事实卡、图片角色分类、人工修正 |
| `brand-template` | Lily 品牌模板、品类/图位参考图检索 |
| `planner` | 根据品类规则生成 shot plan 和单张任务包 |
| `generation` | 调用 Image 2，管理生成状态、单张重生 |
| `export` | 结果命名、打包、导出 |

## 数据对象草案

- `GarmentSku`：货号、素材目录、图片列表、识别状态
- `GarmentFactCard`：品类、颜色、材质、结构、比例、人工修正字段
- `ModelProfile`：模特身份图、头部角度、身体参数、禁止项
- `BrandTemplate`：Lily 背景、光线、Pose、鞋包、构图边界
- `ShotPlan`：一组图的图位、镜头、角度、展示重点
- `GenerationTask`：单张任务包、输入图片、短文本任务、状态、输出路径

## 分层建议

```text
API Controller
  -> Application Service
  -> Domain Planner / Adapter
  -> Repository / File Store
  -> AI Provider Adapter
```

## MVP 约束

- 单客户 Lily，不做 tenant。
- 本地文件系统优先，不要求上传平台化。
- 任务状态要可恢复、可复查。
- AI provider 通过 adapter 包装，避免业务代码绑定单一模型 SDK。

