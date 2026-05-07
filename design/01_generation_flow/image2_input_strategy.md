# Image 2 输入职责拆分与提示词策略

## 1. 核心原则

Image 2 的输入同时包含大量图片和文本时，应该遵守一个原则：

`图片决定事实，文本决定任务。`

更完整的产品原则是：

`预处理层负责理解，策划层负责决策，Image 2 负责生成。`

Image 2 不应承担以下职责：

- 每次重新推理 Lily 品牌调性
- 判断什么鞋包符合 Lily
- 临场发明模特 pose
- 临场决定组图镜头
- 用长文本修正服装比例
- 通过审美词猜测商品结构

这些职责应在 Image 2 之前完成，并以结构化任务包的形式传给 Image 2。

也就是说：

- 服装长什么样，尽量由服装图片决定。
- 模特长什么样，尽量由模特头部图片决定。
- Lily 的拍摄调性，尽量由真实成片参考图决定。
- 文本不要重复描述图片里已经明确的细节。
- 文本只负责告诉模型：这些图片分别是什么角色、哪些信息必须保留、当前要生成哪张图、哪些边界不能越过。

如果文本过度描述图片内容，容易出现冲突：

- 图片是轻 A 字，文本写 A-line，模型可能夸大成大摆。
- 图片是短裙，文本写 dress，模型可能自动变长。
- 图片里鞋履很轻松，文本写通勤，模型可能生成老气职业鞋。
- 图片是某个具体模特，文本写 young female model，模型可能只保留年龄和风格，不保留身份。

所以后续产品应该把“输入图片”和“提示词文本”的职责拆开。

## 2. 输入图片的职责

### 2.1 服装图片负责商品事实

服装图包决定：

- 服装品类
- 颜色
- 面料质感
- 版型
- 领型
- 袖型
- 裤长/裙长/衣长
- 下摆宽度
- 口袋、纽扣、拉链、开衩、褶皱
- 图案、印花、条纹
- 正面和背面结构
- 局部细节

文本不要重复写成一大段服装描述，只在必要时写：

`Use the garment reference images as the source of truth for garment shape, color, material, proportion, and construction.`

### 2.2 模特头部图负责身份

模特头部图包决定：

- 同一张脸
- 脸型
- 五官
- 发型
- 刘海
- 发色
- 耳饰
- 头部角度
- 表情边界

文本只需要说明：

- 当前使用哪个头部角度
- 必须保持同一身份
- 不允许重新生成另一个相似风格的模特

### 2.3 Lily 真实成片负责品牌调性

真实成片参考图决定：

- 光线
- 背景
- 构图
- 人物占比
- pose 语法
- 鞋包和配饰的自由度边界
- 商品优先级
- 画面克制程度

文本只需要说明：

`Use the Lily real e-commerce references as the brand-style boundary for pose, styling, composition, and mood. Do not copy a specific outfit unless it is provided as the garment reference.`

### 2.4 Pose 参考图负责身体动作

Pose 参考图决定：

- 身体朝向
- 重心
- 手臂位置
- 腿部姿态
- 头部朝向
- 走动感或静态感

文本只需要指派：

- 参考哪张 pose 图
- 当前生成的镜头目的是什么
- 哪些动作不能夸张

## 3. 文本提示词的职责

文本只做 6 件事：

1. 标注图片角色
2. 定义当前镜头任务
3. 指定保真优先级
4. 指定生成边界
5. 指定平台输出格式
6. 指定禁止项

文本不做这些事：

- 不重新描述服装图里已经清晰可见的款式、颜色和细节。
- 不用 `fashionable`、`premium`、`modern` 这类泛审美词替代 Lily 模板。
- 不用 `A-line`、`short dress` 这类可能导致比例漂移的抽象词单独控制裙长和裙摆。
- 不让 Image 2 自己判断“适合 Lily 的鞋子”。
- 不让 Image 2 自己决定本款该拍哪几张。

## 4. 图片角色设计

每次生成单张图时，建议传入以下图片角色。

