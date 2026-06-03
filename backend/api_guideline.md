# Backend API Guideline

API 面向前台 AI 摄影棚工作台，不暴露 prompt 工程细节。旧 `/api/garment-skus`、`/api/shot-plans`、`/api/generation-runs`、`/api/generation-images` 和 `/api/ai-policy-config` 已删除。

## 认证与基础

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/auth/login` | POST | 登录 |
| `/api/auth/logout` | POST | 登出 |
| `/api/auth/session` | GET | 当前会话 |
| `/api/media` | GET | 稳定媒体访问入口 |
| `/api/logs` | GET | 事件日志 |

## 搭配服饰库

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/styling-garments` | GET | 列表 |
| `/api/styling-garments` | POST | 创建搭配服饰并上传图片 |
| `/api/styling-garments/{id}` | GET | 详情 |
| `/api/styling-garments/{id}` | PATCH | 编辑状态、人工标签和归档信息 |
| `/api/styling-garments/{id}/images` | POST | 追加图片 |
| `/api/styling-garment-images/{image_id}` | PATCH | 调整图片 |
| `/api/styling-garment-images/{image_id}` | DELETE | 删除图片链接 |
| `/api/styling-garments/{id}/tag` | POST | 发起 AI 打标 |

打标会保存完整 `ai_tags_json` 和 attempt 快照；选搭阶段不会把完整标签 JSON 直接塞入 prompt。

## AI 摄影棚任务

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/generation-tasks` | GET | 任务列表 |
| `/api/generation-tasks` | POST | 创建草稿任务 |
| `/api/generation-tasks/{id}` | GET | 任务详情 |
| `/api/generation-tasks/{id}` | PATCH | 编辑草稿 |
| `/api/generation-tasks/{id}` | DELETE | 归档或删除任务 |
| `/api/generation-tasks/{id}/images` | POST | 上传主推款图片 |
| `/api/generation-task-images/{image_id}` | PATCH | 调整主推款图片 |
| `/api/generation-task-images/{image_id}` | DELETE | 删除主推款图片 |
| `/api/generation-tasks/{id}/ready` | POST | 冻结草稿，进入选搭 |
| `/api/generation-tasks/{id}/select-styling` | POST | 调用 AI Stylist |
| `/api/generation-tasks/{id}/shoot-script` | POST | 调用 AI Director |
| `/api/generation-tasks/{id}/preview` | POST | 生成 3x2 分镜预览 |
| `/api/generation-tasks/{id}/generate-final` | POST | 并发生成前 5 张成片 |
| `/api/generation-result-images/{image_id}/regenerate` | POST | 重生单张最终图 |
| `/api/generation-records` | GET | 生成记录列表 |

所有会调用外部 AI/provider 的端点都必须由用户动作触发；本地验证和测试不得调用外部 AI/provider。

## 配置

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/model-profiles` | GET/POST | 数字模特列表和创建 |
| `/api/model-profiles/{id}` | PATCH/DELETE | 编辑或删除数字模特 |
| `/api/model-profiles/{id}/head-references` | POST | 上传头部参考图 |
| `/api/brand-info` | GET/PATCH | 品牌资料 |
| `/api/shoot-config` | GET/PATCH | 拍摄规则 |
| `/api/prompt-configs` | GET | Prompt 配置列表 |
| `/api/prompt-configs/{key}/preview` | GET | 本地渲染预览，不调用模型 |
| `/api/prompt-configs/{key}` | PATCH | 保存 Prompt |

## Prompt Runtime

Prompt 配置可使用以下 runtime：

- `runtime.candidate_retrieval`：AI Stylist 候选服饰紧凑卡片。
- `runtime.styling_result`：AI Director 使用的选搭导演上下文。
- `runtime.model_identity_context`：AI Director 使用的数字模特身份契约和参考图上下文。
- `runtime.shoot_script_result`：分镜预览使用的拍摄剧本上下文。
- `runtime.current_shot_context`：最终成片单镜头上下文。
- `runtime.image_reference_manifest`：Image-2 输入参考图序号、角色和事实源优先级。

不再支持 `runtime.ai_policy`、`runtime.ai_policy_card`、旧 `runtime.styling_plan`、旧 `runtime.shot_plans` 等兼容占位符。相关规则应直接写入当前 Prompt 正文或品牌/拍摄配置。

## 选搭输入 DTO

`runtime.candidate_retrieval` 中每个候选只允许包含业务决策必要字段：

```json
{
  "code": "LOOK-A",
  "category": "上装",
  "colors": ["白色"],
  "materials": ["梭织"],
  "fit": "合体",
  "silhouette": "短款利落",
  "length": "及腰",
  "wearing_capabilities": ["可塞腰", "可外搭"],
  "style_summary": "通勤、清爽、轻量",
  "pairing_risks": ["可能遮挡腰线"],
  "manual_flags": {},
  "review": {"confidence": 0.86}
}
```

不要把 `ai_tags_json`、`effective_tags_json`、`manual_tags_json`、`tag_meta_json` 整包进入 prompt。

## 生成输入原则

- 主推款和选中搭配服饰图片是商品事实来源。
- 数字模特参考图是人物身份事实来源，必须作为 `model_identity` 前置输入并写入 `image_reference_manifest`。
- 分镜预览是构图参考，不是商品事实来源，也不是模特身份事实来源。
- ShootScript、分镜预览、品牌妆发和 provider 默认审美不得覆盖数字模特的脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色和气质。
- Image-2 prompt 只给当前阶段必要结构；最终单张图只给当前镜头上下文。
- 第 6 个 ShotPlan 只用于分镜预览，不进入最终成片。
