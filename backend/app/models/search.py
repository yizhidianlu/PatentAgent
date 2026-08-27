"""查新（CNIPA 检索 / 人工兜底）API 与服务层契约模型。

对应 backend-architecture.md §2 的 `search_queries` / `search_hits` 两张表与 §3.5 的
五个端点。硬规则（prompt-porting-spec §2 A4 / R8）：

- `url` 一律**照抄**国知局条目的 `link` 字段，禁止拼接或编造；人工录入同样要求可访问 URL；
- 检索失败（超时 / WAF / 零解析）不抛异常，`SearchResult.status='failed'`，
  由流水线转人工兜底门控（重试 / 粘贴在先文献 / 跳过）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SearchSource = Literal["cnipa", "manual", "fallback_web"]
SearchStatus = Literal["running", "done", "failed", "manual_pending"]
PatentTypeParam = Literal["invention", "utility_model", "design", "all"]

# 单次会话最多检索词数（与 tools/cnipa_epub_search.py 的 _MAX_TERMS 一致）
MAX_TERMS = 8


# ---------------------------------------------------------------------------
# 表行映射
# ---------------------------------------------------------------------------


class SearchHit(BaseModel):
    """一条检索命中（search_hits 行）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    query_id: str | None = None
    case_id: str
    pub_no: str | None = None
    title: str | None = None
    abstract: str | None = None
    applicant: str | None = None
    pub_date: str | None = None
    url: str
    selected: bool = True
    manual_entry: bool = False
    digest: str | None = None
    created_at: str = ""


class SearchQuery(BaseModel):
    """一次检索会话（search_queries 行；raw_json 不外露给前端）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    case_id: str
    source: SearchSource
    patent_type: str | None = None
    terms: list[str] = Field(default_factory=list)
    status: SearchStatus
    error: str | None = None
    created_at: str = ""
    hit_count: int = 0
    cached: bool = False
    skipped: bool = False


# ---------------------------------------------------------------------------
# 服务层返回
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    """`services.cnipa.search()` 的返回：**永不抛网络异常**，失败即 status='failed'。"""

    model_config = ConfigDict(extra="ignore")

    status: Literal["done", "failed"]
    hits: list[SearchHit] = Field(default_factory=list)
    error: str | None = None
    query_id: str | None = None
    terms: list[str] = Field(default_factory=list)
    patent_type: str = "all"
    cached: bool = False
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        """检索是否成功（供调用方门控判断）。"""
        return self.status == "done"


class BrowserProbe(BaseModel):
    """浏览器探测结果（tools/browser.py --probe 的 JSON）。"""

    model_config = ConfigDict(extra="allow")

    ok: bool = False
    channel: str | None = None          # 'chrome' | 'msedge' | 'chromium'
    playwright: bool = False
    error: str | None = None
    hint: str | None = None


# ---------------------------------------------------------------------------
# 请求 / 响应体
# ---------------------------------------------------------------------------


class SearchStartIn(BaseModel):
    """POST /cases/{id}/search/cnipa 请求体。"""

    terms: list[str] = Field(default_factory=list, description="检索词（一次会话最多 8 个）")
    patent_type: PatentTypeParam | None = Field(
        default=None, description="缺省取案件的 patent_type，再缺省 all"
    )
    use_cache: bool = Field(default=True, description="6 小时内同 terms+type 复用历史结果")

    @field_validator("terms")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not [t for t in v if str(t).strip()]:
            raise ValueError("terms 不能为空")
        return v


class SearchStartOut(BaseModel):
    """POST /cases/{id}/search/cnipa 响应（202；检索在后台任务中推进）。"""

    query_id: str | None = None
    status: Literal["running", "done", "failed"] = "running"
    terms: list[str] = Field(default_factory=list)
    patent_type: str = "all"
    cached: bool = False


class ManualHitIn(BaseModel):
    """人工兜底录入的一条在先文献（url 必填且须可访问）。"""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1, description="公开文本 URL，照抄来源，不得编造")
    pub_no: str | None = None
    title: str | None = None
    abstract: str | None = None
    applicant: str | None = None
    pub_date: str | None = None
    digest: str | None = None
    selected: bool = True


class ManualHitsIn(BaseModel):
    """POST /cases/{id}/search/hits 请求体。"""

    hits: list[ManualHitIn] = Field(min_length=1)
    note: str | None = Field(default=None, description="来源说明（写入 search_queries.raw_json）")


class SearchHitPatchIn(BaseModel):
    """PATCH /search/hits/{id} 请求体。"""

    selected: bool | None = None
    digest: str | None = None


class SearchSkipIn(BaseModel):
    """POST /cases/{id}/search/skip 请求体。"""

    reason: str = Field(default="", description="跳过原因（会体现在 1.1 的检索说明里）")


class SearchHitsOut(BaseModel):
    """GET /cases/{id}/search/hits 响应。"""

    hits: list[SearchHit] = Field(default_factory=list)
    queries: list[SearchQuery] = Field(default_factory=list)
    count: int = 0
    selected_count: int = 0
    latest_status: str | None = None
    latest_error: str | None = None


# ---------------------------------------------------------------------------
# 查新专用 LLM 契约（A4：search_terms / abstract_digest）
# ---------------------------------------------------------------------------


class SearchTermsPlan(BaseModel):
    """search_terms.md 的 STRUCT 输出：2-8 个语义块 + 类型参数 + 理由。"""

    model_config = ConfigDict(extra="ignore")

    blocks: list[str] = Field(default_factory=list)
    type_param: PatentTypeParam = "invention"
    rationale: str = ""

    @field_validator("blocks")
    @classmethod
    def _block_count(cls, v: list[str]) -> list[str]:
        cleaned = [str(b).strip() for b in v if str(b).strip()]
        if not 2 <= len(cleaned) <= MAX_TERMS:
            raise ValueError(f"blocks 须为 2-{MAX_TERMS} 个语义块，当前 {len(cleaned)} 个")
        return cleaned


class AbstractDigestItem(BaseModel):
    """abstract_digest.md 的单条消化结果（url 只准照抄 hits.link）。"""

    model_config = ConfigDict(extra="ignore")

    pub_no: str = ""
    title: str = ""
    applicant: str = ""
    url: str = ""
    scheme_summary: str = ""
    application: str = ""
    limitation: str = ""
    abstract_used: bool = False
    note: str = ""


class AbstractDigests(BaseModel):
    """abstract_digest.md 的 STRUCT 输出。"""

    model_config = ConfigDict(extra="ignore")

    digests: list[AbstractDigestItem] = Field(default_factory=list)


def hit_row_to_model(row: Any) -> SearchHit:
    """sqlite3.Row / dict → SearchHit（INTEGER 布尔位转 bool）。"""
    d = dict(row)
    d["selected"] = bool(d.get("selected", 1))
    d["manual_entry"] = bool(d.get("manual_entry", 0))
    return SearchHit.model_validate(d)
