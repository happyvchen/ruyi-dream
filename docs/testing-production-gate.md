# 上线前测试门禁

`scripts/test-production-gate.sh` 是默认的本地生产级门禁入口。默认模式只执行不消耗外部 AI/provider 额度的检查：

```bash
scripts/test-production-gate.sh
```

默认覆盖：

- 后端 pytest 业务/API 测试；
- 前端 Vitest 逻辑与页面交互测试；
- 前端 TypeScript typecheck；
- 前端生产构建；
- k6 压测脚本可解析性检查（仅在本机已安装 k6 时）。

## 外部 AI 额度边界

默认门禁不得调用 Gemini、Image-2、ToAPIs、fal 等外部 provider。生成链路测试通过本地 stub、任务包、状态机、Prompt 渲染和错误分类验证。

## 压测

真实压测需要运行中的后端、对象存储和准备好的测试环境：

```bash
RUN_K6_LOADTESTS=1 BASE_URL=http://127.0.0.1:8000 USERNAME=user PASSWORD=xxx scripts/test-production-gate.sh
```

写入压测会创建 `LOADTEST-` 前缀测试数据，并尝试在 teardown 清理。只应在备份过的测试/预发环境执行：

```bash
RUN_K6_LOADTESTS=1 RUN_K6_WRITE_LOADTEST=1 BASE_URL=http://127.0.0.1:8000 USERNAME=user PASSWORD=xxx scripts/test-production-gate.sh
```

更详细的压测说明见 `loadtest.md`。

## 不能承诺的事

任何自动测试都不能保证“人工一定测不出问题”。上线前仍需人工抽检：

- 真实浏览器视觉布局、不同分辨率和真实图片加载体验；
- 一次受控真实 AI 生成链路（会消耗额度）；
- 生产/预发服务器上的 k6 压测结果；
- 对象存储、网络、GPU/provider 出口和真实账号权限配置。

自动门禁的目标是把可重复、可本地验证、不会消耗额度的风险前移；真实环境和视觉质量仍需要上线前检查。

## P0 性能契约回归

后端 `test_network_performance_contract.py` 与 `test_media_repair_batch.py` 覆盖轻列表查询上限、gzip/status 字节预算、无原图回退、下载缓存、修订号内容/心跳/回滚边界和有界修复。前端 `networkPerformance.test.ts`、`MediaImage.test.tsx` 与生产页面门禁覆盖五分钟轮询预算、隐藏恢复、共享 PUT、重试退避、GET 取消、写操作保留、首屏图片预算与检查器资源切换。测试仅使用隔离数据库/mock fetch，不触发 AI。

同数据旧/新序列化测量是开发期辅助测试：Git HEAD 已包含 P0 时跳过旧基线对比，稳定预算测试仍执行。结果与生产验收边界记录在 `loadtest.md`。
