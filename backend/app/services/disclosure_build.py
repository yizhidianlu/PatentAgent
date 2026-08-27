"""发明交底书分章生成辅助（模块 A · A6 G0–G7 + A7 组装，prompt-porting-spec §2 模块A）。

本模块承载「骨架先行、逐章生成」的全部服务端逻辑，供 `pipelines/disclosure.py`
的 build / self_check / deliver 三步调用：

- **G0 骨架**（STRUCT）→ `Skeleton`，标题实词贯穿校验（`terminology.skeleton_title_lint`）
  不过即带错 REPAIR；
- **G1–G7 逐章生成**（GEN 流式 → doc 通道）：每章生成后统一做
  ① `terms_delta` 剥离与术语表合并；② 术语漂移 lint；③ 敏感词脱敏 lint；
  ④ 各章专属 lint（URL 白名单 / 检索说明元信息 / mermaid 渲染门禁 / 模块集合 /
  S 号可见标签 / 符号跨节同形 / 实施例 S 号 ⊆ 骨架步骤）；命中即带错 REPAIR ≤2；
- **mermaid 门禁**：真渲染（`services/mermaid`）做语法校验，两次 REPAIR 仍失败则
  降级为编号文字清单并标注「图待补」（R2）；
- **G4 公式门禁**：`services/formula.validate_formula_plan(--eval)`，失败 REPAIR ≤2
  → 仍败交调用方 [PAUSE]；`degraded=True`（门禁环境坏）时**不**跑 REPAIR；
- **A7 组装**：文头模板逐字取自 `invention/template_reference.md`（注意事项三条），
  拼接六章正文；元信息泄漏禁词扫描与文末清洁亦在此。

纪律：本模块不注册步骤、不落交付物（那是 pipelines/disclosure.py 的职责），
只做「生成 + 校验 + 文本装配」，便于单测。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..db import database as db
from ..models.disclosure import ClaimFormAudit, FormulaPlan, Skeleton, TerminologySheet
from . import assembler, assets_loader, terminology
from . import figure_plan as figure_plan_service
from . import formula as formula_service
from . import mermaid as mermaid_service

logger = logging.getLogger(__name__)

# 文档通道 doc_id（前端右侧文档面板按 doc_id 归集流式正文）
DOC_ID = "disclosure"

# 各类 REPAIR 次数上限（§0.1：REPAIR ≤2 → 降级策略）
MAX_CHAPTER_REPAIR = 2
MAX_SKELETON_REPAIR = 2
MAX_FORMULA_REPAIR = 2

# 注入下游调用的前章摘要长度（§3.6 上下文经济：只注入骨架 + 前章 300 字摘要）
SUMMARY_CHARS = 300

# 中文近似 token 估算系数（与 paper2patent 同口径）
CHARS_PER_TOKEN = 1.7


# ---------------------------------------------------------------------------
# prompt 装配清单（§0.2：平台角色头 → 模块 Role → 类型规则 → 脱敏 → 术语 → 阶段指令）
# ---------------------------------------------------------------------------

BUILDER_BASE = [
    "common/system_base",
    "disclosure/invention/builder_core",
    "common/desensitization",
    "common/terminology_rules",
]

G0_PARTS = [*BUILDER_BASE]
G1_PARTS = [*BUILDER_BASE, "disclosure/prior_art/section_1_1_writer", "disclosure/invention/template_reference"]
G2_PARTS = [*BUILDER_BASE, "disclosure/invention/mermaid_rules", "disclosure/invention/template_reference"]
G3_PARTS = [*BUILDER_BASE, "disclosure/invention/mermaid_rules", "disclosure/invention/template_reference"]
G4_PARTS = ["common/system_base", "disclosure/invention/formula_style"]
G5_PARTS = [*BUILDER_BASE, "disclosure/invention/formula_style", "disclosure/invention/template_reference"]
G6_PARTS = [*BUILDER_BASE, "disclosure/invention/template_reference"]
G7_PARTS = [*BUILDER_BASE, "disclosure/invention/template_reference"]

# 章节顺序（3.4.1/3.5 由 G5 产出，接在 G3 之后）
CHAPTER_ORDER: list[str] = ["g1", "g2", "g3", "g5", "g6", "g7"]

CHAPTER_NAMES: dict[str, str] = {
    "g0": "骨架",
    "g1": "第一、二章（现有技术与技术问题）",
    "g2": "3.1–3.3（背景 / 系统框图 / 模块功能）",
    "g3": "3.4 系统流程说明",
    "g4": "公式计划 formula_plan",
    "g5": "3.4.1 符号与公式 + 3.5 关键技术参数",
    "g6": "第四、五章（优点与保护点）",
    "g7": "第六章（实施例与技术效果）",
}

CHAPTER_PARTS: dict[str, list[str]] = {
    "g1": G1_PARTS, "g2": G2_PARTS, "g3": G3_PARTS,
    "g5": G5_PARTS, "g6": G6_PARTS, "g7": G7_PARTS,
}

# ---- 实用新型 / 外观设计分支（§2 A6 实用新型段 / 外观段） ----

UTILITY_BASE = [
    "common/system_base",
    "disclosure/utility_model/builder",
    "common/desensitization",
    "common/terminology_rules",
]
DESIGN_BASE = [
    "common/system_base",
    "disclosure/design/builder",
    "common/desensitization",
    "common/terminology_rules",
]

U0_PARTS = [*UTILITY_BASE]
U1_PARTS = [*UTILITY_BASE, "disclosure/prior_art/section_1_1_writer",
            "disclosure/utility_model/template_reference"]
U2_PARTS = [*UTILITY_BASE, "disclosure/utility_model/template_reference"]
U3_PARTS = [*UTILITY_BASE, "disclosure/utility_model/template_reference"]
U4_PARTS = [*UTILITY_BASE, "disclosure/utility_model/template_reference"]

D0_PARTS = [*DESIGN_BASE]
D1_PARTS = [*DESIGN_BASE, "disclosure/design/template_reference"]
D2_PARTS = [*DESIGN_BASE, "disclosure/prior_art/section_1_1_writer",
            "disclosure/design/template_reference"]

# 实用新型：一二章 → 3.1–3.3 → 3.4/3.5 → 四五六章
UTILITY_CHAPTER_ORDER: list[str] = ["u1", "u2", "u3", "u4"]
# 外观：一二三章（含视图说明）→ 四五章
DESIGN_CHAPTER_ORDER: list[str] = ["d1", "d2"]

CHAPTER_NAMES.update(
    {
        "u1": "第一、二章（现有结构与技术问题）",
        "u2": "3.1–3.3（总体构成 / 连接与配合 / 空间布局与附图说明）",
        "u3": "3.4–3.5（结构作用与使用拆装过程 / 关键尺寸或材料）",
        "u4": "第四、五、六章（优点 / 欲保护点 / 其它）",
        "d1": "第一、二、三章（产品名称与用途 / 设计要点 / 视图说明）",
        "d2": "第四、五章（与在先外观的差异 / 其它）",
    }
)

CHAPTER_PARTS.update(
    {"u1": U1_PARTS, "u2": U2_PARTS, "u3": U3_PARTS, "u4": U4_PARTS,
     "d1": D1_PARTS, "d2": D2_PARTS}
)


def chapter_order(patent_type: str) -> list[str]:
    """按专利类型取章节装配顺序。"""
    if patent_type == "utility_model":
        return list(UTILITY_CHAPTER_ORDER)
    if patent_type == "design":
        return list(DESIGN_CHAPTER_ORDER)
    return list(CHAPTER_ORDER)


def section5_key(patent_type: str) -> str | None:
    """「第五章」所在的章节 key（外观无欲保护点章节）。"""
    return {"invention": "g6", "utility_model": "u4"}.get(patent_type)


# ---------------------------------------------------------------------------
# 通用 LLM 调用封装（每次调用经 assembler 装配 system prompt；供本模块与流水线复用）
# ---------------------------------------------------------------------------


def messages(system: str, user: str) -> list[dict[str, str]]:
    """两段式消息体（system 为装配结果，user 为本次阶段指令）。"""
    return [{"role": "system", "content": str(system)}, {"role": "user", "content": user}]


async def stream_gen(
    ctx: Any,
    *,
    tag: str,
    system: str,
    user: str,
    channel: str = "doc",
    doc_id: str = DOC_ID,
    max_output_tokens: int | None = None,
) -> str:
    """GEN 流式调用：逐 token 推 SSE（chat 或 doc 通道），返回完整文本。"""
    kwargs: dict[str, Any] = {
        "case_id": ctx.case_id,
        "step_key": f"{ctx.step_key}.{tag}",
        **assembler.call_params("GEN"),
    }
    if max_output_tokens is not None:
        kwargs["max_output_tokens"] = max_output_tokens
    chunks: list[str] = []
    async for delta in ctx.llm.chat_stream(messages(system, user), **kwargs):
        chunks.append(delta)
        if channel == "chat":
            await ctx.chat_delta(delta)
        else:
            await ctx.doc_delta(doc_id, delta)
    if channel == "chat":
        await ctx.chat_done()
    else:
        await ctx.doc_done(doc_id)
    return "".join(chunks)


async def call_text(
    ctx: Any, *, tag: str, system: str, user: str, call_class: str = "REPAIR"
) -> str:
    """非流式文本调用（REPAIR / CHAT 缓冲用）。"""
    return await ctx.llm.chat(
        messages(system, user),
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.{tag}",
        **assembler.call_params(call_class),
    )


async def call_struct(
    ctx: Any, model_cls, *, tag: str, system: str, user: str, call_class: str = "STRUCT"
):
    """结构化调用（STRUCT / REPAIR / AUDIT）。"""
    return await ctx.llm.structured(
        messages(system, user),
        model_cls,
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.{tag}",
        **assembler.call_params(call_class),
    )


def estimate_tokens(text: str) -> int:
    """中文近似 token 数（不引入 tiktoken 依赖，只用于分片与预算）。"""
    return int(len(text or "") / CHARS_PER_TOKEN)


def text_digest(text: str, limit: int = SUMMARY_CHARS) -> str:
    """章节摘要：折叠空白后截断（下游只注入摘要，不塞全文，§3.6）。"""
    body = " ".join((text or "").split())
    return body[:limit]


# ---------------------------------------------------------------------------
# 案件卡片与文头模板
# ---------------------------------------------------------------------------

PATENT_TYPE_LABEL = {"invention": "发明", "utility_model": "实用新型", "design": "外观设计"}

_FALLBACK_HEADER = """# 技术交底书

**案件名称**：[待填写]一种XXX方法及系统

**技术联系人**：
- 姓名：[待填写]
- 电话：[待填写]
- 邮箱：[待填写]

**专利类型**：发明

---

## 注意事项

（1）交底书应使代理人能看懂，尤其是背景技术和详细技术方案，一定要写得全面、清楚、完整；
（2）技术的公开程度，应以本领域普通技术人员不需付出创造性劳动即可进行实施为准。
（3）在与代理人沟通时，对于代理人咨询的技术问题，应给予回答并认真讲解，并且按要求及时正确地补充相应技术材料。"""

_HEADER_FENCE_RE = re.compile(r"```markdown[ \t]*\r?\n(.*?)```", re.DOTALL)


_FALLBACK_HEADER_UTILITY = """# 技术交底书

**案件名称**：[待填写]一种……装置/结构

**技术联系人**：
- 姓名：[待填写]
- 电话：[待填写]
- 邮箱：[待填写]

**专利类型**：实用新型"""

_FALLBACK_HEADER_DESIGN = """# 外观设计说明（交底底稿）

**产品名称**：[待填写]

**技术联系人**：
- 姓名：[待填写]
- 电话：[待填写]
- 邮箱：[待填写]

**专利类型**：外观设计"""

# 类型 → 文头模板所在资产（实用新型 §7.2 / 外观 §7.2 的 markdown 围栏）
_HEADER_ASSET = {
    "invention": "prompts/disclosure/invention/template_reference",
    "utility_model": "prompts/disclosure/utility_model/builder",
    "design": "prompts/disclosure/design/builder",
}

_NOTES_RE = re.compile(r"##\s*注意事项.*", re.DOTALL)
_TITLE_LINE_RE = re.compile(r"^\*\*(?:案件名称|产品名称)\*\*：.*$", re.MULTILINE)


def _header_fence(patent_type: str) -> str | None:
    """某类型资产里的文头 markdown 围栏（逐字）；取不到返回 None。"""
    key = _HEADER_ASSET.get(patent_type)
    if not key:
        return None
    try:
        body = assets_loader.get_text(key)
    except KeyError:  # pragma: no cover —— 资产缺失时用内置副本兜底
        logger.warning("%s 缺失，文头改用内置副本", key)
        return None
    for m in _HEADER_FENCE_RE.finditer(body):
        block = m.group(1).strip()
        if "技术联系人" in block and "专利类型" in block:
            return block
    return None


def notes_block() -> str:
    """「## 注意事项」三条（逐字取自发明 template_reference；三类型口径相同）。"""
    m = _NOTES_RE.search(header_template("invention"))
    return m.group(0).strip() if m else ""


