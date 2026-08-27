---
source_repo: handsomestWei/patent-disclosure-skill
source_path: assets/patent_note_template.md
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 11 节结构逐字保留（见下「模板正文」）；Obsidian frontmatter 改为报告 meta JSON；Obsidian 导航区删除；callout 语法映射为站内 block 类型；模板内 wikilink 示例（[[clues/…]] 等）在站内不生效，线索区块由平台按 grounding 产物装配。
---

# 专利解读 · 站内报告模板（11 节结构）

本文件供报告撰写各调用（report_writer.md 的三次调用）对齐节结构与节内格式。报告最终由平台装配为 `report.json`（meta + sections[blocks]）在站内渲染，并可导出 markdown。

## 报告 meta JSON（替代 Obsidian frontmatter）

原模板的 Obsidian frontmatter 字段映射为报告 meta（由平台在 C0-C1 阶段填充，撰写调用只读）：

```json
{
  "pub_number": "CN…",
  "title": "发明名称",
  "patent_type": "invention | utility_model | design",
  "domain": "领域短标签（原 frontmatter: domain）",
  "ipc": "主 IPC（原 frontmatter: ipc）",
  "assignees": ["申请人（原 frontmatter: assignees）"],
  "read_date": "YYYY-MM-DD（原 frontmatter: read_date）",
  "perspective": "入门 | 研发 | 规避（原 frontmatter: perspective）",
  "evidence_scope": "full_text | abstract_only | partial（原 frontmatter: evidence_scope）",
  "confidence_speculative": false
}
```

弃用的 Obsidian 专属字段：`tags`、`aliases`、`cssclasses`（站内渲染不需要）。

## callout → 站内 block 类型映射

模板正文中的 Obsidian callout 语法在站内报告中落为对应 block 类型（平台 C6 装配时转换；撰写时保留 callout 记号即可）：

| 模板记号 | 站内 block 类型 | 说明 |
|---|---|---|
| `> [!patent-meta]` | `callout-patent-meta` | 著录项卡片 |
| `> [!patent-claim]` | `callout-patent-claim` | 独立权利要求精读卡片 |
| `> [!grounding]` | `callout-grounding` | 应用场景/落地线索卡片 |
| `> [!warning]-` | `callout-warning` | 警示卡片（默认折叠，用于推测性内容） |
| `> [!figure]` | `callout-figure` | 附图占位卡片 |
| markdown 表格 | `table` | 原样 |
| 第三节树形表 | `claim_tree` | 由平台按 claim_tree + claim_deltas 生成 |
| mermaid 围栏 | `mermaid` | 服务端渲染校验 |
| 其余正文 | `markdown` | 原样 |

## 模板正文（11 节结构逐字保留；「写作提示 · 勿写入交付稿」仅供撰写参考，不得留在正文）

# 专利解读：{{发明名称或公开号}}

> [!patent-meta] 著录项
> - **公开号**：{{CN…}}
> - **领域**：{{domain}}
> - **IPC**：{{ipc}}
> - **申请人**：{{assignees}}
> - **视角**：{{入门|研发|规避}}
> - **证据范围**：{{全文|仅摘要|部分}}
> - **运行 ID**：{{RUN}}

## 一、一句话

（写作提示 · 勿写入交付稿：≤3 句，解决什么问题、核心手段、对读者意味着什么。）

## 二、连贯叙事

（写作提示 · 勿写入交付稿：按「问题 → 思路 → 怎么做 → 效果」讲清，避免权项编号堆砌。）

## 三、权利要求树

（写作提示 · 勿写入交付稿：入库由 `claim_tree.json` 生成**单一树形表**「结构 | 权 | 本项新增」。不要同时再贴一份 mermaid 主视图。）

> 共 N 项 · 独立 x / 从属 y。独立权展开见第四节。

| 结构 | 权 | 本项新增 |
| --- | ---: | --- |
| `◆` | 1 | （短句） |
| `├─` | 2 | （短句） |
| `└─` | 3 | （短句） |

## 四、独立权利要求精读

> [!patent-claim] 权利要求 {{N}}

> 【{{公开号}}·权利要求{{N}}】{{原文逐字片段}}

| 特征 | 大白话 | 说明书依据 |
|------|--------|------------|
| F1 | | 说明书 0006 / 说明书 0058–0061 |

## 五、专利内术语表

| 术语 | 本文含义/位置 | 备注 |
|------|---------------|------|
| | | 来自说明书定义句 / 一般理解 |

## 六、特征—说明书—附图对照

| 特征 | 说明书位置 | 附图 |
|------|------------|------|
| | | 图 N |

（写作提示 · 勿写入交付稿：有精修图用 `![[images/…]]`；扫描件由入库脚本追加「附图」整页预览。）

## 七、和现有技术的差别

## 八、阅读建议

（写作提示 · 勿写入交付稿：3–5 条可执行建议；规避视角可含「删/换/绕」初判方向。）

## 九、技术应用场景

> [!grounding] 应用场景
>
> | 场景/模块 | 大白话 | 专利内依据 |
> |-----------|--------|------------|
> | | | 说明书 0056 / 实施例… |

（写作提示 · 勿写入交付稿：不得含 URL 或 WebSearch 推测。）

## 十、附录：行业坐标与公开线索

### A. IPC 行业坐标

（写作提示 · 勿写入交付稿：行业坐标来自离线 IPC 词表；正文「来源」只写「离线 IPC 行业词表」等自然语言，禁止写脚本名、JSON 字段路径如 context_anchor.*。）

### B. 公开检索线索

> [!warning]- 公开检索线索
>
> 详情见 [[clues/_线索索引|线索文件夹]]（最多 3 条；摘要由 Agent 读取 URL 后写入）。
>
> - **线索**：[[clues/01-标题|标题]] — 置信度：中 — [来源](URL) — 理由：…
>
> 无可靠 URL 时写：「未发现可核验的公开对应，可能为防御性/储备专利。」

## 十一、免责声明

本解读仅供技术理解辅助，不构成法律意见；专利保护范围以官方法律文本为准。重大决策请咨询专利代理师/律师。

## 平台适配说明（撰写调用须知）

- 上方模板正文中 `{{…}}` 为占位符，由撰写调用按运行时上下文填充；「（写作提示 · 勿写入交付稿：…）」行仅供参考，交付正文必须删除。
- 第三节树形表由平台按已确认的 claim_tree + claim_deltas 装配（block 类型 `claim_tree`），撰写调用只写节导语（「共 N 项 · 独立 x / 从属 y。独立权展开见第四节。」）；有 ≥2 项独立权利要求时可另附 mermaid 视图，但不得与树形表双份主展示。
- 模板内的 Obsidian wikilink 写法（`[[Research/…]]`、`[[clues/…]]`、`![[images/…]]`）在站内不生效：术语不加链接、附图由平台以 `callout-figure`/图片 block 装配、第十节 B 的线索列表由平台按 grounding 产物渲染。
- 「说明书 0002」引用格式等风格纪律见 report_style.md（随撰写调用注入）。
- 第十一节免责声明文字照模板逐字输出，不得改写。
