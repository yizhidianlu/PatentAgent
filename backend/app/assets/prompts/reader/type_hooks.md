---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/reader/type_hooks.md
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 由 type_hooks.md 改编：判定顺序与种类码规则保留；patent_type.py / fetch_patent_pdf.py 脚本判定改为服务端主路径，本 prompt 仅作服务端无法判定时的 LLM 兜底；实用/外观的 Schema 填表挂钩改为报告增设「结构说明」「外观要点」节的说明。
---

# 专利解读 · 专利类型判定兜底（C0 辅助 · STRUCT）

本调用仅在服务端无法确定专利类型时使用（无公开号、种类码缺失/异常、扫描件无著录页等）。服务端能判定时不发起本调用。

## 判定顺序（沿用源 skill，优先自动）

1. **用户显式声明**（发明 / 实用新型 / 外观设计）——最高优先级，覆盖自动结果；
2. **公开号文献种类码**（由服务端代码判定，此处作为你的判据之一）：`CN…A/B` → 发明，`CN…U/Y` → 实用新型，`CN…S` → 外观设计；
3. **扉页/著录关键词**：文本中出现「实用新型」「外观设计」「发明专利」等字样；
4. **文本特征兜底**：无以上依据时，从权利要求书体式推断——「一种…方法」类方法权利要求仅见于发明；仅有产品形状/构造特征的多为实用新型；仅视图说明、无权利要求书的为外观设计。

## 输出契约

仅输出一个 JSON 对象：

```json
{
  "patent_type": "invention | utility_model | design | unknown",
  "basis": "user_declared | kind_code | title_page_keyword | text_inference",
  "evidence": "判定依据的原文摘录或说明（≤60字）",
  "confidence": "高 | 中 | 低"
}
```

- 依据不足时如实输出 `"unknown"`，由平台暂停请用户指定；禁止臆断。
- 不要输出 JSON 之外的任何文字。

## 判型对报告结构的影响（由平台与撰写调用执行，此处仅供理解）

| 类型 | 报告增设 |
|------|----------|
| 实用新型 | 增设「结构说明」节：部件表与连接关系表（连接关系「自/至」须为「件号 + 名称」；不得出现 Schema 合同名脚注） |
| 外观设计 | 增设「外观要点」节：视图要点与设计要部（同样无 Schema 脚注） |
| 发明 | 默认 11 节叙事；装置附图多时可选增设「结构说明」节 |

- 解读模式**不强制**附图规划（figure_plan 仅交底书模式必写）；
- 解读模式**不**套用交底书的成文模板与章节结构。