def header_template(patent_type: str = "invention") -> str:
    """文头模板（含注意事项三条）——**逐字**取自各类型的 template/builder 资产。"""
    if patent_type == "invention":
        block = _header_fence("invention")
        if block and "注意事项" in block:
            return block
        return _FALLBACK_HEADER
    block = _header_fence(patent_type) or (
        _FALLBACK_HEADER_UTILITY if patent_type == "utility_model" else _FALLBACK_HEADER_DESIGN
    )
    # 实用新型 §7.2「注意事项三条口径与发明文头相同」；外观 §7.1 第 1 项同样要求注意事项
    notes = notes_block()
    return f"{block}\n\n---\n\n{notes}" if notes else block


def document_header(
    case_title: str,
    contact: Mapping[str, Any] | None = None,
    patent_type: str = "invention",
) -> str:
    """按案件卡片填充文头模板（无值保留「[待填写]」占位）。"""
    text = header_template(patent_type)
    title = (case_title or "").strip()
    if title:
        if patent_type == "invention":
            text = text.replace("[待填写]一种XXX方法及系统", title)
        else:
            label = "产品名称" if patent_type == "design" else "案件名称"
            line = f"**{label}**：{title}"
            text = _TITLE_LINE_RE.sub(lambda _m: line, text, count=1)
    info = dict(contact or {})
    for field_name, label in (("name", "姓名"), ("phone", "电话"), ("email", "邮箱")):
        value = str(info.get(field_name) or "").strip()
        if value and value != "待填写":
            text = text.replace(f"- {label}：[待填写]", f"- {label}：{value}")
    label = PATENT_TYPE_LABEL.get(patent_type, "发明")
    if label != "发明":
        text = text.replace("**专利类型**：发明", f"**专利类型**：{label}")
    return text


def case_card(ctx: Any) -> dict[str, Any]:
    """注入 prompt 的案件卡片。"""
    case = getattr(ctx, "case", None) or {}
    state = getattr(ctx, "state", None) or {}
    ptype = str(state.get("patent_type") or case.get("patent_type") or "invention")
    explicit = bool((state.get("intake") or {}).get("patent_type_explicit"))
    label = PATENT_TYPE_LABEL.get(ptype, "发明")
    return {
        "case_id": getattr(ctx, "case_id", ""),
        "module": "disclosure（专利交底书）",
        "case_title": state.get("working_title") or case.get("title") or "",
        "patent_type": ptype,
        "patent_type_label": label if explicit else f"{label}（默认）",
        "contact": state.get("contact") or {"name": "待填写", "phone": "待填写", "email": "待填写"},
    }


async def set_case_title(ctx: Any, case_title: str) -> str:
    """用骨架的案件名称回填 cases.title 并广播 case_title 事件。"""
    title = (case_title or "").strip()[:120]
    if not title:
        return ""
    await db.aexecute(
        "UPDATE cases SET title=?, updated_at=? WHERE id=?", (title, db.now_str(), ctx.case_id)
    )
    await ctx.reload_case()
    await ctx.emit("case_title", {"title": title})
    return title


# ---------------------------------------------------------------------------
# mermaid 门禁（真渲染做语法校验；失败降级为编号文字清单）
# ---------------------------------------------------------------------------

_MERMAID_FENCE_RE = re.compile(r"```mermaid[ \t]*\r?\n(.*?)```", re.DOTALL)

# 渲染结果缓存：code sha256 → (png_bytes | None, error | None)
_render_cache: dict[str, tuple[bytes | None, str | None]] = {}


def mermaid_blocks(text: str) -> list[tuple[int, int, str]]:
    """正文中的 mermaid 围栏：[(fence_start, fence_end, code)]。"""
    return [(m.start(), m.end(), m.group(1).strip()) for m in _MERMAID_FENCE_RE.finditer(text or "")]


async def render_mermaid(code: str) -> tuple[bytes | None, str | None]:
    """渲染一段 mermaid（带进程内缓存）：返回 (png_bytes, error)。"""
    key = hashlib.sha256((code or "").encode("utf-8")).hexdigest()
    hit = _render_cache.get(key)
    if hit is not None:
        return hit
    try:
        png = await mermaid_service.render_mermaid_png(code)
        result: tuple[bytes | None, str | None] = (png, None)
    except mermaid_service.MermaidRenderError as exc:
        result = (None, str(exc)[:400])
    except Exception as exc:  # noqa: BLE001 —— 渲染环境异常同样按门禁失败处理
        logger.warning("mermaid 渲染异常：%s", exc)
        result = (None, f"{type(exc).__name__}: {exc}"[:400])
    _render_cache[key] = result
    return result


async def mermaid_check(text: str) -> list[str]:
    """mermaid 语法门禁：逐块真渲染，失败项带错误信息（前缀 `[mermaid]`）。"""
    problems: list[str] = []
    blocks = mermaid_blocks(text)
    if not blocks:
        problems.append("[mermaid] 本章缺少 fenced mermaid 图（禁止用 ASCII 文字框图代替）")
        return problems
    for i, (_s, _e, code) in enumerate(blocks, 1):
        _png, error = await render_mermaid(code)
        if error:
            problems.append(f"[mermaid] 第 {i} 个 mermaid 围栏渲染失败：{error}")
    return problems


_NODE_RE = re.compile(r'([A-Za-z_][\w]*)\s*(?:\[\s*"?(.*?)"?\s*\]|\{\s*"?(.*?)"?\s*\}|\(\s*"?(.*?)"?\s*\))')
_EDGE_RE = re.compile(r'([A-Za-z_][\w]*)\s*-{1,3}[.-]?>\s*(?:\|[^|]*\|\s*)?([A-Za-z_][\w]*)')


def _node_labels(code: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for m in _NODE_RE.finditer(code or ""):
        node_id = m.group(1)
        label = next((g for g in m.groups()[1:] if g), "") or node_id
        labels.setdefault(node_id, label.strip())
    return labels


def mermaid_to_text_list(code: str) -> str:
    """把渲染失败的 mermaid 降级为编号文字清单（R2 降级策略）。"""
    labels = _node_labels(code)
    lines = ["> 图待补：本图 mermaid 渲染未通过，以下为等价的编号文字清单，交付前需人工补图。", ""]
    for i, (node_id, label) in enumerate(labels.items(), 1):
        lines.append(f"{i}. {label}（节点 {node_id}）")
    edges = [f"{labels.get(a, a)} → {labels.get(b, b)}" for a, b in _EDGE_RE.findall(code or "")]
    if edges:
        lines.append("")
        lines.append("连接关系：" + "；".join(edges) + "。")
    return "\n".join(lines)


async def degrade_mermaid(text: str) -> tuple[str, int]:
    """把正文中所有渲染失败的 mermaid 围栏替换为编号文字清单；返回 (新正文, 降级块数)。"""
    out = text or ""
    degraded = 0
    for start, end, code in reversed(mermaid_blocks(out)):
        _png, error = await render_mermaid(code)
        if not error:
            continue
        out = out[:start] + mermaid_to_text_list(code) + out[end:]
        degraded += 1
    return out, degraded


# ---------------------------------------------------------------------------
# 确定性 lint 束
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s)\]，。；、\"'<>）】]+")

# 元信息泄漏禁词（R9；服务端 lint 兜底，prompt 亦有禁令）
META_FORBIDDEN: tuple[str, ...] = (
    "自检",
    "检查清单",
    "教学示例",
    "不构成法律承诺",
    "不构成任何法律",
    "cnipa_epub",
    "脚本",
    "patent-disclosure-skill",
    "examples/",
    "Playwright",
    "WebSearch",
    "虚构",
)

# 1.1「检索说明」禁写的内部/流程元信息（section_1_1_writer.md「禁止写入 1.1 正文」）
SEARCH_NOTE_FORBIDDEN: tuple[str, ...] = (
    "cnipa_epub_search",
    "cnipa_epub_crawler",
    ".py",
    "Playwright",
    "WebSearch",
    "爬虫",
    "降级",
    "脚本",
    "检索工具",
    "Agent",
)


def url_lint(text: str, allowed: Iterable[str]) -> list[str]:
    """1.1 URL 白名单：正文出现的每个 URL 必须 ∈ 命中链接集合（禁止编造）。"""
    allow = {str(u).strip().rstrip("/") for u in allowed if str(u).strip()}
    problems: list[str] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip("/。，；、)]")
        if url in seen:
            continue
        seen.add(url)
        if url.rstrip("/") not in allow:
            problems.append(
                f"正文出现未经核验的链接「{url}」：1.1 的 URL 只准照抄查新笔记中的 url 字段，禁止编造或改写"
            )
    return problems


def search_note_lint(text: str) -> list[str]:
    """检索说明禁现脚本名与内部流程元信息（面向代理人表述）。"""
    problems: list[str] = []
    for word in SEARCH_NOTE_FORBIDDEN:
        if word in (text or ""):
            problems.append(f"1.1 检索说明出现内部流程用语「{word}」：只写公开数据库名称与检索词")
    return problems


def meta_leak_lint(text: str) -> list[dict[str, Any]]:
    """元信息泄漏扫描：返回 [{word, line, snippet}]（空=通过）。"""
    hits: list[dict[str, Any]] = []
    for lineno, line in enumerate((text or "").splitlines(), 1):
        for word in META_FORBIDDEN:
            if word in line:
                hits.append({"word": word, "line": lineno, "snippet": line.strip()[:80]})
    return hits


def clean_tail(text: str) -> tuple[str, list[str]]:
    """文末清洁：删除结尾处的元信息脚注行（斜体/引用/免责/仓库署名）。"""
    lines = (text or "").rstrip().splitlines()
    removed: list[str] = []
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        looks_meta = any(word in last for word in META_FORBIDDEN)
        decorated = last.startswith(("*", "_", ">", "—", "-", "（注"))
        if looks_meta and (decorated or len(last) < 120):
            removed.append(last)
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip() + "\n", removed


def heading_lint(text: str, required: Sequence[str]) -> list[str]:
    """章节标题存在性（章节结构 §7.1）。"""
    return [f"缺少章节标题/小节号「{h}」" for h in required if h not in (text or "")]


def module_lint(text: str, modules: Sequence[str]) -> list[str]:
    """3.3 模块集合须 = 骨架 modules（一模块一项）。"""
    body = text or ""
    missing = [m for m in modules if m and m not in body]
    problems = [
        f"3.3 缺少骨架模块「{'、'.join(missing)}」：模块集合须与系统框图/骨架完全一致，一模块一项"
    ] if missing else []
    return problems


_STEP_ID_RE = re.compile(r"\bS(\d+)\b")


def _step_numbers(steps: Sequence[Any]) -> list[int]:
    nums: list[int] = []
    for step in steps:
        sid = step.get("sid") if isinstance(step, Mapping) else getattr(step, "sid", "")
        m = re.search(r"(\d+)", str(sid or ""))
        if m:
            nums.append(int(m.group(1)))
    return nums


def step_label_lint(text: str, steps: Sequence[Any]) -> list[str]:
    """3.4 流程图：步骤号必须写进**可见标签**（`S1["S1 …"]`），且 S1..Sn 齐全连续。"""
    problems: list[str] = []
    nums = _step_numbers(steps)
    if not nums:
        return problems
    expected = list(range(1, max(nums) + 1))
    if sorted(set(nums)) != expected:
        problems.append(f"骨架步骤号不连续：{sorted(set(nums))}，应为 S1–S{max(nums)}")

    blocks = mermaid_blocks(text)
    code = "\n".join(b[2] for b in blocks)
    labels = _node_labels(code)
    for n in expected:
        sid = f"S{n}"
        label = labels.get(sid)
        if label is None:
            problems.append(f"3.4 流程图缺少步骤节点 {sid}")
        elif sid not in label:
            problems.append(
                f"3.4 流程图节点 {sid} 的可见标签「{label}」未写出步骤号，应写成 {sid}[\"{sid} …\"]"
            )
    return problems


