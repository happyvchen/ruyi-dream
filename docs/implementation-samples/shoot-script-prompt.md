# 拍摄剧本 Prompt 实施样例（已废弃）

> 0.1.0 当前主流程不再提供 `assembly.shoot_script` 槽位，不生成 `ShootScript` / `ShotPlan`，也不向分镜或成片注入 `runtime.shoot_script_result` / `runtime.resolved_accessories`。本文仅保留为历史参考，不得复制到 `Prompt 维护`。

当前链路为：上传主推款 → AI 智能选搭 → 三类固定分镜 Prompt 直接生成分镜预览 → 按前 5 格生成成片。分镜和成片的稳定镜头规则应维护在：

- `assembly.storyboard_preview_top`
- `assembly.storyboard_preview_bottom`
- `assembly.storyboard_preview_one_piece`
- `assembly.final_image_top`
- `assembly.final_image_bottom`
- `assembly.final_image_one_piece`

可用 Runtime 占位以 `backend/api_guideline.md` 和 Prompt 维护页展示为准。

历史样例已移除，避免误用旧动态拍摄剧本链路。
