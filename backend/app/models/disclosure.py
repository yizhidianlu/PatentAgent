"""专利交底书（模块 A）数据契约模型。

两类：

1. **STRUCT / AUDIT 输出契约** —— 各阶段 LLM 结构化输出，供 `services/llm.structured()`
   校验。字段严格对照 `assets/prompts/disclosure/**` 的 JSON 契约与
   `assets/references/schemas/*.schema.yaml`（formula_plan / structure / appearance /
   figure_plan），字段名一律与 YAML 原文保持一致（下游 tools 脚本按这些键读写）；
2. **术语表家族**（TermEntry / SymbolEntry / TermsDelta / TerminologySheet）——
   prompt-porting-spec §3.1 的贯穿机制数据结构；`services/terminology.py` 直接复用
   （从该模块 re-export，调用方两处 import 皆可）。

约定：
- 除显式必填外一律给缺省值，保证「服务端确定性装配」不因缺字段炸掉；
- 模型统一 `extra="ignore"`（LLM 多吐的键静默丢弃），YAML 事实合同类
  （StructureSchema / AppearanceSchema / FigurePlan）用 `extra="allow"` 保留原文扩展键。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

PatentType = Literal["invention", "utility_model", "design"]

# ---------------------------------------------------------------------------
# 一、术语表家族（§3.1；services/terminology.py 的数据结构）
# ---------------------------------------------------------------------------


class TermEntry(BaseModel):
    """术语族一条：规范词 + 场景定义 + 别名 + 禁用变体。

    兼容 prompt 侧的键名（`terminology_rules.md` 的 terms_delta 用 `term` /
    `definition`），故 canonical / definition_zh 均带 validation alias。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    canonical: str = Field(validation_alias=AliasChoices("canonical", "term"), min_length=1)
    definition_zh: str = Field(
        default="", validation_alias=AliasChoices("definition_zh", "definition")
    )
    aliases: list[str] = Field(default_factory=list)
    forbidden_variants: list[str] = Field(default_factory=list)
    source_section: str = ""