def step_prose_lint(text: str, steps: Sequence[Any]) -> list[str]:
    """3.4 流程说明须按步骤号逐项覆盖 S1—Sn。"""
    problems: list[str] = []
    body = re.sub(r"```mermaid.*?```", "", text or "", flags=re.DOTALL)
    for n in _step_numbers(steps):
        if f"S{n}" not in body:
            problems.append(f"3.4 流程说明缺少步骤 S{n} 的逐项文字说明")
    return problems


def embodiment_lint(text: str, steps: Sequence[Any]) -> list[str]:
    """第六章：实施例引用的 S 号 ⊆ 骨架 steps；须有 2–3 个有名字的实施例。"""
    problems: list[str] = []
    allowed = {f"S{n}" for n in _step_numbers(steps)}
    used = {f"S{m}" for m in _STEP_ID_RE.findall(text or "")}
    extra = sorted(used - allowed, key=lambda s: int(s[1:]))
    if extra:
        problems.append(
            f"第六章引用了骨架中不存在的步骤号 {'、'.join(extra)}：实施例的 S 号须 ⊆ 3.4 流程步骤"
        )
    if (text or "").count("实施例") < 2:
        problems.append("第六章须给出 2～3 个有名字的实施例（走主路径 + 一条第三章已有分支）")
    return problems


_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s*(.+)$", re.MULTILINE)


def section5_lint(text: str) -> list[str]:
    """第五章保护点须为**可实施机制**（禁止仅罗列角色/复述标题对象）。"""
    problems: list[str] = []
    idx = (text or "").find("五、")
    body = (text or "")[idx:] if idx >= 0 else (text or "")
    items = [m.group(1).strip() for m in _LIST_ITEM_RE.finditer(body)]
    items = [i for i in items if i]
    if not items:
        problems.append("第五章未分条列出技术关键点/欲保护点")
        return problems
    for item in items:
        plain = re.sub(r"[*`\s]", "", item)
        if len(plain) < 18:
            problems.append(f"第五章保护点「{item[:24]}」过于简略，须写清对什么输入、如何判定或控制、得到什么输出")
        elif re.match(r"^有\d+个", plain) or re.match(r"^包含\d+个", plain):
            problems.append(f"第五章保护点「{item[:24]}」只是角色枚举，须改写为可实施机制")
    return problems


# ---- 实用新型专项 lint（§2 A6 实用新型段 + self_check_utility §8.4） ----


def _part_names(parts: Sequence[Any]) -> list[tuple[str, str]]:
    """StructureSchema.parts → [(件号, 名称)]。"""
    out: list[tuple[str, str]] = []
    for part in parts or []:
        data = part if isinstance(part, Mapping) else getattr(part, "__dict__", {})
        pid = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        if name:
            out.append((pid, name))
    return out


def parts_lint(text: str, parts: Sequence[Any]) -> list[str]:
    """3.1 总体构成：部件表须覆盖 StructureSchema.parts 的全部件号与名称。"""
    body = text or ""
    missing = [f"{pid}·{name}" for pid, name in _part_names(parts) if name not in body]
    if missing:
        return [
            "3.1 总体构成缺少事实合同中的部件「" + "、".join(missing) + "」："
            "部件表须与 StructureSchema.parts 逐条对齐（件号 + 名称 + 形状要点）"
        ]
    return []


def relations_lint(text: str, relations: Sequence[Any], parts: Sequence[Any]) -> list[str]:
    """3.2 连接与配合：每条 relations 的两端部件与连接类型都要在正文出现。"""
    body = text or ""
    names = {pid: name for pid, name in _part_names(parts)}
    problems: list[str] = []
    for rel in relations or []:
        data = rel if isinstance(rel, Mapping) else getattr(rel, "__dict__", {})
        src = str(data.get("from") or data.get("from_") or "").strip()
        dst = str(data.get("to") or "").strip()
        rtype = str(data.get("type") or "").strip()
        left, right = names.get(src, src), names.get(dst, dst)
        if left and left not in body:
            problems.append(f"3.2 未写出连接关系中的部件「{left}」（件号 {src}）")
        if right and right not in body:
            problems.append(f"3.2 未写出连接关系中的部件「{right}」（件号 {dst}）")
        if rtype and rtype not in body:
            problems.append(
                f"3.2 未写出「{left or src}—{right or dst}」的连接类型「{rtype}」："
                "连接与配合须与 StructureSchema.relations 一一对齐"
            )
    return problems


# uncertain 项被写成既定事实时，附近应出现的对冲措辞
_HEDGE_WORDS = (
    "待确认", "不确定", "无法确认", "尚未确认", "有待", "暂未", "看不清", "未见",
    "以实物为准", "视具体实施", "可选", "示例", "未标注", "未给出", "存疑", "待补",
)


def uncertain_lint(text: str, uncertain: Sequence[Any], *, window: int = 80) -> list[str]:
    """`uncertain` 项不得写成既定事实（附近须有对冲措辞）。"""
    body = text or ""
    problems: list[str] = []
    for raw in uncertain or []:
        item = str(raw).strip()
        if not item:
            continue
        # 取 uncertain 描述里最长的中文片段作为探针（整句通常不会逐字出现在正文）
        probes = [p for p in re.split(r"[，,。；;：:（）()\s]+", item) if len(p) >= 3]
        probe = max(probes, key=len) if probes else item
        pos = body.find(probe)
        if pos < 0:
            continue
        around = body[max(0, pos - window) : pos + len(probe) + window]
        if not any(word in around for word in _HEDGE_WORDS):
            problems.append(
                f"事实合同的 uncertain 项「{item[:24]}」在正文被写成了既定事实："
                "看不清 / 材料未写明的内容须标注为待确认，禁止写死"
            )
    return problems


# 第五章装置书式（builder §7.5：「一种……装置，包括……；其特征在于……」）
_DEVICE_FORM_RE = re.compile(
    r"一种[^。；\n]{0,60}?(装置|结构|机构|组件|部件|设备|系统|支架|壳体|模组)[^。\n]{0,60}?(包括|包含|由)"
)
_METHOD_FORM_RE = re.compile(r"一种[^。；\n]{0,60}?方法[^。\n]{0,20}?(包括|包含)[^。\n]{0,20}?步骤")
_STEP_ONLY_RE = re.compile(r"^\s*(?:步骤\s*[一二三四五六1-9]|S\d+[：:、.])")


