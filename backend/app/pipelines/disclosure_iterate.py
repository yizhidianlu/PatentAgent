"""专利交底书 · 迭代流水线（模块 A-迭代，prompt-porting-spec.md §2 模块A「A-迭代」）。

作用于**已交付定稿**的案件，`run_group='iteration-<n>'`，五步：

    iterate_route(IT0 意图路由) → iterate_plan(增量/纠正点识别)
        → iterate_rewrite(仅受影响章节改写) → iterate_check(自检/lint 束)
        → iterate_deliver(新时间戳交付 + 留档摘要 + 修订记录)

分支（IT0 STRUCT，`iteration/intent_router.md`）：

- `merge`   —— 新材料消化 → 增量识别 → 受影响章节非破坏性重写（merger.md 规则注入）
              → needs_search 触发补充查新 → figure_plan / formula_plan 同步
              → **复用主流水线 `disclosure.self_check`** → 新版本 md/docx/pdf
              → 强制在 chat 通道输出「## 合并摘要（留档）」→ 再附 claim_bias 引导；
- `correct` —— 纠正点五分类 → 逐点补丁（术语类走全文 find-replace 并回报替换计数；
              公式类过 formula 门禁复跑）→ lint 束 → 新版本（iteration_type='correction'）
              → 「## 纠正摘要（留档）」；
- `question`—— CHAT 答疑，**不动稿**、不产新版本，后续步骤空转；
- `new_case`—— 提示建议新开案件并结束。

纪律：
- **禁止覆盖旧稿**：所有产物经 `services/artifacts.py` 落新时间戳文件（version+1）；
- **基准稿以交付定稿为准**：迭代起点是 `state.final_markdown`（含首轮自检补丁），
  按固定模板锚点切回分章视图（`split_document`），避免用未打补丁的 `chapters` 回退正文；
- 生成/校验/装配一律复用 `services/disclosure_build.py` 与 `pipelines/disclosure.py`
  的既有函数，本文件只做迭代编排。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import anyio.to_thread
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.disclosure import MaterialDigest, SelfCheckReport
from ..services import artifacts as artifacts_service
from ..services import assembler, cnipa, revision_log, terminology
from ..services import disclosure_build as build_service
from ..services import export_docx as export_docx_service
from ..services import export_pdf as export_pdf_service
from ..services import formula as formula_service
from ..services import patches as patches_service
from . import disclosure as main_pipeline
from .engine import Ctx, InteractionRequest, StepDef

logger = logging.getLogger(__name__)

DOC_ID = build_service.DOC_ID
BASE = main_pipeline.BASE

# ---------------------------------------------------------------------------
# prompt 装配清单（§0.2）
# ---------------------------------------------------------------------------

INTENT_PARTS = [*BASE, "disclosure/iteration/intent_router"]
MERGE_PARTS = [
    *BASE,
    "disclosure/iteration/merger",
    "common/desensitization",
    "common/terminology_rules",
]
CORRECT_PARTS = [
    *BASE,
    "disclosure/iteration/correction",
    "common/desensitization",
    "common/terminology_rules",
]
ANSWER_PARTS = [*BASE, "disclosure/invention/builder_core"]

# 上限常量
MAX_MERGE_REPAIR = 1            # 受影响章节改写的服务端校验修复次数
MAX_CORRECTIONS = 8             # 单轮最多处理的纠正点数
MAX_INCREMENTS = 8              # 单轮最多处理的增量条数
SEARCH_TERM_LIMIT = 8           # 补充查新的检索单位上限（与 cnipa 一致）

MERGE_HEADING = "## 合并摘要（留档）"
CORRECT_HEADING = "## 纠正摘要（留档）"

CORRECTION_CATEGORIES: tuple[str, ...] = (
    "事实与技术",
    "术语与可读性",
    "符号与公式体例",
    "查新与区别",
    "保护点与表述与附图",
)

# 纠正分类 → 默认落点章节（用户未指明章节时的兜底）
CATEGORY_CHAPTERS: dict[str, str] = {
    "事实与技术": "g3",
    "术语与可读性": "",          # 全文族替换，不限章
    "符号与公式体例": "g5",
    "查新与区别": "g1",
    "保护点与表述与附图": "g6",
}


# ---------------------------------------------------------------------------
# STRUCT 契约（迭代专用；主流水线契约在 models/disclosure.py）
# ---------------------------------------------------------------------------


class IterationIntent(BaseModel):
    """IT0 意图路由输出（`iteration/intent_router.md`）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    intent: Literal["merge", "correct", "question", "new_case"] = "correct"
    rationale: str = Field(default="", validation_alias=AliasChoices("rationale", "reason"))
    evidence: str = ""
    points: list[str] = Field(default_factory=list, description="用户提出的要点（逐条摘要）")


class MergeIncrement(BaseModel):
    """一条增量：落到哪一章、写什么、是否需要补查新。"""

    model_config = ConfigDict(extra="ignore")

    target_chapter: str = ""
    summary: str = ""
    new_terms: list[str] = Field(default_factory=list)
    needs_search: bool = False


class MergePlan(BaseModel):
    """IT-merge 的增量识别输出。"""

    model_config = ConfigDict(extra="ignore")

    increments: list[MergeIncrement] = Field(default_factory=list)
    figure_plan_updated: bool = False
    note: str = ""


class CorrectionPoint(BaseModel):
    """一条纠正点（五分类 + 用户原话摘录 + 期望）。"""

    model_config = ConfigDict(extra="ignore")

    category: str = "事实与技术"
    quote: str = ""
    fix_intent: str = ""
    target_chapter: str = ""


class CorrectionExtract(BaseModel):
    """IT-correct 的纠正点提取输出。"""

    model_config = ConfigDict(extra="ignore")

    corrections: list[CorrectionPoint] = Field(default_factory=list)
    note: str = ""


