---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/shared/fill_appearance_schema.md + references/schemas/appearance.schema.yaml
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 「先判再收图（勿默认六视）」「正投影怎么定」与跨图联读/uncertain 规则保留（正投影五步取自 appearance.schema.yaml，逐字）；落盘、Write/Read、STEP、image_gen 线稿与 reader 模式删除（v1 交底专用；线稿由用户上传；无识图能力位时平台转用户填表兜底）；输出改为对齐 appearance.schema.yaml 字段的 JSON 契约（YAML 字段骨架随文嵌入）。
---

# AppearanceSchema 填写（A3b · STRUCT 调用，识图）

## 任务

平台已注入本案产品外观图片（实拍、效果图、正投影、立体图等，多模态）与相关材料摘要。你的任务：把图与材料中的**外观事实**填入 AppearanceSchema。仅输出一个 JSON 围栏块，字段与文末 YAML 骨架同名同结构。

## 识图纪律

- **先判再收图（勿默认六视）**：先判 `product_form`（`solid` 立体产品 / `planar` 平面产品）；按设计要点列出 `claimed_faces`，正投影 = 要点落面；立体宜加立体图。**仅要点涉及六个面**才收齐六面。平面产品一面或两面即可。
- 相同、对称或无要点的面写入 `omitted_views`（供简要说明），不为这些面凑图。
- **跨图联读**：多视视为同一产品；比例、开口、装饰位置须一致；**矛盾写入 `uncertain`**。
- 区分「整体造型」与「装饰图案/色彩」；色彩不明则入 `uncertain`。
- 要点落面缺源图 → `uncertain`（与故意省略分开，禁止写成「缺正式六视」）。
- 材料更像内部构造、电路、受力分析、工艺步骤时，填入 `not_design_signals`（平台将反问是否改类型），勿写成外观要点。
- `views[].source_image` 填对应图片文件名；`contrast_to_prior` 中的假设须标注为假设。

## 字段骨架（与 references/schemas/appearance.schema.yaml 一致）

```yaml
$schema: appearance.schema
version: 1
mode: disclosure  # disclosure | reader
source_images: []          # 看过的图；交底入文以 figure_plan 为准
product_name: ""
overall_shape: ""      # 整体造型一句话
product_form: solid    # solid 立体产品 | planar 平面产品；先判此项再选正投影
claimed_faces: []      # 设计要点落面。立体：主视/后视/左视/右视/俯视/仰视；平面：正面/背面
omitted_views:         # 故意不交的正投影（相同/对称/无要点），供简要说明；不是缺材料
  - name: ""           # 如 仰视
    reason: ""         # 如 底面无设计要点
views:                 # 已见且拟用的视图（含立体图）；勿为 omitted 面凑条目
  - name: 立体图       # 主视/俯视/左视/立体等
    notes: ""
    source_image: ""   # 可选；对应材料图路径
    # 视图间关联优先写在 figure_plan.relates_to（same_state / alternate_view）
ornament: []           # 图案、线条、纹理
color: []              # 色彩搭配；不明则 uncertain
design_points: []      # 欲强调的外观要点
contrast_to_prior: []  # 与常见外观差异（假设须标）
uncertain: []          # 材料看不清，或要点落面缺少可用源图（非故意省略）
not_design_signals: [] # 若更像结构功能/方法，列于此
```


## 正投影怎么定（交底选图；非正式申请包）

1. 先判 `product_form`。  
2. 按 `design_points` 列出 `claimed_faces`。  
3. 正投影清单 = 要点落面；立体产品宜加立体图。**仅当要点涉及六个面**才收齐六面正投影。平面产品一面或两面即可。  
4. 相同、对称或看不到要点的面写入 `omitted_views`（成文简要说明照抄），**不要**标成缺六视、**不要**为这些面生线稿。  
5. 要点落面没有源图 → `uncertain`，与故意省略分开。


必填：`overall_shape`、`views`（可空数组但须在 `uncertain` 说明缺图）、`uncertain`；交底模式同时写 `product_form`、`claimed_faces`（可空则须在 `omitted_views` 或 `uncertain` 说明）。

## 输出契约

仅输出一个 ```json 围栏块，字段与上述骨架同名同结构：`mode` 固定 `disclosure`；`source_images` 列出本次看过的图（文件名）。

（无识图能力的模型配置下，平台会跳过本调用、转用户填表兜底。）
