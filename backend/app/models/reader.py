"""专利解读（模块 C）数据契约模型。

对应 prompt-porting-spec.md §2 模块 C 的五步流水线：

- **C2 权要树**（`reader/claim_tree.md` STRUCT）→ :class:`ClaimTree`
  （`claims[{no,type,refs,ref_mode,preamble,features[{fid,text}]}]` + `ambiguities`）；
- **C3 白话增量**（`reader/claim_deltas.md` STRUCT）→ :class:`ClaimDeltas`；
- **C4 落地线索**（`reader/grounding.md` STRUCT）→ :class:`Grounding`；
- **C0 判型兜底**（`reader/type_hooks.md` STRUCT）→ :class:`PatentTypeGuess`；
- **C7 自检**（`reader/self_check.md` AUDIT）→ :class:`ReaderSelfCheck`（补丁契约 §3.3）；
- **C6 装配**（服务端确定性）→ :class:`ReaderReport`（`report.json`：
  `{meta, sections:[{id,title,blocks:[{type,content}]}]}`），前端按 block 类型渲染。

宽容原则（R6）：除 `claims` / `deltas` 这类「没有就没法往下走」的字段外一律给缺省值，
模型少吐一个可选键不至于让整步失败；模型多吐的键用 `extra="allow"` 保留（如 prompt
契约里的 `kind/candidates/chosen/reason`），便于把选型理由原样呈现给用户。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# 落地线索固定标注文字（grounding.md 硬性要求逐字一致）
GROUNDING_DISCLAIMER = "推断自公开文本，未联网核验"

# 无可靠线索时的固定话术（report_template.md 第十节 B）
NO_CLUE_NOTE = "未发现可核验的公开对应，可能为防御性/储备专利。"

# 白话增量长度区间（claim_deltas.md：每句 12～40 字）
DELTA_MIN_CHARS = 12
DELTA_MAX_CHARS = 40


# ---------------------------------------------------------------------------
# 一、启动载荷
# ---------------------------------------------------------------------------


class ReaderStartPayload(BaseModel):
    """模块 C 启动载荷：`{pub_no?, file_id?}`。

    两者皆缺（或抓取失败）时由 acquire 步骤走 `await_user` 请用户手工上传 PDF，
    因此此处**不**做「二选一」的硬校验。
    """

    model_config = ConfigDict(extra="allow")

    pub_no: str = Field(default="", description="公开号/公告号，如 CN209861402U")
    file_id: str = Field(default="", description="已上传专利 PDF 的 files.id（优先于 pub_no）")
    patent_type: str = Field(default="", description="用户显式声明的专利类型（覆盖自动判定）")
    # 视角只影响报告口吻（入门 / 研发 / 规避），写错不该炸掉整条流水线，故不做 Literal 约束
    perspective: str = "研发"


# ---------------------------------------------------------------------------
# 二、C2 权利要求树
# ---------------------------------------------------------------------------


def coerce_int_list(value: Any) -> list[int]:
    """把 refs / candidates 之类的字段收敛为 int 列表（保序去重，跳过非数字项）。

    模型常见的走样：吐单个整数、吐字符串 `"1"`、吐 `"权利要求1"`、混入 `null`。
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []
    for item in items:
        try:
            num = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if num not in out:
            out.append(num)
    return out


class ClaimFeature(BaseModel):
    """一条技术特征（「其特征在于」之后按分号/语义拆出的最小单元）。"""

    model_config = ConfigDict(extra="allow")

    fid: str = Field(default="", description="特征编号，如 F1 或 1-F1")
    text: str = Field(default="", description="原文逐字片段")

    @property
    def short_fid(self) -> str:
        """报告特征表里用的短编号（`1-F2` → `F2`）。"""
        raw = (self.fid or "").strip()
        if "-" in raw:
            tail = raw.rsplit("-", 1)[-1].strip()
            if tail:
                return tail
        return raw


class ClaimNode(BaseModel):
    """一项权利要求。"""

    model_config = ConfigDict(extra="allow")

    no: int = Field(description="权利要求编号（1 起）")
    type: Literal["independent", "dependent"] = "dependent"
    refs: list[int] = Field(default_factory=list, description="被引用的权号（主引用在首位）")
    ref_mode: Literal["none", "single", "alternative", "range", "multiple"] = "none"
    preamble: str = ""
    features: list[ClaimFeature] = Field(default_factory=list)

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> list[int]:
        return coerce_int_list(value)

    @property
    def is_multi_ref(self) -> bool:
        """是否为多项引用（引用两项以上，或 ref_mode 明示多引）。"""
        return len(self.refs) > 1 or self.ref_mode in ("alternative", "range", "multiple")

    def feature_text(self) -> str:
        """全部特征拼成的一段文字（服务端 lint 与上下文注入用）。"""
        return "；".join(f.text.strip() for f in self.features if f.text.strip())

    def full_text(self) -> str:
        """权利要求原文近似还原（前序 + 特征）。"""
        parts = [self.preamble.strip(), self.feature_text()]
        return "".join(p for p in parts if p)