def section5_device_lint(text: str) -> list[str]:
    """第五章书式 lint（实用新型）：必须是装置书式，禁止纯方法步骤作主保护点。"""
    body = text or ""
    idx = body.find("五、")
    section = body[idx:] if idx >= 0 else body
    # 只取到下一章为止
    nxt = section.find("## 六、")
    if nxt > 0:
        section = section[:nxt]
    problems: list[str] = []
    if not section.strip():
        return ["第五章缺失：实用新型须写「技术关键点与欲保护点」（装置/结构书式）"]

    if not _DEVICE_FORM_RE.search(section):
        problems.append(
            "第五章缺少装置书式的主保护点：须写成「一种……装置，包括……；其特征在于……"
            "（连接/形状限定）」，不得只作功能描述"
        )
    if "其特征在于" not in section:
        problems.append("第五章主保护点缺少「其特征在于」的区别特征表述（装置书式硬要求）")
    if _METHOD_FORM_RE.search(section):
        problems.append(
            "第五章出现「一种……方法，包括以下步骤」的纯方法书式：实用新型不得以方法步骤作主保护点，"
            "须改写为装置/结构书式（部件 + 连接关系 + 形状限定）"
        )
    items = [m.group(1).strip() for m in _LIST_ITEM_RE.finditer(section)]
    step_items = [i for i in items if _STEP_ONLY_RE.match(i)]
    if step_items and len(step_items) >= max(1, len(items) // 2):
        problems.append(
            "第五章的欲保护点以工艺/操作步骤罗列为主（如「步骤一…」「S1…」）："
            "实用新型主保护点须落在构造上，步骤仅可作使用过程说明放在 3.4"
        )
    return problems


# ---- 外观设计专项 lint（§2 A6 外观段 + self_check_design §8.5） ----

_SOLID_FACES = ("主视", "后视", "左视", "右视", "俯视", "仰视")

# 外观正文禁写的内部构造/功能用语（builder §7.3：只写看得见的造型/图案/色彩）
_INVISIBLE_WORDS = (
    "内部电路", "电路板", "受力分析", "工艺步骤", "算法", "内部结构", "装配公差",
    "扭矩", "热仿真", "程序", "控制逻辑", "接口协议",
)


def visible_only_lint(text: str) -> list[str]:
    """外观正文只写看得见的造型/图案/色彩（禁内部构造、功能、工艺）。"""
    body = text or ""
    hits = [w for w in _INVISIBLE_WORDS if w in body]
    if hits:
        return [
            "外观说明出现不可见的构造/功能用语「" + "、".join(hits) + "」："
            "只写看得见的形状、图案、色彩，内部结构与工艺不属于外观设计的保护对象"
        ]
    return []


def view_lint(text: str, appearance: Mapping[str, Any] | None, plan: Mapping[str, Any] | None = None) -> list[str]:
    """三、视图说明：按 `product_form` + `claimed_faces` 覆盖，省略面须写简要说明。"""
    body = text or ""
    data = dict(appearance or {})
    problems: list[str] = []
    faces = [str(f).strip() for f in (data.get("claimed_faces") or []) if str(f).strip()]
    for face in faces:
        if face not in body:
            problems.append(
                f"三、视图说明缺少设计要点落面「{face}」的正投影说明（视图集合须与 claimed_faces 对齐）"
            )
    omitted = [o for o in (data.get("omitted_views") or []) if isinstance(o, Mapping)]
    for item in omitted:
        name = str(item.get("name") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if name and name not in body:
            problems.append(f"故意省略的视图「{name}」未在简要说明中写出（应写明省略理由：{reason or '相同/对称/无要点'}）")
    views = [str((v or {}).get("name") or "").strip() for v in (data.get("views") or []) if isinstance(v, Mapping)]
    for name in views:
        if name and name not in body:
            problems.append(f"事实合同已列视图「{name}」但正文未说明")
    if len(faces) < 6 and re.search(r"六(个)?面?视图|六视图|六面正投影", body):
        problems.append(
            "正文出现「六视图」表述，但本案设计要点未涉及六个面：交底按要点落面选正投影，"
            "不要默认补六视（相同/对称/无要点的面写进省略说明）"
        )
    if plan is not None:
        problems.extend(figure_plan_service.figure_ref_lint(body, plan))
    return problems


def sensitive_lint(text: str, terms: Iterable[str], *, keep: Iterable[str] = ()) -> list[str]:
    """案件级敏感词扫描（§3.2）：命中即须脱敏；`keep` 内的标题领域对象不算敏感词。"""
    protect = {str(k).strip() for k in keep if str(k).strip()}
    problems: list[str] = []
    for raw in terms:
        term = str(raw).strip()
        if not term or len(term) < 2 or term in protect:
            continue
        if any(term in k or k in term for k in protect):
            continue
        if term in (text or ""):
            problems.append(f"正文出现敏感词「{term}」：须按脱敏规则改为「某系统」「一定规模」「分类A」等表述")
    return problems


def drift_problems(findings: Sequence[Mapping[str, Any]]) -> list[str]:
    """术语漂移 lint 结果 → REPAIR 问题清单（forbidden_variant 与标题贯穿必修）。"""
    out: list[str] = []
    for f in findings:
        line = f.get("line")
        where = f"（第 {line} 行）" if line else ""
        out.append(f"术语漂移[{f.get('rule')}]{where}：{f.get('msg')}")
    return out


# ---- 3.5 参数表「符号」列 ⊆ formula_plan.symbols（逐字同形） ----

_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$", re.MULTILINE)

# 参数表里「本行无符号」的占位写法（不参与逐字同形比对）
_SYMBOL_PLACEHOLDERS = {"无", "—", "－", "-", "–", "/", "N/A", "n/a", "不适用", "空"}


def _strip_math(cell: str) -> str:
    """去掉单元格里的 LaTeX 分隔符与装饰，取符号本体。"""
    s = (cell or "").strip()
    s = re.sub(r"^\\\(|\\\)$|^\\\[|\\\]$", "", s.strip())
    s = s.strip().strip("$").strip("`").strip()
    s = re.sub(r"^\\\(|\\\)$", "", s)
    return s.strip()


def symbol_column(text: str) -> list[str]:
    """抽取 markdown 表格中「符号」列的单元格（3.4.1 符号表 / 3.5 参数表通用）。"""
    rows = [m.group(1) for m in _TABLE_ROW_RE.finditer(text or "")]
    symbols: list[str] = []
    col = -1
    for row in rows:
        cells = [c.strip() for c in row.split("|")]
        header = next((i for i, c in enumerate(cells) if c.replace(" ", "") == "符号"), -1)
        if header >= 0:          # 表头行（多张表各自重新定位「符号」列）
            col = header
            continue
        if col < 0:
            continue
        if all(set(c) <= set("-: ") for c in cells if c):
            continue             # 分隔行
        if col < len(cells):
            value = _strip_math(cells[col])
            if value and value not in _SYMBOL_PLACEHOLDERS and value not in symbols:
                symbols.append(value)
    return symbols


def symbols_lint(section_text: str, plan: Any) -> list[str]:
    """3.5 参数表符号列 ⊆ formula_plan.symbols（逐字），并跑 LaTeX 体例 lint。"""
    problems: list[str] = []
    plan_symbols = [_strip_math(s) for s in formula_service.plan_symbols(plan)]
    used = symbol_column(section_text)
    if plan_symbols and used:
        report = formula_service.symbols_consistency(
            plan_symbols, used, label_a="formula_plan.symbols", label_b="3.5 参数表符号列"
        )
        for extra in report.get("only_in_b") or []:
            problems.append(
                f"3.5 参数表出现未登记符号「{extra}」：符号须与 3.4.1 符号表 / formula_plan 逐字同形"
            )
        for near in report.get("near_miss") or []:
            problems.append(
                f"符号写法不同形：formula_plan 用「{near.get('a')}」而 3.5 用「{near.get('b')}」，须逐字一致"
            )
    for finding in formula_service.lint_latex(section_text):
        if finding.get("rule") in ("accent", "superscript_dimension", "delimiter_mix"):
            problems.append(f"LaTeX 体例[{finding.get('rule')}]：{finding.get('msg')}")
    return problems


def needs_formula(text: str) -> bool:
    """本案是否含公式（3.4 出现 LaTeX 片段或明确提到公式即需走 G4/G5 公式部分）。"""
    body = text or ""
    if formula_service.latex_segments(body):
        return True
    return "公式" in body or "3.4.1" in body


# ---------------------------------------------------------------------------
# 章节生成核心
# ---------------------------------------------------------------------------

CheckFn = Callable[[str], "list[str] | Awaitable[list[str]]"]


@dataclass
class ChapterResult:
    """一章的生成结果。"""

    key: str
    text: str
    sheet: TerminologySheet
    problems: list[str] = field(default_factory=list)
    repairs: int = 0
    degraded: int = 0

    def report(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name_zh": CHAPTER_NAMES.get(self.key, self.key),
            "chars": len(self.text),
            "repairs": self.repairs,
            "degraded": self.degraded,
            "unresolved": list(self.problems),
        }


async def run_checks(checks: Sequence[CheckFn], text: str) -> list[str]:
    """依次跑校验（同步/异步皆可），汇总问题清单。"""
    problems: list[str] = []
    for check in checks:
        result = check(text)
        if inspect.isawaitable(result):
            result = await result
        problems.extend(result or [])
    return problems


def _repair_user(original_user: str, text: str, problems: list[str], hint: str = "") -> str:
    """REPAIR 指令：原任务 + 违规清单 + 当前正文（要求整章重出，不要输出解释）。"""
    lines = [
        (
            "以下章节正文未通过平台的服务端校验。请**逐条**修正后重新输出**完整章节正文**"
            "（markdown，保持原有小节标题层级；不要输出解释、不要输出补丁清单）。"
        ),
        "",
        "【未通过的检查项】",
        *[f"- {p}" for p in problems],
    ]
    if hint:
        lines += ["", f"【修复要点】{hint}"]
    lines += [
        "",
        "【本章原始任务】",
        original_user,
        "",
        "【当前正文】",
        text,
    ]
    return "\n".join(lines)


async def generate_chapter(
    ctx: Any,
    *,
    key: str,
    parts: Sequence[str],
    runtime: Mapping[str, Any],
    user: str,
    sheet: TerminologySheet,
    checks: Sequence[CheckFn] = (),
    repair_hint: str = "",
    channel: str = "doc",
    max_repair: int = MAX_CHAPTER_REPAIR,
) -> ChapterResult:
    """生成一章：GEN 流式 → terms_delta 剥离合并 → lint 束 → 带错 REPAIR ≤2。"""
    system = assembler.assemble(list(parts), runtime_ctx=dict(runtime))
    raw = await stream_gen(ctx, tag=key, system=system, user=user, channel=channel)
    text, delta = terminology.extract_terms_delta(raw)
    sheet = terminology.merge_terms_delta(sheet, delta)

    problems = await run_checks(checks, text)
    repairs = 0
    while problems and repairs < max_repair:
        repairs += 1
        await ctx.emit(
            "log",
            {
                "message": (
                    f"{CHAPTER_NAMES.get(key, key)} 未通过服务端校验，正在第 {repairs} 次修复："
                    + "；".join(problems[:3])
                ),
                "kind": "chapter_repair",
                "step": key,
            },
        )
        raw = await call_text(
            ctx,
            tag=f"{key}.repair{repairs}",
            system=system,
            user=_repair_user(user, text, problems, repair_hint),
        )
        fixed, delta2 = terminology.extract_terms_delta(raw)
        if fixed.strip():
            text = fixed
            sheet = terminology.merge_terms_delta(sheet, delta2)
        problems = await run_checks(checks, text)

    return ChapterResult(key=key, text=text.strip(), sheet=sheet, problems=problems, repairs=repairs)


async def rewrite_chapter(
    ctx: Any,
    *,
    key: str,
    text: str,
    runtime: Mapping[str, Any],
    problems: Sequence[str],
    tag: str | None = None,
) -> str:
    """整章重写（§3.3 补丁应用率 <80% 的兜底）：REPAIR 调用，返回新正文。"""
    parts = CHAPTER_PARTS.get(key, G6_PARTS)
    system = assembler.assemble(list(parts), runtime_ctx=dict(runtime))
    user = _repair_user(
        f"重写「{CHAPTER_NAMES.get(key, key)}」，保持原有小节标题层级与已确认的技术事实。",
        text,
        list(problems),
    )
    raw = await call_text(ctx, tag=tag or f"rewrite.{key}", system=system, user=user)
    fixed, _delta = terminology.extract_terms_delta(raw)
    return fixed.strip() or text


# ---------------------------------------------------------------------------
# G0 骨架
# ---------------------------------------------------------------------------

G0_USER = """请为本案输出**交底书骨架**（仅一个 JSON 对象，不要正文）。

要求：
1. `case_title` 写成「一种……方法及系统」形式，须含本案领域对象核心词；
2. `title_core_terms` 列出案件名称中的**领域实词**（2–4 个，去掉「一种/基于/方法/系统」等套话）；
3. `modules` 为系统框图节点（3–6 个），`steps` 为流程步骤（`sid` 从 S1 连续编号，`label` 为场景短词）；
   **标题实词至少 1 个须出现在 modules 中，且至少 1 个须出现在 steps 的 label 中**（平台会校验）；
4. `chapter_outline` 用一句话概括每章要写什么（六章）；
5. `terms_init` 登记后文将反复出现的领域词（term + 一句场景定义），供全文术语贯穿。"""


async def generate_skeleton(
    ctx: Any,
    runtime: Mapping[str, Any],
    *,
    max_repair: int = MAX_SKELETON_REPAIR,
    parts: Sequence[str] | None = None,
    user: str | None = None,
    lint: Callable[[Skeleton], list[str]] | None = None,
) -> tuple[Skeleton, dict[str, Any]]:
    """G0：STRUCT 产骨架 + 标题贯穿校验（不过则带错 REPAIR ≤2）。

    缺省即发明分支（`G0_PARTS` + `G0_USER` + `terminology.skeleton_title_lint`）；
    实用新型 / 外观分支传入各自的 parts / 阶段指令 / 校验函数。
    """
    system = assembler.assemble(list(parts or G0_PARTS), runtime_ctx=dict(runtime))
    stage_user = user or G0_USER
    check = lint or terminology.skeleton_title_lint
    skeleton: Skeleton = await call_struct(
        ctx, Skeleton, tag="g0", system=system, user=stage_user
    )
    errors = check(skeleton)
    repairs = 0
    while errors and repairs < max_repair:
        repairs += 1
        await ctx.emit(
            "log",
            {"message": f"骨架标题贯穿校验未通过，正在第 {repairs} 次修复：{'；'.join(errors)}"},
        )
        skeleton = await call_struct(
            ctx,
            Skeleton,
            tag=f"g0.repair{repairs}",
            system=system,
            user=(
                "上一版骨架未通过平台的标题贯穿校验，问题如下：\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\n\n请修正后重新输出完整的骨架 JSON（标题实词须同时落到 modules 与 steps 标签里）。\n\n"
                + stage_user
            ),
            call_class="REPAIR",
        )
        errors = check(skeleton)
    return skeleton, {"repairs": repairs, "errors": errors}


# ---------------------------------------------------------------------------
# G4 公式计划
# ---------------------------------------------------------------------------


def formula_user(theme: str, flow_digest: str) -> str:
    """G4 的阶段指令（注入范式紧凑目录）。"""
    catalog = formula_service.catalog_summary()
    return (
        "请为本案输出 **formula_plan**（仅一个 JSON 对象）。\n\n"
        f"【本案主题】{theme}\n"
        f"【3.4 流程摘要】{flow_digest}\n\n"
        "硬性要求：\n"
        "1. `paradigm_ids` 只能取自下方范式目录中的 id（禁止自创式）；\n"
        "2. `equations` 每式含 `tag`（1/2…）、`paradigm_id`、`latex`、`role`；块级式尽量单行写完；\n"
        "3. `symbols` 逐个登记式中出现的符号（symbol / meaning_zh / unit_zh），"
        "维度写下标 `_{\\mathrm{cpu}}`，禁止上标维度与 \\tilde/\\hat/\\bar 装饰音；\n"
        "4. `numeric_example` 必须给出可代入的 `given` 与可复算的 `result`（平台会代入复算，算不平即打回）；\n"
        "5. `plain_zh` 用一句人话说明主式在算什么。\n\n"
        "【可选范式目录】\n" + catalog
    )


async def generate_formula_plan(
    ctx: Any,
    runtime: Mapping[str, Any],
    *,
    theme: str,
    flow_digest: str,
    max_repair: int = MAX_FORMULA_REPAIR,
) -> tuple[FormulaPlan | None, dict[str, Any]]:
    """G4：STRUCT 产公式计划 → `validate_formula_plan(--eval)` 门禁 → REPAIR ≤2。

    返回 `(plan, gate)`；`gate['ok']` 为 False 时由调用方 [PAUSE] 询问用户。
    门禁 `degraded=True`（环境坏）时**不**跑 REPAIR，直接放行并标记。
    """
    system = assembler.assemble(G4_PARTS, runtime_ctx=dict(runtime))
    user = formula_user(theme, flow_digest)
    plan: FormulaPlan = await call_struct(ctx, FormulaPlan, tag="g4", system=system, user=user)
    gate = await formula_service.avalidate_formula_plan(plan)
    repairs = 0
    while gate.get("errors") and not gate.get("degraded") and repairs < max_repair:
        repairs += 1
        await ctx.emit(
            "log",
            {
                "message": (
                    f"公式门禁未通过（第 {repairs} 次修复）："
                    + "；".join(str(e) for e in gate["errors"][:3])
                )
            },
        )
        plan = await call_struct(
            ctx,
            FormulaPlan,
            tag=f"g4.repair{repairs}",
            system=system,
            user=(
                "上一版 formula_plan 未通过平台公式门禁（含数值例复算），问题如下：\n"
                + "\n".join(f"- {e}" for e in gate["errors"])
                + ("\n提示：\n" + "\n".join(f"- {w}" for w in gate.get("warnings") or []) if gate.get("warnings") else "")
                + "\n\n请修正公式或数值例后重新输出完整的 formula_plan JSON。\n\n"
                + user
            ),
            call_class="REPAIR",
        )
        gate = await formula_service.avalidate_formula_plan(plan)
    gate = dict(gate)
    gate["repairs"] = repairs
    return plan, gate


# ---------------------------------------------------------------------------
# 文档装配（A7 前半）
# ---------------------------------------------------------------------------


def assemble_document(
    chapters: Mapping[str, str],
    *,
    case_title: str,
    contact: Mapping[str, Any] | None = None,
    patent_type: str = "invention",
    order: Sequence[str] | None = None,
) -> str:
    """文头模板 + 各章正文 → 交底书全文（章节间空一行，文末只留一个换行）。

    `order` 缺省按 `patent_type` 取：发明六章 / 实用新型 u1–u4 / 外观 d1–d2。
    """
    blocks = [document_header(case_title, contact, patent_type)]
    for key in order if order is not None else chapter_order(patent_type):
        body = (chapters.get(key) or "").strip()
        if body:
            blocks.append(body)
    return "\n\n".join(blocks).rstrip() + "\n"


def apply_term_family(text: str, sheet: Any) -> tuple[str, list[dict[str, Any]]]:
    """术语类补丁的联动：全文把 `forbidden_variants` 整族替换为规范词，返回替换计数。"""
    body = text or ""
    replaced: list[dict[str, Any]] = []
    data = terminology.to_prompt_dict(sheet)
    for term in data.get("terms") or []:
        canonical = str(term.get("term") or "").strip()
        if not canonical:
            continue
        for variant in term.get("forbidden_variants") or []:
            v = str(variant).strip()
            if not v or v == canonical or v not in body:
                continue
            count = body.count(v)
            body = body.replace(v, canonical)
            replaced.append({"from": v, "to": canonical, "count": count})
    return body, replaced


def prior_art_notes_text(notes: Sequence[Mapping[str, Any]]) -> str:
    """查新笔记 → 注入 G1 的全文文本（1.1 的唯一事实来源）。"""
    if not notes:
        return "（本案未进行专利检索或未纳入任何命中条目。）"
    lines: list[str] = []
    for i, note in enumerate(notes, 1):
        lines.append(
            "\n".join(
                [
                    f"{i}. 公开号：{note.get('pub_no') or '（无）'}",
                    f"   标题：{note.get('title') or ''}",
                    f"   申请方：{note.get('applicant') or ''}",
                    f"   技术方案：{note.get('scheme_summary') or ''}",
                    f"   应用场景：{note.get('application') or ''}",
                    f"   局限性：{note.get('limitation') or ''}",
                    f"   来源链接：{note.get('url') or ''}",
                    f"   备注：{note.get('note') or ''}（摘要已消化：{'是' if note.get('abstract_used') else '否'}）",
                ]
            )
        )
    return "\n".join(lines)


def json_block(value: Any) -> str:
    """注入 prompt 的 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 各章阶段指令（§2 A6 G1–G7）
# ---------------------------------------------------------------------------

G1_USER_HEAD = """请撰写交底书的**第一章**与**第二章**，小节标题固定如下（各占一行）：

## 一、介绍相关技术背景，描述与本发明技术最相近的现有技术，并说明该现有技术存在的缺点
### 1.1 现有技术
### 1.2 现有技术存在的缺点
## 二、针对上述缺点，说明本发明所要解决的技术问题

硬性要求：
1. **1.1 开头写「检索说明」**：只写公开数据库名称与本案主要检索词，面向代理人表述；
2. 按**技术方向**分类列举现有技术，每条含：专利号/文献标识、申请方、技术方案、应用场景、**局限性**，
   并在该条下用「**来源链接**：…」单独一行给出 URL；
3. **URL 只准照抄查新笔记中的 `url` 字段**（平台按命中链接集合做白名单校验），禁止编造或改写链接；
4. 有摘要消化结果的条目，技术方案叙述须与摘要理解一致，不得大段粘贴官方摘要；
5. 1.1 结尾写检索总结与**本发明与现有技术的本质区别**；1.2 分点列举缺点并与 1.1 局限性呼应；
6. 第二章对应 1.2 的缺点逐条说明本发明所要解决的技术问题；
7. 正文之后按术语规则附一个 `terms_delta` JSON 围栏块。"""

G1_UNSEARCHED = """
**本案未进行专利检索**：请在检索说明位置**如实写明**未进行系统性专利检索（一句即可，面向代理人），
1.1 依据材料中已知的现有技术概述；**严禁编造检索结果、专利号或链接**。"""

G2_USER = """请撰写第三章前三节，标题固定如下（各占一行）：

## 三、本发明技术方案的详细阐述
### 3.1 背景
### 3.2 系统框图
### 3.3 模块功能说明

硬性要求：
1. **3.1 成文顺序固定为三小段**（不另开小标题）：①场景与参与者；②标题对象如何进入系统；
   ③后文高频领域词的场景定义（每词一句人话，定义后全文同词）；
2. **3.2 用 fenced mermaid**（`flowchart TB`/`LR`，可用 `subgraph` 分层），节点标签用标题领域对象与场景短词，
   **禁止 ASCII 文字框图**；节点须覆盖骨架 `modules` 的全部模块；
3. **3.3 一模块一项**：按框图节点逐条写「作用 + 关联关系」，模块集合须与骨架 `modules` **完全一致**；
4. 用词与术语表同词同形，禁用变体不得出现在正文与图示标签中；
5. 正文之后附一个 `terms_delta` JSON 围栏块。"""

G3_USER = """请撰写 **### 3.4 系统流程说明**（本次只写 3.4，不要写 3.4.1 与 3.5）。

硬性要求：
1. 先给一段 **fenced mermaid 流程图**（`flowchart TD`），**步骤号必须写进可见标签**：
   正例 `S1["S1 采集节点指标"]`、判断节点 `S3{"S3 是否达阈"}`；反例 `S1[采集节点指标]`（出图丢序号）；
   步骤须与骨架 `steps` 一一对应且连续；
2. 再按步骤号**逐项文字说明** S1—Sn（与图中节点一一对应，不得几步合并成一段而不点步骤号），
   每步让标题领域对象做事；
3. 涉及打分、阈值、权重等形式化变量时，在文字中以「详见 3.4.1」引出，**本节不展开公式**；
4. 正文之后附一个 `terms_delta` JSON 围栏块。"""

G5_USER_FORMULA = """请撰写 **#### 3.4.1 符号与公式** 与 **### 3.5 关键技术参数**。

硬性要求：
1. 3.4.1 **先给符号表**（markdown 表格，列：`符号 | 含义 | 下标/量纲`），再给核心公式（式 (1)，
   必要时另列触发式 (2)），最后写文字解释与 3.4 流程的衔接；
2. 公式、符号必须与下方**已通过平台门禁的 formula_plan 逐字一致**，不得新增未登记符号；
3. LaTeX 全文统一：行内 `\\(...\\)`、块级 `\\[...\\]`；**禁止** `^{cpu}` 类上标写维度，
   **禁止** `\\tilde`/`\\hat`/`\\bar` 装饰音；块级公式尽量单行写完；
4. **3.5 参数表须设「符号」列**，与 3.4.1 **逐字同形**；另列含义、取值范围/约束；
5. 正文之后附一个 `terms_delta` JSON 围栏块。"""

G5_USER_PLAIN = """请撰写 **### 3.5 关键技术参数**（本案不含公式，**不要**写 3.4.1，也不要出现 LaTeX 公式）。

硬性要求：
1. 用 markdown 表格列出置信度/阈值类与控制类参数：参数名、含义、取值范围、约束条件；
2. 参数中文名与 3.1 领域术语同词；需要写清判定方式时用「领域名（如何判定/如何记录）」；
3. 与正文叙述、第六章参数示例保持一致；
4. 正文之后附一个 `terms_delta` JSON 围栏块。"""

G6_USER = """请撰写第四章与第五章，标题固定如下（各占一行）：

## 四、与现有技术相比，本发明具有哪些优点？
## 五、本发明的技术关键点和欲保护点是什么？

硬性要求：
1. 第四章先给概括性观点再分点详述，与第二章的技术问题、第五章的保护点呼应，技术细节以第三章为准；
2. **第五章每条保护点须是可实施机制**（对什么输入、如何判定或控制、得到什么输出），
   **禁止**写成仅有角色枚举（如「有三个××」）或只复述标题对象名称；详细方案引用第三章；
3. 用语与 3.1 场景术语同词，避免与第三章重复大段技术细节；
4. 正文之后附一个 `terms_delta` JSON 围栏块。"""

G7_USER = """请撰写第六章，标题固定如下：

## 六、其它
### 实施例
### 技术效果
### 参数设置示例

硬性要求：
1. **2～3 个有名字的实施例**（名称与 3.1/3.2 同词），各写清职责、可调用能力、交付物与相互依赖；
   走完**主路径**并至少写清**一条第三章已有的分支**（变更/中断/冲突/限频等）；
2. 实施例引用的步骤号必须 **⊆ 骨架 steps 的 S1—Sn**，不得出现新的步骤号；
3. 技术效果量化或定性说明；参数设置示例注明「不作为权利要求限制」，并与 3.5 及数值例一致；
4. 应用场景须脱敏（公司名、真实数值），但标题领域对象与实施例角色**不脱敏抽掉**；
5. 正文之后附一个 `terms_delta` JSON 围栏块。"""


def chapter_runtime(
    card: Mapping[str, Any],
    sheet: Any,
    skeleton: Skeleton,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一次章节生成调用的运行时上下文（案件卡片 + 术语表 + 骨架 + 阶段补充）。"""
    return {
        "case_card": dict(card),
        "terminology_sheet": terminology.to_prompt_dict(sheet),
        "outline": skeleton.model_dump(),
        "extra": dict(extra or {}),
    }


# ---------------------------------------------------------------------------
# 发明分支主流程（A6：G0 → G1 → G2 → G3 → G4 → G5 → G6 → G7）
# ---------------------------------------------------------------------------


async def build_invention(ctx: Any) -> dict[str, Any]:
    """发明交底书分章成文：返回合并进 `cases.state_json` 的产物。"""
    state = getattr(ctx, "state", None) or {}
    card = case_card(ctx)
    sensitive = [str(t) for t in (state.get("sensitive_terms") or [])]
    notes = [dict(n) for n in (state.get("prior_art_notes") or []) if isinstance(n, Mapping)]
    prior = dict(state.get("prior_art") or {})
    searched = bool(prior.get("searched"))
    allowed_urls = {str(n.get("url")) for n in notes if n.get("url")}
    points = list(state.get("selected_points") or [])
    digest_cards = list((state.get("material_digest") or {}).get("files") or [])
    preview_md = str((state.get("preview") or {}).get("markdown") or "")

    # ---------- G0 骨架 ----------
    await ctx.progress("产出骨架（案件名称 / 模块 / 流程步骤 / 章节大纲）", index=1, total=8, waiting_for="模型")
    await ctx.emit("log", {"message": "开始成文：先产出骨架（案件名称 / 模块 / 流程步骤 / 章节大纲）。"})
    skeleton, g0_report = await generate_skeleton(
        ctx,
        {
            "case_card": card,
            "extra": {
                "selected_points": points,
                "preview_summary": preview_md,
                "material_digest": digest_cards[:6],
                "prior_art_notes": notes,
                "searched": searched,
            },
        },
    )
    case_title = await set_case_title(ctx, skeleton.case_title) or (card.get("case_title") or "")
    card = {**card, "case_title": case_title}
    sheet = terminology.new_sheet(case_title, skeleton.title_core_terms, skeleton.terms_init)
    title_terms = list(sheet.title_core_terms)
    modules = list(skeleton.modules)
    steps = [s.model_dump() for s in skeleton.steps]

    chapters: dict[str, str] = {}
    reports: list[dict[str, Any]] = []
    degraded_total = 0

    def drift_check(*, title: bool) -> CheckFn:
        def _check(text: str) -> list[str]:
            findings = terminology.drift_lint(text, sheet, check_title_terms=title)
            # 只有禁用变体与标题贯穿属于必修项；别名提示不阻断成文
            hard = [f for f in findings if f.get("rule") != "alias_instead_of_canonical"]
            return drift_problems(hard)

        return _check

    def sensitive_check(text: str) -> list[str]:
        return sensitive_lint(text, sensitive, keep=[*title_terms, case_title])

    # ---------- G1 一 + 二章 ----------
    await ctx.progress("撰写第一、二章（技术领域与背景技术）", index=2, total=8, waiting_for="模型")
    g1_user = G1_USER_HEAD + ("" if searched else G1_UNSEARCHED) + (
        "\n\n【查新笔记（1.1 的唯一事实来源）】\n" + prior_art_notes_text(notes)
    )
    g1 = await generate_chapter(
        ctx,
        key="g1",
        parts=G1_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {"prior_art_notes": notes, "searched": searched,
             "search_terms": prior.get("terms") or [], "selected_points": points},
        ),
        user=g1_user,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["一、", "1.1", "1.2", "二、"]),
            lambda t: url_lint(t, allowed_urls),
            search_note_lint,
            sensitive_check,
            drift_check(title=False),
        ],
        repair_hint="URL 必须逐字取自查新笔记的 url 字段；检索说明只写公开数据库名称与检索词。",
    )
    sheet, chapters["g1"] = g1.sheet, g1.text
    reports.append(g1.report())

    # ---------- G2 3.1–3.3 ----------
    await ctx.progress("撰写 3.1–3.3（技术方案总体与模块框图）", index=3, total=8, waiting_for="模型")
    g2 = await generate_chapter(
        ctx,
        key="g2",
        parts=G2_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {"modules": modules, "chapter_1_2_digest": text_digest(chapters["g1"])},
        ),
        user=G2_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["三、", "3.1", "3.2", "3.3"]),
            mermaid_check,
            lambda t: module_lint(t, modules),
            sensitive_check,
            drift_check(title=True),
        ],
        repair_hint="mermaid 必须语法正确（平台会真实渲染校验）；3.3 的模块集合须与骨架 modules 完全一致。",
    )
    sheet = g2.sheet
    text, n = await degrade_mermaid(g2.text)
    degraded_total += n
    chapters["g2"] = text
    g2.degraded = n
    reports.append(g2.report())

    # ---------- G3 3.4 ----------
    await ctx.progress("撰写 3.4（核心流程）", index=4, total=8, waiting_for="模型")
    g3 = await generate_chapter(
        ctx,
        key="g3",
        parts=G3_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {"steps": steps, "chapter_3_digest": text_digest(chapters["g2"])},
        ),
        user=G3_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["3.4"]),
            mermaid_check,
            lambda t: step_label_lint(t, steps),
            lambda t: step_prose_lint(t, steps),
            sensitive_check,
            drift_check(title=True),
        ],
        repair_hint='步骤号必须写进可见标签，如 S1["S1 采集节点指标"]；S1—Sn 齐全连续。',
    )
    sheet = g3.sheet
    text, n = await degrade_mermaid(g3.text)
    degraded_total += n
    chapters["g3"] = text
    g3.degraded = n
    reports.append(g3.report())

    # ---------- G4 公式门禁 ----------
    await ctx.progress("公式门禁（范式选型与数值复算）", index=5, total=8, waiting_for="模型")
    formula_plan: FormulaPlan | None = None
    formula_gate: dict[str, Any] = {"needed": False}
    want_formula = state.get("formula")
    has_formula = needs_formula(chapters["g3"]) if want_formula is None else bool(want_formula)
    if has_formula:
        plan, gate = await generate_formula_plan(
            ctx,
            {"case_card": card, "outline": skeleton.model_dump(),
             "terminology_sheet": terminology.to_prompt_dict(sheet)},
            theme=case_title or skeleton.case_title,
            flow_digest=text_digest(chapters["g3"], 600),
        )
        formula_plan, formula_gate = plan, {**gate, "needed": True}
        if gate.get("errors") and not gate.get("degraded"):
            answer = await ctx.await_user(
                kind="form",
                prompt=(
                    "公式计划两轮修复后仍未通过平台门禁（数值例复算/范式合法性）：\n"
                    + "\n".join(f"· {e}" for e in gate["errors"][:5])
                    + "\n\n请选择：继续但本案不写公式（推荐），或手工给出正确的公式与数值例。"
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string", "title": "处置",
                            "enum": ["no_formula", "manual"], "default": "no_formula",
                        },
                        "latex": {"type": "string", "title": "手工公式（LaTeX，可留空）"},
                        "note": {"type": "string", "title": "补充说明"},
                    },
                },
                default={"action": "no_formula", "latex": "", "note": ""},
            )
            data = answer if isinstance(answer, Mapping) else {}
            action = str(data.get("action") or "no_formula")
            formula_gate["user_action"] = action
            formula_gate["user_note"] = str(data.get("note") or "")
            if action == "manual" and str(data.get("latex") or "").strip():
                formula_gate["manual_latex"] = str(data["latex"]).strip()
            else:
                has_formula = False
                formula_plan = None
        elif gate.get("degraded"):
            await ctx.emit(
                "log",
                {"message": "公式门禁环境不可用，已跳过复算校验（不触发修复），公式计划按原样使用。"},
            )

    # ---------- G5 3.4.1 + 3.5 ----------
    await ctx.progress("撰写 3.4.1 与 3.5（符号表与实现细节）", index=6, total=8, waiting_for="模型")
    plan_dump = formula_plan.model_dump() if formula_plan is not None else None
    g5_extra: dict[str, Any] = {"chapter_34_digest": text_digest(chapters["g3"])}
    if plan_dump is not None:
        g5_extra["formula_plan"] = plan_dump
    if formula_gate.get("manual_latex"):
        g5_extra["manual_formula"] = formula_gate["manual_latex"]
    g5_checks: list[CheckFn] = [
        lambda t: heading_lint(t, ["3.4.1", "3.5"] if has_formula else ["3.5"]),
        sensitive_check,
        drift_check(title=False),
    ]
    if plan_dump is not None:
        g5_checks.insert(1, lambda t: symbols_lint(t, plan_dump))
    g5 = await generate_chapter(
        ctx,
        key="g5",
        parts=G5_PARTS,
        runtime=chapter_runtime(card, sheet, skeleton, g5_extra),
        user=G5_USER_FORMULA if has_formula else G5_USER_PLAIN,
        sheet=sheet,
        checks=g5_checks,
        repair_hint="3.5 参数表「符号」列须与 3.4.1 / formula_plan 逐字同形；禁止上标维度与装饰音。",
    )
    sheet, chapters["g5"] = g5.sheet, g5.text
    reports.append(g5.report())

    # ---------- G6 四 + 五章 ----------
    await ctx.progress("撰写第四、五章（有益效果与权利要求要点）", index=7, total=8, waiting_for="模型")
    prev_digests = {CHAPTER_NAMES[k]: text_digest(chapters[k]) for k in ("g1", "g2", "g3", "g5")}
    g6 = await generate_chapter(
        ctx,
        key="g6",
        parts=G6_PARTS,
        runtime=chapter_runtime(card, sheet, skeleton, {"previous_chapters_digest": prev_digests}),
        user=G6_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["四、", "五、"]),
            section5_lint,
            sensitive_check,
            drift_check(title=False),
        ],
        repair_hint="第五章每条保护点都要写清输入、判定/控制方式与输出，不能只罗列角色。",
    )
    sheet, chapters["g6"] = g6.sheet, g6.text
    reports.append(g6.report())

    # ---------- G7 六章 ----------
    await ctx.progress("撰写第六章（附图说明与收尾）", index=8, total=8, waiting_for="模型")
    g7_extra: dict[str, Any] = {
        "previous_chapters_digest": {
            **prev_digests, CHAPTER_NAMES["g6"]: text_digest(chapters["g6"])
        },
        "steps": steps,
    }
    if plan_dump is not None:
        g7_extra["numeric_example"] = plan_dump.get("numeric_example")
    g7 = await generate_chapter(
        ctx,
        key="g7",
        parts=G7_PARTS,
        runtime=chapter_runtime(card, sheet, skeleton, g7_extra),
        user=G7_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["六、", "实施例"]),
            lambda t: embodiment_lint(t, steps),
            sensitive_check,
            drift_check(title=True),
        ],
        repair_hint="实施例的 S 号必须来自骨架步骤；至少写 2 个有名字的实施例。",
    )
    sheet, chapters["g7"] = g7.sheet, g7.text
    reports.append(g7.report())

    draft = assemble_document(
        chapters,
        case_title=case_title,
        contact=state.get("contact"),
        patent_type="invention",
    )
    unresolved = [p for r in reports for p in r["unresolved"]]
    await ctx.emit(
        "log",
        {
            "message": (
                f"六章成文完成（{len(draft)} 字）："
                + "、".join(f"{r['name_zh']}修复 {r['repairs']} 次" for r in reports if r["repairs"])
                + ("；" if any(r["repairs"] for r in reports) else "")
                + (f"mermaid 降级 {degraded_total} 处。" if degraded_total else "图示全部渲染通过。")
            )
        },
    )
    return {
        "skeleton": skeleton.model_dump(),
        "terminology_sheet": sheet.model_dump(),
        "chapters": chapters,
        "case_title": case_title,
        "formula_plan": plan_dump,
        "formula_gate": formula_gate,
        "draft_markdown": draft,
        "build_report": {
            "skeleton": g0_report,
            "chapters": reports,
            "mermaid_degraded": degraded_total,
            "unresolved": unresolved,
        },
    }


