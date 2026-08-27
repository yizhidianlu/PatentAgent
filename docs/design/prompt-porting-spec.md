# 引途医疗专利智能体 — Prompt 移植与 Agent 编排层规格

> 由规划 Agent 产出，2026-08-25。适用范围：backend 的 LLM 流水线层。

## 0. 全局编排约定

### 0.1 调用分类（Call Class）

| Class | 用途 | temperature | 流式 | 输出契约 | 失败处理 |
|---|---|---|---|---|---|
| `STRUCT` | 结构化抽取/规划（JSON） | 0.1 | 否（缓冲） | JSON Schema 校验 | 错误反馈重试 ≤2 → 阶段失败挂起给用户 |
| `GEN` | 章节/文书生成（markdown） | 0.5 | 是（SSE 直推前端） | markdown + 可选尾部 ```json terms_delta``` 块（服务端剥离） | lint 失败 → REPAIR |
| `REPAIR` | 带错误反馈的修复调用 | 0.2 | 否 | 与被修复调用同契约 | ≤2 次 → 降级策略 |
| `AUDIT` | 自检/审校（补丁清单） | 0.2 | 否 | patch JSON（§3.3） | 补丁应用率<80% → 局部重写 |
| `CHAT` | 暂停点交互复述/答疑 | 0.6 | 是 | 自由 markdown | 无 |

JSON 调用优先使用 `response_format={"type":"json_object"}`（模型配置里加 `supports_json_mode`、`context_window` 能力位；不支持时改为「仅输出一个 ```json 围栏块」指令 + 服务端提取第一个合法 JSON）。

### 0.2 System Prompt 装配器（Prompt Assembler）

每次调用的 system prompt 由装配器按固定顺序拼接：

```
[1] common/system_base.md          # 平台角色头（重写）：你是专利文书生成引擎的一个阶段执行者，只完成本阶段任务
[2] <module role 文件>              # 该模块的 Role/Task（多为 verbatim 移植片段）
[3] <patent-type 规则文件>          # 仅模块A：invention/utility_model/design 的 builder 规则
[4] common/desensitization.md      # 脱敏规则（§7.5 adapted），所有 GEN 类注入
[5] common/terminology_rules.md    # 标题贯穿/术语族规则（§7.9 adapted），所有 GEN 类注入
[6] <stage 指令文件>                # 本阶段专属指令
[7] === 运行时上下文（定界符包裹）===
    <案件卡片>（案件名称、专利类型、联系人占位）
    <术语表 terminology_sheet JSON>
    <骨架/大纲 outline JSON>（生成类）
    <前序阶段产物摘要>（按阶段需要）
