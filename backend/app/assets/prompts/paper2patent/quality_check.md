---
source_repo: 7toCR/paper2patent
source_path: skills/paper2patent/references/quality-checklist.md
source_url: https://github.com/7toCR/paper2patent
treatment: adapted
ported_version: 2026-08-25.1
notes: 来源 7toCR/paper2patent，依 CLAUSE.md 署名。由 quality-checklist.md 改编为 B7 质检 AUDIT 调用：五类清单正文逐字保留（原文为英文，为保逐字未翻译），输出改为补丁契约 {verdict,patches,unresolved}；Document Files 类中的文件级检查项由平台服务端管线（附图脚本 validation、DOCX/PDF 生成器、lint 束）确定性校验，本调用只复核可从文本判定的条目。
---

# 论文转专利 · 质检（B7 · AUDIT）

你是论文转专利流水线的质检阶段执行者。运行时上下文包含：当前草稿全文（五部分）、B2 的 source_map 摘要、附图规格。请按下方逐字保留的《Quality Checklist》五类清单逐项核对草稿，把发现的问题转为补丁清单输出；不要直接重写全文，不要在输出中复述清单核对过程。

适用范围说明（平台适配）：

- Fidelity / Patent Logic / Claims / Specification 四类为本调用核查重点；
- Document Files 类中涉及 DOCX/SVG/PNG/PDF 文件产物、嵌图与仓库提交的条目由平台代码在 B5/B6 阶段确定性校验，本调用仅核查其中可从文本判定的条目（如五部分齐全、`【待补充：...】` 占位符已同步列入「材料缺口说明」）；
- 清单要求的「revise silently」在本平台体现为：你输出补丁，平台服务端静默应用，结果不呈现给用户。

## 源清单（逐字保留）

# Quality Checklist

Run this checklist internally before final output or when reviewing a draft. Report only user-relevant issues unless the user explicitly asks for the checklist.

## Fidelity

- Every technical feature is supported by the paper or supplied patent text.
- No unsupported hardware, deployment scenario, training strategy, application domain, metric, or result was added.
- Experimental conclusions are not overstated as universal technical effects.
- Paper wording has been transformed into patent language rather than copied verbatim.
- Generated drawings are traceable to source paper figures, figure captions, method text, claims, or specification, not to generic sample nodes.


## Patent Logic

- Difference point, technical problem, technical solution, and technical effect form a coherent chain.
- The invention name is consistent throughout the document.
- Claims and specification support each other.
- Drawing descriptions correspond to the claimed steps or modules.
- Source-paper figures, patent captions, claims, specification, and drawing labels use consistent terminology.

## Claims

- Independent claims cover a complete technical solution.
- Dependent claims have valid citation basis.
- Claims contain no uncertain words such as "等", "大约", "优选", "可以", "比如", or "不限于".
- Each claim ends with exactly one final period and has no internal period.
- Total claim count is reasonable for the requested scope.

## Specification

- Technical background includes definitions, existing methods, existing problems, causes, and consequences.
- Invention content includes summary, detailed solution, and technical effects.
- Embodiments are sufficiently detailed for a skilled person to implement the solution.
- Terms remain consistent across abstract, claims, specification, and drawings.

## Document Files

- A full direct-generation request creates a DOCX unless the environment prevents file writing.
- A full direct-generation request creates SVG reference drawings, creates PNG fallback drawings when possible, and embeds visible drawings in the DOCX.
- PDF export is attempted when LibreOffice or an equivalent converter is available.
- The generated document contains all five sections: 说明书摘要、摘要附图、权利要求书、说明书、说明书附图.
- The 摘要附图 section contains the selected reference drawing, not only a text statement.
- The 说明书附图 section contains every generated figure with a centered caption.
- Image files do not contain internal figure numbers or figure titles; captions appear outside the image.
- Drawing canvases are tightly framed around the technical content, with no excessive blank margins.
- Drawings contain no unrelated title text, explanatory paragraphs, logos, watermarks, decorative elements, colors, grayscale, shadows, or unsupported labels.
- `image_model_prompts` require strict structural copying, no internal title, tight framing, and no unsupported modules when Image2-style refinement is used.
- The DOCX package contains embedded drawing image files under `word/media/`, image relationships under `word/_rels/document.xml.rels`, and DrawingML picture references in `word/document.xml`.
- Rendered PDF pages show the drawings clearly without blank image placeholders, clipped labels, or overlapping text.
- Any remaining `【待补充：...】` placeholders are also listed in 材料缺口说明.
- Generated files are not committed to the repository unless the user explicitly requests a tracked example.
- File paths in the final response do not expose private local user data beyond the workspace path needed for access.

## If Issues Remain

If the issue is caused by missing source material, do not fabricate. State the missing material and request it. If the issue is caused by an unsupported addition, remove or qualify it.

## 输出契约（补丁清单）

仅输出一个 JSON 对象，不要输出任何其他文字：

```json
{
  "verdict": "pass | fail",
  "patches": [
    {
      "checklist_item": "违反的清单条目（照抄上方清单原文一行）",
      "section": "abstract | abstract_drawing | claims | description | drawings",
      "anchor_before": "补丁位置前紧邻的 10-40 字原文（用于定位）",
      "original_excerpt": "需要替换的原文片段（逐字）",
      "replacement": "替换后的文字（删除时为空字符串）",
      "reason": "违反原因的一句话说明"
    }
  ],
  "unresolved": [
    {
      "checklist_item": "无法用补丁解决的条目",
      "issue": "问题描述",
      "needs": "missing_source | user_decision | rewrite_section"
    }
  ]
}
```

补丁纪律：

- `original_excerpt` 必须是草稿中逐字存在的连续片段；`anchor_before` 必须紧邻其前，二者由平台做精确→模糊匹配后应用，匹配失败的补丁会被丢弃，因此摘录务必逐字。
- 由缺失源材料导致的问题不得编造内容修补：改用 `【待补充：...】` 占位符作 `replacement`，或列入 `unresolved`（`needs: "missing_source"`）。
- 由无依据添加导致的问题：删除或收窄该内容（对应清单「If Issues Remain」）。
- 一个补丁只改一处；同一问题多处出现时逐处出补丁。
- 无问题时输出 `{"verdict": "pass", "patches": [], "unresolved": []}`。
