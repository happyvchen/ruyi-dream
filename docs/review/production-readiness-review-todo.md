# ruyi-dream Production Readiness Review Todo

## Goal

把 ruyi-dream 从当前 MVP 状态审查到“可生产交付”的质量水位。Review 不只覆盖代码，也覆盖产品定位、业务流程、UI/UX、架构、AI 生成链路、部署、安全、测试、文档和 GitHub 协作规范。

## Review Principles

- 先质疑产品和流程，再审查工程实现。
- 每个 review 任务聚焦一个主题，输出可执行发现，不做泛泛建议。
- 发现按优先级标记：P0 上线阻断，P1 生产高风险，P2 质量/体验改进，P3 后续优化。
- 需要产品或架构决策时暂停推进，并向负责人提出明确选项。
- Review 结果先沉淀到 `docs/review/production-readiness-review-findings.md`，后续再拆成修复 PR。

## Review Tasks

| ID | Topic | Scope | Status |
| --- | --- | --- | --- |
| R01 | 产品定位与范围 | ruyi-dream 是否应从组图 MVP 升级为电商视觉生产系统；Seedance 视频扩展边界；MVP 成功标准 | Initial findings logged |
| R02 | 核心业务流程 | 服装素材、默认方案、分镜预览、最终图、重生、导出、记录恢复是否匹配真实电商生产 | Initial findings logged |
| R03 | UI/UX 工作台 | 信息层级、导航、按钮状态、错误提示、空状态、批量导入、等待态、历史恢复 | Initial findings logged |
| R04 | 前端架构与状态管理 | `App.tsx` 规模、组件拆分、路由恢复、轮询、错误处理、API 封装、类型一致性 | Initial findings logged |
| R05 | 后端 API 契约 | REST 清晰度、错误结构、幂等、重试、外部调用可能性、前后端契约一致性 | Partially remediated: MVP API token added |
| R06 | 数据模型与迁移 | SQLite schema、JSON 快照、attempts、历史可追溯、未来 PostgreSQL/多租户迁移 | Initial findings logged |
| R07 | 异步任务与生成链路 | fal queue、轮询、失败恢复、重复提交、任务取消、超时、并发控制 | Initial findings logged |
| R08 | AI 策划层与任务包 | Gemini 策划、品牌规则、Image 2 职责边界、服装保真、模特一致性、Seedance 抽象 | Initial findings logged |
| R09 | 对象存储与媒体安全 | S3 key、预签名 URL、私有对象、导出 ZIP、生命周期、备份、素材泄露风险 | Initial findings logged |
| R10 | 部署与运维 | Dockerfile、Compose、容器名、环境变量、健康检查、日志轮转、升级回滚 | Partially remediated: pinned tags and non-root containers |
| R11 | 安全与配置 | `.env`、密钥、CORS、上传校验、文件大小、鉴权、私有部署边界、多用户权限 | Initial findings logged |
| R12 | 测试策略 | 单元测试、API 测试、前端交互测试、任务模拟、对象存储 mock、Docker smoke test | Initial findings logged |
| R13 | 文档与交接 | README、AGENTS、设计文档、API 文档、部署/运维文档一致性和可接手性 | Remediated: production runbook added |
| R14 | GitHub 协作规范 | 分支保护、PR 模板、CODEOWNERS、review 要求、commit 规范、CI、release/changelog | Partially remediated: CI/PR template/CODEOWNERS added |
| R15 | 生产级交付清单 | 汇总 P0/P1/P2，形成上线前必须完成、后续迭代、可暂缓路线图 | Updating with repair status |

## Open Decision Log

| ID | Decision Needed | Owner | Status |
| --- | --- | --- | --- |
| D01 | ruyi-dream 是否正式定位为“电商视觉生产系统”，而不是“AI 模特组图生成工具” | Product owner | Decided: yes |
| D02 | Seedance 2.0 主视频能力是否进入近期架构设计边界，还是仅作为未来扩展备注 | Product owner / Tech owner | Decided: note as next design/development phase after this review |