| 角色 | 是否必需 | 决定内容 |
| --- | --- | --- |
| Garment Front | 必需 | 服装正面结构 |
| Garment Back | 视镜头而定 | 服装背面结构 |
| Garment Detail | 建议 | 面料、局部、工艺 |
| Model Head Angle | 必需 | 当前镜头的人脸身份 |
| Brand Style References | 建议 | Lily 调性、鞋包、pose、构图 |
| Pose Reference | 建议 | 当前镜头身体动作 |
| Previous Approved Output | 可选 | 同一组图一致性 |

## 5. 文本结构模板

正式 prompt 应尽量短，结构如下：

```text
Task:
Generate one Lily e-commerce model photo for shot {shot_id}: {shot_role}.

Image roles:
- Garment images are the source of truth for the exact clothing design, color, fabric, construction, length, and proportions.
- Model head image is the source of truth for identity, hairstyle, and face.
- Lily real-photo references define brand mood, pose language, styling boundary, lighting, and composition.
- Pose reference defines body posture only.

Priorities:
1. Preserve model identity from the head reference.
2. Preserve garment facts from garment references.
3. Follow Lily brand-style references for pose, styling, shoes, accessories, background, and composition.
4. Follow this shot's framing requirement.

Shot:
{framing / angle / product focus}

Optional numeric constraints:
{model measurements / garment measurements / target wearing length if provided}

Do not:
Create a different face, change garment design, change garment length/proportion, add unreferenced decorative items, generate styling outside Lily brand references, add text or watermark.
```

## 6. 反例：不推荐的冗余文本

不推荐：

```text
black sleeveless A-line dress, round neckline, blue striped ruffle, short skirt, soft fabric, fashionable, modern, high quality...
```

原因：

- 容易把图片里的具体比例变成抽象语义。
- `A-line` 可能放大裙摆。
- `short skirt` 可能不够精确。
- `fashionable` 可能引入不需要的造型变化。

推荐：

```text
Use the garment references as the exact source of truth. Preserve the garment's actual length, hem width, ruffle exposure, fabric, and construction.
```

如果有尺寸，再补：

```text
The garment length on the selected model should match: black body ends at {x} cm above knee; striped ruffle exposure is {y} cm.
```

## 7. 产品功能映射

产品上应把输入拆成以下模块：

- `服装事实图组`
- `模特身份图组`
- `Lily 品牌调性参考图组`
- `Pose 参考图`
- `可选尺寸参数`
- `单张镜头任务`
- `禁止项`

前台用户看到的是结构化配置，不是长 prompt。

底层 prompt 由系统自动合成，而且尽量短。

## 8. 与前置推理/策划层的关系

Image 2 的上游应该有一个 `组图策划器`。它可以由推理模型、视觉模型、规则引擎和模板检索共同组成。

组图策划器负责：

- 从服装图包生成商品事实卡。
- 判断品类和高风险点，例如裙长、裤长、下摆宽度、细节易丢失。
- 从 Lily 品牌模板库中选择同品类、同图位、同季节感的真实参考图。
- 选择鞋包和配饰的风格边界，而不是让 Image 2 随机选择。
- 为每张图绑定 Pose 模板、头部角度、镜头范围和构图规则。
- 生成每张图的短文本任务。

Image 2 只接收策划器组装好的 `单张生成任务包`：

```json
{
  "shotId": "02",
  "shotRole": "侧身全身",
  "imageInputs": [
    "garment_front",
    "garment_back",
    "garment_detail",
    "model_head_left45",
    "lily_dress_style_refs",
    "pose_dress_side_walk_01"
  ],
  "constraints": {
    "identity": "preserve Face-01",
    "garment": "preserve exact garment facts from references",
    "styling": "within Lily dress template",
    "proportion": "use measurement constraints if provided"
  },
  "prompt": "Generate one Lily e-commerce side full-body photo. Use images as source of truth. Keep styling within Lily references. Do not change face, garment design, garment proportions, or add text."
}
```
