---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/disclosure/invention/disclosure_builder.md（§7.4）
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: §7.4 系统框图与流程图要点逐字保留；mermaid_render.py/Playwright 宿主工具括注改为「由平台服务端渲染为 PNG」，「脚本补救」改为「平台补救」表述；末尾新增平台渲染校验说明。发明 G2/G3 生成调用注入。
---

# 系统框图与流程图要点（§7.4）

- **系统框图与流程图**均仅用 **fenced mermaid**（由平台服务端渲染为 PNG）；Word 中以 PNG 为准，**无需**再附 ASCII 文字框图
- mermaid 内节点标签用**场景短词**（中文/数字），避免 ①②③；**用标题里的领域对象**，不要换成更空的上位词。可另写简短「流程说明」段落概括各步，**不得**用 ASCII 框线箭头代替图示。实现细节（校验值、序号、租约字段等）放正文，不要堆进节点标签。
- **步骤号必须写进可见标签**。节点 id（`S1`）**不会**出现在 PNG 上。正确：`S1["S1 采集节点指标"]`、`S5{"S5 是否达阈"}`。错误：`S1[采集节点指标]`（出图只剩中文、序号丢失）。定稿渲染时平台可把缺失序号补进标签再出图；成文时仍须按正确写法写，勿依赖补救。

平台会对每个 mermaid 围栏做渲染校验；渲染失败将带错误信息要求修复，仍失败则降级为编号文字清单并标注「图待补」。
