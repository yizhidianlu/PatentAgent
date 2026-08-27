"""论文转专利（模块 B）数据契约模型。

两类：
1. **PatentContent 家族** —— 逐字段转写 `assets/references/p2p/document-generation.md`
   的「Structured Content Contract」JSON 契约（必填 invention_name / abstract /
   claims / description），是附图脚本、DOCX 生成器、PDF 兜底三支脚本共同的输入
   契约，字段名一律与脚本读取的键名保持一致（不得改名）；
2. **各阶段 STRUCT / AUDIT 输出模型** —— B1 输入评估、B2 深读提取、B3a 权项、
   B3d 附图规格、B4b 忠实性审计、B7 质检补丁，供 services/llm.structured() 校验。

约定：
- 脚本会往 drawing_assets[].validation / drawing_validation[] 里回写 canvas_width、
  max_margin_ratio 等附加键，故这些子模型 extra="allow"，避免回读时被 pydantic 丢弃；
- 契约中未标注必填的字段一律给缺省值，保证「服务端确定性装配」时不因缺字段炸掉。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# 一、PatentContent 契约（document-generation.md 逐字段转写）
# ---------------------------------------------------------------------------


class SourceFigure(BaseModel):
    """论文原图元数据（source_figures[] 条目）。"""

    model_config = ConfigDict(extra="allow")

    figure_no: int
    source_label: str = ""          # 论文中的原图标号，如 "Figure 2"
    caption: str = ""               # 论文原图图注
    page: int | str | None = None   # 页码（PDF 转换产物里可能是字符串）
    notes: str = ""                 # 从原图与正文提取的节点/箭头/布局依据


class DrawingValidation(BaseModel):
    """附图脚本回写的画布检查结果（drawing_validation[] 条目 / asset.validation）。"""

    model_config = ConfigDict(extra="allow")

    figure_no: int | None = None
    content_width_ratio: float | None = None
    content_height_ratio: float | None = None
    internal_title: bool = False    # 图内是否混入图号/图题（必须为 false）
    passes: bool = False


class DrawingAsset(BaseModel):
    """生成的 SVG/PNG 参考附图元数据（drawing_assets[] 条目）。"""

    model_config = ConfigDict(extra="allow")

    figure_no: int
    title: str = ""
    type: str = ""                  # method_flow | system_block | data_flow
    spec: str = ""                  # 附图规格原文（脚本据此抽步骤/模块）
    svg_path: str | None = None     # 相对 patent_content.json 所在目录
    png_path: str | None = None
    caption: str = ""               # 图题（在图外，不得画进画布）
    abstract_candidate: bool = False
    validation: DrawingValidation | None = None
    image_model_prompt: str = ""
    source_figure: SourceFigure | None = None


class ImageModelPrompt(BaseModel):
    """Image2 类图像模型精修提示词（image_model_prompts[] 条目）。"""

    model_config = ConfigDict(extra="allow")

    figure_no: int
    prompt: str


class PatentDescription(BaseModel):
    """说明书（description）：五个子节，drawing_description 为逐图一句话说明。"""

    model_config = ConfigDict(extra="allow")

    # 四节正文必须非空：空串意味着生成调用漏写了小节，须回指 REPAIR 而不是静默出稿
    technical_field: str = Field(min_length=1)
    background: str = Field(min_length=1)
    invention_content: str = Field(min_length=1)
    drawing_description: list[str] = Field(default_factory=list)
    embodiments: str = Field(min_length=1)


class PatentContent(BaseModel):
    """完整专利内容契约（DOCX/PDF/附图脚本的唯一输入）。

    必填：invention_name / abstract / claims / description（document-generation.md
    「Required fields for a clean final document」）。
    """

    model_config = ConfigDict(extra="allow")

    invention_name: str = Field(min_length=1)
    source_title: str = ""
    abstract: str = Field(min_length=1)
    abstract_drawing: str = ""
    claims: list[str] = Field(min_length=1)
    description: PatentDescription
    drawings: list[str] = Field(default_factory=list)
    source_figures: list[SourceFigure] = Field(default_factory=list)
    drawing_assets: list[DrawingAsset] = Field(default_factory=list)
    image_model_prompts: list[ImageModelPrompt] = Field(default_factory=list)
    drawing_validation: list[DrawingValidation] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 二、B1 输入评估（input_assessment.md 输出契约）
# ---------------------------------------------------------------------------


class Completeness(BaseModel):
    """信息完整性评估。"""

    model_config = ConfigDict(extra="allow")

    title: bool = False
    abstract: bool = False
    method: bool = False
    effects: bool = False
    figures: bool = False
    overall: Literal["sufficient", "partial", "insufficient"] = "partial"
    note: str = ""


class Gap(BaseModel):
    """信息缺口（direct 模式转【待补充：…】占位；hil 模式作门控提问）。"""

    model_config = ConfigDict(extra="allow")

    item: str
    severity: Literal["blocking", "minor"] = "minor"
    affects: Literal["claims", "description", "drawings", "effects"] = "description"
    suggested_question: str = ""

    def placeholder(self) -> str:
        """转为可直接写进 gaps 的【待补充：…】文案。"""
        text = (self.suggested_question or self.item).strip()
        if text.startswith("【待补充"):
            return text
        return f"【待补充：{text.rstrip('。')}。】"


class InputAssessment(BaseModel):
    """B1 输入评估 STRUCT 产物。"""

    model_config = ConfigDict(extra="allow")

    completeness: Completeness = Field(default_factory=Completeness)
    patentability_clues: list[str] = Field(default_factory=list)
    fidelity_risks: list[str] = Field(default_factory=list)
    drawing_readiness: Literal["ready", "partial", "not_ready"] = "partial"
    gaps: list[Gap] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 三、B2 深读提取（extraction.md 输出契约）
# ---------------------------------------------------------------------------


class Contribution(BaseModel):
    """核心贡献条目。"""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    title: str = ""
    detail: str = ""


class MethodStep(BaseModel):
    """方法步骤条目（S1、S2…，编号与名称须全文一致）。"""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    label: str = ""
    detail: str = ""
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class SystemComponent(BaseModel):
    """系统模块条目（M1、M2…）。"""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    function: str = ""
    relations: list[str] = Field(default_factory=list)


class SourceMapEntry(BaseModel):
    """落源表条目：技术断言 → 论文原文逐字摘录。"""

    model_config = ConfigDict(extra="allow")

    claim_like: str = ""
    source_quote: str = ""


class Extraction(BaseModel):
    """B2 深读提取 STRUCT 产物（四要素闭环 + source_map）。"""

    model_config = ConfigDict(extra="allow")

    contributions: list[Contribution] = Field(default_factory=list)
    method_steps: list[MethodStep] = Field(default_factory=list)
    system_components: list[SystemComponent] = Field(default_factory=list)
    distinctions: list[str] = Field(default_factory=list)
    tech_problem: str = ""
    tech_effects: list[str] = Field(default_factory=list)
    source_map: list[SourceMapEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 四、B3a 权利要求书 / B3d 附图规格
# ---------------------------------------------------------------------------


class DraftClaims(BaseModel):
    """B3a 权项调用的尾部 JSON 契约（也用作权项 REPAIR 的输出契约）。"""

    model_config = ConfigDict(extra="allow")

    invention_name: str = Field(min_length=1)
    claims: list[str] = Field(min_length=1)


class DrawingSpecs(BaseModel):
    """B3d 附图规格 STRUCT 产物（原图优先）。"""

    model_config = ConfigDict(extra="allow")

    drawings: list[str] = Field(default_factory=list)
    source_figures: list[SourceFigure] = Field(default_factory=list)
    abstract_drawing: str = ""


class DescriptionSections(BaseModel):
    """说明书分节 REPAIR 的输出契约（按缺失节回填）。"""

    model_config = ConfigDict(extra="allow")

    technical_field: str = ""
    background: str = ""
    invention_content: str = ""
    drawing_description: list[str] = Field(default_factory=list)
    embodiments: str = ""


class AbstractDraft(BaseModel):
    """摘要 REPAIR 的输出契约。"""

    model_config = ConfigDict(extra="allow")

    abstract: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 五、B4b 忠实性审计 / B7 质检补丁
# ---------------------------------------------------------------------------


class FaithfulnessMapping(BaseModel):
    """断言 → 论文原文摘录的映射。"""

    model_config = ConfigDict(extra="allow")

    location: str = ""
    assertion: str = ""
    source_quote: str = ""
    status: Literal["sourced", "generalized"] = "sourced"


class UnsourcedAssertion(BaseModel):
    """落不了源的断言（direct 转占位、hil 交用户裁决）。"""

    model_config = ConfigDict(extra="allow")

    location: str = ""
    assertion: str = ""
    reason: Literal["no_support", "over_generalized", "contradicts_source"] = "no_support"
    suggested_action: Literal["delete", "narrow", "mark_placeholder"] = "mark_placeholder"
    suggested_text: str = ""


class FaithfulnessAudit(BaseModel):
    """B4b 忠实性审计 AUDIT 产物。"""

    model_config = ConfigDict(extra="allow")

    verdict: Literal["pass", "fail"] = "pass"
    mappings: list[FaithfulnessMapping] = Field(default_factory=list)
    unsourced: list[UnsourcedAssertion] = Field(default_factory=list)


PatchSection = Literal["abstract", "abstract_drawing", "claims", "description", "drawings"]


class QualityPatch(BaseModel):
    """质检补丁（锚点 + 逐字原文片段 + 替换文字）。"""

    model_config = ConfigDict(extra="allow")

    checklist_item: str = ""
    section: PatchSection = "description"
    anchor_before: str = ""
    original_excerpt: str = ""
    replacement: str = ""
    reason: str = ""


class QualityUnresolved(BaseModel):
    """无法用补丁解决的质检问题（呈现给用户，不入正文）。"""

    model_config = ConfigDict(extra="allow")

    checklist_item: str = ""
    issue: str = ""
    needs: Literal["missing_source", "user_decision", "rewrite_section"] = "user_decision"


class QualityAudit(BaseModel):
    """B7 质检 AUDIT 产物（补丁契约）。"""

    model_config = ConfigDict(extra="allow")

    verdict: Literal["pass", "fail"] = "pass"
    patches: list[QualityPatch] = Field(default_factory=list)
    unresolved: list[QualityUnresolved] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 六、流水线启动载荷与 API 契约
# ---------------------------------------------------------------------------

P2pMode = Literal["direct", "human_in_loop", "flash"]


class P2pStartPayload(BaseModel):
    """POST /pipeline/start 的模块 B 载荷。"""

    model_config = ConfigDict(extra="allow")

    file_id: str = ""                # 论文文件（M2 上传并转换后的 files.id）
    mode: P2pMode = "direct"
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("file_id", mode="before")
    @classmethod
    def _null_file_id(cls, v: Any) -> Any:
        """前端在"未选文件"时会传 null —— 按"未指定"处理，交由流水线回落到本案最近上传件。"""
        return "" if v is None else v

    @field_validator("mode", mode="before")
    @classmethod
    def _null_mode(cls, v: Any) -> Any:
        """同上：mode 传 null 时按默认 direct 处理。"""
        return "direct" if v is None else v


class P2pContentOut(BaseModel):
    """GET/PUT /cases/{id}/p2p/content 响应体。"""

    content: PatentContent
    artifact_id: str | None = None
    version: int | None = None
    filename: str | None = None
    lint: list[dict[str, Any]] = Field(default_factory=list)


class P2pContentIn(BaseModel):
    """PUT /cases/{id}/p2p/content 请求体。"""

    content: PatentContent


class P2pDrawingsOut(BaseModel):
    """POST /cases/{id}/p2p/drawings 响应体。"""

    figures: list[DrawingAsset] = Field(default_factory=list)
    degraded: list[int] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    artifact_id: str | None = None      # 回写后的 patent_content_json 版本


class P2pBuildOut(BaseModel):
    """POST /cases/{id}/p2p/build 响应体。"""

    docx: dict[str, Any] | None = None
    pdf: dict[str, Any] | None = None
    pdf_engine: str | None = None
    pdf_error: str | None = None
    placeholders: list[str] = Field(default_factory=list)


class P2pImagePromptsOut(BaseModel):
    """GET /cases/{id}/p2p/image-prompts 响应体。"""

    prompts: list[ImageModelPrompt] = Field(default_factory=list)
    template: str = ""                  # drawing_image_prompt.md 原文（供用户自行精修）
