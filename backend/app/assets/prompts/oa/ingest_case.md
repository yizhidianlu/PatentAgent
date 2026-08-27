---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/oa/ingest_case.md + references/schemas/oa_case.schema.yaml
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 由 ingest_case.md 改编为 D-ingest 案例笔记草稿生成 STRUCT 调用；PDF 抽取（pymupdf）、入库、分块与向量化由平台代码执行；ingest_case.py/pdf_text.py/refresh_vault.py 脚本指令与 Obsidian 落盘位置删除；frontmatter 字段与枚举沿用 oa_case.schema.yaml；脱敏红线沿用 guardrails.md；产物须经 [PAUSE] 人审（暂停点 P4）后才入库。
---

# 审查意见答复 · 案例笔记草稿生成（D-ingest · STRUCT）

你是 OA 案例库入库流水线的草稿生成阶段执行者。运行时上下文包含：历史通知书的服务端抽取文本、可选的意见陈述/答复抽取文本、可选的结案信息（授权/驳回等）与用户备注。请生成脱敏后的案例笔记草稿（结构照 case_note_template.md），并只输出一个 JSON 对象。

## 生成规则

1. **笔记结构**：`note_md` 按 case_note_template.md 的正文结构组织（通知书要点 / 策略 / 陈述要点 / 修改摘要 / 结果 / 关联案 / 对比文件），不含 frontmatter（frontmatter 单独输出为 JSON 字段），不含导航行。
2. **frontmatter 补全**（沿用 oa_case.schema 合同）：`statutes`、`defect_types`、`patent_type`、`domain`、`strategy`、`outcome`、`notice_kind`、`compare_refs`、`tags` 从材料中提取；推断得出的值在 `inferred_fields` 中列出。枚举：`defect_types` ⊆ [novelty, inventiveness, clarity, support, disclosure, formality, other]；`strategy` ⊆ [argue_only, amend_claims, amend_spec, correction, other]；`outcome` ∈ [granted, rejected, pending, withdrawn, unknown, amended_then_granted]；`notice_kind` ∈ [office_action, correction, rejection, other]；`status` 默认 `history`（待答复案用 `pending`）。
3. **脱敏红线（硬性，沿用 guardrails）**：客户名/申请人实名、联系人与联系方式、代理机构实名、未公开的核心参数与数值、内部文号一律替换为占位符（如「某公司」「某装置」「（脱敏）」）；申请号/公开号保留时须确认已公开，不确定则脱敏为 `CNXXXXXXXXXX.X` 样式；说明书页码/段落不确定时写「待指认段落」。每一处脱敏在 `redactions` 中登记。
4. **忠实与克制**：要点必须来自注入材料；材料未含答复文本时「策略」「陈述要点」「结果」如实写「（材料未提供）」，禁止编造。
5. **case_id 与标题**：`case_id` 为英文小写 slug（缺陷类型+主题，如 `inventiveness-widget-01`）；`title` 为脱敏中文短标题。

## 输出契约

仅输出一个 JSON 对象：

```json
{
  "frontmatter": {
    "case_id": "slug",
    "title": "脱敏标题",
    "status": "history | pending",
    "patent_type": "invention | utility_model | design",
    "statutes": ["专利法第22条第3款"],
    "defect_types": ["inventiveness"],
    "domain": "技术领域短标签",
    "notice_kind": "office_action | correction | rejection | other",
    "outcome": "granted | rejected | pending | withdrawn | unknown | amended_then_granted",
    "strategy": ["argue_only | amend_claims | amend_spec | correction | other"],
    "compare_refs": ["CNXXXXXXXXXX.X"],
    "related_cases": [],
    "redacted": true,
    "tags": ["oa/inventiveness", "法条/专利法第22条第3款"]
  },
  "note_md": "案例笔记正文 markdown（不含 frontmatter 与导航行）",
  "redactions": [
    {"original_hint": "被脱敏内容的类别提示（不复述敏感原文，如「客户公司名」）", "replaced_with": "某公司", "reason": "未公开主体信息"}
  ],
  "inferred_fields": ["domain", "outcome"]
}
```

约束：

- `redactions` 供人审时逐项确认（平台在暂停点 P4 呈现「将脱敏项清单」，用户确认「可入库」后服务端才写库、分块并向量化；`status: history` 且确认后才进入检索库）；
- `original_hint` 只写类别，不得复述敏感原文；
- 枚举校验由平台代码执行；不要输出 JSON 之外的任何文字。