# ---------------------------------------------------------------------------
# 实用新型分支（A6：G0 骨架 → G1 一二章 → G2 3.1–3.3 → G3 3.4/3.5 → G4 四五六章）
# ---------------------------------------------------------------------------

U0_USER = """请为本案输出**实用新型交底书骨架**（仅一个 JSON 对象，不要正文）。

要求：
1. `case_title` 写成「一种……装置/结构」形式，须含本案的产品对象核心词（**不要**写成「方法」）；
2. `title_core_terms` 列出案件名称中的领域实词（2–4 个，去掉「一种/用于/装置/结构」等套话）；
3. `modules` 为**部件清单**：逐条取自事实合同 `parts` 的 `name`（按件号顺序，名称逐字一致，勿改叫法）；
4. `steps` 为**使用或拆装过程**的关键步骤（`sid` 从 S1 连续编号，`label` 为动作短词；
   材料未给出过程时，按可见构造给 2–4 步安装或使用步骤，不得杜撰工艺参数）；
5. `chapter_outline` 用一句话概括各节（一、二、3.1、3.2、3.3、3.4、3.5、四、五、六）要写什么；
6. `terms_init` 登记部件名与连接方式术语（term + 一句场景定义），供全文术语贯穿。"""

U1_USER_HEAD = """请撰写实用新型交底书的**第一章**与**第二章**，小节标题固定如下（各占一行）：

## 一、相关技术背景与最接近现有技术的缺点
### 1.1 现有技术
### 1.2 现有技术存在的缺点
## 二、本实用新型所要解决的技术问题

硬性要求：
1. **1.1 开头写「检索说明」**：只写公开数据库名称与本案主要检索词，面向代理人表述；
2. 现有技术按**结构方向**分类列举，每条含：专利号/文献标识、申请方、结构方案、应用场景、**局限性**，
   并在该条下用「**来源链接**：…」单独一行给出 URL；
3. **URL 只准照抄查新笔记中的 `url` 字段**（平台按命中链接集合做白名单校验），禁止编造或改写链接；
4. 1.2 的缺点须是**结构缺陷**（难拆装、占孔多、干涉、导热路径长、定位不稳等），
   与 1.1 的局限性逐条呼应，**不要**写成算法或流程缺陷；
5. 第二章对应 1.2 的缺点逐条说明本实用新型所要解决的技术问题；
6. 正文之后按术语规则附一个 `terms_delta` JSON 围栏块。"""

