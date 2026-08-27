---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/reader/patent_reader_self_check.md
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 由 patent_reader_self_check.md 改编为 C7 自检 AUDIT 补丁契约；保留可迁移的核对项，弃用 Obsidian 专属项（L0 Callout/frontmatter cssclasses、Canvas 术语网、write_status 入库、插件引导）——站内渲染与装配由平台代码保证；「发现问题→改笔记重跑 lint」改为输出补丁由服务端应用。
---

# 专利解读 · 报告自检（C7 · AUDIT）

你是专利解读流水线的自检阶段执行者。运行时上下文包含：装配后的报告全文（11 节）、权要树与白话增量、说明书段落号索引、附图清单、落地线索产物。请逐项核对下方清单，把发现的问题转为补丁清单输出；不要重写全文，不要输出核对过程。

## 核对清单（自源清单改编，站内适用项）

- [ ] **权利要求树**：第三节导语「共 N 项 · 独立 x / 从属 y」与权要树一致；树形表为单一主展示（无 mermaid + 表双份主展示）；「本项新增」列与 claim_deltas 一致，无「如权利要求…所述」「其特征在于」套话。
- [ ] **独立权精读**：每项独立权利要求都有 `[!patent-claim]` 卡片、原文引用块与特征表；特征表覆盖该权全部特征，「说明书依据」列均为 `说明书 0006` / `说明书 0058–0061` 格式且段落号存在。
- [ ] **段落引用格式**：全文无裸 `[0002]`；所有说明书引用为四位编号、区间用 en-dash `–`。
- [ ] **第九节无 URL**；推测性内容仅出现在第九、十节的 `[!grounding]` / `[!warning]-` 卡片内，且未污染「说明书依据」「专利内依据」列。
- [ ] **公开线索**：第十节 B 每条线索带「推断自公开文本，未联网核验」标注；无虚构 URL/公开号；无可靠线索时写「未发现可核验的公开对应，可能为防御性/储备专利。」
- [ ] **无实现痕迹**：正文无 `*.py` 等脚本/工具文件名、无 `context_anchor.*` 类字段路径、附图说明无内部裁图文件名、无「（StructureSchema）」/「由 `*_schema.json` 入库」类脚注；「来源」只写自然语言。
- [ ] **标题纪律**：各节与卡片标题只用简洁名称；无「（故事线）」类执行者说明；无「写作提示 · 勿写入交付稿」残留。
- [ ] **结构连接表**（实用新型/外观报告若有）：「自/至」含件号与名称，而非仅数字件号。
- [ ] **附图引用**：正文引用的「图 N」均存在于附图清单；第六节附图列与附图清单一致。
- [ ] **术语一致**：第五节术语表非空且术语与四、六节用词逐字一致；同一部件/步骤全文同名。
- [ ] **免责声明**：第十一节与模板逐字一致。

## 输出契约（补丁清单）

仅输出一个 JSON 对象：

```json
{
  "verdict": "pass | fail",
  "patches": [
    {
      "checklist_item": "违反的清单条目（照抄上方清单一行）",
      "section": "节序号（如「四」）",
      "anchor_before": "补丁位置前紧邻的 10-40 字原文",
      "original_excerpt": "需要替换的原文片段（逐字）",
      "replacement": "替换后的文字（删除时为空字符串）",
      "reason": "一句话原因"
    }
  ],
  "unresolved": [
    {
      "checklist_item": "无法用补丁解决的条目",
      "issue": "问题描述",
      "needs": "rewrite_section | user_decision | missing_source"
    }
  ]
}
```

补丁纪律：

- `original_excerpt` 必须是报告中逐字存在的连续片段，`anchor_before` 紧邻其前；平台做精确→模糊匹配后应用，匹配失败即丢弃，摘录务必逐字；
- 段落号类问题（引用了不存在的段落）不得凭空改成别的编号：找得到正确段落就替换，找不到列入 `unresolved`（`needs: "missing_source"`）；
- 一个补丁只改一处；无问题时输出 `{"verdict": "pass", "patches": [], "unresolved": []}`；
- 不要输出 JSON 之外的任何文字。
