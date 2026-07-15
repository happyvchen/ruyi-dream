# 技术路线与生产实操

## 当前技术路线

0.1.0 采用 FastAPI + SQLite + React/Vite + S3 兼容对象存储。生成链路固定为：

```text
上传主推款
→ AI 智能选搭
→ 三类固定 Prompt 生成分镜预览
→ 三类固定 Prompt 生成前 5 张成片
```

后端不做动态拍摄剧本，不保存镜头计划，不调用大模型动态生成分镜或成片 Prompt。Prompt 维护页是稳定业务规则的唯一维护入口；代码只注入少量动态事实。

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
- 三类分镜预览；
- 三类成片。

代码负责：

- 对搭配服装和主推款图片执行每套最多 15 张的硬上限；
- 校验 Gemini 返回的 `selected_files` 是否存在且可唯一映射；
- 从 `styling_plan.hero_understanding.shot_category_family` 路由拍摄图位族；缺失、非法或无法映射时直接失败，不由商品 `category` / `sub_category` fallback；
- 按顺序组装数字模特图、商品图、分镜图、环境图；
- 渲染图号区间；
- 注入商品信息、用户本次补充要求、选中造型摘要、当前格号和五个条件段标记；
- 记录 request / response / provider request id / output asset。

代码不负责：

- 维护隐藏摄影规则；
- 动态生成六镜头文本；
- 评分、验收、切格或自动重试；
- 替用户编辑 Prompt。

## 分镜预览

分镜预览是 3:2 横版六格图。上装、下装、连衣裙/连体/套装分别读取对应固定 Prompt。镜头规则、姿态边界、背景墙地缝、镜头 5 中央 2:3 安全区等要求写在 Prompt 正文。

## 最终成片

最终成片固定创建 5 个槽位，对应分镜预览前 5 格。每格单独调用 Image-2，参考当前分镜单格、商品图、模特图和环境图。成片 Prompt 负责逐格兑现分镜意图，不能改变商品款式、人物身份或背景语法。

## 验证

默认验证使用：

- Python 编译；
- 后端单元测试；
- 前端构建；
- 数据库 schema 检查；
- Prompt 配置槽检查；
- 本地 mock/provider-free 路径。

未经用户明确要求，不触发 Gemini、Image-2、ToApis、fal 等外部额度。