U2_USER = """请撰写第三章前三节，标题固定如下（各占一行）：

## 三、技术方案的详细阐述
### 3.1 总体构成
### 3.2 连接与配合
### 3.3 空间布局与附图说明

硬性要求：
1. **先表后文**：3.1 先给**部件表**（列：`件号 | 名称 | 形状要点`），逐条对齐事实合同 `parts`
   （件号与名称逐字一致，不得增删部件），再用一段话说明整体构成；
2. **3.2 给连接关系表**（列：`自 | 至 | 类型 | 位置`），逐条对齐事实合同 `relations`，
   表后用文字说明每处配合如何实现（咬合、限位、导向、密封等）；
3. **3.3 写空间布局与附图说明**：先写上下/内外/同轴/阵列等相对位置，再按**入文附图清单**
   逐图写「如图 N…」一句话说明其展示内容；有总装图与局部图时须写成
   「如图 M 为图 N 的局部放大/剖视…」，与 `relates_to` 一致；
4. **只准引用入文清单中的图号**（平台按 figure_plan 校验），不得凭空写图，也不得漏图；
5. 事实合同 `uncertain` 中的内容一律标注为待确认，**禁止**写成既定尺寸、公差或材料；
6. 正文之后附一个 `terms_delta` JSON 围栏块。"""

U3_USER = """请撰写 **### 3.4 结构作用与使用或拆装过程**{u35}。

硬性要求：
1. 3.4 先逐条写清各构造**解决什么装配/受力/通道/拆装问题**（一条一句，落到具体件号与连接处），
   再按骨架 `steps` 的 S 号写出使用或拆装过程（S1—Sn，每步一句）；
2. **不得写成方法专利主线**：不要展开算法、控制策略、工艺参数曲线，过程只用于说明结构如何被使用；
3. 涉及看不清或材料未写明的尺寸/公差/材料时，写「待确认」，**禁止**杜撰数值；
4. 需要示意连接关系时可用一段简短 fenced mermaid `flowchart`（如 `A -->|卡扣| B`），
   **不要**画发明那套系统框图或调度流程图；
5. 正文之后附一个 `terms_delta` JSON 围栏块。"""