```

每个 prompt 文件头部带 YAML front-matter：`source_repo / source_path / source_url / treatment / ported_version`；装配器记录所用文件 hash 到 `llm_calls`，保证可复现。`prompts/manifest.yaml` 驱动搬运脚本与 NOTICE 校验。

### 0.3 术语表贯穿机制
`terminology_sheet`（JSON，随 case 持久化）注入所有 GEN 调用；每次 GEN 调用要求在文末输出一个 `terms_delta` JSON 围栏块（服务端剥离、合并进 sheet；缺失时补一次小型 STRUCT 抽取调用）。

### 0.4 材料摄取与上下文经济
上传材料先服务端转 markdown，再逐文件跑 `material_digest` STRUCT 调用产出摘要卡；下游阶段默认只注入摘要卡，仅在该阶段明确需要时注入原文选段（按 token 预算裁剪，预算 = context_window − 系统prompt − 期望输出 − 20% 余量）。

---

## 1. 资产清单（Asset Manifest）

目标目录：`backend/app/assets/prompts/{common,disclosure,paper2patent,reader,oa}/`、`backend/app/schemas/`（JSON Schema）、`backend/app/assets/references/`（数据型 YAML）、`backend/app/tools/`、`NOTICE.md`。

### 1.1 patent-disclosure-skill（handsomestWei，MIT）——处置要点

| 源文件 | 处理 | 说明 |
|---|---|---|
| `SKILL.md` | **改写为代码** | 4模式路由、8步表、检查清单落为状态机；硬性约定分散进 stage prompt 与 lint 配置 |
| `prompts/disclosure/intake.md` | 改编 | 三问 Q&A 保留原话术做前端表单文案 + 复述调用指令 |
| `project_scan.md` | 改编→`material_digest.md` | 扫描优先级表保留；Office 转换、CAD 分类改为服务端摄取步骤 |
| `prior_art_search.md` | 改编（拆三份）| `prior_art/search_terms.md`、`abstract_digest.md`、`section_1_1_writer.md` |
| `disclosure_preview.md` | 改编 | 确认语改为平台暂停点 UI 文案 |
| `disclosure_self_check.md` | 改编（按类型拆3份）| 8.1+8.2+8.3 发明版；8.1+8.3+8.4 实用版；8.1+8.3+8.5 外观版；输出改补丁清单 |
| `iteration_context.md`/`merger.md`/`correction_handler.md` | 改编 | 意图判别→STRUCT 分类调用；「合并摘要（留档）」输出格式 verbatim |
| `invention/patent_points_analyzer.md` | 改编 | 输出加 JSON 契约 |
| `invention/disclosure_builder.md`（§7.1-7.9）| 改编（拆分）| 拆 builder_core(§7.1)/desensitization(§7.5)/terminology_rules(§7.9)/mermaid_rules(§7.4)/formula_style(§7.7)/claim_bias(§7.6)；§7.3 命名改写为代码；规则文本逐字保留 |
| `invention/template_reference.md` | **verbatim** | 六章模板+文头+mermaid模版+公式正反例+脱敏检查表整体保留，按需注入 |
| `utility_model/*`、`design/*` | 同上模式 | 图际关系启发式表保留 |
| `shared/fill_*_schema.md` | 改编 | 识图打分指令保留；输出改 JSON Schema 校验；vision 能力位缺失时用户填表兜底 |
| `shared/image_gen.md`、`*_lineart_assist.md` | 改编 | 降级为**可选图像模型配置**；未配置时用户上传线稿兜底；gen_prompt 模板 verbatim |
| `references/formulas/paradigms.yaml` | **verbatim** | 注入时用摘要目录（id+名称+式形），选中后注入全文 |
| `references/schemas/*.schema.yaml` | 改写为代码（→JSON Schema）| 同时保留 YAML 原文供 prompt 注入 |
| `references/patent_type_search.yaml`、`ipc_application_hints.yaml`、`patent_pdf_sources.yaml` | **verbatim** | |
| `reader/patent_plain_reader.md` | 改编（拆6份）| Obsidian 指令弃用；「说明书 0002」引用纪律保留 |
| `assets/patent_note_template.md` | 改编 | callout→站内 block 类型；11节结构 verbatim |
| `evolution/*` | **弃用** | 模式C不入本期 |
| `oa/*` | 改编 | configure_embedding→设置页表单；respond 拆 strategy/respond_issue |
| `tools/shared/*`、`tools/crawl/*` | 改编移植 | 保留 CLI+stdout 协议；`check_formula_plan.py --eval` 作门禁 |
| `tools/shared/cad_scan`等 | 弃用（本期）| CAD 仅按扩展名分类提示 |
| `examples/*` | → tests/fixtures | 端到端金样 |

### 1.2 paper2patent（7toCR，MIT + CLAUSE.md）——处置要点

| 源资产 | 处理 | 说明 |
|---|---|---|
| README 内 **Pro Prompt**（~12K字）| **verbatim（拆9份）** | role_task/part1_abstract/part2_abstract_fig/part3_claims/part4_description/part5_drawings/critical_rules/content_constraints+writing_methods/final_format；15禁用词全表、12条 Critical Rules、四阶段执行流逐字保留；`reference_skills/meterial.md` 原仓库不存在，以 README Pro 原文为准 |
| README 内 **Flash Prompt** | **verbatim** | 快速模式单调用 |
| README 内 **Gemini 附图 Prompt** | **verbatim** | 供可选图像模型精修 + 导出给用户 |
| `skills/paper2patent/SKILL.md` | 改写为代码 | 三种输出模式/8步落为模块B状态机 |
| `references/input-requirements.md` | 改编 | 表单字段 + 评估调用 JSON 契约 |
| `references/claims-and-specification-rules.md`、`drawing-generation.md` | **verbatim** | |
| `references/document-generation.md` | 改写为代码+改编 | JSON 契约转正式 JSON Schema |
| `references/quality-checklist.md` | 改编 | 五类清单→AUDIT 补丁契约 |
| `scripts/generate_patent_drawings.py`（31KB）/`generate_patent_docx.py`/`export_patent_pdf.py` | 改编移植 | 全功能保留；封装函数接口 |
| 宿主 Agent 安装物、`.claude/` 镜像 | 弃用 | |

### 1.3 许可与署名（NOTICE.md）
- patent-disclosure-skill © handsomestWei，MIT — 全文引用 + 源仓库 URL + 移植说明
- paper2patent © 7toCR，MIT + CLAUSE.md — **显著注明来源 7toCR/paper2patent**
- 每个移植 prompt 文件 front-matter 带 `source_url`

---

## 2. 分模块流水线规格

记法：`Sx`=阶段；`[PAUSE]`=人机暂停态；`(srv)`=服务端步骤；`(llm:CLASS)`=LLM 调用。

### 模块 A：专利交底书

- **A0 建案**(srv)：`{case_id, module:"disclosure", patent_type:"invention"(默认), contact_placeholder}`
- **A1 边界录入 [PAUSE]**：前端表单（intake.md 三问）；提交后 (llm:CHAT) 3-6 行 bullet 复述（必含专利类型）
- **A2 材料摄取与消化**：(srv) 逐文件转换；(llm:STRUCT) material_digest 逐文件（>3k tokens 分片归并），输出 `{file, priority, summary_zh, tech_points[], components[], figures_mentioned[], sensitive_hits[], type_signals{}}`；(srv) 归并+聚合 type_signals
- **A2b 类型改判建议 [PAUSE 条件]**：信号与当前类型不符时反问一次（intake.md 反问模板 verbatim）
- **A3 专利点挖掘 [PAUSE]**：(llm:GEN 流式) 候选点分析 + 尾部 JSON `{candidates:[{id,title,background,innovation,distinction,feasibility,score}], recommended, fusion_note}`；发明 3-5/实用 2-4/外观 1-3；用户勾选/融合
- **A3b（实用/外观）填表与线稿**：(llm:STRUCT) fill_schema → structure/appearance JSON（not_*_signals 非空→回 A2b）；(llm:STRUCT) figure_plan 逐图 `{fig, role, kind, score, use_in_disclosure, relates_to[]}`（合格线 score≥70）；线稿分支：已有合格 lineart 直用 / 配置图像模型走 gen_prompt+门禁+叠标+语义自查≤2轮 / **未配置（默认）[PAUSE]** 生成「线稿绘制说明」请用户上传；入文规则 verbatim：外观=实拍+线稿、实用=仅线稿、CAD 永不入文
- **A4 联网查新**：(llm:STRUCT) search_terms `{blocks:[2-8个语义块], type_param, rationale}`（块数/块长校验，违规 REPAIR）→ (srv) cnipa_epub_search（浏览器池、一次会话多词、限速、6h 缓存）→ **失败 [PAUSE]** 三选项（重试/用户粘贴在先文献/跳过→1.1 如实写明未检索；prompt 显式禁止编造检索结果）→ (llm:STRUCT) abstract_digest（每批≤8条；消化改写禁粘贴；URL 只准照抄 link 字段——lint：产物 URL ∈ hits.link 集合）
- **A5 摘要预览 [PAUSE]**：(llm:GEN) 名称/类型/问题/核心模块/与最近现有技术区别；确认/调整方向(带反馈重跑)/跳过
- **A6 成文（发明）**——骨架先行逐章生成：

| 步 | 调用 | 输入/输出契约 | 服务端校验 |
|---|---|---|---|
| G0 骨架 | STRUCT | → `skeleton.json`{case_title(含"一种…方法及系统"), title_core_terms[], modules[], steps[{sid:"S1",label}], chapter_outline, terms_init[]} | 标题实词≥1 出现于 modules 与 steps 标签 |
| G1 一+二章 | GEN | prior_art_notes 全文+骨架 → 1.1(每条附核验URL+检索说明)/1.2/二 | URL∈hits；检索说明含数据库名与检索词、禁脚本名 |
| G2 3.1–3.3 | GEN | → 3.1 固定三小段；3.2 mermaid flowchart(+subgraph)；3.3 一模块一项 | mermaid 渲染校验；3.3 模块集合=骨架 modules |
| G3 3.4 | GEN | → mermaid 流程图（S号入可见标签 `S1["S1 …"]`）+逐项文字 | 正则标签齐全连续；渲染校验 |
| G4 formula_plan | STRUCT | paradigms 目录 → formula_plan.json{paradigm_ids,equations[{tag,paradigm_id,latex,role}],symbols[],numeric_example} | check_formula_plan --eval 复算+范式合法性；失败 REPAIR≤2→[PAUSE] |
| G5 3.4.1+3.5 | GEN | 通过的 formula_plan → 符号表+公式叙述+关键参数 | 符号逐字同形；装饰音/上标维度 regex lint |
| G6 四+五章 | GEN | 骨架+前章 300 字摘要 | 保护点术语 ⊆ 术语表核心词 |
| G7 六章 | GEN | numeric_example+3.4 步骤 → 2-3 个有名字的实施例 | 实施例 S 号 ⊆ 骨架 steps |

实用新型：G0(parts/relations 取 schema) → G1 → G2(3.1–3.3 对齐 schema/figure_plan) → G3(3.4/3.5) → G4(四五六；第五章书式 lint「一种…装置，包括…；其特征在于…」禁纯方法主保护点)。外观：G0 简化 → G1(一二三章视图说明按 claimed_faces) → G2(四五章)。
- **A7 组装与自检**：(srv) 章节拼接+文头模板；(llm:AUDIT) self_check 全文进上下文（唯一位置）→ 补丁契约 → (srv) 应用（公式补丁触发 G4 门禁复跑；术语补丁触发全文族替换）≤2 轮；(srv) 确定性 lint 束（元信息泄漏/文件名/URL 白名单/mermaid 可渲染/文末清洁）
- **A8 交付 [PAUSE]**：(srv) mermaid→PNG → md_to_docx → 落盘时间戳命名；(llm:STRUCT) claim_bias `{groups:[{axis,option_a{label,basis_quote},option_b}]}`——**basis_quote 必须是终稿子串**（substring 校验，不命中 REPAIR→丢弃该组）；用户选择仅存记录不入正文
- **A-迭代**：IT0 意图路由(STRUCT `{intent:merge|correct|question|new_case}`) → IT-merge（新材料 A2 消化→增量识别→受影响章节重写→needs_search 触发补查新→figure_plan 同步→A7→新时间戳+「## 合并摘要（留档）」3-6句+再 claim_bias）/ IT-correct（纠正点五分类→逐点补丁（公式类过 G4 门禁；术语类全文 find-replace 计划+替换计数）→lint→新时间戳+「## 纠正摘要（留档）」2-5句）

### 模块 B：论文转专利

模式：direct / human-in-loop / text-only；另 flash 快速档（单调用流式，不进脚本管线）。

- B0 建案(srv)：PDF→md+插图；选模式
- B1 输入评估(STRUCT)：`{completeness{}, patentability_clues[], fidelity_risks[], drawing_readiness, gaps[]}`；hil→[PAUSE]；direct→gaps 转【待补充】占位
- B2 深读提取(STRUCT)：四要素闭环+剥离数学证明 → `{contributions[], method_steps[], system_components[], distinctions[], tech_problem, tech_effects[], source_map:[{claim_like,source_quote}]}`；超预算时 Abstract/Intro/Method 全文优先
- 单调用捷径(GEN)：论文≤25k tokens 且 context≥128k 时 Pro 全文一次流式产五部分；产物仍进 B4 lint 与 B5 装配
- B3a 权利要求书(GEN)：markdown+尾部 JSON `{invention_name, claims[]}`（6-10 项，1-2 独权）；hil→[PAUSE]
- B3b 摘要(GEN)：≤300字四段式；名称与权1 逐字一致（srv 校验）
- B3c 说明书(GEN×2)：①技术领域+背景三层+发明内容三层（注入 claims 全文）②附图说明+具体实施方式（最长产物，独占调用+全额输出预算）
- B3d 附图规格(STRUCT)：原图优先 → drawings[]+source_figures[]
- B4 权项 lint(srv+REPAIR)：15 禁用词/句号/引用基础/多引不引多引/名称一致 → 违规清单反馈 REPAIR ≤2
- B4b 忠实性审计(AUDIT)：断言逐条映射论文原文摘录（复用 source_map）；落不了源→direct 改【待补充】/hil [PAUSE]
- B7 质检(AUDIT)：quality-checklist 五类补丁（实现顺序 B4b→B7→B5→B6）；结果静默
- B5 契约装配(srv)：**确定性组装**（非 LLM）→ jsonschema 校验
- B6 附图+文件(srv)：drawings 脚本(--update-json；validation 不过→REPAIR≤2→降级 image_model_prompt-only 入 gaps)→hil [PAUSE] 预览→docx(--require-drawings)→pdf
- B8 交付(srv)：DOCX+PDF+SVG/PNG+每图 image_model_prompt；交付摘要只列文件/占位符/限制

### 模块 C：专利解读（站内 11 节报告）

C0 输入(srv)：公开号→fetch_patent_pdf（patent_pdf_sources.yaml 优先级）或 PDF 上传；种类码判型 → C1 文本化(srv)：pymupdf 按结构切分+段落号索引 → C2 权要树(STRUCT)：claim_tree.json{claims[{no,type,refs,ref_mode,preamble,features[]}],ambiguities[]}+树 lint（refs<no、无环、多引不引多引）；ambiguities→[PAUSE 可选] → C3 白话增量(STRUCT)：每权 12-40 字（长度校验）→ C4 落地线索(STRUCT)：ipc_application_hints 注入；anchor_fits=每条锚定说明书段落号 → C5 报告撰写(GEN×3)：①结构节(权要树表+独权精读+术语表+对照表) ②叙事节 ③附录（「说明书 0002」格式，禁裸[0002]）→ C6 装配(srv)：report.json{meta,sections[{blocks:[{type:markdown|table|claim_tree|mermaid|callout-*}]}]}+引用 lint → C7 自检(AUDIT) → 交付站内渲染+md 导出

### 模块 D：审查意见答复

D0 建案(srv)：OA 通知书 PDF（+可选本申请/对比文件）→ D1 结构化(STRUCT)：notice_struct{notice_kind,application_no,patent_type,issues:[{id,statute,defect_type,claims_involved,examiner_argument_digest,cited_documents}]}+枚举校验→**[PAUSE] 人审核对** → D2 案例检索(srv)：逐 issue 元数据预过滤+sqlite-vec top-5（无配置→关键词降级并明示 retrieval_mode）→ D3 策略(STRUCT→[PAUSE])：逐 issue options{strategy: argue_only|amend_claims|amend_description|formal_fix, rationale, risk, amendment_sketch}+recommended→用户勾选 → D4 逐条起草(GEN)：每 issue 流式；amend_claims 输出修改对照；修改后权项过权项 lint → D5 汇总(srv+AUDIT)：装配意见陈述书+跨条一致性→[PAUSE] 终稿人审→时间戳落盘 → D-ingest：结案后案例笔记草稿(STRUCT)→[PAUSE] 人审 frontmatter 与脱敏→入库+embedding

---

## 3. 横切设计

- **3.1 术语一致性**：terminology_sheet 注入+terms_delta 合并+漂移 lint（标题实词出现率/forbidden_variants 命中→章 REPAIR/符号同形）
- **3.2 脱敏**：desensitization.md verbatim 注入所有 GEN；material_digest 的 sensitive_hits 形成案件级敏感词表+srv lint 扫描；用户可增补
- **3.3 自检补丁契约**：`{verdict, patches:[{checklist_item, anchor_before(10-40字), original_excerpt, replacement, reason}], unresolved[]}`；精确→模糊匹配；应用率<80% 或重叠→整章重写兜底；≤2 轮；unresolved 呈现给用户不入正文
- **3.4 版本纪律**：命名函数 §7.3；revision_log 五要素；「交底书修订对话记录.md」由 DB 导出
- **3.5 查新专用调用**：search_terms/abstract_digest 契约见 A4；1.1 写作以 prior_art_notes.json 为唯一来源；禁写脚本名/流程元信息（prompt 规定+srv lint）
- **3.6 上下文经济**：tiktoken 近似计数+每调用预算表；发明逐章生成下游只注入骨架+前章 300 字摘要+术语表；全文只在 A7 self_check 进一次（超限时分批 AUDIT+跨章一致性专项）
- **3.7 流式与暂停点**：GEN/CHAT SSE 直推；STRUCT/AUDIT 完成后一次性推送；[PAUSE]=pipeline_state 持久化+前端交互+恢复即从下一 stage 继续

---

## 4. 质量风险登记册（R1-R16）

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 长章节连贯性崩坏 | 骨架先行+逐章+前章摘要+术语表+A7 全文 AUDIT；实施方式独占调用 |
| R2 | mermaid 语法非法 | 服务端试渲染门禁；REPAIR≤2；降级编号文字清单+「图待补」；S 号标签 regex；禁 ASCII 框图检测 |
| R3 | 公式失律 | formula_plan 门禁 --eval+范式白名单+regex(`\\(tilde|hat|bar)`、上标维度、分隔符混用)+符号跨节同形 |
| R4 | 权项禁用词/标点/引用违规 | 15 禁用词表（CJK 无词界→**命中只标记不自动删**，交 REPAIR 判断防「等离子」误伤）；句号计数；引用号<自身；多引不引多引；「其特征在于」存在性 |
| R5 | 发明名称不一致 | srv 逐字一致校验+REPAIR |
| R6 | 大 JSON 截断 | 分段结构化+服务端确定性装配；schema 失败带错重试≤2→[PAUSE] 人工放行 |
| R7 | 编造（忠实红线） | source_map 落源审计；claim_bias basis_quote 子串校验 |
| R8 | CNIPA WAF | channel=chrome 非 headless+限速+缓存；失败三选项门控；禁编检索结果；URL 白名单 |
| R9 | 元信息泄漏 | 禁词标题扫描→剥离+局部 REPAIR |
| R10 | 术语漂移 | terminology_sheet+forbidden_variants lint+§7.9 注入 |
| R11 | p2p 终稿混入 markdown 记号 | srv 扫描剥离或 REPAIR |
| R12 | 上下文溢出 | 预算器自动切换摘要注入与分批 AUDIT |
| R13 | 附图不合格 | 脚本 validation→规格 REPAIR≤2→降级 prompt-only 入 gaps |
| R14 | 无图像模型线稿缺失 | 可选配置+用户上传兜底；外观仅实拍时 [PAUSE] 明示风险 |
| R15 | 弱模型不守四阶段流 | 捷径仅能力位达标启用；截取「一、说明书摘要」锚点丢弃前文 |
| R16 | OA 条款幻觉 | statute/defect_type 枚举校验+D1 强制人审 |

## 5. 实施顺序

1. 地基：prompt 装配器+LLM 客户端+状态机引擎+存储+lint 框架
2. 资产搬运（可与 1 并行）
3. 模块 B（契约最清晰）→ 打通 GEN/STRUCT/AUDIT/REPAIR 全链路
4. 模块 A 发明分支 → 实用/外观分支
5. 模块 C → 模块 D
6. 回归：examples/ 四教学案件金样测试
