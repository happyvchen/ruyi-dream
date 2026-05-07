# simulations/ — Agent Guide

## 职责 (Scope)

本目录存放单 SKU 生成实验、素材板、prompt 试验、输出结果和结果观察。这里的内容用于复盘和验证，不是权威产品设计。

## 文档索引 (Index)

| 路径 | 内容 | 使用方式 |
|---|---|---|
| `125309C7948510/docs/` | V1 生成组图流程记录和长 prompt | 了解早期 5 宫格方案和问题起点 |
| `125309C7948510-v2/docs/` | V2 短提示词、单张生成、结果观察 | 了解从长 prompt 转向图片角色 + 短任务文本的过程 |
| `125309C7948510-v3/docs/` | 新版三层流程和结构化任务包验证 | 了解预处理层 -> 策划层 -> Image 2 逐张生成的验证过程 |
| `125309C7948510-v4/docs/` | 无同款实拍输入限制验证与可信度说明 | 了解生产环境模拟和上下文污染风险 |

## 本地规则 (Local Rules)

1. **实验记录不是权威设计**。实现产品功能时必须回到 `../design/CLAUDE.md` 中列出的权威源头文档。
2. **不要从某一轮 prompt 直接复制为产品策略**。prompt 只能作为实验样例，正式策略应沉淀到 `../design/01_generation_flow/image2_input_strategy.md` 或 `../design/04_implementation_guide/technical_route_and_operations.md`。
3. **不要把同款实拍验证结果误认为真实生产能力**。尤其注意 v4 记录的同一对话线程上下文污染风险。
4. **新增实验轮次时**，建议使用 `simulations/<sku>-vN/`，并在本文件索引中增加一行说明。
5. **清理、覆盖、移动 outputs/assets/real 之前必须获得用户确认**。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 权威设计：`../design/CLAUDE.md`
