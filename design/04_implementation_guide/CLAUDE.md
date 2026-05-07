# design/04_implementation_guide/ — Agent Guide

## 职责 (Scope)

技术路线、模型能力分工、生产操作、品牌初始化、任务包落地方式。这里回答“如何把设计转成可实现的生产链路”。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `technical_route_and_operations.md` | 技术架构、模型分工、品牌初始化、生产操作、任务包结构 | 工程实现、技术拆分、生产流程设计时 |

## 本地规则 (Local Rules)

- 工程实现先保持 MVP：本地素材目录、单客户、单主入口、人工确认。
- 生成链路按预处理层、策划层、生成层拆分，不把所有逻辑塞进一个接口或一个 prompt。
- 如果实现细节需要长期沉淀，应同步到 `../../backend/` 或 `../../frontend/` 对应文档。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 后端实现：`../../backend/CLAUDE.md`
- 前端实现：`../../frontend/CLAUDE.md`

