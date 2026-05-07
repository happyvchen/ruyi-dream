# Frontend Architecture

## 产品形态

前端是一个面向普通业务用户的“拍摄任务工作台”，不是 AI 参数控制台。

默认主流程：

```text
选择服装 -> 选择模特 -> 确认组图方案 -> 生成/确认/导出
```

## 建议页面

| 页面 | 职责 |
|---|---|
| `GenerateWorkspace` | 主工作台，承载完整生成流程 |
| `GarmentLibrary` | 货号图包列表和素材预览 |
| `ModelProfiles` | 数字模特档案维护 |
| `BrandTemplates` | Lily 品牌模板查看/维护，MVP 可弱化 |
| `GenerationHistory` | 历史生成结果和导出记录 |

## 状态分工

- Server state：货号、模特、品牌模板、shot plan、生成任务
- Client state：当前步骤、选中图位、展开/折叠、高级设置显隐
- 不把后端生成状态复制成多份本地状态，以任务 ID 为准轮询或订阅。

## 组件建议

- `GarmentCard`
- `ModelProfileCard`
- `ShotPlanBoard`
- `ShotCard`
- `GenerationProgress`
- `ResultGallery`
- `RegeneratePanel`
- `ExportPanel`

