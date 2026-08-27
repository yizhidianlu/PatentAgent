# 来源与许可声明（NOTICE）

「引途医疗专利智能体」平台的专利写作规则、模板、流程设计与部分工具脚本，**移植自以下两个开源项目**，
并非本项目原创。特此声明来源、许可与逐项处置方式，并向原作者致谢。

---

## 1. patent-disclosure-skill（中国专利.skill）

| 项 | 内容 |
|---|---|
| 来源仓库 | https://github.com/handsomestWei/patent-disclosure-skill |
| 作者 | handsomestWei |
| 版权 | Copyright (c) 2026 handsomestWei |
| 许可 | MIT License |
| 许可全文 | `backend/app/assets/vendor/patent-disclosure-skill/LICENSE` |
| 原文快照 | `backend/app/assets/vendor/patent-disclosure-skill/` |

### 本项目使用了什么

- 专利交底书八步流程与三类型（发明 / 实用新型 / 外观设计）撰写规则；
- 自检清单（§8.1–8.5）、迭代规范（合并 / 纠正）、版本纪律与命名规则（§7.3）；
- 脱敏规则（§7.5）、术语与标题贯穿规则（§7.9）、mermaid 规范（§7.4）、公式书式（§7.7）、
  权利要求倾向（§7.6）；
- 公式范式库 `references/formulas/paradigms.yaml`（逐字保留）；
- 九份 YAML schema 合同 `references/schemas/*.schema.yaml`（逐字保留，另转写为 pydantic 模型）；
- `patent_type_search.yaml`、`ipc_application_hints.yaml`、`patent_pdf_sources.yaml`（逐字保留）；
- 专利解读 11 节报告模板与「说明书 0002」引用纪律；
- 审查意见答复流程、案例笔记模板与 frontmatter 契约；
- Python 工具脚本：`md_to_docx.py`、`mermaid_render.py`、`math_to_omml.py`、`math_render.py`、
  `docx_to_md.py`、`pptx_to_md.py`、`browser.py`、`patent_type.py`、
  `cnipa_epub_search.py`、`cnipa_epub_crawler.py`、`cnipa_epub_parse.py`、
  `check_formula_plan.py` 等（保留原 CLI 接口与 stdout 机读协议）。

### 处置方式

- **逐字保留（verbatim）** 的资产放在 `backend/app/assets/references/` 与
  `backend/app/assets/vendor/`；
- **改编（adapted）** 的运行时 prompt 放在 `backend/app/assets/prompts/`，
  每个文件头部的 YAML front-matter 都记录了 `source_repo` / `source_path` / `source_url` /
  `treatment` / `ported_version` / `notes`；
- 改编只剥离宿主 Agent 指令（`Read xxx.md`、`运行 xxx.py`、落盘路径、allowed-tools），
  **写作规则正文逐字保留**，相应行为改由服务端代码承担；
- 逐条移植清单见各模块的资产清单：
  `backend/app/assets/prompts/{disclosure,reader,oa}/manifest.yaml`。

---

## 2. paper2patent

| 项 | 内容 |
|---|---|
| 来源仓库 | **https://github.com/7toCR/paper2patent** |
| 作者 | **7toCR** |
| 版权 | Copyright (c) 2026-present [7toCR]，Prompt 模板内容版权归原作者所有 |
| 许可 | MIT License **+ 社区使用条款 CLAUSE.md** |
| 许可全文 | `backend/app/assets/vendor/paper2patent/LICENSE` |
| 社区条款全文 | `backend/app/assets/vendor/paper2patent/CLAUSE.md` |
| 原文快照 | `backend/app/assets/vendor/paper2patent/` |

### ⚠️ 依 CLAUSE.md 要求的显著署名

> **本平台「论文转专利」模块的 Prompt 模板、规则文档、JSON 内容契约与附图 / DOCX / PDF
> 生成脚本，全部来源于开源项目 [7toCR/paper2patent](https://github.com/7toCR/paper2patent)，
> 由 7toCR 原创，非本项目原创成果。**

### 本项目使用了什么

- README 内的 **Pro Prompt**（约 12000 字）—— 逐字拆为 9 份
  （role_task / part1_abstract / part2_abstract_fig / part3_claims / part4_description /
  part5_drawings / critical_rules / content_constraints + writing_methods / final_format），
  其中 12 条 Critical Rules、禁用词全表、四阶段执行流**一字未改**；
- README 内的 **Flash Prompt**（快速模式单调用）—— 逐字保留；
- README 内的 **Gemini 附图 Prompt** —— 逐字保留，用于导出给用户做图像模型精修；
- `references/` 六份规则文档：`input-requirements.md`、`text-conversion-workflow.md`、
  `claims-and-specification-rules.md`、`drawing-generation.md`、`document-generation.md`、
  `quality-checklist.md`；
- `document-generation.md` 定义的 JSON 内容契约（转写为 pydantic / JSON Schema，字段语义未改）；
- 三个脚本：`generate_patent_drawings.py`、`generate_patent_docx.py`、`export_patent_pdf.py`。

逐条移植清单见 `backend/app/assets/prompts/paper2patent/manifest.yaml`。

### CLAUSE.md 合规自查

CLAUSE.md 第三节「引用规范」要求 GitHub 仓库场景下**在 README 中添加链接指向原仓库**。
本项目的落实情况：

| CLAUSE.md 要求 | 本项目的落实位置 |
|---|---|
| README 中添加指向原仓库的链接 | [`README.md`](README.md) 「一、能做什么」表格下方、「四、四个模块的输入与产出 · 论文转专利」、「八、来源与许可」共三处，均为可点链接 |
| 不得直接复制并声称原创 | 本文件、README「八、来源与许可」、`prompts/paper2patent/manifest.yaml` 与每个 prompt 文件的 front-matter 均**明确声明非本项目原创** |
| 不得在商业牟利时不注明来源 | 署名固定写在产品仓库的 README 与 NOTICE 中，随代码分发 |
| 不得冒充作者 | 本项目从未以 7toCR 名义发布任何内容 |

同时保留了原仓库的 `LICENSE` 与 `CLAUSE.md` 全文快照，未做任何删改。

---

## 3. 其他第三方组件

- **mermaid.js**（MIT）—— `backend/app/tools/vendor/mermaid.min.js`，用于服务端离线渲染流程图。
- 后端 Python 依赖与前端 npm 依赖各自遵循其原始许可，清单见
  `backend/pyproject.toml` 与 `frontend/package.json`。

---

## 4. 视觉风格

界面视觉风格参考 https://www.fojiaoai.cn/dashboard/ 的**公开页面样式**（布局、配色、组件风格），
全部代码为本项目独立实现，**未复制其任何代码、图片、字体或品牌资产**。

---

## 5. 本项目自身

本项目在上述移植内容之外的部分（FastAPI 后端架构、流水线引擎、SSE 推送、
React 前端、数据模型与 API 契约）为本项目原创实现。

---

**免责声明**：本平台产出的所有内容均由大语言模型生成，可能存在事实错误、法条引用错误或表述缺陷。
请务必由具备资质的专利代理师或知识产权专业人员审核后再使用；本项目不构成法律意见。
