# 实施样例

本目录保存 0.1.0 实施阶段可参考的 Prompt 样例。它们用于现场实施、客户确认和人工填充 `Prompt 维护`，不是系统初始化数据，也不是代码 fallback。

| 文档 | 对应 Prompt 槽位 |
|---|---|
| [品牌信息 Prompt 实施样例](brand-info-prompt.md) | `segment.brand_info` |
| [AI 打标 Prompt 实施样例](ai-tagging-prompt.md) | `assembly.ai_tagging` |
| [AI 选搭 Prompt 实施样例](ai-styling-prompt.md) | `assembly.ai_styling` |
| [拍摄剧本 Prompt 实施样例](shoot-script-prompt.md) | `assembly.shoot_script` |
| [生成成片 Prompt 实施样例](final-image-prompt.md) | `assembly.final_image` |

实施规则：

- 生产系统仍只初始化空 Prompt 槽位。
- 样例内容必须经现场确认后人工写入 `Prompt 维护`。
- 修改 Prompt 样例时，不代表自动修改运行中环境。