class ClaimAmbiguity(BaseModel):
    """一条疑难登记（多引用消歧 / 跳号 / 引用基础存疑 / OCR 疑似错字）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # prompt 契约用 `claim`，平台内部与任务书契约用 `no`：两种键都收
    no: int = Field(default=0, validation_alias=AliasChoices("no", "claim", "claim_no"))
    kind: str = "other"
    issue: str = ""
    candidates: list[int] = Field(default_factory=list)
    chosen: int | None = None
    reason: str = ""

    @field_validator("candidates", mode="before")
    @classmethod
    def _coerce_candidates(cls, value: Any) -> list[int]:
        return coerce_int_list(value)


class ClaimTree(BaseModel):
    """`reader/claim_tree.md` 的 STRUCT 输出契约。"""

    model_config = ConfigDict(extra="allow")

    claims: list[ClaimNode] = Field(default_factory=list)
    ambiguities: list[ClaimAmbiguity] = Field(default_factory=list)

    def by_no(self) -> dict[int, ClaimNode]:
        return {c.no: c for c in self.claims}

    def independents(self) -> list[ClaimNode]:
        return [c for c in self.claims if c.type == "independent"]

    def dependents(self) -> list[ClaimNode]:
        return [c for c in self.claims if c.type != "independent"]

    def summary_line(self) -> str:
        """第三节导语：「共 N 项 · 独立 x / 从属 y。独立权展开见第四节。」"""
        return (
            f"共 {len(self.claims)} 项 · 独立 {len(self.independents())} / "
            f"从属 {len(self.dependents())}。独立权展开见第四节。"
        )


class ClaimDelta(BaseModel):
    """一项权利要求的白话增量（12～40 字）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    claim: int = Field(default=0, validation_alias=AliasChoices("claim", "no", "claim_no"))
    delta: str = Field(default="", validation_alias=AliasChoices("delta", "text"))

    @property
    def length(self) -> int:
        """折叠空白后的字数（长度校验口径）。"""
        return len("".join((self.delta or "").split()))


class ClaimDeltas(BaseModel):
    """`reader/claim_deltas.md` 的 STRUCT 输出契约。"""

    model_config = ConfigDict(extra="allow")

    deltas: list[ClaimDelta] = Field(default_factory=list)

    def by_claim(self) -> dict[int, str]:
        return {d.claim: d.delta.strip() for d in self.deltas}


# ---------------------------------------------------------------------------
# 三、C4 落地线索（离线，anchor_fits 必须锚定说明书段落号）
# ---------------------------------------------------------------------------


class AnchorFit(BaseModel):
    """一条锚定：把场景对应到本案的特征 / 权号 / 术语 + 说明书段落号。"""

    model_config = ConfigDict(extra="allow")

    kind: Literal["feature", "claim", "term"] = "feature"
    key: str = ""
    paragraph: str = Field(default="", description="四位段落号，如 0056；区间写 0056-0058")
    fit: str = ""


class GroundingClue(BaseModel):
    """一条应用场景线索（离线推断，禁 URL）。"""

    model_config = ConfigDict(extra="allow")

    scene: str = ""
    plain: str = ""
    anchor_fits: list[AnchorFit] = Field(default_factory=list)
    confidence: Literal["高", "中", "低"] = "中"
    disclaimer: str = GROUNDING_DISCLAIMER


class IpcCoordinate(BaseModel):
    """一条 IPC 行业坐标。"""

    model_config = ConfigDict(extra="allow")

    ipc_prefix: str = ""
    industry: str = ""
    basis: Literal["hints_match", "text_inference"] = "text_inference"


class Grounding(BaseModel):
    """`reader/grounding.md` 的 STRUCT 输出契约。"""

    model_config = ConfigDict(extra="allow")

    ipc_coordinates: list[IpcCoordinate] = Field(default_factory=list)
    clues: list[GroundingClue] = Field(default_factory=list)
    no_clue_note: str = ""


# ---------------------------------------------------------------------------
# 四、C0 判型兜底 / C7 自检
# ---------------------------------------------------------------------------


class PatentTypeGuess(BaseModel):
    """`reader/type_hooks.md` 的 STRUCT 输出契约（服务端判不出时的兜底）。"""

    model_config = ConfigDict(extra="allow")

    patent_type: Literal["invention", "utility_model", "design", "unknown"] = "unknown"
    basis: str = ""
    evidence: str = ""
    confidence: Literal["高", "中", "低"] = "低"