class TermReplacement(BaseModel):
    """术语族替换的一条 find-replace 计划。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    old: str = Field(default="", validation_alias=AliasChoices("old", "from"))
    new: str = Field(default="", validation_alias=AliasChoices("new", "to"))
    reason: str = ""


class TermReplacePlan(BaseModel):
    """术语类纠正的全文替换计划（服务端确定性执行并回报替换计数）。"""

    model_config = ConfigDict(extra="ignore")

    replacements: list[TermReplacement] = Field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# 章节定位：定稿全文 ↔ 分章视图
# ---------------------------------------------------------------------------

# 固定模板锚点（`invention/template_reference.md` 的六章标题层级）
_SPLIT_ANCHORS: list[tuple[str, re.Pattern[str]]] = [
    ("g1", re.compile(r"^##\s*一[、.]", re.MULTILINE)),
    ("g2", re.compile(r"^##\s*三[、.]", re.MULTILINE)),
    ("g3", re.compile(r"^###\s*3\.4(?!\.\d)", re.MULTILINE)),
    ("g5", re.compile(r"^(?:####\s*3\.4\.1|###\s*3\.5)", re.MULTILINE)),
    ("g6", re.compile(r"^##\s*四[、.]", re.MULTILINE)),
    ("g7", re.compile(r"^##\s*六[、.]", re.MULTILINE)),
]


def split_document(text: str) -> tuple[str, dict[str, str]]:
    """把交底书定稿切回「文头 + 分章视图」（迭代的改写单元）。

    以固定模板标题为锚点；任一锚点缺失时该章并入上一章，绝不丢字。
    返回 `(header, {chapter_key: text})`；无法识别时返回 `(text, {})`。
    """
    body = text or ""
    found: list[tuple[int, str]] = []
    for key, pattern in _SPLIT_ANCHORS:
        m = pattern.search(body)
        if m is not None:
            found.append((m.start(), key))
    if not found:
        return body, {}
    found.sort()
    header = body[: found[0][0]].rstrip() + "\n"
    chapters: dict[str, str] = {}
    for index, (start, key) in enumerate(found):
        end = found[index + 1][0] if index + 1 < len(found) else len(body)
        chapters[key] = body[start:end].strip()
    return header, chapters


# 章节定位别名（数字小节优先于中文章号，最后才是关键词兜底）
_CHAPTER_ALIASES: list[tuple[str, str]] = [
    ("3.4.1", "g5"), ("3.5", "g5"),
    ("3.4", "g3"),
    ("3.1", "g2"), ("3.2", "g2"), ("3.3", "g2"),
    ("1.1", "g1"), ("1.2", "g1"),
    ("六", "g7"), ("五", "g6"), ("四", "g6"), ("三", "g2"), ("二", "g1"), ("一", "g1"),
    ("实施例", "g7"), ("技术效果", "g7"),
    ("保护点", "g6"), ("优点", "g6"),
    ("符号", "g5"), ("公式", "g5"), ("参数", "g5"),
    ("流程", "g3"),
    ("框图", "g2"), ("模块", "g2"), ("背景", "g2"),
    ("现有技术", "g1"), ("技术问题", "g1"), ("检索", "g1"),
]


def chapter_key(target: str) -> str | None:
    """增量/纠正点的 `target_chapter` → 章节 key（无法定位返回 None）。"""
    raw = str(target or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in build_service.CHAPTER_ORDER:
        return lowered
    for token, key in _CHAPTER_ALIASES:
        if token in raw:
            return key
    return None


def normalize_category(value: str) -> str:
    """纠正点分类归一化（未知分类归入「事实与技术」）。"""
    raw = str(value or "").strip()
    for name in CORRECTION_CATEGORIES:
        if name == raw or name in raw or raw in name:
            return name
    if "术语" in raw or "可读" in raw:
        return "术语与可读性"
    if "公式" in raw or "符号" in raw:
        return "符号与公式体例"
    if "查新" in raw or "现有技术" in raw:
        return "查新与区别"
    if "保护" in raw or "附图" in raw:
        return "保护点与表述与附图"
    return "事实与技术"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _payload(ctx: Ctx) -> dict[str, Any]:
    return main_pipeline._payload(ctx)


def _iteration(ctx: Ctx) -> dict[str, Any]:
    return dict(ctx.state.get("iteration") or {})


def _intent(ctx: Ctx) -> str:
    return str(_iteration(ctx).get("intent") or "")


def _active(ctx: Ctx) -> bool:
    """本轮是否需要改稿（question / new_case 不改稿，后续步骤空转）。"""
    return _intent(ctx) in ("merge", "correct")


def _case_title(ctx: Ctx) -> str:
    return str(ctx.state.get("case_title") or (ctx.case or {}).get("title") or "")


def _sheet(ctx: Ctx):
    return ctx.state.get("terminology_sheet")


def _title_terms(ctx: Ctx) -> list[str]:
    data = terminology.to_prompt_dict(_sheet(ctx))
    return [str(t) for t in (data.get("title_core_terms") or [])]


def _sensitive(ctx: Ctx) -> list[str]:
    return [str(t) for t in (ctx.state.get("sensitive_terms") or [])]


def _allowed_urls(ctx: Ctx) -> set[str]:
    notes = ctx.state.get("prior_art_notes") or []
    return {str(n.get("url")) for n in notes if isinstance(n, Mapping) and n.get("url")}


def _base_markdown(ctx: Ctx) -> str:
    """迭代基准稿：交付定稿优先，其次成文草稿。"""
    return str(ctx.state.get("final_markdown") or ctx.state.get("draft_markdown") or "")


def _instruction(ctx: Ctx) -> str:
    return str(_iteration(ctx).get("instruction") or "")


def _runtime(ctx: Ctx, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "case_card": main_pipeline._card(ctx),
        "terminology_sheet": terminology.to_prompt_dict(_sheet(ctx)),
        "outline": ctx.state.get("skeleton"),
        "extra": dict(extra or {}),
    }


def next_run_group_sync(case_id: str) -> str:
    """下一个迭代 run_group（`iteration-<n>`，n 从 1 起递增）。"""
    rows = db.query_all(
        "SELECT DISTINCT run_group FROM pipeline_runs WHERE case_id=? AND run_group LIKE 'iteration-%'",
        (case_id,),
    )
    used: list[int] = []
    for row in rows:
        m = re.search(r"iteration-(\d+)", str(row["run_group"] or ""))
        if m:
            used.append(int(m.group(1)))
    return f"iteration-{(max(used) + 1) if used else 1}"


async def next_run_group(case_id: str) -> str:
    """`next_run_group_sync` 的异步包装。"""
    return await db.arun(next_run_group_sync, case_id)


def iteration_round(run_group: str) -> int:
    m = re.search(r"iteration-(\d+)", str(run_group or ""))
    return int(m.group(1)) if m else 1


# ---------------------------------------------------------------------------
# 章节生成（带自定义 tag，避免与主流水线的 g1..g7 脚本位冲突）
# ---------------------------------------------------------------------------


async def generate_section(
    ctx: Ctx,
    *,
    key: str,
    tag: str,
    parts: Sequence[str],
    runtime: Mapping[str, Any],
    user: str,
    sheet: Any,
    checks: Sequence[build_service.CheckFn] = (),
    repair_hint: str = "",
    max_repair: int = MAX_MERGE_REPAIR,
) -> tuple[str, Any, list[str], int]:
    """GEN 流式重写一章 → terms_delta 剥离合并 → lint 束 → 带错 REPAIR。

    与 `build_service.generate_chapter` 同构，唯一差别是 tag 与 key 解耦
    （迭代的调用 tag 形如 `merge.g7`，不复用首轮成文的脚本位）。
    返回 `(text, sheet, problems, repairs)`。
    """
    system = assembler.assemble(list(parts), runtime_ctx=dict(runtime))
    raw = await build_service.stream_gen(
        ctx, tag=tag, system=system, user=user, channel="doc", doc_id=DOC_ID
    )
    text, delta = terminology.extract_terms_delta(raw)
    sheet = terminology.merge_terms_delta(sheet, delta)

    problems = await build_service.run_checks(checks, text)
    repairs = 0
    while problems and repairs < max_repair:
        repairs += 1
        await ctx.emit(
            "log",
            {
                "message": (
                    f"{build_service.CHAPTER_NAMES.get(key, key)} 合并后未通过服务端校验，"
                    f"正在第 {repairs} 次修复：" + "；".join(problems[:3])
                ),
                "kind": "iteration_repair",
                "step": key,
            },
        )
        fixed_raw = await build_service.call_text(
            ctx,
            tag=f"{tag}.repair{repairs}",
            system=system,
            user=(
                "以下章节正文未通过平台的服务端校验。请**逐条**修正后重新输出**完整章节正文**"
                "（markdown，保持原有小节标题层级；不要输出解释）。\n\n"
                "【未通过的检查项】\n"
                + "\n".join(f"- {p}" for p in problems)
                + (f"\n\n【修复要点】{repair_hint}" if repair_hint else "")
                + "\n\n【本章原始任务】\n"
                + user
                + "\n\n【当前正文】\n"
                + text
            ),
        )
        fixed, delta2 = terminology.extract_terms_delta(fixed_raw)
        if fixed.strip():
            text = fixed
            sheet = terminology.merge_terms_delta(sheet, delta2)
        problems = await build_service.run_checks(checks, text)
    return text.strip(), sheet, problems, repairs


def _chapter_checks(ctx: Ctx, key: str) -> list[build_service.CheckFn]:
    """按章挑选与首轮成文同口径的确定性校验。"""
    sheet = _sheet(ctx)
    sensitive = _sensitive(ctx)
    keep = [*_title_terms(ctx), _case_title(ctx)]
    skeleton = ctx.state.get("skeleton") or {}
    modules = list(skeleton.get("modules") or [])
    steps = list(skeleton.get("steps") or [])
    plan = ctx.state.get("formula_plan")

    def sensitive_check(text: str) -> list[str]:
        return build_service.sensitive_lint(text, sensitive, keep=keep)

    def drift_check(text: str, *, title: bool) -> list[str]:
        findings = terminology.drift_lint(text, sheet, check_title_terms=title)
        hard = [f for f in findings if f.get("rule") != "alias_instead_of_canonical"]
        return build_service.drift_problems(hard)

    checks: list[build_service.CheckFn] = []
    if key == "g1":
        allowed = _allowed_urls(ctx)
        checks += [
            lambda t: build_service.heading_lint(t, ["一、", "1.1", "1.2", "二、"]),
            lambda t: build_service.url_lint(t, allowed),
            build_service.search_note_lint,
            sensitive_check,
            lambda t: drift_check(t, title=False),
        ]
    elif key == "g2":
        checks += [
            lambda t: build_service.heading_lint(t, ["三、", "3.1", "3.2", "3.3"]),
            build_service.mermaid_check,
            lambda t: build_service.module_lint(t, modules),
            sensitive_check,
            lambda t: drift_check(t, title=True),
        ]
    elif key == "g3":
        checks += [
            lambda t: build_service.heading_lint(t, ["3.4"]),
            build_service.mermaid_check,
            lambda t: build_service.step_label_lint(t, steps),
            lambda t: build_service.step_prose_lint(t, steps),
            sensitive_check,
            lambda t: drift_check(t, title=True),
        ]
    elif key == "g5":
        checks += [lambda t: build_service.heading_lint(t, ["3.5"]), sensitive_check]
        if plan:
            checks.append(lambda t: build_service.symbols_lint(t, plan))
        checks.append(lambda t: drift_check(t, title=False))
    elif key == "g6":
        checks += [
            lambda t: build_service.heading_lint(t, ["四、", "五、"]),
            build_service.section5_lint,
            sensitive_check,
            lambda t: drift_check(t, title=False),
        ]
    elif key == "g7":
        checks += [
            lambda t: build_service.heading_lint(t, ["六、", "实施例"]),
            lambda t: build_service.embodiment_lint(t, steps),
            sensitive_check,
            lambda t: drift_check(t, title=True),
        ]
    return checks


def _assemble(ctx: Ctx, chapters: Mapping[str, str]) -> str:
    return build_service.assemble_document(
        chapters,
        case_title=_case_title(ctx),
        contact=ctx.state.get("contact"),
        patent_type="invention",
    )


# ---------------------------------------------------------------------------
# 步骤 1：iterate_route —— IT0 意图路由
# ---------------------------------------------------------------------------

ROUTE_USER = (
    "用户在已有交底书定稿上继续输入（下方为本轮说明与新材料清单）。"
    "请判定其意图类别，只输出一个 JSON 围栏块，字段为 "
    '{"intent","rationale","evidence","points"}；`points` 逐条摘出用户提出的要点。'
)


async def iterate_route(ctx: Ctx) -> dict[str, Any]:
    """IT0：意图路由（显式 mode 优先；auto 走 STRUCT 判类）。

    `question` → CHAT 答疑不动稿并结束；`new_case` → 提示建议新开案件并结束。
    """
    main_pipeline._require_invention(ctx)
    payload = _payload(ctx)
    mode = str(payload.get("mode") or "auto").strip() or "auto"
    instruction = str(payload.get("instruction") or "").strip()
    file_ids = [str(f) for f in (payload.get("file_ids") or []) if str(f).strip()]
    base = _base_markdown(ctx)
    if not base.strip():
        raise ValueError("本案尚无交底书定稿，无法进入迭代（请先完成首轮成文与交付）")

    if mode in ("merge", "correct"):
        route = IterationIntent(intent=mode, rationale="用户在发起迭代时显式指定了处理模式")
    else:
        system = assembler.assemble(
            INTENT_PARTS,
            runtime_ctx=_runtime(
                ctx,
                {
                    "user_instruction": instruction,
                    "new_material_count": len(file_ids),
                    "current_draft_digest": build_service.text_digest(base, 800),
                },
            ),
        )
        route = await build_service.call_struct(
            ctx,
            IterationIntent,
            tag="intent",
            system=system,
            user=(
                ROUTE_USER
                + "\n\n【本轮用户说明】\n"
                + (instruction or "（用户未附文字说明，仅上传了新材料）")
                + f"\n\n【本轮新材料数量】{len(file_ids)}"
            ),
        )

    iteration: dict[str, Any] = {
        "round": iteration_round(ctx.run_group),
        "run_group": ctx.run_group,
        "mode": mode,
        "intent": route.intent,
        "rationale": route.rationale,
        "evidence": route.evidence,
        "points": list(route.points),
        "instruction": instruction,
        "file_ids": file_ids,
        "status": "routed",
        "base_chars": len(base),
    }
    await ctx.emit(
        "log",
        {
            "message": f"本轮迭代意图判定为「{route.intent}」：{route.rationale or '（未给出理由）'}",
            "kind": "iteration",
        },
    )

    if route.intent == "question":
        system = assembler.assemble(
            ANSWER_PARTS,
            runtime_ctx=_runtime(ctx, {"user_question": instruction}),
        )
        answer = await build_service.stream_gen(
            ctx,
            tag="answer",
            system=system,
            user=(
                "用户只是就当前交底书定稿提问、要求解释，**不要求改稿**。"
                "请基于下方定稿全文如实回答（可引用小节号），不要输出修订稿、不要改写正文。\n\n"
                f"【用户提问】{instruction or '（未附具体问题）'}\n\n"
                "===== 交底书定稿全文 =====\n" + base
            ),
            channel="chat",
        )
        iteration.update({"status": "answered", "answer": answer.strip()})
        return {"iteration": iteration}

    if route.intent == "new_case":
        note = (
            "本轮诉求属于**重新挖掘专利点 / 从头再走查新**，超出「在现有定稿上迭代」的范围。\n"
            "建议**新开一个案件**重跑八步主流程（当前案件与已交付版本保持不变）；"
            "若只是想补材料或改表述，请重新描述诉求，平台会按合并 / 纠正处理。"
        )
        await ctx.chat_delta(note)
        await ctx.chat_done()
        iteration.update({"status": "declined", "note": note})
        return {"iteration": iteration}

    # merge / correct：把定稿切回分章视图作为改写基准
    _header, chapters = split_document(base)
    if not chapters:
        chapters = dict(ctx.state.get("chapters") or {})
        if not chapters:
            raise ValueError("无法从定稿中识别章节结构，迭代终止（请检查定稿是否符合六章模板）")
        await ctx.emit(
            "log", {"message": "定稿章节锚点识别失败，已回退到工作内存中的分章正文。", "kind": "iteration"}
        )
    iteration["chapter_keys"] = sorted(chapters)
    return {"iteration": iteration, "chapters": chapters, "draft_markdown": base}


# ---------------------------------------------------------------------------
# 步骤 2：iterate_plan —— 增量识别（merge）/ 纠正点提取（correct）
# ---------------------------------------------------------------------------


def _new_material_rows_sync(case_id: str, file_ids: Sequence[str], known: Sequence[str]) -> list[dict[str, Any]]:
    """本轮的新材料：显式 file_ids 优先，否则取未消化过的上传件。"""
    rows = main_pipeline._material_rows_sync(case_id, list(file_ids))
    if file_ids:
        return rows
    seen = {str(n) for n in known}
    return [r for r in rows if str(r["orig_name"]) not in seen]


async def _digest_new_materials(ctx: Ctx) -> list[dict[str, Any]]:
    """新材料逐文件跑 material_digest（复用主流水线的分片与归并口径）。"""
    iteration = _iteration(ctx)
    known = [str(c.get("file")) for c in ((ctx.state.get("material_digest") or {}).get("files") or [])]
    rows = await db.arun(
        _new_material_rows_sync, ctx.case_id, iteration.get("file_ids") or [], known
    )
    cards: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        text = await db.arun(main_pipeline._read_md_sync, str(row["md_path"]))
        if not text.strip():
            continue
        name = str(row["orig_name"])
        chunks = main_pipeline._split_chunks(text)
        parsed: list[MaterialDigest] = []
        for k, chunk in enumerate(chunks, 1):
            tag = f"iter.digest.{index}" if len(chunks) == 1 else f"iter.digest.{index}.p{k}"
            system = assembler.assemble(
                main_pipeline.DIGEST_PARTS,
                runtime_ctx=_runtime(
                    ctx,
                    {
                        "file": name,
                        "chunk": k,
                        "chunk_total": len(chunks),
                        "iteration": "本文件为迭代轮新增材料",
                    },
                ),
            )
            user = (
                f"【材料文件名】{name}"
                + (f"（第 {k}/{len(chunks)} 片，只描述本片内容）" if len(chunks) > 1 else "")
                + "\n\n【材料正文（markdown）】\n"
                + chunk
            )
            parsed.append(
                await build_service.call_struct(ctx, MaterialDigest, tag=tag, system=system, user=user)
            )
        merged = main_pipeline._merge_cards(name, parsed)
        cards.append(merged)
        await ctx.emit(
            "log",
            {"message": f"新材料《{name}》消化完成（技术点 {len(merged.get('tech_points') or [])} 条）。"},
        )
    return cards


INCREMENT_USER = (
    "请按合并流程第 1 步「识别增量」判断本轮新内容主要影响哪些章节，只输出一个 JSON 围栏块："
    '{"increments":[{"target_chapter","summary","new_terms":[],"needs_search":false}],'
    '"figure_plan_updated":false,"note":""}。\n'
    "`target_chapter` 写章节号或小节号（如「1.1」「3.4」「六」）；"
    "`summary` 用一两句说明该章要补什么；"
    "`needs_search` 仅在增量改变了技术实质、需要补充检索时为 true。"
    "不要输出正文、不要推翻未涉及的章节。"
)

CORRECTION_USER = (
    "请按纠正流程第 1–2 步提取并分类本轮纠正点，只输出一个 JSON 围栏块："
    '{"corrections":[{"category","quote","fix_intent","target_chapter"}],"note":""}。\n'
    "`category` 只能取：事实与技术 / 术语与可读性 / 符号与公式体例 / 查新与区别 / 保护点与表述与附图；"
    "`quote` 摘录**定稿中的逐字原文片段**（10–40 字，用于服务端定位；术语类可只写要替换的词）；"
    "`fix_intent` 写清用户期望改成什么。不要输出修订后的正文。"
)


async def iterate_plan(ctx: Ctx) -> dict[str, Any]:
    """IT-merge 增量识别 / IT-correct 纠正点提取（question / new_case 空转）。"""
    if not _active(ctx):
        return {}
    main_pipeline._require_invention(ctx)
    iteration = _iteration(ctx)
    chapters = dict(ctx.state.get("chapters") or {})
    instruction = _instruction(ctx)

    if iteration["intent"] == "merge":
        cards = await _digest_new_materials(ctx)
        system = assembler.assemble(
            MERGE_PARTS,
            runtime_ctx=_runtime(
                ctx,
                {
                    "user_instruction": instruction,
                    "new_material_digest": cards,
                    "chapter_digests": {
                        build_service.CHAPTER_NAMES.get(k, k): build_service.text_digest(v)
                        for k, v in chapters.items()
                    },
                },
            ),
        )
        plan: MergePlan = await build_service.call_struct(
            ctx,
            MergePlan,
            tag="increments",
            system=system,
            user=(
                INCREMENT_USER
                + "\n\n【本轮用户说明】\n"
                + (instruction or "（用户未附文字说明）")
                + "\n\n【新材料摘要卡】\n"
                + build_service.json_block(cards)
            ),
        )
        increments = [i.model_dump() for i in plan.increments[:MAX_INCREMENTS]]
        affected: list[str] = []
        for item in increments:
            key = chapter_key(item.get("target_chapter") or "")
            item["chapter_key"] = key
            if key and key in chapters and key not in affected:
                affected.append(key)
        affected.sort(key=lambda k: build_service.CHAPTER_ORDER.index(k))
        iteration["merge"] = {
            "digested": cards,
            "increments": increments,
            "affected": affected,
            "figure_plan_updated": bool(plan.figure_plan_updated),
            "needs_search": any(i.get("needs_search") for i in increments),
            "note": plan.note,
        }
        await ctx.emit(
            "log",
            {
                "message": (
                    f"增量识别完成：{len(increments)} 条增量，受影响章节 "
                    + ("、".join(build_service.CHAPTER_NAMES.get(k, k) for k in affected) or "（无）")
                    + "。"
                ),
                "kind": "iteration",
            },
        )
        material = dict(ctx.state.get("material_digest") or {})
        files = list(material.get("files") or []) + cards
        sensitive = list(ctx.state.get("sensitive_terms") or [])
        for card in cards:
            for hit in card.get("sensitive_hits") or []:
                value = str(hit).strip()
                if value and value not in sensitive:
                    sensitive.append(value)
        return {
            "iteration": iteration,
            "material_digest": {"files": files, "count": len(files)},
            "sensitive_terms": sensitive,
        }

    # ---- correct ----
    system = assembler.assemble(
        CORRECT_PARTS,
        runtime_ctx=_runtime(ctx, {"user_instruction": instruction}),
    )
    extract: CorrectionExtract = await build_service.call_struct(
        ctx,
        CorrectionExtract,
        tag="corrections",
        system=system,
        user=(
            CORRECTION_USER
            + "\n\n【本轮用户说明】\n"
            + (instruction or "（用户未附文字说明）")
            + "\n\n===== 交底书定稿全文 =====\n"
            + _base_markdown(ctx)
        ),
    )
    corrections: list[dict[str, Any]] = []
    for point in extract.corrections[:MAX_CORRECTIONS]:
        item = point.model_dump()
        item["category"] = normalize_category(item.get("category"))
        item["chapter_key"] = chapter_key(item.get("target_chapter") or "") or (
            CATEGORY_CHAPTERS.get(item["category"]) or ""
        )
        corrections.append(item)
    iteration["correct"] = {"corrections": corrections, "note": extract.note}
    await ctx.emit(
        "log",
        {
            "message": (
                f"纠正点提取完成：{len(corrections)} 条（"
                + "、".join(sorted({c['category'] for c in corrections}))
                + "）。"
                if corrections
                else "未从本轮说明中提取到可执行的纠正点，将只做确定性 lint 复核。"
            ),
            "kind": "iteration",
        },
    )
    return {"iteration": iteration}


# ---------------------------------------------------------------------------
# 步骤 3：iterate_rewrite —— 受影响章节改写 / 逐点补丁
# ---------------------------------------------------------------------------


async def _supplementary_search(ctx: Ctx, new_terms: Sequence[str]) -> dict[str, Any]:
    """needs_search 为真时的补充查新（失败可跳过，绝不阻断迭代）。"""
    prior = dict(ctx.state.get("prior_art") or {})
    terms = [str(t).strip() for t in (list(new_terms) + list(prior.get("terms") or [])) if str(t).strip()]
    seen: list[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    blocks = seen[:SEARCH_TERM_LIMIT]
    problems = main_pipeline.lint_search_blocks(blocks)
    report: dict[str, Any] = {"terms": blocks, "lint": problems, "added": 0}
    if len(blocks) < 2:
        report["error"] = "补充检索词不足 2 个，已跳过补充查新"
        await ctx.emit("log", {"message": report["error"], "kind": "iteration"})
        return report

    progress = cnipa.hub_progress(ctx.case_id, ctx.step_key)
    result = await cnipa.search(
        ctx.case_id,
        blocks,
        str(prior.get("type_param") or "invention"),
        on_progress=progress,
        use_cache=False,
    )
    report["status"] = result.status
    report["error"] = result.error
    if not (result.ok and result.hits):
        await ctx.emit(
            "log",
            {
                "message": f"补充查新未取得可用结果（{result.error or '零命中'}），本轮按未补检索处理，1.1 不做改动。",
                "kind": "iteration",
            },
        )
        return report

    existing = _allowed_urls(ctx)
    fresh = [h for h in result.hits if str(getattr(h, "url", "") or "") not in existing]
    if not fresh:
        await ctx.emit("log", {"message": "补充查新未发现新的在先文献，1.1 不做改动。", "kind": "iteration"})
        return report
    notes = await main_pipeline._digest_hits(ctx, fresh)
    report["added"] = len(notes)
    report["notes"] = notes
    await ctx.emit(
        "log", {"message": f"补充查新新增 {len(notes)} 条在先文献，1.1 将同步更新。", "kind": "iteration"}
    )
    return report


def _merge_user(key: str, original: str, increments: Sequence[Mapping[str, Any]], instruction: str) -> str:
    """merger.md 非破坏性合并 + 术语族替换指令 + 原章全文 + 本轮增量。"""
    lines = [
        (
            f"请对「{build_service.CHAPTER_NAMES.get(key, key)}」执行**增量合并**，"
            "以**追加或局部重写**为主，不推翻本章未涉及、用户也未要求修改的内容。"
        ),
        "",
        "硬性要求：",
        "1. 保持本章原有小节标题层级与编号，已确认的技术事实不得改写或删除；",
        "2. 只把下方增量落进本章，不新增本案材料没有的模块、场景或数值；",
        (
            "3. 若本轮改了叫法，须做**术语族替换**：把旧叫法在本章正文与图示标签中整族换成新叫法，"
            "禁止只改一处、禁止靠「先定义」把错位词留下；"
        ),
        "4. 用词与术语表同词同形，禁用变体不得出现；应用场景须脱敏；",
        "5. 只输出**本章完整正文**（markdown），不要输出合并摘要、不要输出其它章节；",
        "6. 正文之后按术语规则附一个 `terms_delta` JSON 围栏块。",
        "",
        "【本轮用户说明】",
        instruction or "（用户未附文字说明）",
        "",
        "【落到本章的增量】",
        build_service.json_block(list(increments)),
        "",
        "【本章当前正文】",
        original,
    ]
    return "\n".join(lines)


async def _rewrite_merge(ctx: Ctx) -> dict[str, Any]:
    """IT-merge：受影响章节逐章非破坏性重写 + 补充查新 + 公式/附图同步。"""
    iteration = _iteration(ctx)
    merge = dict(iteration.get("merge") or {})
    chapters = dict(ctx.state.get("chapters") or {})
    increments = list(merge.get("increments") or [])
    affected = list(merge.get("affected") or [])
    instruction = _instruction(ctx)
    sheet = _sheet(ctx)
    output: dict[str, Any] = {}

    # ---- 查新联动（merger.md 步骤 4）----
    search_report: dict[str, Any] = {"triggered": False}
    if merge.get("needs_search"):
        new_terms = [t for item in increments for t in (item.get("new_terms") or [])]
        search_report = {"triggered": True, **await _supplementary_search(ctx, new_terms)}
        if search_report.get("added"):
            notes = list(ctx.state.get("prior_art_notes") or []) + list(search_report.get("notes") or [])
            output["prior_art_notes"] = notes
            ctx.state["prior_art_notes"] = notes
            prior = dict(ctx.state.get("prior_art") or {})
            prior["searched"] = True
            prior["selected_count"] = len(notes)
            prior["hit_count"] = int(prior.get("hit_count") or 0) + int(search_report["added"])
            output["prior_art"] = prior
            ctx.state["prior_art"] = prior
            if "g1" not in affected and "g1" in chapters:
                affected.insert(0, "g1")
                increments.append(
                    {
                        "target_chapter": "1.1",
                        "chapter_key": "g1",
                        "summary": f"补充查新新增 {search_report['added']} 条在先文献，须写进 1.1 并更新区别论述",
                        "new_terms": [],
                        "needs_search": False,
                    }
                )
    merge["search"] = search_report

    # ---- 逐章重写 ----
    reports: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for key in affected:
        original = chapters.get(key) or ""
        if not original.strip():
            continue
        mine = [i for i in increments if i.get("chapter_key") == key]
        extra: dict[str, Any] = {
            "chapter": build_service.CHAPTER_NAMES.get(key, key),
            "increments": mine,
            "user_instruction": instruction,
            "new_material_digest": merge.get("digested") or [],
        }
        if key == "g1":
            extra["prior_art_notes"] = ctx.state.get("prior_art_notes") or []
        if key in ("g3", "g5", "g7") and ctx.state.get("formula_plan"):
            extra["formula_plan"] = ctx.state.get("formula_plan")
        parts = [*build_service.CHAPTER_PARTS.get(key, build_service.G6_PARTS), "disclosure/iteration/merger"]
        text, sheet, problems, repairs = await generate_section(
            ctx,
            key=key,
            tag=f"merge.{key}",
            parts=parts,
            runtime=_runtime(ctx, extra),
            user=_merge_user(key, original, mine, instruction),
            sheet=sheet,
            checks=_chapter_checks(ctx, key),
            repair_hint="只补增量、不推翻原有内容；保持小节标题层级与术语同形。",
        )
        degraded = 0
        if key in ("g2", "g3"):
            text, degraded = await build_service.degrade_mermaid(text)
        chapters[key] = text
        unresolved.extend(problems)
        reports.append(
            {
                "key": key,
                "name_zh": build_service.CHAPTER_NAMES.get(key, key),
                "chars": len(text),
                "repairs": repairs,
                "degraded": degraded,
                "unresolved": problems,
            }
        )

    # ---- formula_plan / figure_plan 同步（merger.md 步骤 3 与 5）----
    merge["formula"] = await _sync_formula(ctx, chapters, touched=affected)
    merge["figure_plan"] = {
        "synced": False,
        "reason": "发明分支不使用 figure_plan（该同步项适用于实用新型 / 外观设计，M5 提供）",
        "model_flag": bool(merge.get("figure_plan_updated")),
    }
    merge["chapters"] = reports
    merge["unresolved"] = unresolved
    iteration["merge"] = merge
    iteration["affected"] = affected

    output.update(
        {
            "iteration": iteration,
            "chapters": chapters,
            "terminology_sheet": sheet.model_dump() if hasattr(sheet, "model_dump") else sheet,
            "draft_markdown": _assemble(ctx, chapters),
        }
    )
    await ctx.emit(
        "log",
        {
            "message": (
                f"合并改写完成：重写 {len(reports)} 节"
                + (f"，修复 {sum(r['repairs'] for r in reports)} 次" if any(r["repairs"] for r in reports) else "")
                + "。"
            ),
            "kind": "iteration",
        },
    )
    return output


async def _sync_formula(ctx: Ctx, chapters: Mapping[str, str], *, touched: Sequence[str]) -> dict[str, Any]:
    """公式体例同步：门禁复跑 + 3.5 符号列逐字同形复核。"""
    plan = ctx.state.get("formula_plan")
    if not plan:
        return {"needed": False, "reason": "本案无 formula_plan"}
    gate = await formula_service.avalidate_formula_plan(plan)
    report: dict[str, Any] = {
        "needed": True,
        "ok": bool(gate.get("ok")),
        "errors": list(gate.get("errors") or []),
        "degraded": bool(gate.get("degraded")),
        "rechecked": True,
    }
    if "g5" in touched or "g5" in chapters:
        report["symbols"] = build_service.symbols_lint(chapters.get("g5") or "", plan)
    return report


def _locate_chapter(chapters: Mapping[str, str], excerpt: str, anchor: str = "") -> str | None:
    """按逐字/模糊定位找出补丁所属章节。"""
    probe = (excerpt or "").strip() or (anchor or "").strip()
    if not probe:
        return None
    for key in build_service.CHAPTER_ORDER:
        text = chapters.get(key)
        if text and patches_service.locate(text, probe, anchor) is not None:
            return key
    return None


def apply_patches_to_chapters(
    chapters: dict[str, str], patches: Sequence[Any]
) -> dict[str, Any]:
    """把补丁按所属章节分组后**逐章**确定性应用（保持分章视图与全文同步）。"""
    items = patches_service.coerce_patches(patches)
    grouped: dict[str, list[Any]] = {}
    dropped: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for patch in items:
        key = _locate_chapter(chapters, patch.original_excerpt, patch.anchor_before)
        if key is None:
            dropped.append({"patch": patch.to_dict(), "reason": patches_service.REASON_MISS})
            unresolved.append(
                f"[{patch.checklist_item or '纠正补丁'}] 未应用（原文片段未在定稿中命中）："
                f"「{' '.join(patch.original_excerpt.split())[:40]}…」"
            )
            continue
        grouped.setdefault(key, []).append(patch)

    applied = 0
    for key, group in grouped.items():
        result = patches_service.apply_patches(chapters[key], group)
        chapters[key] = result["text"]
        applied += int(result["applied"])
        dropped.extend(result["dropped"])
        unresolved.extend(result["unresolved"])
    total = len(items)
    rate = 1.0 if total == 0 else applied / total
    return {
        "applied": applied,
        "total": total,
        "dropped": len(dropped),
        "dropped_detail": dropped,
        "unresolved": unresolved,
        "rate": round(rate, 3),
        "low_rate": total > 0 and rate < patches_service.PATCH_APPLY_FLOOR,
        "chapters": list(grouped),
    }


def apply_term_plan(
    chapters: dict[str, str], plan: TermReplacePlan
) -> tuple[list[dict[str, Any]], list[str]]:
    """执行术语族全文 find-replace；返回 (每条的替换计数, 被改动的章节 key)。"""
    replaced: list[dict[str, Any]] = []
    touched: list[str] = []
    for item in plan.replacements:
        old = str(item.old or "").strip()
        new = str(item.new or "").strip()
        if not old or not new or old == new:
            continue
        count = 0
        for key, text in chapters.items():
            hits = text.count(old)
            if hits:
                chapters[key] = text.replace(old, new)
                count += hits
                if key not in touched:
                    touched.append(key)
        replaced.append({"from": old, "to": new, "count": count, "reason": item.reason})
    return replaced, touched


TERM_PLAN_USER = (
    "用户指出某个叫法与标题 / 3.1 场景不匹配，须做**术语族替换**。"
    "请只输出一个 JSON 围栏块：{\"replacements\":[{\"old\",\"new\",\"reason\"}],\"note\":\"\"}。\n"
    "`old` 必须是定稿中**逐字出现**的写法（含其常见搭配可分条列出）；`new` 为领域表述；"
    "禁止把词写成全局禁词表、禁止靠「先定义」保留错位词、禁止新增材料没有的模块。"
)

PATCH_USER = (
    "请针对下方**一条**纠正点输出修订补丁清单，只输出一个 JSON 围栏块："
    '{"verdict":"patched","patches":[{"checklist_item","anchor_before","original_excerpt",'
    '"replacement","reason"}],"unresolved":[]}。\n'
    "`anchor_before` 与 `original_excerpt` 必须是定稿的**逐字子串**（服务端按逐字定位应用，"
    "定位不到即丢弃）；各补丁的 `original_excerpt` 互不重叠；不要输出修订后全文。"
)


async def _rewrite_correct(ctx: Ctx) -> dict[str, Any]:
    """IT-correct：逐纠正点生成补丁并确定性应用（术语类走全文替换、公式类过门禁）。"""
    iteration = _iteration(ctx)
    correct = dict(iteration.get("correct") or {})
    corrections = list(correct.get("corrections") or [])
    chapters = dict(ctx.state.get("chapters") or {})
    base = _base_markdown(ctx)
    sheet = _sheet(ctx)

    patch_reports: list[dict[str, Any]] = []
    term_replacements: list[dict[str, Any]] = []
    unresolved: list[str] = []
    touched: list[str] = []
    formula_touched = False

    for index, item in enumerate(corrections, 1):
        category = str(item.get("category") or "事实与技术")
        system = assembler.assemble(
            CORRECT_PARTS,
            runtime_ctx=_runtime(
                ctx,
                {
                    "correction": item,
                    "user_instruction": _instruction(ctx),
                    "prior_art_notes": ctx.state.get("prior_art_notes") or []
                    if category == "查新与区别"
                    else None,
                    "formula_plan": ctx.state.get("formula_plan")
                    if category == "符号与公式体例"
                    else None,
                },
            ),
        )
        head = (
            f"【本条纠正点】分类：{category}；用户原话/定位：{item.get('quote') or '（未给出摘录）'}；"
            f"期望：{item.get('fix_intent') or '（未给出）'}"
        )

        if category == "术语与可读性":
            plan: TermReplacePlan = await build_service.call_struct(
                ctx,
                TermReplacePlan,
                tag=f"correct.terms{index}",
                system=system,
                user=(
                    TERM_PLAN_USER
                    + "\n\n"
                    + head
                    + "\n\n===== 交底书定稿全文 =====\n"
                    + base
                ),
            )
            replaced, changed = apply_term_plan(chapters, plan)
            term_replacements.extend(replaced)
            hit = sum(r["count"] for r in replaced)
            if not hit:
                unresolved.append(
                    f"[术语与可读性] 未应用：替换计划中的旧叫法未在定稿中命中"
                    f"（{'、'.join(r['from'] for r in replaced) or '计划为空'}）"
                )
            else:
                touched = sorted(set(touched) | set(changed))
            patch_reports.append(
                {"index": index, "category": category, "kind": "term_family", "replaced": replaced}
            )
            for entry in replaced:
                if entry["count"]:
                    sheet = terminology.merge_terms_delta(
                        sheet,
                        {
                            "add": [
                                {
                                    "term": entry["to"],
                                    "definition": entry.get("reason") or "",
                                    "forbidden_variants": [entry["from"]],
                                    "source_section": item.get("target_chapter") or "",
                                }
                            ]
                        },
                    )
            await ctx.emit(
                "log",
                {
                    "message": (
                        f"纠正点 {index}（术语与可读性）：全文族替换 "
                        + "、".join(f"「{r['from']}」→「{r['to']}」×{r['count']}" for r in replaced)
                        + "。"
                        if replaced
                        else f"纠正点 {index}（术语与可读性）：未产出可执行的替换计划。"
                    ),
                    "kind": "iteration",
                },
            )
            continue

        report: SelfCheckReport = await build_service.call_struct(
            ctx,
            SelfCheckReport,
            tag=f"correct.patch{index}",
            system=system,
            user=(PATCH_USER + "\n\n" + head + "\n\n===== 交底书定稿全文 =====\n" + base),
            call_class="AUDIT",
        )
        result = apply_patches_to_chapters(chapters, report.patches)
        unresolved.extend([str(u) for u in report.unresolved])
        unresolved.extend(result["unresolved"])
        touched = sorted(set(touched) | set(result["chapters"]))
        patch_reports.append(
            {
                "index": index,
                "category": category,
                "kind": "patches",
                "applied": result["applied"],
                "total": result["total"],
                "dropped": result["dropped"],
                "rate": result["rate"],
                "chapters": result["chapters"],
            }
        )
        if category == "符号与公式体例":
            formula_touched = True
        await ctx.emit(
            "log",
            {
                "message": (
                    f"纠正点 {index}（{category}）：应用补丁 {result['applied']}/{result['total']} 条。"
                ),
                "kind": "iteration",
            },
        )

    formula_report: dict[str, Any] = {"needed": False}
    if formula_touched or any(r.get("count") for r in term_replacements):
        formula_report = await _sync_formula(ctx, chapters, touched=touched)
        if formula_report.get("errors"):
            unresolved.append(
                "公式类纠正后门禁复跑仍有问题：" + "；".join(str(e) for e in formula_report["errors"][:3])
            )

    correct.update(
        {
            "patches": patch_reports,
            "term_replacements": term_replacements,
            "unresolved": unresolved,
            "touched": touched,
            "formula": formula_report,
        }
    )
    iteration["correct"] = correct
    iteration["affected"] = touched
    return {
        "iteration": iteration,
        "chapters": chapters,
        "terminology_sheet": sheet.model_dump() if hasattr(sheet, "model_dump") else sheet,
        "draft_markdown": _assemble(ctx, chapters),
    }


async def iterate_rewrite(ctx: Ctx) -> dict[str, Any]:
    """受影响章节改写（merge）/ 逐点补丁（correct）；question / new_case 空转。"""
    if not _active(ctx):
        return {}
    main_pipeline._require_invention(ctx)
    if _intent(ctx) == "merge":
        return await _rewrite_merge(ctx)
    return await _rewrite_correct(ctx)


# ---------------------------------------------------------------------------
# 步骤 4：iterate_check —— 自检（merge 复用主流水线）/ lint 束（correct）
# ---------------------------------------------------------------------------


async def _lint_bundle(ctx: Ctx, text: str) -> tuple[str, dict[str, Any]]:
    """确定性 lint 束（元信息泄漏 / 文末清洁 / mermaid 可渲染 / URL 白名单 / 脱敏）。"""
    text, tail_removed = build_service.clean_tail(text)
    meta_hits = build_service.meta_leak_lint(text)
    meta_fixed: list[dict[str, Any]] = []
    if meta_hits:
        text, meta_fixed = await main_pipeline._repair_meta_leaks(ctx, text, meta_hits)
    text, degraded = await build_service.degrade_mermaid(text)
    text = text.rstrip() + "\n"

    unresolved: list[str] = []
    remaining = build_service.meta_leak_lint(text)
    if remaining:
        unresolved.append("正文仍残留元信息词：" + "、".join(sorted({h["word"] for h in remaining})))
    allowed = _allowed_urls(ctx)
    unresolved.extend(build_service.url_lint(text, allowed))
    unresolved.extend(
        build_service.sensitive_lint(
            text, _sensitive(ctx), keep=[*_title_terms(ctx), _case_title(ctx)]
        )
    )
    return text, {
        "tail_removed": tail_removed,
        "meta_fixed": meta_fixed,
        "mermaid_degraded": degraded,
        "unresolved": unresolved,
    }


async def iterate_check(ctx: Ctx) -> dict[str, Any]:
    """merge 走主流水线 `self_check`（全文 AUDIT + 补丁 + lint 束）；correct 只跑 lint 束。"""
    if not _active(ctx):
        return {}
    main_pipeline._require_invention(ctx)
    if _intent(ctx) == "merge":
        # 复用主流水线的自检通道（禁止复制粘贴其实现）
        return await main_pipeline.self_check(ctx)

    chapters = dict(ctx.state.get("chapters") or {})
    text = str(ctx.state.get("draft_markdown") or "") or _assemble(ctx, chapters)
    text, report = await _lint_bundle(ctx, text)
    iteration = _iteration(ctx)
    correct = dict(iteration.get("correct") or {})
    report["unresolved"] = list(correct.get("unresolved") or []) + list(report["unresolved"])
    correct["lint"] = report
    iteration["correct"] = correct
    await ctx.emit(
        "log",
        {
            "message": (
                f"纠正后 lint 复核完成：文末清理 {len(report['tail_removed'])} 行；"
                f"元信息剥离 {len(report['meta_fixed'])} 处；mermaid 降级 {report['mermaid_degraded']} 处。"
            ),
            "kind": "iteration",
        },
    )
    return {
        "iteration": iteration,
        "final_markdown": text,
        "self_check": {
            "rounds": [],
            "unresolved": report["unresolved"],
            "term_replacements": list(correct.get("term_replacements") or []),
            "formula_recheck": correct.get("formula"),
            "tail_removed": report["tail_removed"],
            "meta_fixed": report["meta_fixed"],
            "mermaid_degraded": report["mermaid_degraded"],
            "rewritten": [],
        },
    }


# ---------------------------------------------------------------------------
# 步骤 5：iterate_deliver —— 新时间戳交付 + 留档摘要 + 修订记录
# ---------------------------------------------------------------------------


def _latest_md_artifact_sync(case_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM artifacts WHERE case_id=? AND kind='disclosure_md' ORDER BY version DESC LIMIT 1",
        (case_id,),
    )
    return dict(row) if row is not None else None


async def _save_mermaid_pngs(ctx: Ctx, markdown: str, case_title: str, iteration_type: str) -> list[dict[str, Any]]:
    """本轮正文里的 mermaid 图重新渲染落 PNG（渲染结果进程内缓存，未变的图不重算）。"""
    saved: list[dict[str, Any]] = []
    for index, (_s, _e, code) in enumerate(build_service.mermaid_blocks(markdown), 1):
        png, error = await build_service.render_mermaid(code)
        if png is None:
            await ctx.emit("log", {"message": f"第 {index} 幅图渲染失败，正文已降级为文字清单：{error}"})
            continue
        artifact = await artifacts_service.save_artifact(
            ctx.case_id,
            "mermaid_png",
            png,
            "png",
            title=f"{case_title or '交底书'}_图{index}",
            run_group=ctx.run_group,
            iteration_type=iteration_type,
            summary=f"图{index}",
        )
        await main_pipeline._emit_artifact(ctx.case_id, artifact)
        saved.append({"index": index, "artifact_id": artifact.id, "filename": artifact.filename})
    return saved


async def _save_files(
    ctx: Ctx,
    markdown: str,
    case_title: str,
    *,
    iteration_type: str,
    summary: str,
) -> dict[str, Any]:
    """落 md/docx(+pdf) 新版本（version+1、新时间戳文件名，**绝不覆盖旧稿**）。"""
    previous = await db.arun(_latest_md_artifact_sync, ctx.case_id)
    md_artifact = await artifacts_service.save_artifact(
        ctx.case_id,
        "disclosure_md",
        markdown,
        "md",
        title=case_title or None,
        md_text=markdown,
        run_group=ctx.run_group,
        iteration_type=iteration_type,
        summary=summary or None,
        source_artifact_id=(previous or {}).get("id"),
    )
    await main_pipeline._emit_artifact(ctx.case_id, md_artifact)
    downloads = [
        {"kind": md_artifact.kind, "artifact_id": md_artifact.id, "filename": md_artifact.filename}
    ]

    docx_artifact = None
    docx_error: str | None = None
    docx_stats: dict[str, Any] = {}
    stem = artifacts_service.strip_timestamp(md_artifact.filename)
    try:
        docx_bytes, docx_stats = await anyio.to_thread.run_sync(
            lambda: main_pipeline._export_docx_sync(markdown, stem)
        )
    except export_docx_service.DocxExportError as exc:
        docx_error = str(exc)
        logger.warning("迭代稿 docx 导出失败：%s", exc)
    else:
        docx_artifact = await artifacts_service.save_artifact(
            ctx.case_id,
            "disclosure_docx",
            docx_bytes,
            "docx",
            title=case_title or None,
            run_group=ctx.run_group,
            iteration_type=iteration_type,
            source_artifact_id=md_artifact.id,
        )
        await main_pipeline._emit_artifact(ctx.case_id, docx_artifact)
        downloads.append(
            {
                "kind": docx_artifact.kind,
                "artifact_id": docx_artifact.id,
                "filename": docx_artifact.filename,
            }
        )

    pdf_artifact = None
    pdf_engine: str | None = None
    pdf_error: str | None = None
    if docx_artifact is not None:
        pdf_path = get_config().tmp_dir / f"{stem}_{ULID()}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            pdf_engine = await export_pdf_service.docx_to_pdf(Path(docx_artifact.stored_path), pdf_path)
        except export_pdf_service.PdfExportError as exc:
            pdf_error = str(exc)
            logger.info("迭代稿 PDF 导出不可用：%s", exc)
        if pdf_error is None and pdf_path.is_file():
            pdf_bytes = await anyio.to_thread.run_sync(pdf_path.read_bytes)
            pdf_artifact = await artifacts_service.save_artifact(
                ctx.case_id,
                "disclosure_pdf",
                pdf_bytes,
                "pdf",
                title=case_title or None,
                run_group=ctx.run_group,
                iteration_type="export",
                source_artifact_id=docx_artifact.id,
            )
            pdf_path.unlink(missing_ok=True)
            await main_pipeline._emit_artifact(ctx.case_id, pdf_artifact)
            downloads.append(
                {
                    "kind": pdf_artifact.kind,
                    "artifact_id": pdf_artifact.id,
                    "filename": pdf_artifact.filename,
                }
            )

    await main_pipeline._emit_doc_version(ctx.case_id, md_artifact, downloads=downloads)
    return {
        "md": json.loads(md_artifact.model_dump_json()),
        "docx": json.loads(docx_artifact.model_dump_json()) if docx_artifact else None,
        "docx_error": docx_error,
        "docx_stats": docx_stats,
        "pdf": json.loads(pdf_artifact.model_dump_json()) if pdf_artifact else None,
        "pdf_engine": pdf_engine,
        "pdf_error": pdf_error,
        "downloads": downloads,
        "previous_md": (previous or {}).get("filename"),
    }


def _summary_user(intent: str, iteration: Mapping[str, Any], instruction: str) -> str:
    """留档摘要的阶段指令（句数与要点逐字照 merger.md / correction.md 的「输出」节）。"""
    if intent == "merge":
        merge = dict(iteration.get("merge") or {})
        return "\n".join(
            [
                (
                    "本轮增量合并已由平台落盘为新时间戳文件。请**只输出**「合并摘要（留档）」小节的正文，"
                    "不要重复小节标题、不要输出正文或权利要求偏向点。"
                ),
                "",
                (
                    "格式硬性要求：用 **3–6 句完整中文**，依次说明：**改了哪些章节**、**原因**、"
                    "**是否影响保护点或检索结论**、**是否已做 8.1（术语/标题/实施例）/8.2/8.3 核对**；"
                    "若做了术语族替换，点明新旧叫法及是否整篇对齐。"
                ),
                "",
                "【本轮用户说明】",
                instruction or "（用户未附文字说明）",
                "",
                "【本轮改动事实（不得杜撰其它改动）】",
                build_service.json_block(
                    {
                        "affected_chapters": [
                            build_service.CHAPTER_NAMES.get(k, k) for k in (iteration.get("affected") or [])
                        ],
                        "increments": merge.get("increments") or [],
                        "search": merge.get("search") or {},
                        "formula": merge.get("formula") or {},
                        "unresolved": merge.get("unresolved") or [],
                    }
                ),
            ]
        )
    correct = dict(iteration.get("correct") or {})
    return "\n".join(
        [
            (
                "本轮对话纠正已由平台落盘为新时间戳文件。请**只输出**「纠正摘要（留档）」小节的正文，"
                "不要重复小节标题、不要输出正文或权利要求偏向点。"
            ),
            "",
            (
                "格式硬性要求：用 **2–5 句完整中文**，说明：**修改位置**、**依据**、"
                "**是否影响保护点或检索**；若做了术语族替换，点明新旧叫法及是否整篇对齐。"
            ),
            "",
            "【本轮用户说明】",
            instruction or "（用户未附文字说明）",
            "",
            "【本轮改动事实（不得杜撰其它改动）】",
            build_service.json_block(
                {
                    "corrections": correct.get("corrections") or [],
                    "patches": correct.get("patches") or [],
                    "term_replacements": correct.get("term_replacements") or [],
                    "unresolved": correct.get("unresolved") or [],
                }
            ),
        ]
    )


async def _archive_summary(ctx: Ctx, intent: str) -> str:
    """强制在 chat 通道输出「## 合并摘要（留档）」/「## 纠正摘要（留档）」。

    标题先行推 chat 通道（保证格式必现），随后流式追加 3–6 / 2–5 句正文。
    """
    heading = MERGE_HEADING if intent == "merge" else CORRECT_HEADING
    iteration = _iteration(ctx)
    system = assembler.assemble(
        MERGE_PARTS if intent == "merge" else CORRECT_PARTS,
        runtime_ctx=_runtime(ctx, {"iteration": {k: v for k, v in iteration.items() if k != "answer"}}),
    )
    await ctx.chat_delta(heading + "\n\n")
    body = await build_service.stream_gen(
        ctx,
        tag="merge_summary" if intent == "merge" else "correct_summary",
        system=system,
        user=_summary_user(intent, iteration, _instruction(ctx)),
        channel="chat",
    )
    text = body.strip()
    if text.startswith(heading):
        text = text[len(heading) :].strip()
    return f"{heading}\n\n{text}" if text else f"{heading}\n\n（本轮未产出摘要正文。）"


async def _claim_bias_gate(ctx: Ctx, markdown: str) -> dict[str, Any]:
    """合并定稿后的「权利要求偏向点」引导（仅存记录、不入正文）。"""
    groups, report = await main_pipeline._claim_bias(ctx, markdown)
    answer = main_pipeline._answer(
        await ctx.await_user(
            InteractionRequest(
                kind="claim_bias",
                schema={
                    "type": "object",
                    "properties": {
                        "skip": {"type": "boolean", "title": "跳过（不选偏向点）", "default": False},
                        "choices": {
                            "type": "array",
                            "title": "各组偏向选择",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "axis": {"type": "string", "title": "维度"},
                                    "choice": {
                                        "type": "string",
                                        "title": "倾向",
                                        "enum": ["option_a", "option_b", "none"],
                                    },
                                },
                            },
                        },
                        "note": {"type": "string", "title": "补充说明（可选）"},
                    },
                },
                prompt=(
                    "合并稿已交付。以下「权利要求偏向点」仅供选用，**不会写入正文**；"
                    "如需按某个偏向再强化第五章，可在下一轮迭代中明确说明。"
                    if groups
                    else "合并稿已交付。本稿只有一条清晰保护主线，未产出可对举的偏向点，可直接跳过。"
                ),
                default={"skip": not groups, "choices": [], "note": "", "groups": groups},
            )
        )
    )
    return {
        "groups": groups,
        **report,
        "selection": {
            "skipped": bool(answer.get("skip")) or not groups,
            "choices": [c for c in (answer.get("choices") or []) if isinstance(c, Mapping)],
            "note": str(answer.get("note") or ""),
            "applied": False,
        },
    }


async def iterate_deliver(ctx: Ctx) -> dict[str, Any]:
    """新时间戳交付 + 留档摘要 + 修订记录 + 修订对话记录导出。"""
    iteration = _iteration(ctx)
    intent = str(iteration.get("intent") or "")
    if intent not in ("merge", "correct"):
        # question / new_case：不动稿、不产新版本，只把本轮记入案件工作内存
        await ctx.emit(
            "log",
            {
                "message": (
                    "本轮为答疑，未产生新版本交底书。"
                    if intent == "question"
                    else "本轮建议新开案件，未产生新版本交底书。"
                ),
                "kind": "iteration",
            },
        )
        return {}

    main_pipeline._require_invention(ctx)
    markdown = str(ctx.state.get("final_markdown") or ctx.state.get("draft_markdown") or "")
    if not markdown.strip():
        raise ValueError("缺少迭代后的交底书终稿，无法交付")
    case_title = _case_title(ctx)
    iteration_type = "merge" if intent == "merge" else "correction"

    # ① 留档摘要（强制在 chat 通道，格式 verbatim）
    summary = await _archive_summary(ctx, intent)

    # ② 新时间戳交付物（禁止覆盖旧稿）
    figures = await _save_mermaid_pngs(ctx, markdown, case_title, iteration_type)
    files = await _save_files(
        ctx, markdown, case_title, iteration_type=iteration_type, summary=summary
    )

    lines = ["本轮迭代已交付："]
    lines.append(f"- Markdown 新版本：{files['md']['filename']}（v{files['md']['version']}）")
    if files.get("docx"):
        lines.append(f"- Word 文件：{files['docx']['filename']}")
    elif files.get("docx_error"):
        lines.append(f"- Word 文件未能生成：{files['docx_error']}")
    if files.get("pdf"):
        lines.append(f"- PDF 文件：{files['pdf']['filename']}（引擎 {files.get('pdf_engine')}）")
    if files.get("previous_md"):
        lines.append(f"- 上一版仍保留：{files['previous_md']}（迭代只增不改，绝不覆盖旧稿）")
    unresolved = list((ctx.state.get("self_check") or {}).get("unresolved") or [])
    if unresolved:
        lines.append(f"- 需人工确认 {len(unresolved)} 项：")
        lines.extend(f"  · {u}" for u in unresolved[:5])
    await ctx.chat_delta("\n".join(lines))
    await ctx.chat_done()

    # ③ 合并轮再附「权利要求偏向点」引导
    bias: dict[str, Any] | None = None
    if intent == "merge":
        try:
            bias = await _claim_bias_gate(ctx, markdown)
        except Exception as exc:  # noqa: BLE001 —— 可选引导失败不影响已交付的新版本
            logger.warning("迭代轮偏向点引导失败：%s", exc)
            bias = {"error": str(exc)}

    # ④ 修订记录五要素 + 累计「交底书修订对话记录.md」
    record = await revision_log.record(
        ctx.case_id,
        iteration_type=iteration_type,
        instruction=_instruction(ctx) or str(iteration.get("rationale") or ""),
        files=[d["filename"] for d in files["downloads"]],
        summary=summary,
        run_group=ctx.run_group,
        artifact_id=files["md"]["id"],
        intent=intent,
    )
    log_artifact = await revision_log.export_log(
        ctx.case_id, case_title=case_title, run_group=ctx.run_group
    )
    await main_pipeline._emit_artifact(ctx.case_id, log_artifact)

    iteration.update(
        {
            "status": "delivered",
            "summary_md": summary,
            "deliver": {
                "figures": figures,
                "files": files,
                "claim_bias": bias,
                "revision": record.model_dump(),
                "revision_log": json.loads(log_artifact.model_dump_json()),
            },
        }
    )
    return {"iteration": iteration}


# ---------------------------------------------------------------------------
# 步骤表
# ---------------------------------------------------------------------------


ITERATION_STEP_KEYS: tuple[str, ...] = (
    "iterate_route",
    "iterate_plan",
    "iterate_rewrite",
    "iterate_check",
    "iterate_deliver",
)


def build_iteration_steps(case: dict[str, Any]) -> list[StepDef]:
    """迭代流水线五步（`run_group='iteration-<n>'`）。"""
    ptype = str(case.get("patent_type") or "invention")
    if ptype != "invention":
        raise main_pipeline.PatentTypeNotSupportedError(main_pipeline._unsupported_message(ptype))
    return [
        StepDef(key="iterate_route", name_zh="意图判别", handler=iterate_route),
        StepDef(key="iterate_plan", name_zh="增量与纠正点识别", handler=iterate_plan),
        StepDef(key="iterate_rewrite", name_zh="受影响章节改写", handler=iterate_rewrite),
        StepDef(key="iterate_check", name_zh="组装与自检", handler=iterate_check),
        StepDef(key="iterate_deliver", name_zh="迭代交付", handler=iterate_deliver, gate="claim_bias"),
    ]
