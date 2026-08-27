"""专利全文 PDF 取证与结构切分（模块 C · C0/C1）。

两块职责：

1. **取证**（`fetch_patent_pdf`）：按 `assets/references/patent_pdf_sources.yaml`
   的 priority 顺序抓取公开号对应的全文 PDF ——

       known_cdn_examples（示例专利已知直链）
         → google_patents_page（解析 citation_pdf_url / patentimages 直链）
         → google_patentimages_cdn（下载）
         → cnipa_epub（元数据核验源，详情页通常无可脚本化全文 PDF）

   全程 httpx、单请求超时 30s、**任何失败都不抛异常**（返回 `FetchResult.ok=False`
   + 逐源 attempts 明细），由流水线转成「请手工上传 PDF」的门控（含可点链接）。

2. **结构切分**（`parse_patent_md`）：把 `services/convert` 产出的 PDF→md 文本切成
   扉页著录项 / 权利要求书（逐项）/ 说明书段落号索引表 `{"0002": "文本"}` / 附图清单。
   中国专利公开文本的版式（`(19)(12)(21)(43)(51)(54)(57)(71)(72)` 著录项、每页页眉
   「权利要求书 1/2 页」、说明书段落 `[0002]`）是解析依据；全角数字/括号先归一化。

本模块只做「抓取 + 解析」：不碰 DB、不发 SSE、不调 LLM，便于单测与 monkeypatch。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml

from . import assets_loader

logger = logging.getLogger(__name__)

# 取证源清单（verbatim 移植的数据型 YAML）
SOURCES_KEY = "references/patent_pdf_sources.yaml"

# 单请求超时（秒）
FETCH_TIMEOUT = 30.0

# 下载体积上限（32MB；防止误抓到大文件把内存吃满）
MAX_PDF_BYTES = 32 * 1024 * 1024

# 抓取用 UA：国内网络下 Google Patents 对无 UA 的请求更容易直接拒绝
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 取证源清单
# ---------------------------------------------------------------------------


def load_sources() -> dict[str, Any]:
    """读取并解析 patent_pdf_sources.yaml（失败返回空 dict，绝不抛）。"""
    try:
        data = yaml.safe_load(assets_loader.get_raw(SOURCES_KEY))
    except (KeyError, yaml.YAMLError) as exc:  # pragma: no cover —— 资产缺失属部署问题
        logger.warning("专利 PDF 取证源清单不可用：%s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _sources_by_priority() -> list[dict[str, Any]]:
    """按 priority 升序（越小越优先）排列的 sources 条目。"""
    items = [s for s in (load_sources().get("sources") or []) if isinstance(s, dict)]
    return sorted(items, key=lambda s: int(s.get("priority") or 999))


def _source(source_id: str) -> dict[str, Any]:
    for item in _sources_by_priority():
        if item.get("id") == source_id:
            return item
    return {}


def normalize_pub_no(pub_no: str | None) -> str:
    """公开号归一化：去空白与分隔符、转大写（`cn 209861402 u` → `CN209861402U`）。"""
    raw = re.sub(r"[\s\-_.]+", "", str(pub_no or "")).upper()
    return raw


def known_cdn_url(pub_no: str) -> str | None:
    """示例专利的已知 CDN 直链（清单 `known_cdn_examples`）。"""
    table = load_sources().get("known_cdn_examples") or {}
    if not isinstance(table, dict):
        return None
    value = table.get(normalize_pub_no(pub_no))
    return str(value) if value else None


def page_urls(pub_no: str) -> list[str]:
    """Google Patents 详情页候选 URL（按清单 page_templates 顺序）。"""
    pub = normalize_pub_no(pub_no)
    if not pub:
        return []
    templates = _source("google_patents_page").get("page_templates") or [
        "https://patents.google.com/patent/{pub}/zh",
        "https://patents.google.com/patent/{pub}/en",
        "https://patents.google.com/patent/{pub}",
    ]
    return [str(t).replace("{pub}", pub) for t in templates]


def cnipa_urls(pub_no: str) -> list[str]:
    """国知局公布公告详情页 URL（元数据核验源）。"""
    pub = normalize_pub_no(pub_no)
    if not pub:
        return []
    templates = _source("cnipa_epub").get("page_templates") or [
        "http://epub.cnipa.gov.cn/patent/{pub}"
    ]
    return [str(t).replace("{pub}", pub) for t in templates]


def manual_links(pub_no: str) -> list[dict[str, str]]:
    """抓取失败时给用户的可点链接（门控表单里展示，用户自行下载后上传）。"""
    pub = normalize_pub_no(pub_no)
    links: list[dict[str, str]] = []
    if not pub:
        return links
    for url in page_urls(pub)[:1]:
        links.append({"label": f"Google Patents 详情页（{pub}）", "url": url})
    for url in cnipa_urls(pub)[:1]:
        links.append({"label": f"国家知识产权局 公布公告（{pub}）", "url": url})
    cdn = known_cdn_url(pub)
    if cdn:
        links.append({"label": f"已知 PDF 直链（{pub}）", "url": cdn})
    return links


# ---------------------------------------------------------------------------
# 取证结果
# ---------------------------------------------------------------------------


@dataclass
class FetchAttempt:
    """一次取证尝试（逐源记录，供门控里向用户交代「都试了什么」）。"""

    source_id: str
    url: str = ""
    ok: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "url": self.url, "ok": self.ok, "error": self.error}


@dataclass
class FetchResult:
    """取证结果；`ok=False` 时 `pdf` 为 None，错误细节在 `attempts`。"""

    pub_no: str
    ok: bool = False
    pdf: bytes | None = None
    source_id: str = ""
    url: str = ""
    error: str = ""
    attempts: list[FetchAttempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """可入 state_json 的摘要（不含 PDF 字节）。"""
        return {
            "pub_no": self.pub_no,
            "ok": self.ok,
            "source_id": self.source_id,
            "url": self.url,
            "error": self.error,
            "size": len(self.pdf or b""),
            "attempts": [a.to_dict() for a in self.attempts],
        }


# Google Patents 详情页里的 PDF 直链线索（三种写法都见过）
_PDF_LINK_PATTERNS = (
    re.compile(r'<meta\s+name="citation_pdf_url"\s+content="([^"]+\.pdf)"', re.IGNORECASE),
    re.compile(r'"pdfLink"\s*:\s*"([^"]+\.pdf)"', re.IGNORECASE),
    re.compile(r'href="(https://patentimages\.storage\.googleapis\.com/[^"]+\.pdf)"', re.IGNORECASE),
    re.compile(r"(https://patentimages\.storage\.googleapis\.com/[^\s\"'<>]+\.pdf)", re.IGNORECASE),
)


def extract_pdf_url(html: str) -> str | None:
    """从详情页 HTML 里解析 PDF 直链（hash 路径必须来自页面，不可臆造）。"""
    text = html or ""
    for pattern in _PDF_LINK_PATTERNS:
        m = pattern.search(text)
        if m:
            url = m.group(1).replace("&amp;", "&").strip()
            if url.startswith("//"):
                url = "https:" + url
            return url
    return None


def looks_like_pdf(payload: bytes | None) -> bool:
    """字节流是否为 PDF（防把 WAF 拦截页当成 PDF 存下来）。"""
    return bool(payload) and payload[:5] == b"%PDF-"


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url, headers={"User-Agent": USER_AGENT})


async def _download_pdf(client: httpx.AsyncClient, url: str) -> tuple[bytes | None, str]:
    """下载并校验 PDF；返回 (bytes|None, error)。"""
    try:
        resp = await _get(client, url)
    except Exception as exc:  # noqa: BLE001 —— 取证失败不抛，交给上层门控
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    payload = resp.content
    if len(payload) > MAX_PDF_BYTES:
        return None, f"文件过大（{len(payload)} 字节）"
    if not looks_like_pdf(payload):
        return None, "响应不是 PDF（可能被反爬拦截或公开号有误）"
    return payload, ""


async def fetch_patent_pdf(
    pub_no: str,
    *,
    timeout: float = FETCH_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> FetchResult:
    """按取证源优先级抓取专利全文 PDF。**任何失败都不抛异常。**

    优先级来自 patent_pdf_sources.yaml：
    `known_cdn_examples` 直链 → Google Patents 详情页解析 → patentimages CDN 下载
    → 国知局详情页（多数情况下没有可脚本化的全文 PDF，仅作最后一试）。
    """
    pub = normalize_pub_no(pub_no)
    result = FetchResult(pub_no=pub)
    if not pub:
        result.error = "公开号为空"
        return result

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        # ① 已知 CDN 直链（示例专利；priority 20 的 download 源的确定分支）
        cdn = known_cdn_url(pub)
        if cdn:
            payload, error = await _download_pdf(client, cdn)
            result.attempts.append(
                FetchAttempt("google_patentimages_cdn", cdn, payload is not None, error)
            )
            if payload is not None:
                result.ok, result.pdf = True, payload
                result.source_id, result.url = "google_patentimages_cdn", cdn
                return result

        # ② Google Patents 详情页 → 解析直链 → 下载
        for url in page_urls(pub):
            html = ""
            try:
                resp = await _get(client, url)
                if resp.status_code != 200:
                    result.attempts.append(
                        FetchAttempt("google_patents_page", url, False, f"HTTP {resp.status_code}")
                    )
                    continue
                html = resp.text
            except Exception as exc:  # noqa: BLE001
                result.attempts.append(
                    FetchAttempt("google_patents_page", url, False, f"{type(exc).__name__}: {exc}")
                )
                continue
            pdf_url = extract_pdf_url(html)
            if not pdf_url:
                result.attempts.append(
                    FetchAttempt("google_patents_page", url, False, "详情页未解析出 PDF 直链")
                )
                continue
            result.attempts.append(FetchAttempt("google_patents_page", url, True, ""))
            payload, error = await _download_pdf(client, pdf_url)
            result.attempts.append(
                FetchAttempt("google_patentimages_cdn", pdf_url, payload is not None, error)
            )
            if payload is not None:
                result.ok, result.pdf = True, payload
                result.source_id, result.url = "google_patentimages_cdn", pdf_url
                return result

        # ③ 国知局（role=verify_meta；详情页一般不给全文 PDF，仅兜底一试）
        for url in cnipa_urls(pub):
            try:
                resp = await _get(client, url)
            except Exception as exc:  # noqa: BLE001
                result.attempts.append(
                    FetchAttempt("cnipa_epub", url, False, f"{type(exc).__name__}: {exc}")
                )
                continue
            if resp.status_code != 200:
                result.attempts.append(
                    FetchAttempt("cnipa_epub", url, False, f"HTTP {resp.status_code}")
                )
                continue
            if looks_like_pdf(resp.content):
                result.ok, result.pdf = True, resp.content
                result.source_id, result.url = "cnipa_epub", url
                result.attempts.append(FetchAttempt("cnipa_epub", url, True, ""))
                return result
            pdf_url = extract_pdf_url(resp.text)
            if pdf_url:
                payload, error = await _download_pdf(client, pdf_url)
                result.attempts.append(
                    FetchAttempt("cnipa_epub", pdf_url, payload is not None, error)
                )
                if payload is not None:
                    result.ok, result.pdf = True, payload
                    result.source_id, result.url = "cnipa_epub", pdf_url
                    return result
            else:
                result.attempts.append(
                    FetchAttempt("cnipa_epub", url, False, "详情页未提供可脚本化的全文 PDF")
                )
    except Exception as exc:  # noqa: BLE001 —— 兜底：取证整体异常同样不抛
        logger.warning("专利 PDF 取证发生未预期异常 pub=%s：%s", pub, exc)
        result.attempts.append(FetchAttempt("internal", "", False, f"{type(exc).__name__}: {exc}"))
    finally:
        if owns_client:
            await client.aclose()

    failed = [a for a in result.attempts if not a.ok and a.error]
    result.error = "；".join(f"{a.source_id}：{a.error}" for a in failed[-3:]) or "全部取证源均未取得 PDF"
    return result


# ---------------------------------------------------------------------------
# 结构切分
# ---------------------------------------------------------------------------

# 全角 → 半角（数字、方括号、圆括号、斜杠、句点）
_FULLWIDTH_MAP = {ord("０") + i: ord("0") + i for i in range(10)}
_FULLWIDTH_MAP.update(
    {
        ord("［"): ord("["),
        ord("］"): ord("]"),
        ord("（"): ord("("),
        ord("）"): ord(")"),
        ord("／"): ord("/"),
        ord("．"): ord("."),
    }
)

# convert.py 写入的页分隔与元信息行
_PAGE_HEADING_RE = re.compile(r"^##\s*第\s*(\d+)\s*页\s*$")
_HTML_COMMENT_RE = re.compile(r"^<!--.*-->\s*$")
_IMAGE_LINE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")

# 每页页眉：「权利要求书 1/2 页」「说明书 3/8 页」「说明书附图 1/2 页」
_PAGE_BANNER_RE = re.compile(r"^(权利要求书|说明书附图|说明书|摘要附图|摘要)\s*\d+\s*/\s*\d+\s*页\s*$")

# 段落号：[0002]
_PARA_RE = re.compile(r"\[\s*(\d{4})\s*\]")

# 权利要求项首：「1.」「1、」「1 .」
_CLAIM_ITEM_RE = re.compile(r"(?m)^[ \t]*(\d{1,3})\s*[.、]\s*")

# 附图说明里的图注：「[0008]图1为…」「图 2 是…」
# 不要求行首——真实版式里图注前常带段落号；但必须跟「为/是/：」，
# 以免把正文里的「图1中」「如图2所示」当成图注。
_FIGURE_RE = re.compile(r"图\s*(\d{1,2})\s*(?:为|是|[:：])\s*([^。\n]{0,120})")

# IPC 分类号：G06T 7/00、A61B5/00
_IPC_RE = re.compile(r"\b([A-HY]\d{2}[A-Z])\s?(\d{1,4}/\d{2,6})")

# 公开号：CN209861402U / CN 114552122 A / ZL2019…
_PUB_RE = re.compile(r"\b((?:CN|ZL)\s?\d{7,13}\s?[ABCUYS]?\d?)\b", re.IGNORECASE)

# 著录项字段：((NN) 编号, 归一化键, 该编号在扉页上可能印的中文标签)
# 标签必须**显式枚举**：用 `[一-龥]{2,8}` 之类的通配会在「(71)申请人北京超声…」这种
# 无空格版式里把值的头几个字一起吃掉。
_BIBLIO_FIELDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("21", "申请号", ("申请号",)),
    ("22", "申请日", ("申请日",)),
    ("43", "公开日", ("申请公布日", "公开日")),
    ("45", "授权公告日", ("授权公告日", "公告日")),
    ("54", "名称", ("发明名称", "实用新型名称", "外观设计名称", "名称")),
    ("57", "摘要", ("摘要",)),
    ("71", "申请人", ("申请人",)),
    ("72", "发明人", ("发明人", "设计人")),
    ("73", "专利权人", ("专利权人",)),
    ("74", "代理机构", ("专利代理机构", "代理机构")),
)

_TITLE_LABELS = ("发明名称", "实用新型名称", "外观设计名称", "名称")


def normalize_patent_text(md: str) -> str:
    """把 PDF→md 文本归一化为可解析的纯文本：去 convert 元信息、页眉与全角。"""
    lines: list[str] = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if _HTML_COMMENT_RE.match(line) or _IMAGE_LINE_RE.match(line.strip()):
            continue
        if _PAGE_HEADING_RE.match(line.strip()):
            continue
        stripped = line.strip()
        if _PAGE_BANNER_RE.match(stripped):
            continue
        lines.append(line)
    text = "\n".join(lines).translate(_FULLWIDTH_MAP)
    # 收拢连续空行，便于按行定位小节
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _find_section_start(lines: list[str], names: Iterable[str], start: int = 0) -> int:
    """找到第一行「独占一行的小节名」（如 `权利要求书`）的行号；找不到返回 -1。

    只认**逐字相等**（去空白与行尾冒号后）：`说明书附图` 不得被当成 `说明书`。
    """
    targets = tuple(names)
    for i in range(start, len(lines)):
        s = lines[i].strip().replace(" ", "").rstrip("：:")
        if s and s in targets:
            return i
    return -1


def split_sections(text: str) -> dict[str, str]:
    """把归一化文本切成 `front`（扉页著录项）/ `claims` / `description` 三段。

    切分锚点为独占一行的「权利要求书」「说明书」；缺锚点时用 `[0001]` 段落号或
    权项首行兜底，保证扫描件/异常版式也能拿到可用的分段。
    """
    lines = text.splitlines()
    claims_at = _find_section_start(lines, ("权利要求书",))
    desc_at = _find_section_start(lines, ("说明书",), max(claims_at + 1, 0))
    if desc_at == -1:
        # 无「说明书」小节名：用第一个段落号所在行兜底
        for i, line in enumerate(lines):
            if _PARA_RE.search(line) and i > claims_at:
                desc_at = i
                break
    if claims_at == -1:
        # 无「权利要求书」小节名：用第一条「1.」权项兜底
        for i, line in enumerate(lines):
            if re.match(r"^\s*1\s*[.、]\s*一种", line) and (desc_at == -1 or i < desc_at):
                claims_at = i
                break

    front_end = claims_at if claims_at != -1 else (desc_at if desc_at != -1 else len(lines))
    front = "\n".join(lines[:front_end]).strip()
    if claims_at == -1:
        claims = ""
        desc_start = desc_at if desc_at != -1 else len(lines)
    else:
        claims_end = desc_at if desc_at != -1 else len(lines)
        claims = "\n".join(lines[claims_at + 1 : claims_end]).strip()
        desc_start = desc_at + 1 if desc_at != -1 else len(lines)

    # 说明书正文到「说明书附图」为止（附图页只有图，抽出来的零散文字会污染段落索引）
    drawings_at = _find_section_start(lines, ("说明书附图", "附图"), desc_start)
    desc_end = drawings_at if drawings_at != -1 else len(lines)
    description = "\n".join(lines[desc_start:desc_end]).strip()
    return {"front": front, "claims": claims, "description": description}


def parse_claims(claims_text: str) -> list[dict[str, Any]]:
    """权利要求书逐项切分 → `[{"no": 1, "text": "..."}]`（按权号升序、去重）。"""
    text = claims_text or ""
    matches = list(_CLAIM_ITEM_RE.finditer(text))
    items: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        no = int(m.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        body = re.sub(r"\s*\n\s*", "", body)          # PDF 硬换行 → 连排
        if not body:
            continue
        items.append({"no": no, "text": body})
    # 页眉重复等原因可能出现同号多条，保留最长的一条
    best: dict[int, dict[str, Any]] = {}
    for item in items:
        prev = best.get(item["no"])
        if prev is None or len(item["text"]) > len(prev["text"]):
            best[item["no"]] = item
    return [best[k] for k in sorted(best)]


def parse_paragraphs(description_text: str) -> dict[str, str]:
    """说明书段落号索引表 `{"0002": "文本"}`（引用 lint 与撰写调用的唯一依据）。"""
    text = description_text or ""
    matches = list(_PARA_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        no = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = re.sub(r"\s*\n\s*", "", text[m.end() : end]).strip()
        if no in out and len(out[no]) >= len(body):
            continue
        out[no] = body
    return dict(sorted(out.items()))


def parse_figures(text: str) -> list[dict[str, Any]]:
    """附图清单：从「附图说明」段（缺失时退回全文）扫图注。

    同一图号只取**首次**出现的图注 —— 附图说明在具体实施方式之前，先到的那条
    才是正式图注，正文里后来的「图1为示意」之类不应覆盖它。
    """
    body = text or ""
    m = re.search(r"附\s*图\s*说\s*明", body)
    scope = body[m.end() :] if m else body
    figures: dict[int, str] = {}
    for match in _FIGURE_RE.finditer(scope):
        no = int(match.group(1))
        if no in figures:
            continue
        figures[no] = re.sub(r"\s+", "", match.group(2)).strip("。;；,，")
    return [{"no": no, "caption": figures[no]} for no in sorted(figures)]


def parse_biblio(front_text: str) -> dict[str, str]:
    """扉页著录项：`(21)申请号` 之类的编号字段 → `{"申请号": "...", ...}`。"""
    text = front_text or ""
    out: dict[str, str] = {}
    for code, key, labels in _BIBLIO_FIELDS:
        alternation = "|".join(sorted(labels, key=len, reverse=True))
        pattern = re.compile(
            rf"\(\s*{code}\s*\)\s*(?:{alternation})?\s*[:：]?\s*"
            rf"(.*?)(?=\n\s*\(\s*\d{{2}}\s*\)|\Z)",
            re.DOTALL,
        )
        m = pattern.search(text)
        if not m:
            continue
        value = re.sub(r"\s*\n\s*", " ", m.group(1)).strip()
        if value:
            out[key] = value[:400]
    return out


def parse_title(front_text: str, biblio: dict[str, str]) -> str:
    """发明/实用新型/外观设计名称：`(54)` 字段优先，其次「XX名称」标签行。"""
    candidate = (biblio.get("名称") or "").strip()
    if candidate:
        for label in _TITLE_LABELS:
            candidate = re.sub(rf"^{label}\s*[:：]?\s*", "", candidate).strip()
        if candidate:
            return candidate[:120]
    text = front_text or ""
    for label in _TITLE_LABELS:
        m = re.search(rf"{label}\s*[:：]?\s*(.+)", text)
        if m:
            value = m.group(1).strip()
            if value:
                return value[:120]
    return ""


# 说明书正文的固定小节名（不是发明名称）
_DESC_SECTION_NAMES = frozenset(
    {
        "技术领域", "背景技术", "发明内容", "实用新型内容", "附图说明",
        "具体实施方式", "实施方式", "说明书", "技术方案", "有益效果",
    }
)


def title_from_description(description: str) -> str:
    """兜底取发明名称：说明书正文的首行标题。

    未公开的申请稿、代理人拟稿、用户自行拼的专利文本都没有国知局扉页的
    `(54)发明名称` 著录项，只靠扉页解析会得到空标题 —— 案件名与交付物文件名
    就会退化成「未命名案件」。而中文说明书正文的第一行按规范必写发明名称，
    据此兜底。
    """
    for raw in (description or "").splitlines():
        line = raw.strip()
        if not line or _PARA_RE.search(line):
            continue
        line = line.lstrip("#").strip()
        if not line or line in _DESC_SECTION_NAMES:
            continue
        # 名称行不带句末标点，且长度有限；不符则认为没取到，宁可留空
        if len(line) > 120 or line[-1] in "。；;：:":
            return ""
        return line
    return ""


def parse_ipc(front_text: str) -> list[str]:
    """IPC 分类号（保序去重，形如 `G06T 7/00`）。"""
    text = front_text or ""
    m = re.search(r"Int\.?\s*Cl\.?", text, re.IGNORECASE)
    scope = text[m.end() :] if m else text
    out: list[str] = []
    for match in _IPC_RE.finditer(scope):
        code = f"{match.group(1)} {match.group(2)}"
        if code not in out:
            out.append(code)
    return out[:8]


def parse_pub_no(front_text: str) -> str:
    """扉页里的公开号/公告号（取第一个带种类码的形态）。"""
    for match in _PUB_RE.finditer(front_text or ""):
        pub = normalize_pub_no(match.group(1))
        if re.search(r"[ABCUYS]\d?$", pub):
            return pub
    m = _PUB_RE.search(front_text or "")
    return normalize_pub_no(m.group(1)) if m else ""


def _split_list(value: str) -> list[str]:
    """申请人/发明人等多值字段拆分（顿号、分号、逗号、空格）。"""
    parts = re.split(r"[、;；,，]|\s{2,}", value or "")
    return [p.strip() for p in parts if p.strip()][:8]


@dataclass
class PatentStructure:
    """专利公开文本的结构化切分结果（C1 产物）。"""

    pub_no: str = ""
    title: str = ""
    ipc: list[str] = field(default_factory=list)
    applicants: list[str] = field(default_factory=list)
    inventors: list[str] = field(default_factory=list)
    pub_date: str = ""
    app_no: str = ""
    abstract: str = ""
    biblio: dict[str, str] = field(default_factory=dict)
    front_text: str = ""
    claims_text: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    description_text: str = ""
    paragraphs: dict[str, str] = field(default_factory=dict)
    figures: list[dict[str, Any]] = field(default_factory=list)
    full_text: str = ""

    # -- 派生视图 ------------------------------------------------------------

    def biblio_text(self) -> str:
        """判型用的著录项短文本（扉页前若干行 + 名称）。"""
        head = "\n".join((self.front_text or "").splitlines()[:40])
        return f"{head}\n{self.title}"

    def claims_markdown(self) -> str:
        """权利要求书逐项文本（注入 prompt 用）。"""
        return "\n".join(f"{c['no']}. {c['text']}" for c in self.claims)

    def paragraph_markdown(self, limit: int | None = None) -> str:
        """段落号索引表文本（`说明书 0002` 引用纪律的依据）。"""
        items = list(self.paragraphs.items())
        if limit is not None:
            items = items[:limit]
        return "\n".join(f"[{no}] {text}" for no, text in items)

    def figures_markdown(self) -> str:
        return "\n".join(f"图{f['no']}：{f['caption']}" for f in self.figures) or "（本案未解析到附图清单）"

    def evidence_scope(self) -> str:
        """证据范围：有权项+说明书段落即 full_text，只有摘要则 abstract_only。"""
        if self.claims and self.paragraphs:
            return "full_text"
        if self.claims or self.paragraphs:
            return "partial"
        return "abstract_only"

    def to_dict(self) -> dict[str, Any]:
        """完整结构（落盘 JSON 用；含全文，勿直接塞 cases.state_json）。"""
        return {
            "pub_no": self.pub_no,
            "title": self.title,
            "ipc": self.ipc,
            "applicants": self.applicants,
            "inventors": self.inventors,
            "pub_date": self.pub_date,
            "app_no": self.app_no,
            "abstract": self.abstract,
            "biblio": self.biblio,
            "front_text": self.front_text,
            "claims_text": self.claims_text,
            "claims": self.claims,
            "description_text": self.description_text,
            "paragraphs": self.paragraphs,
            "figures": self.figures,
            "full_text": self.full_text,
        }

    def summary(self) -> dict[str, Any]:
        """进 cases.state_json 的轻量摘要（不含全文）。"""
        return {
            "pub_no": self.pub_no,
            "title": self.title,
            "ipc": self.ipc,
            "applicants": self.applicants,
            "pub_date": self.pub_date,
            "app_no": self.app_no,
            "claim_count": len(self.claims),
            "paragraph_count": len(self.paragraphs),
            "figure_count": len(self.figures),
            "evidence_scope": self.evidence_scope(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatentStructure:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def parse_patent_md(md: str) -> PatentStructure:
    """PDF→md 文本 → 结构化切分（扉页著录项 / 权项 / 段落号索引 / 附图清单）。"""
    text = normalize_patent_text(md)
    parts = split_sections(text)
    front, claims_text, description = parts["front"], parts["claims"], parts["description"]
    biblio = parse_biblio(front)
    structure = PatentStructure(
        pub_no=parse_pub_no(front) or parse_pub_no(text),
        title=parse_title(front, biblio) or title_from_description(description),
        ipc=parse_ipc(front),
        applicants=_split_list(biblio.get("申请人", "")),
        inventors=_split_list(biblio.get("发明人", "")),
        pub_date=biblio.get("公开日") or biblio.get("授权公告日") or "",
        app_no=biblio.get("申请号", ""),
        abstract=biblio.get("摘要", ""),
        biblio=biblio,
        front_text=front,
        claims_text=claims_text,
        claims=parse_claims(claims_text),
        description_text=description,
        paragraphs=parse_paragraphs(description),
        figures=parse_figures(description or text),
        full_text=text,
    )
    return structure
