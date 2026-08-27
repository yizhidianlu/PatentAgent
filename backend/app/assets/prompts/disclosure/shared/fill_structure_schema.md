---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/shared/fill_structure_schema.md + references/schemas/structure.schema.yaml
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 识图纪律（跨图联读/先填表后成文/打分判定/uncertain 规则）保留；落盘目录、Write/Read、CAD/STEP、image_gen 线稿与 reader 模式删除（v1 交底专用；线稿由用户上传；无识图能力位时平台转用户填表兜底）；输出改为对齐 structure.schema.yaml 字段的 JSON 契约（YAML 字段骨架随文嵌入）。
---

# StructureSchema 填写（A3b · STRUCT 调用，识图）

## 任务

平台已注入本案结构类图片（照片、CAD 截图、爆炸图、专利附图等，多模态）与相关材料摘要。你的任务：把图与材料中的**结构事实**填入 StructureSchema。仅输出一个 JSON 围栏块，字段与文末 YAML 骨架同名同结构。

## 识图纪律

- **跨图联读**：总装 / 爆炸 / 局部须对照**同一套件号**；先建立「图角色」再填 `parts`（**禁止**每张图各起一套命名）。
- **先填 schema，再写交底**；禁止看图直接长文。
- 对每张图判定其角色（总装/局部/剖视/爆炸/场景）与质量（清晰度、遮挡、是否能看清连接），作为取舍依据；打分明细（relevance/quality/score）由后续 figure_plan 阶段输出，本调用只填结构事实。
- 关键 `relations[].seen_in` 可列出能看见该连接的图（用图片文件名或序号）。
- `uncertain` 不得写成确定保护点；**跨图对不上的（比例、件号、连接方式矛盾）写入 `uncertain`**。
- 材料更像纯算法 / 纯外观美感 / 方法步骤为主时，填入 `not_utility_model_signals`（平台将反问是否改类型），勿硬凑结构事实。
- `material_hint` 看不出则写 `unknown`；schema 未见的尺寸公差**勿杜撰**。

## 字段骨架（与 references/schemas/structure.schema.yaml 一致）

```yaml
$schema: structure.schema
version: 1
mode: disclosure  # disclosure | reader
source_images: []  # 看过的图（路径或说明）；交底入文以 figure_plan 为准
parts:
  - id: "1"
    name: ""           # 部件名
    shape: ""          # 板状/杆状/壳体/鳍片阵列等
    material_hint: ""  # 可空；unknown 则写 unknown
relations:
  - from: "1"
    to: "2"
    type: ""           # 卡扣|螺栓|铰接|焊接|一体成型|过盈|粘接|其他
    where: ""          # 位置
    seen_in: []        # 可选；figure_plan 的 fig 号，表示哪些图能看见该连接
spatial: []            # 上下/内外/同轴/阵列等短句
function_of_structure: []  # 该结构解决什么问题（非纯方法步骤）
delta_hypothesis: []   # 相对常见结构可能新在哪
uncertain: []          # 看不清 / 说明书未写明
not_utility_model_signals: []  # 若更像方法/外观，列于此供分流
```

必填：`parts`（至少 1；或在 `uncertain` 说明无法识出部件）、`relations`（或显式 `[]` + 说明）、`spatial`、`uncertain`（可为 `[]`）。

## 输出契约

仅输出一个 ```json 围栏块，字段与上述骨架同名同结构：`mode` 固定 `disclosure`；`source_images` 列出本次看过的图（文件名）；`relations[].type` 取骨架注释中的枚举（卡扣|螺栓|铰接|焊接|一体成型|过盈|粘接|其他）。

（无识图能力的模型配置下，平台会跳过本调用、转用户填表兜底。）
