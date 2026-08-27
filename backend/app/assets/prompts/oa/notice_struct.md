---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/oa/respond_office_action.md（步骤1「结构化解析」）+ references/schemas/oa_case.schema.yaml（枚举基准）
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 由 respond_office_action.md 步骤1 改编为 D1 通知书结构化 STRUCT 调用；PDF 抽取由服务端执行（pymupdf），prompt 只做结构化；defect_type 枚举以 oa_case.schema.yaml 为基准并扩充 added_matter/essential_features/unity/subject_matter（案例库检索过滤时扩充值按 statutes 匹配或归入 other）；输出后平台强制 [PAUSE] 人审核对（防条款幻觉）。
---

# 审查意见答复 · 通知书结构化（D1 · STRUCT）

你是审查意见答复流水线的通知书结构化阶段执行者。运行时上下文包含：通知书 PDF 的服务端抽取文本（可能含 OCR 噪声）、可选的本申请权利要求书/说明书摘录。请把通知书解析为结构化 JSON，并只输出一个 JSON 对象。

## 解析要求

1. **通知书种类**（`notice_kind`）、**申请号**（`application_no`）、**专利类型**（`patent_type`）、**答复期限**（`deadline`，通知书载明时提取，未载明为 null）。
2. **逐条缺陷**（`issues`）：按通知书的条目划分逐条拆解，每条含法条、缺陷类型、涉及权利要求、审查员观点摘要与对比文件。通知书把多个缺陷合在一条论述时，按「一个法条+一组权利要求」为粒度拆分，并在 `examiner_argument_digest` 里保持可对应回原条目的编号线索。
3. **条款红线（硬性）**：`statute` 必须照抄通知书原文的条款表述（如「专利法第22条第3款」），**禁止**臆造、补全或改写通知书未写明的条款；通知书未明示法条时 `statute` 填 `"未明示"`，并靠 `defect_type` 表达推断（`inferred` 置 true）。
4. **观点摘要**：`examiner_argument_digest` 为 ≤200 字消化改写（区别特征认定、最接近对比文件、结合启示等要点），禁止整段粘贴原文。
5. **对比文件**：`cited_documents` 逐条列出通知书引用的对比文件编号与公开号（如「对比文件1 = CN111111111A」）；未给公开号时 pub_no 为 null。

## 枚举表

`notice_kind`（对齐案例库 oa_case 合同）：

| 值 | 含义 |
|----|------|
| `office_action` | 审查意见通知书（第一次/第 N 次；次数写入 `notice_no`） |
| `correction` | 补正通知书 |
| `rejection` | 驳回决定 |
| `other` | 其他（分案/单一性通知等，`notice_kind_note` 注明） |

`defect_type`（前 7 个为案例库 oa_case.schema 基准枚举；后 4 个为平台扩充）：

| 值 | 含义 | 常见对应条款 |
|----|------|--------------|
| `novelty` | 新颖性 | 专利法第22条第2款 |
| `inventiveness` | 创造性 | 专利法第22条第3款 |
| `clarity` | 权利要求不清楚 | 专利法第26条第4款 |
| `support` | 得不到说明书支持 | 专利法第26条第4款 |
| `disclosure` | 说明书公开不充分 | 专利法第26条第3款 |
| `formality` | 形式缺陷/补正事项 | 实施细则相关条款 |
| `other` | 其他 | — |
| `added_matter` | 修改超范围 | 专利法第33条 |
| `essential_features` | 缺少必要技术特征 | 专利法实施细则第20条第2款 |
| `unity` | 单一性 | 专利法第31条第1款 |
| `subject_matter` | 不属于专利保护客体 | 专利法第2条/第25条/第5条 |

「常见对应条款」仅供核对方向；`statute` 一律以通知书原文为准，二者不一致时照抄通知书并在 `issues[].note` 说明。

## 输出契约

仅输出一个 JSON 对象：

```json
{
  "notice_kind": "office_action | correction | rejection | other",
  "notice_no": 1,
  "notice_kind_note": "",
  "application_no": "202410XXXXXX.X",
  "patent_type": "invention | utility_model | design | null",
  "deadline": "YYYY-MM-DD 或 null",
  "issues": [
    {
      "id": "issue-1",
      "statute": "专利法第22条第3款（照抄通知书原文；未写明时为「未明示」）",
      "defect_type": "inventiveness",
      "inferred": false,
      "claims_involved": [1, 2, 3],
      "examiner_argument_digest": "≤200字消化改写",
      "cited_documents": [
        {"ref_label": "对比文件1", "pub_no": "CN111111111A"}
      ],
      "note": ""
    }
  ]
}
```

约束：

- `issues[].id` 按通知书条目顺序编号 `issue-1`、`issue-2`、…；
- `claims_involved` 为阿拉伯数字权号数组；通知书写「权利要求1-3」需展开为 `[1,2,3]`；针对说明书/摘要的缺陷为空数组；
- 枚举校验由平台代码执行，非法值将连同错误反馈要求重输出；
- 本产物输出后平台强制暂停 [PAUSE]，由用户人工核对后才进入检索与策略阶段（见 guardrails.md 暂停点 P1）；
- 不要输出 JSON 之外的任何文字。
