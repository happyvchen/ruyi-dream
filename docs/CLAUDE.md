# docs/ — Agent Guide

## 职责 (Scope)

本目录存放面向人类使用、开发、部署和运维的项目文档。用户侧操作教程与工程侧运行说明都放在这里；产品和生成链路的权威设计仍以 `design/` 下文档为准。

## 文档索引 (Index)

| 文件 | 内容 | 何时需要读 |
|---|---|---|
| `user-guide.md` | 普通 user 用户视角的 Ruyi-Dream 使用教程：AI 摄影棚、搭配服装库、数字模特、生成记录、重试和版本限制 | 编写用户培训材料、客服说明、演示脚本，或确认普通用户操作口径时 |
| `local-development.md` | 本地开发环境配置和运行说明 | 本地开发、启动前后端和 RustFS 时 |
| `deployment.md` | 部署手册:首次部署、数据迁移、日常运维、备份恢复、故障排查、安全要点 | 部署、迁移、升级、处理线上故障时 |
| `testing-production-gate.md` | 上线前测试门禁：本地不耗额度测试入口、k6 压测开关和人工验收边界 | 准备上线前测试、CI 门禁或解释自动化覆盖边界时 |
| `loadtest.md` | 单机部署压测与体验评估 Runbook,配套 `scripts/loadtest/` k6 脚本 | 评估网络/存储/并发瓶颈、执行压测或分析用户体验问题时 |
| `implementation-samples/README.md` | 实施样例索引 | 查看 Prompt 和实现样例时 |

## 本地规则 (Local Rules)

- 面向普通用户的文档只使用摄影、电商、商品和操作语言，不引导用户写 prompt 或调整模型参数。
- 新增、移动或删除本目录 Markdown 文件时，同步更新本文件索引。
- 工程部署文档不得混入生产密钥、账号密码或真实 provider 调试凭证。

## 相关 (See Also)

- 父级：`../CLAUDE.md`
- 设计权威索引：`../design/CLAUDE.md`
- 项目介绍：`../README.md`
