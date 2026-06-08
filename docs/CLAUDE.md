# docs/ — Agent Guide

## 职责 (Scope)

本目录存放面向人类使用、开发、部署和运维的项目文档。用户侧操作教程与工程侧运行说明都放在这里；产品和生成链路的权威设计仍以 `design/` 下文档为准。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `user-guide.md` | 普通 user 用户视角的 Ruyi-Dream 使用教程：AI 摄影棚、搭配服装库、数字模特、生成记录、重试和版本限制 | 编写用户培训材料、客服说明、演示脚本，或确认普通用户操作口径时 |
| `local-development.md` | 本地开发环境配置和运行说明 | 本地开发、启动前后端和 RustFS 时 |
| `docker-compose-operations.md` | Docker Compose 部署架构、首次部署、日常管理、备份恢复和故障排查 | 使用 Compose 部署、更新、查看服务状态，或确认单机部署边界时 |
| `runbook.md` | 生产故障排查 Runbook | 处理线上故障、排查服务状态和恢复流程时 |
| `implementation-samples/README.md` | 实施样例索引 | 查看 Prompt 和实现样例时 |

## 本地规则 (Local Rules)

- 面向普通用户的文档只使用摄影、电商、商品和操作语言，不引导用户写 prompt 或调整模型参数。
- 新增、移动或删除本目录 Markdown 文件时，同步更新本文件索引。
- 工程部署文档不得混入生产密钥、账号密码或真实 provider 调试凭证。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计权威索引：`../design/CLAUDE.md`
- 项目介绍：`../README.md`
