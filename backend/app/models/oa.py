"""模块 D（审查意见答复）与 OA 案例库的 pydantic 契约。

两类模型：

1. **LLM 结构化输出契约**（`NoticeStruct` / `StrategyPlan` / `OaCaseDraft` /
   `ConsistencyAudit`）——枚举字段一律声明为 `str` 而非 `Literal`：条款/缺陷类型的
   枚举校验由服务端代码执行（`validate_statute` / `validate_defect_type`），非法值
   要连同错误清单走 REPAIR 重出（prompt-porting-spec.md R16「OA 条款幻觉」），
   若在 pydantic 层就拒掉，`llm.structured` 的通用重试会吞掉这条防线的可观测性。
2. **API 契约**（`OaLibraryItem` / `OaSearchOut` …）——前端 OAPage / OACasesPage 依据。

枚举基准：`assets/references/schemas/oa_case.schema.yaml`（案例库 frontmatter）
+ `assets/prompts/oa/notice_struct.md`（通知书结构化的 4 个平台扩充 defect_type）。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# 枚举表（oa_case.schema.yaml + notice_struct.md）
# ---------------------------------------------------------------------------

#: 案例库 frontmatter 的基准缺陷类型（oa_case.schema.yaml）
BASE_DEFECT_TYPES: tuple[str, ...] = (
    "novelty", "inventiveness", "clarity", "support", "disclosure", "formality", "other",
)

#: 平台扩充的缺陷类型（notice_struct.md 枚举表后 4 项）
EXTRA_DEFECT_TYPES: tuple[str, ...] = (
    "added_matter", "essential_features", "unity", "subject_matter",
)

#: 通知书结构化允许的缺陷类型全集
DEFECT_TYPES: tuple[str, ...] = (*BASE_DEFECT_TYPES, *EXTRA_DEFECT_TYPES)

DEFECT_TYPE_LABEL: dict[str, str] = {
    "novelty": "新颖性",
    "inventiveness": "创造性",
    "clarity": "权利要求不清楚",
    "support": "得不到说明书支持",
    "disclosure": "说明书公开不充分",
    "formality": "形式缺陷/补正事项",
    "other": "其他",
    "added_matter": "修改超范围",
    "essential_features": "缺少必要技术特征",
    "unity": "单一性",
    "subject_matter": "不属于专利保护客体",
}

#: 扩充缺陷类型 → 案例库基准枚举的归并（检索过滤时用，notice_struct.md 注记）
DEFECT_TYPE_TO_BASE: dict[str, str] = {
    "added_matter": "other",
    "essential_features": "other",
    "unity": "other",
    "subject_matter": "other",
}

NOTICE_KINDS: tuple[str, ...] = ("office_action", "correction", "rejection", "other")
NOTICE_KIND_LABEL: dict[str, str] = {
    "office_action": "审查意见通知书",
    "correction": "补正通知书",
    "rejection": "驳回决定",
    "other": "其他通知",
}

PATENT_TYPES: tuple[str, ...] = ("invention", "utility_model", "design")
PATENT_TYPE_LABEL: dict[str, str] = {
    "invention": "发明", "utility_model": "实用新型", "design": "外观设计",
}

OUTCOMES: tuple[str, ...] = (
    "granted", "rejected", "pending", "withdrawn", "unknown", "amended_then_granted",
)

#: 案例库 frontmatter 的 strategy 枚举（oa_case.schema.yaml，含 amend_spec / correction）
CASE_STRATEGIES: tuple[str, ...] = (
    "argue_only", "amend_claims", "amend_spec", "correction", "other",
)

#: D3 答复策略枚举（strategy.md）
RESPONSE_STRATEGIES: tuple[str, ...] = (
    "argue_only", "amend_claims", "amend_description", "formal_fix",
)

RESPONSE_STRATEGY_LABEL: dict[str, str] = {
    "argue_only": "仅意见陈述",
    "amend_claims": "修改权利要求",
    "amend_description": "修改说明书",
    "formal_fix": "补正形式",
}

#: D3 策略 → 案例库 frontmatter strategy 的映射（归档回库时用）
RESPONSE_TO_CASE_STRATEGY: dict[str, str] = {
    "argue_only": "argue_only",
    "amend_claims": "amend_claims",
    "amend_description": "amend_spec",
    "formal_fix": "correction",
}

#: 案例库行的人审闸门状态（DB CHECK 约束：confirmed 才向量化）
LIBRARY_STATUSES: tuple[str, ...] = ("draft", "confirmed")

#: oa_case.schema.yaml 的 status 别名（history=已结案可检索 → confirmed；pending=待答复 → draft）
LIBRARY_STATUS_ALIASES: dict[str, str] = {
    "history": "confirmed",
    "pending": "draft",
    "draft": "draft",
    "confirmed": "confirmed",
}

#: 通知书未写明法条时的占位（notice_struct.md 第 3 条红线）
UNSPECIFIED_STATUTE = "未明示"

#: 常见法条白名单（notice_struct.md「常见对应条款」列 + oa_case.schema.yaml 示例）
KNOWN_STATUTES: tuple[str, ...] = (
    "专利法第2条", "专利法第5条", "专利法第9条", "专利法第20条",
    "专利法第22条第2款", "专利法第22条第3款", "专利法第22条第4款",
    "专利法第25条", "专利法第26条第3款", "专利法第26条第4款",
    "专利法第31条第1款", "专利法第33条",
    "专利法实施细则第20条第2款", "专利法实施细则第23条",
    "实施细则第23条",
)

#: 合法条款表述的形态（照抄通知书原文时的常见书写；不匹配即视为幻觉/走样，触发 REPAIR）
_STATUTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^专利法第\d{1,3}条(之[一二三四五六七八九十\d]{1,3})?"
               r"(第[一二三四五六七八九十百零\d]{1,4}款)?(第[一二三四五六七八九十百零\d]{1,4}项)?$"),
    re.compile(r"^(专利法)?实施细则第\d{1,3}条"
               r"(第[一二三四五六七八九十百零\d]{1,4}款)?(第[一二三四五六七八九十百零\d]{1,4}项)?$"),
    re.compile(r"^专利审查指南第[\d一二三四五六七八九十]{1,3}部分"
               r"第[\d一二三四五六七八九十]{1,3}章第[\d\.]{1,8}节$"),
)


def validate_statute(value: str) -> str | None:
    """校验条款表述；合法返回 None，非法返回错误说明（供 REPAIR 反馈）。

    合法 = 「未明示」 / 白名单 / 形如「专利法第22条第3款」「专利法实施细则第20条第2款」
    「专利审查指南第二部分第四章第3.2节」。**不做补全或改写**（R16：条款一律照抄原文）。
    """
    text = (value or "").strip()
    if not text:
        return "statute 为空：须照抄通知书原文条款；通知书未写明时填「未明示」"
    if text == UNSPECIFIED_STATUTE or text in KNOWN_STATUTES:
        return None
    if any(p.match(text) for p in _STATUTE_PATTERNS):
        return None
    return (
        f"statute「{text}」不是合法的条款表述：须照抄通知书原文（如「专利法第22条第3款」"
        f"「专利法实施细则第20条第2款」），通知书未写明法条时填「{UNSPECIFIED_STATUTE}」，"
        "不得臆造、简写或改写"
    )


def validate_defect_type(value: str) -> str | None:
    """校验缺陷类型枚举；合法返回 None，非法返回错误说明（供 REPAIR 反馈）。"""
    text = (value or "").strip()
    if text in DEFECT_TYPES:
        return None
    return (
        f"defect_type「{text or '（空）'}」不在枚举内，允许值："
        + "、".join(DEFECT_TYPES)
    )


def validate_response_strategy(value: str) -> str | None:
    """校验答复策略枚举；合法返回 None，非法返回错误说明。"""
    text = (value or "").strip()
    if text in RESPONSE_STRATEGIES:
        return None
    return (
        f"strategy「{text or '（空）'}」不在枚举内，允许值："
        + "、".join(RESPONSE_STRATEGIES)
    )


def normalize_issue_id(value: Any, index: int = 0) -> str:
    """issue id 归一化为 `I{n}`（兼容 prompt 契约的 `issue-1` 与裸数字）。"""
    text = str(value or "").strip()
    m = re.fullmatch(r"(?:issue[-_ ]?|I|i|第)?(\d{1,3})(?:条)?", text)
    if m:
        return f"I{int(m.group(1))}"
    return text or f"I{index + 1}"


def expand_claim_numbers(value: Any) -> list[int]:
    """把 `[1,2]` / `"1-3"` / `["1","3至5"]` 统一展开为升序去重的权项号列表。"""
    out: list[int] = []

    def _add(num: int) -> None:
        if 0 < num < 1000 and num not in out:
            out.append(num)

    items = value if isinstance(value, (list, tuple)) else [value]
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            _add(item)
            continue
        text = str(item or "").strip()
        if not text:
            continue
        for part in re.split(r"[、,，;；\s]+", text):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(?:权利要求)?(\d{1,3})\s*[-–—~～至到]\s*(\d{1,3})", part)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                for n in range(min(a, b), max(a, b) + 1):
                    _add(n)
                continue
            m = re.search(r"(\d{1,3})", part)
            if m:
                _add(int(m.group(1)))
    return sorted(out)


# ---------------------------------------------------------------------------
# D1 通知书结构化（notice_struct.md）
# ---------------------------------------------------------------------------


class CitedDocument(BaseModel):
    """对比文件条目。

    兼容两套字段名：prompt 契约的 `{ref_label, pub_no}` 与前端契约的
    `{code, identifier, used_against}`（`code` 形如 `D1`）。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    code: str = ""                       # 「D1」（由 ref_label 归一化而来）
    ref_label: str = ""                  # 通知书原文标号，如「对比文件1」
    identifier: str = ""                 # 公开号，如 CN111111111A
    used_against: list[int] = Field(default_factory=list)   # 被用于评述的权项号

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        label = str(out.get("ref_label") or out.get("label") or "").strip()
        code = str(out.get("code") or "").strip()
        if not code and label:
            m = re.search(r"(\d{1,2})", label)
            code = f"D{int(m.group(1))}" if m else label
        if not label and code:
            m = re.search(r"(\d{1,2})", code)
            label = f"对比文件{int(m.group(1))}" if m else code
        out["code"] = code
        out["ref_label"] = label
        ident = out.get("identifier")
        if ident in (None, ""):
            ident = out.get("pub_no") or out.get("pub_number") or ""
        out["identifier"] = str(ident or "").strip()
        out["used_against"] = expand_claim_numbers(out.get("used_against") or [])
        return out


