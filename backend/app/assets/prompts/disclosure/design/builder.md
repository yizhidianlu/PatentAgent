---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/disclosure/design/disclosure_builder.md
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 7.1 建议结构、7.2 文头、7.3 写作硬性要求逐字保留（image_gen 线稿生成改为用户上传；查新脚本参数改为平台执行）；7.4 命名与交付改为平台职责；7.5 自检节删除（由 self_check_design.md 独立阶段执行）。
---

# 外观设计 · 交底/设计说明成文（Step 7）

**本文件仅用于外观设计。** 发明 / 实用新型由平台路由至对应 builder。

脱敏、禁止仓库脚注等公共纪律以随本调用注入的公共规则为准；命名时间戳与落盘由平台负责。  
成文前平台已注入本案 AppearanceSchema 与 figure_plan（填表阶段产出）。

外观文件实务格式因代理所而异；平台交付 **Markdown + 同名 Word**。视图说明须同时嵌入**干净实拍**与**线稿**（两套文档一致；勿漏实拍或漏线稿）。

## 7.1 建议结构

```
1. 注意事项（代理人可读、公开充分）
2. 一、产品名称与用途
3. 二、设计要点（形状 / 图案 / 色彩或其结合；对齐 schema.design_points）
4. 三、视图说明（按 `product_form` + `claimed_faces` 的正投影/立体图；对齐 schema.views + **仅嵌 figure_plan 入文图**；省略面写入简要说明）
5. 四、与在先外观的主要差异（查新后写；禁止无依据贬低）
6. 五、其它（可选：使用状态参考图说明；勿写内部结构）
```

## 7.2 文头

```markdown
# 外观设计说明（交底底稿）

**产品名称**：[待填写]

**技术联系人**：
- 姓名：[待填写]
- 电话：[待填写]
- 邮箱：[待填写]

**专利类型**：外观设计
```

## 7.3 写作硬性要求

- 只写**看得见的造型/图案/色彩**，不写内部电路、卡扣受力、工艺步骤。  
- 视图与 `claimed_faces` 对齐；故意不交的面用 `omitted_views` 在简要说明写清（相同/对称/无要点），**不要**默认补六视。要点落面缺源图才写入 `uncertain`。正文「见图 N」**只引用** `figure_plan` 中 `use_in_disclosure: true` 的条目（按 `fig`），勿临场扫全目录。  
- **多视联读**：立体/正交/局部之间用 `relates_to`（`same_state` / `alternate_view` / `detail_of`）；正文说明须与之一致，跨图造型特征勿互相矛盾。  
- **入文同时嵌干净实拍与合格线稿**（`photo_clean` + `kind: lineart`）。实拍不得标成线稿；CAD 禁止入文。交底对照用，**不**把交底视图集写成「申请必须六视」。  
- **线稿（必做）**：成文前已由用户上传或从材料中选用合格线稿（v1 不做线稿生成）。  
- **交付**：`.md` 与同名 `.docx` 均须含上述实拍与线稿。  
- 查新：类型为外观设计（design），由平台执行；每条在先外观须可核验来源。  
- `not_design_signals` 非空时由平台反问是否改实用新型/发明。  
- **禁止**交付正文末尾追加技能仓库 / `examples/` /「虚构教学」脚注。

## 7.4 命名与交付（平台负责）

- 主文件名、时间戳与落盘由平台负责（规则同发明：去占位、去非法字符、≤80 字、凡交付必时间戳、勿覆盖旧交付）。
- 配图由平台按 `figure_plan.path` 归集嵌入；实拍与线稿都入文。
- 同名 `.docx` 由平台从 md 生成（与 md 同一套入文图）。

## 自检

自检由平台在组装后阶段执行（self_check_design.md：通用项 + §8.5 外观设计专项），不在本调用内进行，也不得写入正文。

表例见同目录 **`template_reference.md`**。