class SymbolEntry(BaseModel):
    """符号表一条（3.4.1 符号表 / 3.5 参数表逐字同形的比对单元）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    symbol: str = Field(min_length=1)
    meaning_zh: str = Field(default="", validation_alias=AliasChoices("meaning_zh", "meaning"))
    unit_zh: str = Field(default="", validation_alias=AliasChoices("unit_zh", "unit"))
    section: str = ""


class TermsDelta(BaseModel):
    """每次 GEN 调用文末 ```json 围栏块中的术语增量（服务端剥离后合并）。"""

    model_config = ConfigDict(extra="ignore")

    add: list[TermEntry] = Field(default_factory=list)
    update: list[TermEntry] = Field(default_factory=list)
    symbols: list[SymbolEntry] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """无任何增量（用于判断是否需要补一次小型 STRUCT 抽取）。"""
        return not (self.add or self.update or self.symbols)


class TerminologySheet(BaseModel):
    """案件级术语表：随 case 持久化，注入所有 GEN 调用。"""

    model_config = ConfigDict(extra="ignore")

    case_title: str = ""
    title_core_terms: list[str] = Field(default_factory=list)
    term_family: list[TermEntry] = Field(default_factory=list)
    symbols: list[SymbolEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 二、A1 边界录入 / A2 材料摄取
# ---------------------------------------------------------------------------


class Contact(BaseModel):
    """文头技术联系人（可全部「待填写」占位）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = "待填写"
    phone: str = "待填写"
    email: str = "待填写"


class IntakeAnswer(BaseModel):
    """A1 表单答案（intake.md 三问）。

    硬性默认：用户未显式指定类型时一律 `invention` 且 `patent_type_explicit=False`
    （复述时须写「专利类型：发明（默认）」）。
    """

    model_config = ConfigDict(extra="ignore")

    topic: str = Field(default="", description="Q1 技术主题或产品模块（一句话）")
    patent_type: PatentType = "invention"
    patent_type_explicit: bool = Field(
        default=False, description="用户是否显式指定了专利类型（否则为默认发明）"
    )
    contact: Contact = Field(default_factory=Contact)
    notes: str = ""


class TypeSignals(BaseModel):
    """材料给出的类型信号计数（聚合后用于 A2b 类型改判反问）。"""

    model_config = ConfigDict(extra="ignore")

    invention: float = 0
    utility_model: float = 0
    design: float = 0


class MaterialDigest(BaseModel):
    """A2 逐文件消化摘要卡（material_digest.md 的 STRUCT 输出）。"""

    model_config = ConfigDict(extra="ignore")

    file: str = ""
    priority: int = 0
    summary_zh: str = ""
    tech_points: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    figures_mentioned: list[str] = Field(default_factory=list)
    sensitive_hits: list[str] = Field(default_factory=list)
    type_signals: TypeSignals = Field(default_factory=TypeSignals)


# ---------------------------------------------------------------------------
# 三、A3 专利点挖掘
# ---------------------------------------------------------------------------


class PatentPointCandidate(BaseModel):
    """候选专利点（points.md 文末 JSON 的 candidates[] 条目）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    title: str = ""
    background: str = ""
    innovation: str = ""
    distinction: str = ""
    feasibility: str = ""
    score: float = 0

    @field_validator("score")
    @classmethod
    def _score_range(cls, v: float) -> float:
        return max(0.0, min(100.0, float(v)))


class PatentPoints(BaseModel):
    """A3 输出：候选点清单 + 建议项 + 融合说明（发明 3-5 / 实用 2-4 / 外观 1-3）。"""

    model_config = ConfigDict(extra="ignore")

    candidates: list[PatentPointCandidate] = Field(default_factory=list)
    recommended: str = ""
    fusion_note: str = ""
    type_mismatch_note: str = Field(
        default="",
        description="实用/外观分支：not_*_signals 非空时的改判建议（发明分支恒为空）",
    )


# ---------------------------------------------------------------------------
# 四、A3b 事实合同（structure / appearance / figure_plan schema.yaml 转写）
# ---------------------------------------------------------------------------


class StructurePart(BaseModel):
    """StructureSchema.parts[] 条目。"""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    shape: str = ""
    material_hint: str = ""


class StructureRelation(BaseModel):
    """StructureSchema.relations[] 条目（卡扣/螺栓/铰接/焊接/一体成型/过盈/粘接/其他）。"""

    model_config = ConfigDict(extra="allow")

    from_: str = Field(default="", validation_alias=AliasChoices("from_", "from"), alias="from")
    to: str = ""
    type: str = ""
    where: str = ""
    seen_in: list[Any] = Field(default_factory=list)


class StructureSchema(BaseModel):
    """实用新型事实合同（references/schemas/structure.schema.yaml）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    version: int = 1
    mode: Literal["disclosure", "reader"] = "disclosure"
    source_images: list[str] = Field(default_factory=list)
    parts: list[StructurePart] = Field(default_factory=list)
    relations: list[StructureRelation] = Field(default_factory=list)
    spatial: list[str] = Field(default_factory=list)
    function_of_structure: list[str] = Field(default_factory=list)
    delta_hypothesis: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    not_utility_model_signals: list[str] = Field(default_factory=list)


class AppearanceView(BaseModel):
    """AppearanceSchema.views[] 条目。"""

    model_config = ConfigDict(extra="allow")

    name: str = ""
    notes: str = ""
    source_image: str = ""


class OmittedView(BaseModel):
    """故意不交的正投影（不是缺材料）。"""

    model_config = ConfigDict(extra="allow")

    name: str = ""
    reason: str = ""


class AppearanceSchema(BaseModel):
    """外观设计事实合同（references/schemas/appearance.schema.yaml）。"""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    mode: Literal["disclosure", "reader"] = "disclosure"
    source_images: list[str] = Field(default_factory=list)
    product_name: str = ""
    overall_shape: str = ""
    product_form: Literal["solid", "planar"] = "solid"
    claimed_faces: list[str] = Field(default_factory=list)
    omitted_views: list[OmittedView] = Field(default_factory=list)
    views: list[AppearanceView] = Field(default_factory=list)
    ornament: list[str] = Field(default_factory=list)
    color: list[str] = Field(default_factory=list)
    design_points: list[str] = Field(default_factory=list)
    contrast_to_prior: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    not_design_signals: list[str] = Field(default_factory=list)


FigureRole = Literal["assembly", "detail", "ortho", "perspective", "reference", "rejected"]
FigureKind = Literal["lineart", "cad", "photo_clean", "photo_scene", "other"]
FigureRelationType = Literal[
    "detail_of", "section_of", "exploded_of", "same_state", "alternate_view", "sequence"
]


class FigureRelation(BaseModel):
    """figure_plan.figures[].relates_to[] 条目。"""

    model_config = ConfigDict(extra="allow")

    fig: int | None = None
    relation: FigureRelationType = "alternate_view"
    note: str = ""


class FigureItem(BaseModel):
    """figure_plan.figures[] 条目（合格线 score≥70；kind=cad 永不入文）。"""

    model_config = ConfigDict(extra="allow")

    fig: int | None = None
    role: FigureRole = "reference"
    path: str = ""
    covers: list[str] = Field(default_factory=list)
    kind: FigureKind = "other"
    relevance: float = 0
    quality: float = 0
    score: float = 0
    use_in_disclosure: bool = False
    reason: str = ""
    relates_to: list[FigureRelation] = Field(default_factory=list)


class FigurePlan(BaseModel):
    """交底附图选用与排序合同（references/schemas/figure_plan.schema.yaml）。"""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    patent_type: Literal["utility_model", "design"] = "utility_model"
    theme_summary: str = ""
    schema_ref: str = ""
    figures: list[FigureItem] = Field(default_factory=list)


class ClaimFormAudit(BaseModel):
    """第五章书式审查（实用新型 · AUDIT 调用）。

    §2 A6 实用新型段的「第五章书式 lint」双查里的 AUDIT 一半：正则先扫一遍，
    再让模型判一次「主保护点是不是装置/结构书式」——两者任一不过即触发 REPAIR。
    """

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "fail"] = "pass"
    is_device_form: bool = True
    problems: list[str] = Field(default_factory=list)
    suggestion: str = ""


# ---------------------------------------------------------------------------
# 五、A5 摘要预览
# ---------------------------------------------------------------------------


class PreviewSummary(BaseModel):
    """A5 摘要预览（preview.md「摘要应包含」按类型裁剪）。"""

    model_config = ConfigDict(extra="ignore")

    case_title: str = ""
    patent_type: PatentType = "invention"
    patent_type_label: str = Field(default="", description="如「发明（默认）」")
    tech_problems: list[str] = Field(default_factory=list)
    core_modules: list[str] = Field(default_factory=list)
    key_parts: list[str] = Field(default_factory=list, description="实用新型：关键部件与连接")
    design_points: list[str] = Field(default_factory=list, description="外观：设计要点")
    figure_notes: list[str] = Field(default_factory=list, description="附图齐备性/视图清单")
    distinction: str = Field(default="", description="与最相近现有技术/在先外观的区别")
    searched: bool = Field(default=False, description="False 时须在摘要标明「待查新」")
    markdown: str = Field(default="", description="呈现给用户的摘要正文")


# ---------------------------------------------------------------------------
# 六、A6-G0 骨架
# ---------------------------------------------------------------------------


class SkeletonStep(BaseModel):
    """流程步骤（S 号入 mermaid 可见标签：`S1["S1 …"]`）。"""

    model_config = ConfigDict(extra="ignore")

    sid: str = Field(min_length=1, description="如 S1")
    label: str = ""


class Skeleton(BaseModel):
    """G0 骨架（骨架先行、逐章生成的唯一结构源）。

    服务端校验（§2 A6 G0）：`title_core_terms` 中至少 1 个实词出现于 `modules`
    与 `steps` 标签 —— 见 `services/terminology.py::skeleton_title_lint`。
    """

    model_config = ConfigDict(extra="ignore")

    case_title: str = Field(default="", description="如「一种…方法及系统」")
    title_core_terms: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    steps: list[SkeletonStep] = Field(default_factory=list)
    chapter_outline: list[str] = Field(default_factory=list)
    terms_init: list[TermEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 七、A6-G4 公式计划（references/schemas/formula_plan.schema.yaml 逐字段转写）
# ---------------------------------------------------------------------------

EquationRole = Literal["score", "trigger", "smooth", "assign", "constraint", "other"]


class FormulaEquation(BaseModel):
    """formula_plan.equations[] 条目。"""

    model_config = ConfigDict(extra="allow")

    tag: int | str = 1
    paradigm_id: str = ""
    latex: str = ""
    role: EquationRole = "other"


class NumericExample(BaseModel):
    """可代入数值例（check_formula_plan --eval 的复算依据）。"""

    model_config = ConfigDict(extra="allow")

    given: dict[str, Any] = Field(default_factory=dict)
    compute_zh: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class FormulaPlan(BaseModel):
    """G4 公式计划（写 3.4.1 前必须通过 services/formula.validate_formula_plan）。"""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    theme_summary: str = ""
    combo_id: str = ""
    paradigm_ids: list[str] = Field(default_factory=list)
    plain_zh: str = ""
    equations: list[FormulaEquation] = Field(default_factory=list)
    symbols: list[SymbolEntry] = Field(default_factory=list)
    numeric_example: NumericExample = Field(default_factory=NumericExample)
    notes_zh: str = ""


# ---------------------------------------------------------------------------
# 八、A7 自检补丁（§3.3 契约）
# ---------------------------------------------------------------------------


class SelfCheckPatch(BaseModel):
    """自检补丁一条：锚点 + 逐字原文片段 + 替换文字。"""

    model_config = ConfigDict(extra="allow")

    checklist_item: str = ""
    anchor_before: str = Field(default="", description="补丁位置之前 10-40 字的原文锚点（逐字）")
    original_excerpt: str = Field(default="", description="被替换的原文片段（逐字）")
    replacement: str = ""
    reason: str = ""


class SelfCheckReport(BaseModel):
    """self_check_{invention,utility,design}.md 的 AUDIT 输出契约。"""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "patched"] = "pass"
    patches: list[SelfCheckPatch] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 九、A8 权利要求偏向点（basis_quote 必须是终稿子串）
# ---------------------------------------------------------------------------


class ClaimBiasOption(BaseModel):
    """偏向点的一个选项。"""

    model_config = ConfigDict(extra="ignore")

    label: str = ""
    basis_quote: str = Field(default="", description="支撑该偏向的终稿原文摘录（逐字子串）")


class ClaimBiasGroup(BaseModel):
    """一组对举的权利要求书式倾向（如：方法流程侧重 vs 装置模块侧重）。"""

    model_config = ConfigDict(extra="ignore")

    axis: str = ""
    option_a: ClaimBiasOption = Field(default_factory=ClaimBiasOption)
    option_b: ClaimBiasOption = Field(default_factory=ClaimBiasOption)


class ClaimBias(BaseModel):
    """claim_bias.md 的 STRUCT 输出（用户选择仅存记录，不入正文）。"""

    model_config = ConfigDict(extra="ignore")

    groups: list[ClaimBiasGroup] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 十、迭代（IT0 意图路由 / 纠正点分类）
# ---------------------------------------------------------------------------


class IntentRoute(BaseModel):
    """iteration/intent_router.md 的 STRUCT 输出。"""

    model_config = ConfigDict(extra="ignore")

    intent: Literal["merge", "correct", "question", "new_case"] = "question"
    reason: str = ""


class CorrectionItem(BaseModel):
    """iteration/correction.md 的一个纠正点（五分类）。"""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["formula", "terminology", "structure", "fact", "format"] = "fact"
    target_section: str = ""
    instruction: str = ""
    old_term: str = ""
    new_term: str = ""


class CorrectionPlan(BaseModel):
    """纠正计划：逐点补丁前的分类结果。"""

    model_config = ConfigDict(extra="ignore")

    items: list[CorrectionItem] = Field(default_factory=list)
    summary: str = ""