U3_SECTION_35 = """ 与 **### 3.5 关键尺寸或材料**"""

U4_USER = """请撰写第四、五、六章，标题固定如下（各占一行）：

## 四、与现有技术相比的优点
## 五、本实用新型的技术关键点与欲保护点
## 六、其它

硬性要求：
1. 第四章先给概括性观点再分点详述，与第二章的结构问题、第五章的保护点呼应，细节以第三章为准；
2. **第五章必须是装置/结构书式**：主保护点写成
   「一种……装置（或结构），包括……；其特征在于……（连接关系/形状限定）」，
   随后可分条列出从属的构造特征；**严禁**把「一种……方法，包括以下步骤」或工艺步骤罗列作为主保护点
   （平台会做书式校验，违规将打回重写）；
3. 保护点只写**可见可制造的构造特征**，引用件号与第三章一致；不得把外观美感或算法当构造创新；
4. 第六章写 1–2 个实施例（同一装置在不同使用场景/规格下的实现）与技术效果，
   参数示例注明「不作为权利要求限制」；
5. 正文之后附一个 `terms_delta` JSON 围栏块。"""

CLAIM_FORM_USER = """以下是实用新型交底书的第五章（技术关键点与欲保护点）。

请判断其**主保护点是否为装置/结构书式**（「一种……装置，包括……；其特征在于……」），
并检查是否存在「以方法步骤/工艺参数作为主保护点」的问题。仅输出一个 JSON 围栏块：
`{"verdict":"pass|fail","is_device_form":true,"problems":["…"],"suggestion":"一句改写建议"}`。

判定口径：主保护点须落在部件、形状与连接关系上；从属点可涉及使用过程，但不得取代装置主保护点。

===== 第五章正文 =====
"""


def utility_skeleton_lint(parts: Sequence[Any]) -> Callable[[Skeleton], list[str]]:
    """实用新型骨架校验：标题实词落到部件清单；modules 与事实合同部件对齐。"""
    names = [name for _pid, name in _part_names(parts)]

    def _check(skeleton: Skeleton) -> list[str]:
        errors: list[str] = []
        title = (skeleton.case_title or "").strip()
        if not title:
            errors.append("case_title 为空")
        core = [t.strip() for t in skeleton.title_core_terms if str(t).strip()]
        if not core:
            core = terminology.extract_title_terms(title)
        if not core:
            errors.append("title_core_terms 为空，且无法从 case_title 提取领域实词")
        if not skeleton.modules:
            errors.append("modules 为空：实用新型骨架的 modules 须为部件清单")
        else:
            blob = " ".join(skeleton.modules)
            if core and not any(t in blob for t in core):
                errors.append(f"部件清单中未出现任何标题实词（{'、'.join(core[:5])}）")
            missing = [n for n in names if n not in blob]
            if missing:
                errors.append("部件清单缺少事实合同中的部件：" + "、".join(missing[:6]))
        if "方法" in title and "装置" not in title and "结构" not in title:
            errors.append("case_title 写成了方法名称：实用新型须写成「一种……装置/结构」")
        return errors

    return _check


async def _claim_form_audit(ctx: Any, card: Mapping[str, Any], text: str, round_no: int) -> list[str]:
    """第五章书式的 AUDIT 一半（与正则双查；调用失败不阻断成文）。"""
    idx = (text or "").find("五、")
    section = (text or "")[idx:] if idx >= 0 else (text or "")
    nxt = section.find("## 六、")
    if nxt > 0:
        section = section[:nxt]
    if not section.strip():
        return []
    system = assembler.assemble(
        ["common/system_base", "disclosure/utility_model/builder"],
        runtime_ctx={"case_card": dict(card)},
    )
    try:
        report: ClaimFormAudit = await call_struct(
            ctx,
            ClaimFormAudit,
            tag=f"u4.claim_audit{round_no}",
            system=system,
            user=CLAIM_FORM_USER + section,
            call_class="AUDIT",
        )
    except Exception as exc:  # noqa: BLE001 —— 审查调用失败时以正则结论为准
        logger.warning("第五章书式 AUDIT 调用失败，仅以正则结论为准：%s", exc)
        return []
    if report.verdict == "pass" and report.is_device_form:
        return []
    problems = [f"第五章书式审查未通过：{p}" for p in report.problems] or [
        "第五章书式审查未通过：主保护点不是装置/结构书式"
    ]
    if report.suggestion:
        problems.append(f"改写建议：{report.suggestion}")
    return problems


async def build_utility_model(ctx: Any) -> dict[str, Any]:
    """实用新型交底书分章成文：返回合并进 `cases.state_json` 的产物。"""
    await ctx.progress("产出骨架", index=1, total=5, waiting_for="模型")
    state = getattr(ctx, "state", None) or {}
    card = case_card(ctx)
    sensitive = [str(t) for t in (state.get("sensitive_terms") or [])]
    notes = [dict(n) for n in (state.get("prior_art_notes") or []) if isinstance(n, Mapping)]
    prior = dict(state.get("prior_art") or {})
    searched = bool(prior.get("searched"))
    allowed_urls = {str(n.get("url")) for n in notes if n.get("url")}
    points = list(state.get("selected_points") or [])
    schema = dict(state.get("structure_schema") or {})
    plan = dict(state.get("figure_plan") or {})
    parts = list(schema.get("parts") or [])
    relations = list(schema.get("relations") or [])
    uncertain = list(schema.get("uncertain") or [])
    figures = figure_plan_service.figure_digest(plan)
    preview_md = str((state.get("preview") or {}).get("markdown") or "")

    await ctx.emit(
        "log",
        {
            "message": (
                f"开始成文（实用新型）：部件 {len(parts)} 项、连接 {len(relations)} 处、"
                f"入文附图 {len(figures)} 幅。"
            )
        },
    )

    skeleton, g0_report = await generate_skeleton(
        ctx,
        {
            "case_card": card,
            "extra": {
                "structure_schema": schema,
                "figure_plan": figures,
                "selected_points": points,
                "preview_summary": preview_md,
                "prior_art_notes": notes,
                "searched": searched,
            },
        },
        parts=U0_PARTS,
        user=U0_USER,
        lint=utility_skeleton_lint(parts),
    )
    case_title = await set_case_title(ctx, skeleton.case_title) or (card.get("case_title") or "")
    card = {**card, "case_title": case_title}
    sheet = terminology.new_sheet(case_title, skeleton.title_core_terms, skeleton.terms_init)
    title_terms = list(sheet.title_core_terms)
    steps = [s.model_dump() for s in skeleton.steps]

    chapters: dict[str, str] = {}
    reports: list[dict[str, Any]] = []
    degraded_total = 0

    def drift_check(*, title: bool) -> CheckFn:
        def _check(text: str) -> list[str]:
            findings = terminology.drift_lint(text, sheet, check_title_terms=title)
            hard = [f for f in findings if f.get("rule") != "alias_instead_of_canonical"]
            return drift_problems(hard)

        return _check

    def sensitive_check(text: str) -> list[str]:
        return sensitive_lint(text, sensitive, keep=[*title_terms, case_title])

    def uncertain_check(text: str) -> list[str]:
        return uncertain_lint(text, uncertain)

    # ---------- G1 一 + 二章 ----------
    await ctx.progress("撰写第一、二章", index=2, total=5, waiting_for="模型")
    u1_user = U1_USER_HEAD + ("" if searched else G1_UNSEARCHED) + (
        "\n\n【查新笔记（1.1 的唯一事实来源）】\n" + prior_art_notes_text(notes)
    )
    u1 = await generate_chapter(
        ctx,
        key="u1",
        parts=U1_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {"prior_art_notes": notes, "searched": searched,
             "search_terms": prior.get("terms") or [], "selected_points": points,
             "structure_schema": schema},
        ),
        user=u1_user,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["一、", "1.1", "1.2", "二、"]),
            lambda t: url_lint(t, allowed_urls),
            search_note_lint,
            sensitive_check,
            drift_check(title=False),
        ],
        repair_hint="URL 必须逐字取自查新笔记的 url 字段；缺点须写成结构缺陷而非流程缺陷。",
    )
    sheet, chapters["u1"] = u1.sheet, u1.text
    reports.append(u1.report())

    # ---------- G2 3.1–3.3（对齐 parts / relations / figure_plan） ----------
    await ctx.progress("撰写 3.1–3.3（部件与连接关系）", index=3, total=5, waiting_for="模型")
    u2 = await generate_chapter(
        ctx,
        key="u2",
        parts=U2_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {
                "structure_schema": schema,
                "parts": parts,
                "relations": relations,
                "spatial": list(schema.get("spatial") or []),
                "uncertain": uncertain,
                "figure_plan": figures,
                "chapter_1_2_digest": text_digest(chapters["u1"]),
            },
        ),
        user=U2_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["三、", "3.1", "3.2", "3.3"]),
            lambda t: parts_lint(t, parts),
            lambda t: relations_lint(t, relations, parts),
            lambda t: figure_plan_service.figure_ref_lint(t, plan),
            uncertain_check,
            sensitive_check,
            drift_check(title=True),
        ],
        repair_hint="部件表与连接关系表须与事实合同逐条对齐；「如图 N」只准引用入文附图清单中的图号。",
    )
    sheet = u2.sheet
    text, n = await degrade_mermaid(u2.text)
    degraded_total += n
    # 附图按 figure_plan 确定性嵌入（md 与 Word 同一套入文图）
    figure_block = figure_plan_service.figure_markdown(plan, heading="#### 附图")
    chapters["u2"] = (text.rstrip() + "\n\n" + figure_block).rstrip() if figure_block else text
    u2.degraded = n
    reports.append(u2.report())

    # ---------- G3 3.4 + 3.5 ----------
    await ctx.progress("撰写 3.4 与 3.5", index=4, total=5, waiting_for="模型")
    want_35 = any(
        str((p if isinstance(p, Mapping) else {}).get("material_hint") or "").strip()
        not in ("", "unknown", "未知")
        for p in parts
    ) or bool(schema.get("delta_hypothesis"))
    u3_user = U3_USER.format(u35=U3_SECTION_35 if want_35 else "（本案不写 3.5）")
    if want_35:
        u3_user += (
            "\n6. **3.5 关键尺寸或材料**：用表格列出材料/尺寸要求（列：`部位 | 材料或尺寸 | 依据`），"
            "只写事实合同中已给出的信息，未给出的写「待确认」。"
        )
    u3 = await generate_chapter(
        ctx,
        key="u3",
        parts=U3_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {
                "structure_schema": schema,
                "steps": steps,
                "function_of_structure": list(schema.get("function_of_structure") or []),
                "uncertain": uncertain,
                "chapter_3_digest": text_digest(chapters["u2"]),
            },
        ),
        user=u3_user,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["3.4", "3.5"] if want_35 else ["3.4"]),
            uncertain_check,
            sensitive_check,
            drift_check(title=True),
        ],
        repair_hint="3.4 只说明结构作用与使用/拆装过程，不得展开算法或工艺参数；uncertain 项写「待确认」。",
    )
    sheet = u3.sheet
    text, n = await degrade_mermaid(u3.text)
    degraded_total += n
    chapters["u3"] = text
    u3.degraded = n
    reports.append(u3.report())

    # ---------- G4 四 + 五 + 六章（第五章书式：正则 + AUDIT 双查） ----------
    await ctx.progress("撰写第四、五、六章", index=5, total=5, waiting_for="模型")
    audit_round = {"n": 0}

    async def claim_form_check(text: str) -> list[str]:
        audit_round["n"] += 1
        return await _claim_form_audit(ctx, card, text, audit_round["n"])

    prev_digests = {CHAPTER_NAMES[k]: text_digest(chapters[k]) for k in ("u1", "u2", "u3")}
    u4 = await generate_chapter(
        ctx,
        key="u4",
        parts=U4_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {"previous_chapters_digest": prev_digests, "parts": parts,
             "relations": relations, "figure_plan": figures},
        ),
        user=U4_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["四、", "五、", "六、"]),
            section5_device_lint,
            section5_lint,
            claim_form_check,
            sensitive_check,
            drift_check(title=False),
        ],
        repair_hint=(
            "第五章主保护点必须写成「一种……装置，包括……；其特征在于……」的装置书式，"
            "严禁以方法步骤作主保护点。"
        ),
    )
    sheet, chapters["u4"] = u4.sheet, u4.text
    reports.append(u4.report())

    draft = assemble_document(
        chapters,
        case_title=case_title,
        contact=state.get("contact"),
        patent_type="utility_model",
    )
    unresolved = [p for r in reports for p in r["unresolved"]]
    await ctx.emit(
        "log",
        {
            "message": (
                f"实用新型交底书成文完成（{len(draft)} 字）："
                + "、".join(f"{r['name_zh']}修复 {r['repairs']} 次" for r in reports if r["repairs"])
                + ("；" if any(r["repairs"] for r in reports) else "")
                + f"入文附图 {len(figures)} 幅。"
            )
        },
    )
    return {
        "skeleton": skeleton.model_dump(),
        "terminology_sheet": sheet.model_dump(),
        "chapters": chapters,
        "case_title": case_title,
        "draft_markdown": draft,
        "build_report": {
            "skeleton": g0_report,
            "chapters": reports,
            "mermaid_degraded": degraded_total,
            "figures": len(figures),
            "claim_form_audits": audit_round["n"],
            "unresolved": unresolved,
        },
    }