class NoticeIssue(BaseModel):
    """通知书中的一条缺陷。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    statute: str = ""                    # 照抄通知书原文；服务端做 validate_statute
    defect_type: str = ""                # 服务端做 validate_defect_type
    inferred: bool = False               # statute 未明示、类型靠推断时置 true
    claims_involved: list[int] = Field(default_factory=list)
    examiner_argument_digest: str = ""
    cited_documents: list[CitedDocument] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out["id"] = normalize_issue_id(out.get("id"))
        out["claims_involved"] = expand_claim_numbers(out.get("claims_involved") or [])
        docs = out.get("cited_documents")
        if isinstance(docs, dict):
            out["cited_documents"] = [docs]
        elif not isinstance(docs, list):
            out["cited_documents"] = []
        return out

    @property
    def defect_label(self) -> str:
        return DEFECT_TYPE_LABEL.get(self.defect_type, self.defect_type or "其他")

    def claims_text(self) -> str:
        """「权利要求1-3」式的可读表述（无涉及权项时返回「说明书/摘要」）。"""
        if not self.claims_involved:
            return "说明书/摘要"
        nums = self.claims_involved
        if len(nums) == 1:
            return f"权利要求{nums[0]}"
        if nums == list(range(nums[0], nums[-1] + 1)):
            return f"权利要求{nums[0]}-{nums[-1]}"
        return "权利要求" + "、".join(str(n) for n in nums)


class NoticeStruct(BaseModel):
    """D1 通知书结构化产物（notice_struct.md 输出契约）。"""

    model_config = ConfigDict(extra="ignore")

    notice_kind: str = "office_action"
    notice_no: int | None = None
    notice_kind_note: str = ""
    application_no: str = ""
    patent_type: str | None = None
    deadline: str | None = None
    issues: list[NoticeIssue] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for key in ("deadline", "patent_type"):
            value = out.get(key)
            if isinstance(value, str) and value.strip().lower() in ("null", "none", "未载明", ""):
                out[key] = None
        issues = out.get("issues")
        if isinstance(issues, list):
            fixed = []
            for i, item in enumerate(issues):
                if isinstance(item, dict):
                    item = dict(item)
                    item["id"] = normalize_issue_id(item.get("id"), i)
                fixed.append(item)
            out["issues"] = fixed
        return out

    def notice_label(self) -> str:
        label = NOTICE_KIND_LABEL.get(self.notice_kind, "通知书")
        if self.notice_kind == "office_action" and self.notice_no:
            return f"第{self.notice_no}次{label}"
        return label


def lint_notice_struct(notice: NoticeStruct) -> list[str]:
    """通知书结构化的枚举/完整性校验（R16 防条款幻觉）；返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    if notice.notice_kind not in NOTICE_KINDS:
        problems.append(
            f"notice_kind「{notice.notice_kind}」不在枚举内，允许值：" + "、".join(NOTICE_KINDS)
        )
    if notice.patent_type is not None and notice.patent_type not in PATENT_TYPES:
        problems.append(
            f"patent_type「{notice.patent_type}」不在枚举内，允许值：" + "、".join(PATENT_TYPES)
        )
    if notice.deadline and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", notice.deadline.strip()):
        problems.append(f"deadline「{notice.deadline}」格式非法：须为 YYYY-MM-DD 或 null")
    if not notice.issues:
        problems.append("issues 为空：通知书至少应拆出一条缺陷条目")
    seen: set[str] = set()
    for issue in notice.issues:
        tag = issue.id or "（无编号）"
        if tag in seen:
            problems.append(f"issue id「{tag}」重复")
        seen.add(tag)
        err = validate_statute(issue.statute)
        if err:
            problems.append(f"{tag}：{err}")
        err = validate_defect_type(issue.defect_type)
        if err:
            problems.append(f"{tag}：{err}")
        if not issue.examiner_argument_digest.strip():
            problems.append(f"{tag}：examiner_argument_digest 为空")
        elif len(issue.examiner_argument_digest) > 400:
            problems.append(f"{tag}：examiner_argument_digest 超过 400 字（要求 ≤200 字消化改写）")
    return problems