class ReaderPatch(BaseModel):
    """自检补丁一条（§3.3 契约；由 services/patches 确定性应用）。"""

    model_config = ConfigDict(extra="allow")

    checklist_item: str = ""
    section: str = ""
    anchor_before: str = ""
    original_excerpt: str = ""
    replacement: str = ""
    reason: str = ""


class ReaderUnresolved(BaseModel):
    """自检中无法用补丁解决的条目（呈现给用户，不入正文）。"""

    model_config = ConfigDict(extra="allow")

    checklist_item: str = ""
    issue: str = ""
    needs: str = ""

    def as_text(self) -> str:
        head = self.checklist_item.strip() or "自检"
        body = self.issue.strip() or self.needs.strip()
        return f"[{head}] {body}".strip()


class ReaderSelfCheck(BaseModel):
    """`reader/self_check.md` 的 AUDIT 输出契约。

    源 prompt 的 verdict 取 `pass | fail`，但模型常吐 `patched` 等近义词，
    故此处不做 Literal 约束（是否有问题以 patches/unresolved 是否为空为准）。
    """

    model_config = ConfigDict(extra="allow")

    verdict: str = "pass"
    patches: list[ReaderPatch] = Field(default_factory=list)
    unresolved: list[ReaderUnresolved] = Field(default_factory=list)

    @field_validator("unresolved", mode="before")
    @classmethod
    def _coerce_unresolved(cls, value: Any) -> list[Any]:
        """容忍模型把 unresolved 写成字符串数组。"""
        if not isinstance(value, list):
            return []
        return [{"issue": v} if isinstance(v, str) else v for v in value]


# ---------------------------------------------------------------------------
# 五、C6 报告契约（report.json）
# ---------------------------------------------------------------------------

BlockType = Literal[
    "markdown",
    "table",
    "claim_tree",
    "mermaid",
    "callout-meta",
    "callout-claim",
    "callout-grounding",
    "callout-warning",
]

# report_template.md「callout → 站内 block 类型」映射表（左列为模板记号里的名字）
CALLOUT_BLOCK_TYPES: dict[str, BlockType] = {
    "patent-meta": "callout-meta",
    "meta": "callout-meta",
    "patent-claim": "callout-claim",
    "claim": "callout-claim",
    "grounding": "callout-grounding",
    "warning": "callout-warning",
    "caution": "callout-warning",
    "danger": "callout-warning",
}


class ReportBlock(BaseModel):
    """报告的一个渲染块。

    `content` 的形态随 `type` 变化：
    - `markdown` / `mermaid`：字符串；
    - `table`：`{headers:[], rows:[[]], markdown:""}`；
    - `claim_tree`：`{summary, rows:[{structure,no,type,delta}], markdown}`；
    - `callout-*`：`{title, markdown}`。
    """

    model_config = ConfigDict(extra="allow")

    type: BlockType = "markdown"
    content: Any = ""


class ReportSection(BaseModel):
    """报告的一节（11 节结构中的一节）。"""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    blocks: list[ReportBlock] = Field(default_factory=list)


class ReaderMeta(BaseModel):
    """报告 meta（替代源模板的 Obsidian frontmatter）。"""

    model_config = ConfigDict(extra="allow")

    pub_no: str = ""
    type: str = ""                       # invention | utility_model | design | ""
    type_label: str = ""                 # 发明 / 实用新型 / 外观设计
    ipc: list[str] = Field(default_factory=list)
    title: str = ""
    applicants: list[str] = Field(default_factory=list)
    domain: str = ""
    pub_date: str = ""
    read_date: str = ""
    perspective: str = "研发"
    evidence_scope: Literal["full_text", "abstract_only", "partial"] = "full_text"
    confidence_speculative: bool = False


class ReaderReport(BaseModel):
    """站内解读报告（report.json）。"""

    model_config = ConfigDict(extra="allow")

    meta: ReaderMeta = Field(default_factory=ReaderMeta)
    sections: list[ReportSection] = Field(default_factory=list)

    def section(self, section_id: str) -> ReportSection | None:
        return next((s for s in self.sections if s.id == section_id), None)


# ---------------------------------------------------------------------------
# 六、API 出参
# ---------------------------------------------------------------------------


class ReaderReportOut(BaseModel):
    """`GET /cases/{id}/reader/report` 出参。"""

    report: ReaderReport | None = None
    markdown: str | None = None
    artifact_id: str | None = None
    version: int | None = None
    filename: str | None = None
    lint: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class ReaderClaimTreeOut(BaseModel):
    """`GET /cases/{id}/reader/claim-tree` 出参。"""

    tree: ClaimTree | None = None
    deltas: list[ClaimDelta] = Field(default_factory=list)
    summary: str = ""
    lint: list[dict[str, Any]] = Field(default_factory=list)
    ambiguities_resolved: bool = False