# ---------------------------------------------------------------------------
# 外观设计分支（A6：G0 简化骨架 → G1 一二三章 → G2 四五章）
# ---------------------------------------------------------------------------

D0_USER = """请为本案输出**外观设计交底底稿的简化骨架**（仅一个 JSON 对象，不要正文）。

要求：
1. `case_title` 写**产品名称**（如「台灯」「输液泵外壳」），与事实合同 `product_name` 一致，
   **不要**写成「一种……方法/装置」；
2. `title_core_terms` 列出产品名称中的领域实词（1–3 个）；
3. `modules` 逐条写**设计要点**（取自事实合同 `design_points`，看得见的形状/线条/比例/图案/配色）；
4. `steps` 逐条写**拟入文的视图名**（`sid` 从 S1 连续编号，`label` 为视图名，如「立体图」「主视」），
   视图集合＝要点落面的正投影（+ 立体图），**不要**默认凑六视；
5. `chapter_outline` 用一句话概括一、二、三、四、五章各写什么；
6. `terms_init` 登记造型术语（term + 一句定义），供全文同词同形。"""

D1_USER = """请撰写外观设计说明的**第一、二、三章**，标题固定如下（各占一行）：

## 一、产品名称与用途
## 二、设计要点
## 三、视图说明

硬性要求：
1. 一、写产品名称（与文头一致）与用途，一段即可；
2. 二、逐条写设计要点，对齐事实合同 `design_points`（形状 / 图案 / 色彩或其结合），
   **只写看得见的造型**，禁止写内部电路、受力、装配公差、工艺步骤与算法；
3. 三、按事实合同的 `product_form` 与 `claimed_faces` 写视图说明：**要点落面**的正投影逐面一句，
   立体产品另加立体图；**不要**默认收齐六视；
4. `omitted_views`（故意不交的面）须写「简要说明」，逐条写明省略理由（相同/对称/无要点）；
   要点落面缺源图的写入待确认，**不要**写成「缺六视」；
5. 正文「见图 N」**只准引用入文附图清单中的图号**，多视之间的关系须与 `relates_to` 一致；
6. 事实合同 `uncertain` 中的内容（看不清的色彩、纹理等）一律写成待确认，**禁止**写成既定事实；
7. 正文之后附一个 `terms_delta` JSON 围栏块。"""

D2_USER = """请撰写外观设计说明的**第四、五章**，标题固定如下（各占一行）：

## 四、与在先外观的主要差异
## 五、其它

硬性要求：
1. 第四章逐条写本设计与在先外观的可见差异（轮廓、比例、线条走向、图案、配色），
   每条须落到具体视图；**禁止无依据贬低**在先设计；
2. 引用在先外观时须给出可核验来源，**URL 只准照抄查新笔记中的 `url` 字段**（平台做白名单校验）；
   未进行检索时如实写明，**严禁编造**公开号或链接；
3. 第五章写使用状态参考图说明、色彩/材质说明等可选内容；**不写内部结构**；
4. 事实合同 `uncertain` 与 `contrast_to_prior` 中的假设须标注为假设或待确认；
5. 正文之后附一个 `terms_delta` JSON 围栏块。"""


def design_skeleton_lint(appearance: Mapping[str, Any]) -> Callable[[Skeleton], list[str]]:
    """外观简化骨架校验：产品名称非空、设计要点与视图清单齐备。"""
    points = [str(p).strip() for p in (appearance.get("design_points") or []) if str(p).strip()]
    faces = [str(f).strip() for f in (appearance.get("claimed_faces") or []) if str(f).strip()]

    def _check(skeleton: Skeleton) -> list[str]:
        errors: list[str] = []
        title = (skeleton.case_title or "").strip()
        if not title:
            errors.append("case_title 为空：外观骨架的 case_title 写产品名称")
        if "方法" in title or "系统" in title:
            errors.append("case_title 不应写成方法/系统名称：外观交底的名称是产品名称")
        if not skeleton.title_core_terms and not terminology.extract_title_terms(title):
            errors.append("title_core_terms 为空，且无法从产品名称提取领域实词")
        if not skeleton.modules:
            errors.append("modules 为空：外观骨架的 modules 须逐条写设计要点")
        elif points:
            blob = " ".join(skeleton.modules)
            missing = [p for p in points if p not in blob]
            if missing:
                errors.append("设计要点缺少事实合同中的条目：" + "、".join(m[:16] for m in missing[:4]))
        if not skeleton.steps:
            errors.append("steps 为空：外观骨架的 steps 须逐条写拟入文的视图名")
        elif faces:
            blob = " ".join(f"{s.sid} {s.label}" for s in skeleton.steps)
            missing = [f for f in faces if f not in blob]
            if missing:
                errors.append("视图清单未覆盖设计要点落面：" + "、".join(missing[:4]))
        return errors

    return _check


async def build_design(ctx: Any) -> dict[str, Any]:
    """外观设计交底底稿分章成文：返回合并进 `cases.state_json` 的产物。"""
    await ctx.progress("产出骨架", index=1, total=3, waiting_for="模型")
    state = getattr(ctx, "state", None) or {}
    card = case_card(ctx)
    sensitive = [str(t) for t in (state.get("sensitive_terms") or [])]
    notes = [dict(n) for n in (state.get("prior_art_notes") or []) if isinstance(n, Mapping)]
    prior = dict(state.get("prior_art") or {})
    searched = bool(prior.get("searched"))
    allowed_urls = {str(n.get("url")) for n in notes if n.get("url")}
    points = list(state.get("selected_points") or [])
    appearance = dict(state.get("appearance_schema") or {})
    plan = dict(state.get("figure_plan") or {})
    uncertain = list(appearance.get("uncertain") or [])
    figures = figure_plan_service.figure_digest(plan)
    preview_md = str((state.get("preview") or {}).get("markdown") or "")

    await ctx.emit(
        "log",
        {
            "message": (
                f"开始成文（外观设计）：设计要点 {len(appearance.get('design_points') or [])} 条、"
                f"要点落面 {len(appearance.get('claimed_faces') or [])} 个、入文附图 {len(figures)} 幅。"
            )
        },
    )

    skeleton, g0_report = await generate_skeleton(
        ctx,
        {
            "case_card": card,
            "extra": {
                "appearance_schema": appearance,
                "figure_plan": figures,
                "selected_points": points,
                "preview_summary": preview_md,
            },
        },
        parts=D0_PARTS,
        user=D0_USER,
        lint=design_skeleton_lint(appearance),
    )
    case_title = await set_case_title(ctx, skeleton.case_title) or (card.get("case_title") or "")
    card = {**card, "case_title": case_title}
    sheet = terminology.new_sheet(case_title, skeleton.title_core_terms, skeleton.terms_init)
    title_terms = list(sheet.title_core_terms)

    chapters: dict[str, str] = {}
    reports: list[dict[str, Any]] = []
    degraded_total = 0

    def drift_check(*, title: bool) -> CheckFn:
        def _check(text: str) -> list[str]:
            findings = terminology.drift_lint(text, sheet, check_title_terms=title)
            hard = [f for f in findings if f.get("rule") != "alias_instead_of_canonical"]
            return drift_problems(hard)

        return _check

    def sensitive_check(text: str) -> list[str]:
        return sensitive_lint(text, sensitive, keep=[*title_terms, case_title])

    def uncertain_check(text: str) -> list[str]:
        return uncertain_lint(text, uncertain)

    # ---------- G1 一 + 二 + 三章（含视图说明） ----------
    await ctx.progress("撰写第一、二、三章（含视图说明）", index=2, total=3, waiting_for="模型")
    d1 = await generate_chapter(
        ctx,
        key="d1",
        parts=D1_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {
                "appearance_schema": appearance,
                "product_form": appearance.get("product_form") or "solid",
                "claimed_faces": list(appearance.get("claimed_faces") or []),
                "omitted_views": list(appearance.get("omitted_views") or []),
                "views": list(appearance.get("views") or []),
                "design_points": list(appearance.get("design_points") or []),
                "ornament": list(appearance.get("ornament") or []),
                "color": list(appearance.get("color") or []),
                "uncertain": uncertain,
                "figure_plan": figures,
                "selected_points": points,
            },
        ),
        user=D1_USER,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["一、", "二、", "三、"]),
            lambda t: view_lint(t, appearance, plan),
            visible_only_lint,
            uncertain_check,
            sensitive_check,
            drift_check(title=True),
        ],
        repair_hint=(
            "视图集合按 claimed_faces 定（不要默认六视）；omitted_views 逐条写简要说明；"
            "「见图 N」只准引用入文附图清单中的图号。"
        ),
    )
    sheet = d1.sheet
    text, n = await degrade_mermaid(d1.text)
    degraded_total += n
    figure_block = figure_plan_service.figure_markdown(plan, heading="#### 附图（实拍与线稿）")
    chapters["d1"] = (text.rstrip() + "\n\n" + figure_block).rstrip() if figure_block else text
    d1.degraded = n
    reports.append(d1.report())

    # ---------- G2 四 + 五章 ----------
    await ctx.progress("撰写第四、五章", index=3, total=3, waiting_for="模型")
    d2_user = D2_USER + (
        "" if searched else "\n\n**本案未进行专利检索**：第四章须如实写明未进行系统性检索，"
        "只据材料中已知的常见外观作对比，**严禁编造**在先外观、公开号或链接。"
    ) + ("\n\n【查新笔记（在先外观的唯一事实来源）】\n" + prior_art_notes_text(notes))
    d2 = await generate_chapter(
        ctx,
        key="d2",
        parts=D2_PARTS,
        runtime=chapter_runtime(
            card, sheet, skeleton,
            {
                "prior_art_notes": notes,
                "searched": searched,
                "contrast_to_prior": list(appearance.get("contrast_to_prior") or []),
                "uncertain": uncertain,
                "figure_plan": figures,
                "chapter_123_digest": text_digest(chapters["d1"]),
            },
        ),
        user=d2_user,
        sheet=sheet,
        checks=[
            lambda t: heading_lint(t, ["四、", "五、"]),
            lambda t: url_lint(t, allowed_urls),
            search_note_lint,
            visible_only_lint,
            uncertain_check,
            sensitive_check,
            drift_check(title=False),
        ],
        repair_hint="在先外观的 URL 必须逐字取自查新笔记；差异须落到具体视图，禁止贬低未检索对象。",
    )
    sheet, chapters["d2"] = d2.sheet, d2.text
    reports.append(d2.report())

    draft = assemble_document(
        chapters,
        case_title=case_title,
        contact=state.get("contact"),
        patent_type="design",
    )
    unresolved = [p for r in reports for p in r["unresolved"]]
    await ctx.emit(
        "log",
        {
            "message": (
                f"外观设计说明成文完成（{len(draft)} 字）："
                + "、".join(f"{r['name_zh']}修复 {r['repairs']} 次" for r in reports if r["repairs"])
                + ("；" if any(r["repairs"] for r in reports) else "")
                + f"入文附图 {len(figures)} 幅（实拍与线稿同时入文）。"
            )
        },
    )
    return {
        "skeleton": skeleton.model_dump(),
        "terminology_sheet": sheet.model_dump(),
        "chapters": chapters,
        "case_title": case_title,
        "draft_markdown": draft,
        "build_report": {
            "skeleton": g0_report,
            "chapters": reports,
            "mermaid_degraded": degraded_total,
            "figures": len(figures),
            "unresolved": unresolved,
        },
    }


BUILDERS: dict[str, Any] = {
    "invention": build_invention,
    "utility_model": build_utility_model,
    "design": build_design,
}


async def build_document(ctx: Any, patent_type: str) -> dict[str, Any]:
    """A6 成文入口：按专利类型分派到三条分支。"""
    builder = BUILDERS.get(patent_type)
    if builder is None:
        raise ValueError(f"未知专利类型：{patent_type}")
    return await builder(ctx)