# ---------------------------------------------------------------------------
# D3 策略选项（strategy.md）
# ---------------------------------------------------------------------------


class StrategyOption(BaseModel):
    """一条可勾选的答复策略。"""

    model_config = ConfigDict(extra="ignore")

    strategy: str = ""
    rationale: str = ""
    risk: str = ""
    amendment_sketch: str = ""


class IssueStrategy(BaseModel):
    """单条 issue 的策略选项集合。"""

    model_config = ConfigDict(extra="ignore")

    issue_id: str = ""
    options: list[StrategyOption] = Field(default_factory=list)
    recommended: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict) and "issue_id" in data:
            data = dict(data)
            data["issue_id"] = normalize_issue_id(data.get("issue_id"))
        return data


class StrategyPlan(BaseModel):
    """多 issue 策略规划（strategy.md 的 `{"strategies": [...]}`）。"""

    model_config = ConfigDict(extra="ignore")

    strategies: list[IssueStrategy] = Field(default_factory=list)


def lint_issue_strategy(plan: IssueStrategy) -> list[str]:
    """策略选项的枚举与自洽校验；返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    if not plan.options:
        problems.append(f"{plan.issue_id or '该 issue'}：options 为空，至少给出 1 个策略选项")
    values: list[str] = []
    for opt in plan.options:
        err = validate_response_strategy(opt.strategy)
        if err:
            problems.append(f"{plan.issue_id}：{err}")
        else:
            values.append(opt.strategy)
        if not opt.rationale.strip():
            problems.append(f"{plan.issue_id}：策略「{opt.strategy}」缺少 rationale")
        if opt.strategy in ("amend_claims", "amend_description") and not opt.risk.strip():
            problems.append(
                f"{plan.issue_id}：修改类策略「{opt.strategy}」的 risk 为空，"
                "必须评估修改超范围（专利法第33条）风险"
            )
    if plan.recommended and plan.recommended not in values:
        problems.append(
            f"{plan.issue_id}：recommended「{plan.recommended}」不在本 issue 的 options 内"
        )
    return problems


# ---------------------------------------------------------------------------
# D5 跨条一致性复核（AUDIT）
# ---------------------------------------------------------------------------


class ConsistencyFinding(BaseModel):
    """一条跨条一致性发现。"""

    model_config = ConfigDict(extra="ignore")

    severity: str = "warning"            # error | warning | info
    message: str = ""
    issue_ids: list[str] = Field(default_factory=list)
    claim_no: int | None = None


class ConsistencyAudit(BaseModel):
    """跨条一致性 AUDIT 产物。"""

    model_config = ConfigDict(extra="ignore")

    verdict: str = "pass"                # pass | revise
    findings: list[ConsistencyFinding] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# 案例库（ingest_case.md + oa_case.schema.yaml）
# ---------------------------------------------------------------------------


class Redaction(BaseModel):
    """一处脱敏登记（人审时逐项确认；original_hint 只写类别不复述原文）。"""

    model_config = ConfigDict(extra="ignore")

    original_hint: str = ""
    replaced_with: str = ""
    reason: str = ""


class OaCaseFrontmatter(BaseModel):
    """案例笔记 frontmatter（oa_case.schema.yaml 合同）。"""

    model_config = ConfigDict(extra="allow")

    case_id: str = ""
    title: str = ""
    status: str = "draft"                # 平台人审闸门：draft | confirmed
    patent_type: str = "invention"
    statutes: list[str] = Field(default_factory=list)
    defect_types: list[str] = Field(default_factory=list)
    domain: str = ""
    notice_kind: str = "office_action"
    outcome: str = "unknown"
    strategy: list[str] = Field(default_factory=list)
    compare_refs: list[str] = Field(default_factory=list)
    related_cases: list[str] = Field(default_factory=list)
    redacted: bool = True
    tags: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @field_validator("statutes", "defect_types", "strategy", "compare_refs",
                     "related_cases", "tags", "source_paths", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class OaCaseDraft(BaseModel):
    """D-ingest 草稿生成产物（ingest_case.md 输出契约）。"""

    model_config = ConfigDict(extra="ignore")

    frontmatter: OaCaseFrontmatter = Field(default_factory=OaCaseFrontmatter)
    note_md: str = ""
    redactions: list[Redaction] = Field(default_factory=list)
    inferred_fields: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 案例库 API 契约
# ---------------------------------------------------------------------------


class OaLibraryItem(BaseModel):
    """案例库列表项。"""

    id: str
    case_id: str = ""
    title: str = ""
    status: Literal["draft", "confirmed"] = "draft"
    embedded: bool = False
    chunk_count: int = 0
    patent_type: str = ""
    statutes: list[str] = Field(default_factory=list)
    defect_types: list[str] = Field(default_factory=list)
    domain: str = ""
    outcome: str = ""
    tags: list[str] = Field(default_factory=list)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class OaLibraryDetail(OaLibraryItem):
    """案例库详情（含笔记正文与脱敏登记）。"""

    note_md: str = ""
    note_full_md: str = ""               # frontmatter + 正文的完整笔记
    redactions: list[Redaction] = Field(default_factory=list)
    inferred_fields: list[str] = Field(default_factory=list)
    chunks: list[str] = Field(default_factory=list)
    embed_error: str = ""


class OaLibraryUpdateIn(BaseModel):
    """PUT /oa/library/{id} 请求体（字段全可选；status='confirmed' 触发切块+嵌入）。"""

    model_config = ConfigDict(extra="ignore")

    status: Literal["draft", "confirmed"] | None = None
    note_md: str | None = None
    frontmatter: dict[str, Any] | None = None


class OaLibraryIngestOut(BaseModel):
    """POST /oa/library/ingest 响应。"""

    item: OaLibraryDetail
    redactions: list[Redaction] = Field(default_factory=list)
    inferred_fields: list[str] = Field(default_factory=list)
    normalized: list[str] = Field(default_factory=list)   # 被服务端归一化的非法枚举说明
    materials: list[str] = Field(default_factory=list)    # 参与生成的材料文件名


class OaSearchHit(BaseModel):
    """一条案例检索命中。"""

    library_id: str
    case_id: str = ""
    title: str = ""
    chunk_id: int = 0
    chunk_index: int = 0
    chunk_text: str = ""
    score: float = 0.0
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class OaSearchOut(BaseModel):
    """GET /oa/library/search 响应；`retrieval_mode` 必须透出给前端明示检索方式。"""

    retrieval_mode: Literal["vector", "brute", "keyword"] = "keyword"
    query: str = ""
    k: int = 5
    total_candidates: int = 0
    hits: list[OaSearchHit] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    error: str = ""


class OaRebuildOut(BaseModel):
    """POST /oa/library/rebuild 响应（后台任务已排队）。"""

    ok: bool = True
    queued: int = 0
    dim: int = 0
    mode: Literal["vector", "brute", "keyword"] = "keyword"
    vec_available: bool = False
    message: str = ""
