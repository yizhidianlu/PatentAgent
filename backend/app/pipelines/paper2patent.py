"""论文转专利流水线（模块 B）。

步骤（backend-architecture.md §4 模块② / prompt-porting-spec.md §2 模块 B）：

    input_check → extraction → draft → rules_check → content_review(review 门控)
                → drawings → build

- **启动载荷**：`{file_id, mode: 'direct'|'human_in_loop'|'flash', extra?}`；
- **flash 快速档**：input_check 内单次 GEN（flash.md + 论文全文）流式吐五部分纯文本，
  存 md 交付物后直接交付结束，后续步骤全部空跑（不进脚本管线）；
- **忠实红线**：任何缺口只写 `【待补充：…】` 占位并汇入 gaps，绝不推断补齐；
- **大 JSON 纪律**（R6）：五部分分次生成，PatentContent 由**服务端确定性装配**，
  绝不让模型一次吐一个大 JSON；pydantic 校验失败回指对应调用做 REPAIR；
- **交付话术**：交付消息只报文件、剩余占位符与转换限制，不外露内部检查过程。

本模块同时导出 `regenerate_drawings()` / `build_files()` 供 api/p2p.py 复用，
保证「流水线自动跑」与「用户手动重跑」走同一段代码。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import anyio.to_thread
from pydantic import ValidationError

from ..db import database as db
from ..models.paper2patent import (
    AbstractDraft,
    DescriptionSections,
    DraftClaims,
    DrawingSpecs,
    Extraction,
    InputAssessment,
    P2pStartPayload,
    PatentContent,
)
from ..services import artifacts as artifacts_service
from ..services import paths as paths_service
from ..services import assembler, claims_lint, faithfulness
from ..services import drawings as drawings_service
from ..services.stream_filter import TailMuter
from ..services import export_pdf as export_pdf_service
from ..services import llm as llm_service
from ..services.convert import run_tool
from ..services.sse import hub
from .engine import Ctx, InteractionRequest, StepDef

logger = logging.getLogger(__name__)

# 文档通道 doc_id（前端右侧文档面板按 doc_id 归集流式正文）
DOC_ID = "patent"

# DOCX 生成子进程超时（秒）
DOCX_TIMEOUT = 300

# 中文近似 token 估算系数：无 tiktoken 依赖，按「1 token ≈ 1.7 个字符」估
# （中英混排的经验值；宁可高估也不要撑爆上下文）
CHARS_PER_TOKEN = 1.7

# 权项 lint 违规后的 REPAIR 次数上限（§2 B4）
MAX_CLAIMS_REPAIR = 2
# PatentContent 装配校验失败后的 REPAIR 轮数上限（R6）
MAX_ASSEMBLE_REPAIR = 2
# 附图规格 REPAIR 次数上限（每图，R13）
MAX_DRAWING_REPAIR = 2
# 缺论文时请用户补传的最多轮数（超出则报错，避免无限挂起）
_MAX_UPLOAD_ROUNDS = 3

# ---------------------------------------------------------------------------
# prompt 装配清单（§0.2：平台角色头 → 模块 Role → 脱敏 → 术语 → 阶段指令）
# ---------------------------------------------------------------------------

BASE_PARTS = [
    "common/system_base",
    "paper2patent/pro/role_task",
    "common/desensitization",
    "common/terminology_rules",
]

FLASH_PARTS = [
    "common/system_base",
    "paper2patent/flash",            # Flash Prompt 自带 Role/Task，占模块 Role 槽位
    "common/desensitization",
    "common/terminology_rules",
]

ASSESS_PARTS = [*BASE_PARTS, "paper2patent/input_assessment"]
EXTRACTION_PARTS = [*BASE_PARTS, "paper2patent/pro/writing_methods", "paper2patent/extraction"]
CLAIMS_PARTS = [
    *BASE_PARTS,
    "paper2patent/pro/part3_claims",
    "paper2patent/pro/critical_rules",
    "paper2patent/claims_spec_rules",
]
ABSTRACT_PARTS = [*BASE_PARTS, "paper2patent/pro/part1_abstract"]
DESCRIPTION_PARTS = [
    *BASE_PARTS,
    "paper2patent/pro/part4_description",
    "paper2patent/claims_spec_rules",
]
DRAWING_SPEC_PARTS = [
    *BASE_PARTS,
    "paper2patent/pro/part5_drawings",
    "paper2patent/drawing_rules",
]


class BuildError(RuntimeError):
    """DOCX/PDF 生成失败。"""


# ---------------------------------------------------------------------------
# 上下文经济：token 估算与论文裁剪
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """中文近似 token 数（len//1.7）；不引入 tiktoken 依赖，只用于预算裁剪。"""
    return int(len(text or "") / CHARS_PER_TOKEN)


def paper_budget_tokens(*, output_tokens: int | None = None, system_chars: int = 0) -> int:
    """论文可注入的 token 预算 = context_window×80% − 期望输出 − 系统 prompt（§0.4）。"""
    cfg = llm_service.load_llm_settings()
    out = output_tokens if output_tokens is not None else cfg.max_output_tokens
    budget = int(cfg.context_window * 0.8) - out - int(system_chars / CHARS_PER_TOKEN)
    return max(1500, budget)


# 论文小节标题优先级（Abstract / Introduction / Method 优先全文注入，§2 B2）
_PRIORITY_PATTERNS = (
    re.compile(r"abstract|摘\s*要", re.IGNORECASE),
    re.compile(r"introduction|引\s*言|绪\s*论|背景", re.IGNORECASE),
    re.compile(r"method|approach|framework|architecture|方\s*法|模\s*型|系统设计", re.IGNORECASE),
)

_HEADING_RE = re.compile(r"^#{1,4}\s+.*$", re.MULTILINE)


def _split_paper_blocks(text: str) -> list[tuple[str, str]]:
    """按 markdown 标题把论文切成 (标题, 块全文) 列表；无标题时整篇作一块。"""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]
    blocks: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        blocks.append(("", text[: matches[0].start()]))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((m.group(0).lstrip("# ").strip(), text[m.start() : end]))
    return blocks


def _priority_rank(title: str) -> int:
    """标题的优先级：0/1/2 为 Abstract/Intro/Method，9 为其它。"""
    for rank, pattern in enumerate(_PRIORITY_PATTERNS):
        if pattern.search(title or ""):
            return rank
    return 9


def budget_paper_text(text: str, budget: int) -> str:
    """论文超预算时按 Abstract/Introduction/Method 优先裁剪（保持原文顺序）。"""
    if estimate_tokens(text) <= budget:
        return text
    blocks = _split_paper_blocks(text)
    ranked = sorted(range(len(blocks)), key=lambda i: (_priority_rank(blocks[i][0]), i))
    picked: set[int] = set()
    used = 0
    for i in ranked:
        cost = estimate_tokens(blocks[i][1])
        if used + cost > budget:
            continue
        picked.add(i)
        used += cost
    kept = "\n".join(blocks[i][1] for i in sorted(picked))
    if not kept:  # 单块超预算：硬截断
        kept = text[: int(budget * CHARS_PER_TOKEN)]
    return (
        kept.rstrip()
        + "\n\n（注：论文全文超出本次调用的上下文预算，已按 Abstract / Introduction / Method 优先保留上述选段。）"
    )


# ---------------------------------------------------------------------------
# 通用调用封装（每次调用都经 assembler 装配 system prompt）
# ---------------------------------------------------------------------------


def _messages(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": str(system)}, {"role": "user", "content": user}]


async def _stream_gen(
    ctx: Ctx,
    *,
    tag: str,
    system: str,
    user: str,
    channel: str = "doc",
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
    # 契约 JSON 尾巴不进人看的通道：完整文本（含尾巴）原样返回给解析方，
    # 但转发给 chat/doc 的只有正文——用户不该看到与正文重复的机器契约
    muter = TailMuter()

    async def _forward(text: str) -> None:
        if not text:
            return
        if channel == "chat":
            await ctx.chat_delta(text)
        else:
            await ctx.doc_delta(DOC_ID, text)

    async for delta in ctx.llm.chat_stream(_messages(system, user), **kwargs):
        chunks.append(delta)
        await _forward(muter.feed(delta))
    await _forward(muter.flush())
    if channel == "chat":
        await ctx.chat_done()
    else:
        await ctx.doc_done(DOC_ID)
    return "".join(chunks)


async def _call_text(ctx: Ctx, *, tag: str, system: str, user: str, call_class: str = "REPAIR") -> str:
    """非流式文本调用（REPAIR / CHAT 缓冲用）。"""
    return await ctx.llm.chat(
        _messages(system, user),
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.{tag}",
        **assembler.call_params(call_class),
    )


async def _call_struct(
    ctx: Ctx, model_cls, *, tag: str, system: str, user: str, call_class: str = "STRUCT"
):
    """结构化调用（STRUCT / REPAIR / AUDIT）。"""
    return await ctx.llm.structured(
        _messages(system, user),
        model_cls,
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.{tag}",
        **assembler.call_params(call_class),
    )


# ---------------------------------------------------------------------------
# 案件 / 论文 / 状态小工具
# ---------------------------------------------------------------------------


def _case_card(ctx: Ctx) -> dict[str, Any]:
    """注入 prompt 的案件卡片。"""
    case = ctx.case or {}
    return {
        "case_id": ctx.case_id,
        "module": "paper2patent（论文转专利）",
        "title": case.get("title"),
        "patent_type": case.get("patent_type") or "invention（发明）",
        "mode": _mode(ctx),
    }


def _mode(ctx: Ctx) -> str:
    """当前输出模式：direct / human_in_loop / flash。"""
    mode = ctx.state.get("mode")
    if mode:
        return str(mode)
    payload = ctx.start_payload if isinstance(ctx.start_payload, dict) else {}
    return str(payload.get("mode") or "direct")


def _is_flash(ctx: Ctx) -> bool:
    return _mode(ctx) == "flash"


def _paper_file_row(case_id: str, file_id: str) -> dict[str, Any]:
    """取论文文件行：指定 file_id 优先，否则回落该案件最近一个有 md 的上传件。"""
    row = None
    if file_id:
        row = db.query_one("SELECT * FROM files WHERE id=? AND case_id=?", (file_id, case_id))
        if row is None:
            raise ValueError(f"论文文件不存在或不属于本案件：{file_id}")
    else:
        row = db.query_one(
            "SELECT * FROM files WHERE case_id=? AND md_path IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (case_id,),
        )
        if row is None:
            raise ValueError("本案件尚未上传可用的论文文件（需先经 /cases/{id}/files 上传并转换）")
    record = dict(row)
    if not record.get("md_path"):
        raise ValueError(f"论文文件《{record.get('orig_name')}》没有转换后的 markdown，无法进入流水线")
    return record


def _read_paper_sync(case_id: str, file_id: str) -> tuple[str, str, str]:
    """返回 (files.id, 论文标题, 论文 markdown 全文)。"""
    record = _paper_file_row(case_id, file_id)
    path = paths_service.resolve_existing(record["md_path"])
    if path is None:
        raise ValueError(f"论文转换文本已不在磁盘：{record['md_path']}")
    text = path.read_text(encoding="utf-8", errors="replace")
    title = _paper_title(text) or Path(record["orig_name"]).stem
    return str(record["id"]), title, text


_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
# convert.py 的元信息注释与分页标记不是标题，取标题时必须跳过
# （否则案件名与交付物文件名会变成「!-- 由 convert.py 自 xx.pdf 抽取…」）
_NON_TITLE_RE = re.compile(r"^\s*(<!--|-->|!\[|\||#{0,6}\s*第\s*\d+\s*页\s*$)")


def _paper_title(text: str) -> str:
    """论文标题：首个一级标题，其次首个非空正文行（跳过转换元信息与分页标记）。"""
    m = _TITLE_RE.search(text)
    if m and not _NON_TITLE_RE.match(m.group(1)):
        return m.group(1).strip()
    in_comment = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if in_comment:  # 多行 <!-- ... --> 注释块
            in_comment = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            in_comment = "-->" not in stripped
            continue
        if _NON_TITLE_RE.match(stripped):
            continue
        return stripped.lstrip("# ").strip()
    return ""


async def _load_paper(ctx: Ctx) -> tuple[str, str, str]:
    """载入论文（不进 state，避免把全文塞进 cases.state_json）。"""
    file_id = str((ctx.state.get("paper") or {}).get("file_id") or "")
    if not file_id:
        payload = ctx.start_payload if isinstance(ctx.start_payload, dict) else {}
        file_id = str(payload.get("file_id") or "")
    return await db.arun(_read_paper_sync, ctx.case_id, file_id)


async def _set_case_title(ctx: Ctx, invention_name: str) -> None:
    """用发明名称回填案件标题并广播 case_title。"""
    title = (invention_name or "").strip()[:80]
    if not title:
        return
    await db.aexecute(
        "UPDATE cases SET title=?, updated_at=? WHERE id=?", (title, db.now_str(), ctx.case_id)
    )
    await ctx.reload_case()
    await ctx.emit("case_title", {"title": title})


async def _emit_artifact(case_id: str, artifact) -> None:
    """artifact_created 事件（载荷即 Artifact 记录）。"""
    await hub.emit(case_id, "artifact_created", json.loads(artifact.model_dump_json()))


async def _emit_doc_version(case_id: str, artifact, downloads: list[dict[str, Any]] | None = None) -> None:
    """doc_version 事件（版本历史 + 下载菜单数据源）。"""
    await hub.emit(
        case_id,
        "doc_version",
        {
            "doc_id": DOC_ID,
            "version_id": artifact.id,
            "version": artifact.version,
            "kind": artifact.kind,
            "filename": artifact.filename,
            "artifact_id": artifact.id,
            "downloads": downloads
            or [{"kind": artifact.kind, "artifact_id": artifact.id, "filename": artifact.filename}],
            "iteration_type": artifact.iteration_type,
            "summary": artifact.summary,
            "created_at": artifact.created_at,
        },
    )


async def save_content_artifact(
    case_id: str, content: dict[str, Any], *, run_group: str | None = None, summary: str | None = None
):
    """落一版 patent_content_json 交付物并广播事件。"""
    artifact = await artifacts_service.save_artifact(
        case_id,
        "patent_content_json",
        json.dumps(content, ensure_ascii=False, indent=2) + "\n",
        "json",
        title=content.get("invention_name") or None,
        run_group=run_group,
        iteration_type="initial",
        summary=summary,
    )
    await _emit_artifact(case_id, artifact)
    await _emit_doc_version(case_id, artifact)
    return artifact


# ---------------------------------------------------------------------------
# 步骤 1：input_check —— 输入评估 / flash 快速档
# ---------------------------------------------------------------------------


async def input_check(ctx: Ctx) -> dict[str, Any]:
    """B1 输入评估：STRUCT 评估 → hil 门控补缺口 / direct 记录 gaps；flash 直接交付。"""
    payload = P2pStartPayload.model_validate(ctx.start_payload or {})
    mode = payload.mode
    ctx.state["mode"] = mode  # 供后续步骤（含 _case_card）读取

    # 缺论文时不崩溃：挂起门控请用户补传，上传后重试本步（用户最常撞上的入口场景）
    file_id_hint = payload.file_id
    for _ in range(_MAX_UPLOAD_ROUNDS):
        try:
            file_id, title, paper_text = await db.arun(_read_paper_sync, ctx.case_id, file_id_hint)
            break
        except ValueError as exc:
            answer = await ctx.await_user(
                InteractionRequest(
                    kind="material_upload",
                    prompt=f"{exc}\n\n请上传论文 PDF 后继续（上传完成会自动接着跑）。",
                    schema={
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string", "title": "已上传的论文文件 ID"},
                            "file_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    default={"accept": ".pdf", "hint": "论文 PDF（支持正文与插图自动抽取）"},
                )
            )
            picked = answer.get("file_id") or (answer.get("file_ids") or [""])[0]
            file_id_hint = str(picked or "")
    else:
        raise ValueError("多次尝试后仍未取得可用的论文文件，请确认上传是否成功。")

    await ctx.emit(
        "log",
        {"message": f"已载入论文《{title}》（{len(paper_text)} 字，约 {estimate_tokens(paper_text)} tokens）"},
    )
    paper_meta = {"file_id": file_id, "title": title, "chars": len(paper_text)}

    if mode == "flash":
        result = await _run_flash(ctx, title, paper_text)
        return {"mode": mode, "paper": paper_meta, **result}

    system = assembler.assemble(
        ASSESS_PARTS,
        runtime_ctx={"case_card": _case_card(ctx), "extra": {"mode": mode, "paper_title": title}},
    )
    user = (
        "以下是论文转换后的 markdown 全文，请按评估维度输出唯一一个 JSON 对象。\n\n"
        f"===== 论文全文 =====\n{budget_paper_text(paper_text, paper_budget_tokens(output_tokens=2048))}"
    )
    assessment: InputAssessment = await _call_struct(
        ctx, InputAssessment, tag="assess", system=system, user=user
    )

    gaps = [g.placeholder() for g in assessment.gaps]
    supplements: list[dict[str, Any]] = []
    if mode == "human_in_loop" and assessment.gaps:
        answer = await ctx.await_user(
            InteractionRequest(
                kind="form",
                prompt=(
                    f"输入评估完成（完整性：{assessment.completeness.overall}，"
                    f"附图就绪度：{assessment.drawing_readiness}）。以下缺口会影响权利要求范围或说明书充分性，"
                    "请补充或确认——留空表示确认无法补充，将在成文中写为【待补充：…】占位。"
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "supplements": {
                            "type": "array",
                            "title": "缺口补充",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "item": {"type": "string", "title": "缺口"},
                                    "question": {"type": "string", "title": "补充问题"},
                                    "reply": {"type": "string", "title": "你的补充（可留空）"},
                                },
                            },
                        },
                        "note": {"type": "string", "title": "其他补充说明"},
                    },
                },
                default={
                    "supplements": [
                        {
                            "item": g.item,
                            "question": g.suggested_question or g.item,
                            "reply": "",
                            "severity": g.severity,
                            "affects": g.affects,
                        }
                        for g in assessment.gaps
                    ],
                    "note": "",
                },
            )
        )
        supplements, gaps = _merge_supplements(assessment, answer)

    if gaps:
        await ctx.emit("log", {"message": f"记录 {len(gaps)} 条材料缺口，将在成文中以【待补充：…】占位。"})

    return {
        "mode": mode,
        "paper": paper_meta,
        "assessment": assessment.model_dump(),
        "gaps": gaps,
        "user_supplements": supplements,
    }


def _merge_supplements(
    assessment: InputAssessment, answer: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    """把用户补充与未补充的缺口分开：有回答的进 supplements，其余仍作 gaps 占位。"""
    data = answer if isinstance(answer, dict) else {}
    replies = {
        str(item.get("item") or ""): str(item.get("reply") or "").strip()
        for item in (data.get("supplements") or [])
        if isinstance(item, dict)
    }
    supplements: list[dict[str, Any]] = []
    gaps: list[str] = []
    for gap in assessment.gaps:
        reply = replies.get(gap.item, "")
        if reply:
            supplements.append({"item": gap.item, "reply": reply})
        else:
            gaps.append(gap.placeholder())
    note = str(data.get("note") or "").strip()
    if note:
        supplements.append({"item": "用户补充说明", "reply": note})
    return supplements, gaps


async def _run_flash(ctx: Ctx, title: str, paper_text: str) -> dict[str, Any]:
    """flash 快速档：单次 GEN 流式产五部分纯文本 → md 交付物 → 直接交付结束。"""
    cfg = llm_service.load_llm_settings()
    system = assembler.assemble(FLASH_PARTS, runtime_ctx={"case_card": _case_card(ctx)})
    user = (
        "【论文原文内容】\n"
        f"{budget_paper_text(paper_text, paper_budget_tokens(output_tokens=cfg.max_output_tokens))}\n\n"
        "请按上述五大部分结构直接输出专利申请文件正文（纯文本，不要输出任何分析过程或清单核对结果）。"
    )
    text = await _stream_gen(
        ctx, tag="flash", system=system, user=user, channel="doc",
        max_output_tokens=cfg.max_output_tokens,
    )

    # 注：artifacts.kind 的 CHECK 约束里没有「专利五部分 md」这一类，
    # 快速档产物按 patent_content_json 归档（扩展名 md），语义上属于同一交付序列。
    artifact = await artifacts_service.save_artifact(
        ctx.case_id,
        "patent_content_json",
        text,
        "md",
        title=title,
        run_group=ctx.run_group,
        iteration_type="initial",
        summary="flash 快速档：五部分纯文本（未进附图与文件管线）",
    )
    await _emit_artifact(ctx.case_id, artifact)
    await _emit_doc_version(ctx.case_id, artifact)

    placeholders = _placeholders_in(text)
    lines = [
        "快速档已完成，交付如下：",
        f"- 五部分专利文本：{artifact.filename}",
        f"- 剩余占位符：{len(placeholders)} 处" + ("（见正文【待补充：…】）" if placeholders else ""),
        "- 转换限制：快速档不生成附图与 Word/PDF 文件；需要完整申请文件请改用直接生成或人工确认模式。",
    ]
    await ctx.chat_delta("\n".join(lines))
    await ctx.chat_done()
    return {"flash_artifact_id": artifact.id, "flash_chars": len(text), "gaps": placeholders}


def _placeholders_in(text: str) -> list[str]:
    """扫描文本中的【待补充：…】占位符。"""
    return re.findall(r"【待补充[^】]*】", text or "")


# ---------------------------------------------------------------------------
# 步骤 2：extraction —— 深读提取（四要素闭环 + source_map）
# ---------------------------------------------------------------------------


async def extraction(ctx: Ctx) -> dict[str, Any]:
    """B2 深读提取 STRUCT：contributions/method_steps/system_components/…/source_map。"""
    if _is_flash(ctx):
        return {}
    _, title, paper_text = await _load_paper(ctx)
    system = assembler.assemble(
        EXTRACTION_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "paper_title": title,
                "input_assessment": ctx.state.get("assessment"),
                "user_supplements": ctx.state.get("user_supplements") or [],
                "known_gaps": ctx.state.get("gaps") or [],
            },
        },
    )
    budget = paper_budget_tokens(output_tokens=4096, system_chars=len(system))
    user = (
        "以下是论文正文，请按方法一步骤一与方法二执行深读提取，只输出一个 JSON 对象。\n\n"
        f"===== 论文正文 =====\n{budget_paper_text(paper_text, budget)}"
    )
    result: Extraction = await _call_struct(ctx, Extraction, tag="extract", system=system, user=user)
    await ctx.emit(
        "log",
        {
            "message": (
                f"深读提取完成：贡献 {len(result.contributions)} 项、方法步骤 "
                f"{len(result.method_steps)} 步、系统模块 {len(result.system_components)} 个、"
                f"落源记录 {len(result.source_map)} 条。"
            )
        },
    )
    return {
        "extraction": result.model_dump(),
        "source_map": [m.model_dump() for m in result.source_map],
    }


# ---------------------------------------------------------------------------
# 步骤 3：draft —— 四次生成调用（权项 / 摘要 / 说明书×2 / 附图规格）
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)

# 说明书小节标题（容忍 markdown 井号、中文序号、冒号）
_SECTION_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:[一二三四五六七八九十]+、[ \t]*|\d+[.．、][ \t]*)?"
    r"(技术领域|背景技术|技术背景|发明内容|附图说明|具体实施方式)[ \t]*[:：]?[ \t]*$",
    re.MULTILINE,
)

_SECTION_ALIAS = {"技术背景": "背景技术"}


def _split_sections(text: str) -> dict[str, str]:
    """按小节标题切分说明书生成结果。"""
    matches = list(_SECTION_RE.finditer(text or ""))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = _SECTION_ALIAS.get(m.group(1), m.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[m.end() : end].strip()
    return sections


def _parse_tail_json(text: str) -> dict[str, Any] | None:
    """取生成结果末尾的 ```json 围栏块（从后往前找第一个能解析的）。"""
    for m in reversed(list(_JSON_FENCE_RE.finditer(text or ""))):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _strip_tail_json(text: str) -> str:
    """从 markdown 正文里剥掉尾部 JSON 围栏块（正文入库前调用）。"""
    return _JSON_FENCE_RE.sub("", text or "").strip()


def _abstract_problems(abstract: str, invention_name: str) -> list[str]:
    """摘要的服务端硬校验：≤300 字 + 名称与权 1 逐字一致。"""
    problems: list[str] = []
    body = re.sub(r"\s", "", abstract or "")
    if not body:
        problems.append("摘要正文为空")
    if len(body) > 300:
        problems.append(f"摘要正文 {len(body)} 字，超过 300 字上限，请压缩到 300 字以内")
    if invention_name and invention_name not in (abstract or ""):
        problems.append(
            f"摘要中未逐字出现发明名称「{invention_name}」；名称必须与权利要求 1 的主题名称完全一致"
        )
    return problems


async def draft(ctx: Ctx) -> dict[str, Any]:
    """B3a-B3d：权利要求书 → 摘要 → 说明书（两次）→ 附图规格。"""
    if _is_flash(ctx):
        return {}
    cfg = llm_service.load_llm_settings()
    extraction_data = ctx.state.get("extraction") or {}
    gaps = list(ctx.state.get("gaps") or [])
    supplements = ctx.state.get("user_supplements") or []
    paper_title = (ctx.state.get("paper") or {}).get("title") or ""
    common_ctx = {
        "case_card": _case_card(ctx),
        "extra": {
            "paper_title": paper_title,
            "extraction": extraction_data,
            "user_supplements": supplements,
            "known_gaps": gaps,
        },
    }

    # ① 权利要求书（GEN，chat 通道流式；尾部 JSON 契约）
    await ctx.progress("撰写权利要求书", index=1, total=5, waiting_for="模型")
    claims_system = assembler.assemble(CLAIMS_PARTS, runtime_ctx=common_ctx)
    claims_user = (
        "请基于上述深读提取产物撰写**权利要求书**（6-10 项，含 1-2 项独立权利要求）。\n"
        "输出要求：\n"
        "1. 先输出权利要求书 markdown 正文，逐项独立成段；\n"
        "2. 正文末尾追加一个 ```json 围栏块，内容为 "
        '{"invention_name": "一种……方法", "claims": ["1.……。", "2.……。"]}，'
        "claims 数组逐项与正文完全一致（含项号与结尾句号）；\n"
        "3. 每项权利要求只有结尾一个句号，中间一律用分号或逗号；严禁出现「等、可以、大约、优选、比如、不限于」等不确定用语；\n"
        "4. 论文未支撑的内容一律写为【待补充：…】，不得推断补齐。"
    )
    claims_md = await _stream_gen(
        ctx, tag="claims", system=claims_system, user=claims_user, channel="chat"
    )
    draft_claims = await _resolve_claims(ctx, claims_md)
    await _set_case_title(ctx, draft_claims.invention_name)

    # ② 说明书摘要（GEN，doc 通道；服务端校验失败触发 REPAIR）
    await ctx.progress("撰写摘要", index=2, total=5, waiting_for="模型")
    abstract_system = assembler.assemble(
        ABSTRACT_PARTS,
        runtime_ctx={
            **common_ctx,
            "extra": {
                **common_ctx["extra"],
                "invention_name": draft_claims.invention_name,
                "claim_1": draft_claims.claims[0] if draft_claims.claims else "",
            },
        },
    )
    abstract_user = (
        "请撰写**说明书摘要**（四段式：名称+领域 / 技术方案核心 / 技术效果 / 可选关键词）。\n"
        f"硬性要求：正文不超过 300 个汉字；必须逐字使用发明名称「{draft_claims.invention_name}」；"
        "结尾只有一个句号；不得出现评价性语言。只输出摘要正文，不要输出小节标题。"
    )
    abstract_text = await _stream_gen(
        ctx, tag="abstract", system=abstract_system, user=abstract_user, channel="doc"
    )
    abstract_text = _strip_tail_json(abstract_text)
    for attempt in range(1, MAX_CLAIMS_REPAIR + 1):
        problems = _abstract_problems(abstract_text, draft_claims.invention_name)
        if not problems:
            break
        await ctx.emit("log", {"message": f"说明书摘要未通过服务端校验（第 {attempt} 次修复）。"})
        repaired: AbstractDraft = await _call_struct(
            ctx,
            AbstractDraft,
            tag=f"abstract.repair{attempt}",
            system=abstract_system,
            user=(
                "以下摘要未通过服务端校验，请按问题清单修正后只输出 "
                '{"abstract": "……"} 一个 JSON 对象。\n\n问题清单：\n'
                + "\n".join(f"- {p}" for p in problems)
                + f"\n\n原摘要：\n{abstract_text}"
            ),
            call_class="REPAIR",
        )
        abstract_text = repaired.abstract.strip()

    # ③ 说明书（GEN×2：前三节 / 附图说明+具体实施方式，后者独占全额输出预算）
    await ctx.progress("撰写说明书前三节", index=3, total=5, waiting_for="模型")
    desc_ctx = {
        **common_ctx,
        "extra": {
            **common_ctx["extra"],
            "invention_name": draft_claims.invention_name,
            "claims": draft_claims.claims,
            "abstract": abstract_text,
        },
    }
    desc_system = assembler.assemble(DESCRIPTION_PARTS, runtime_ctx=desc_ctx)
    part1_md = await _stream_gen(
        ctx,
        tag="description.part1",
        system=desc_system,
        user=(
            "请撰写说明书的前三节，并严格使用下列小节标题各占一行：\n"
            "技术领域 / 背景技术 / 发明内容\n"
            "要求：技术领域一句带过；背景技术必须覆盖技术定义、现有方法、现有问题、问题成因与后果；"
            "发明内容必须覆盖简要概括、技术方案细化、技术效果，且与权利要求逐项对应。"
            "不要撰写附图说明与具体实施方式。"
        ),
        channel="doc",
    )
    part1 = _split_sections(part1_md)

    # 全流程最慢的一次调用：独占全额输出预算，实测可达二十多分钟
    await ctx.progress(
        "撰写附图说明与具体实施方式", index=4, total=5,
        detail="本节最长，输出期间会持续有正文流入右侧文档面板",
        waiting_for="模型",
    )
    part2_md = await _stream_gen(
        ctx,
        tag="description.part2",
        system=desc_system,
        user=(
            "请撰写说明书的**附图说明**与**具体实施方式**两节，严格使用下列小节标题各占一行：\n"
            "附图说明 / 具体实施方式\n"
            "要求：附图说明每图一句话（「图1为……示意图。」），图号与后续附图规格一致；"
            "具体实施方式结合附图对每个重要步骤回答「是什么、解决了什么问题、怎么解决的、达到了什么效果」，"
            "使用步骤化专利语言，禁止粘贴论文原话；论文未支撑处写【待补充：…】。"
        ),
        channel="doc",
        max_output_tokens=cfg.max_output_tokens,   # 最长产物：独占全额输出预算
    )
    part2 = _split_sections(part2_md)
    drawing_description = _as_lines(part2.get("附图说明", ""))

    description = {
        "technical_field": part1.get("技术领域", "").strip(),
        "background": part1.get("背景技术", "").strip(),
        "invention_content": part1.get("发明内容", "").strip(),
        "drawing_description": drawing_description,
        "embodiments": part2.get("具体实施方式", "").strip(),
    }

    # ④ 附图规格（STRUCT，原图优先）
    await ctx.progress("规划附图", index=5, total=5, waiting_for="模型")
    specs_system = assembler.assemble(
        DRAWING_SPEC_PARTS,
        runtime_ctx={
            **desc_ctx,
            "extra": {
                **desc_ctx["extra"],
                "drawing_description": drawing_description,
                "embodiments_digest": description["embodiments"][:2000],
            },
        },
    )
    specs_user = (
        "请输出附图规格 JSON（原图优先：能追溯到论文原图/图注的写入 source_figures）。\n"
        "drawings 数组每项一幅图，必须写成平台附图脚本可解析的格式：\n"
        "- 流程图：「图1：一种……方法流程图，包含步骤S101，……；S102，……；S103，……。」"
        "（步骤号必须是 S101/S102 三位数字形式，步骤之间用分号分隔）\n"
        "- 系统框图/数据流图：「图2：一种……系统结构示意图，包含第一模块、第二模块、第三模块。」"
        "（必须出现「包含」二字，模块名用顿号分隔，2-8 个）\n"
        "abstract_drawing 写成「（注：建议选取说明书附图中的图X作为摘要附图）」。\n"
        "图号、步骤号、模块名必须与权利要求书、说明书、附图说明完全一致；不得新增未被支撑的模块或步骤。"
    )
    specs: DrawingSpecs = await _call_struct(
        ctx, DrawingSpecs, tag="drawings", system=specs_system, user=specs_user
    )

    draft_data = {
        "invention_name": draft_claims.invention_name,
        "source_title": paper_title,
        "abstract": abstract_text,
        "abstract_drawing": specs.abstract_drawing,
        "claims": draft_claims.claims,
        "description": description,
        "drawings": list(specs.drawings),
        "source_figures": [f.model_dump() for f in specs.source_figures],
    }
    abstract_chars = len(re.sub(r"\s", "", abstract_text))
    await ctx.emit(
        "log",
        {
            "message": (
                f"五部分草稿完成：权利要求 {len(draft_claims.claims)} 项、"
                f"摘要 {abstract_chars} 字、附图 {len(specs.drawings)} 幅。"
            )
        },
    )
    return {"draft": draft_data, "claims_markdown": _strip_tail_json(claims_md)}


def _as_lines(text: str) -> list[str]:
    """把一段多行文本转成去装饰的行列表（附图说明用）。"""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        line = re.sub(r"^\d+[.．、]\s*", "", line)
        if line:
            lines.append(line)
    return lines


async def _resolve_claims(ctx: Ctx, claims_md: str) -> DraftClaims:
    """从权项生成结果解析尾部 JSON；缺失或非法时补一次 STRUCT 抽取调用。"""
    tail = _parse_tail_json(claims_md)
    if tail is not None:
        try:
            return DraftClaims.model_validate(tail)
        except ValidationError as exc:
            logger.warning("权项尾部 JSON 校验失败，改用抽取调用：%s", exc)
    system = assembler.assemble(
        [*BASE_PARTS, "paper2patent/claims_spec_rules"],
        runtime_ctx={"case_card": _case_card(ctx)},
    )
    return await _call_struct(
        ctx,
        DraftClaims,
        tag="claims.json",
        system=system,
        user=(
            "请把下面的权利要求书正文原样结构化为 "
            '{"invention_name": "…", "claims": ["1.…。", "2.…。"]}，'
            "不得改写、增删任何技术内容。\n\n" + claims_md
        ),
        call_class="STRUCT",
    )


# ---------------------------------------------------------------------------
# 步骤 4：rules_check —— 权项 lint + 忠实性审计 + 质检 + 确定性装配
# ---------------------------------------------------------------------------


def _lint_summary(findings: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- 权利要求{f['claim_no']}｜{f['rule']}｜{f['severity']}：{f['message']}" for f in findings
    )


def _blocking(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """需要交 REPAIR 判断的发现：全部 error + 禁用词命中（CJK 无词界，只标记不自动删）。"""
    return [f for f in findings if f["severity"] == "error" or f["rule"] == "forbidden_word"]


async def _lint_and_repair(ctx: Ctx, draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """权项 lint → 违规清单反馈 REPAIR ≤2；返回 (新草稿, lint 报告)。"""
    claims = list(draft.get("claims") or [])
    name = str(draft.get("invention_name") or "")
    findings = claims_lint.lint_claims(claims, name)
    attempts = 0
    while _blocking(findings) and attempts < MAX_CLAIMS_REPAIR:
        attempts += 1
        await ctx.emit(
            "log",
            {"message": f"权利要求校验发现 {len(_blocking(findings))} 处违规，正在第 {attempts} 次修复。"},
        )
        system = assembler.assemble(
            CLAIMS_PARTS,
            runtime_ctx={
                "case_card": _case_card(ctx),
                "extra": {"extraction": ctx.state.get("extraction"), "invention_name": name},
            },
        )
        repaired: DraftClaims = await _call_struct(
            ctx,
            DraftClaims,
            tag=f"claims.repair{attempts}",
            system=system,
            user=(
                "以下权利要求书未通过服务端确定性校验。请逐条修正违规（禁用词命中可能是技术词的一部分，"
                "由你判断是否需要改写；不得删改技术内容本身），只输出 "
                '{"invention_name": "…", "claims": [...]} 一个 JSON 对象。\n\n'
                f"违规清单：\n{_lint_summary(_blocking(findings))}\n\n"
                f"当前权利要求书：\n{faithfulness.claims_text(claims)}"
            ),
            call_class="REPAIR",
        )
        claims = list(repaired.claims)
        name = repaired.invention_name or name
        findings = claims_lint.lint_claims(claims, name)

    draft = {**draft, "claims": claims, "invention_name": name}
    report = {
        "findings": findings,
        "blocking": _blocking(findings),
        "repair_attempts": attempts,
        "passed": not _blocking(findings),
    }
    return draft, report


async def rules_check(ctx: Ctx) -> dict[str, Any]:
    """B4 + B4b + B7 + B5：lint/REPAIR → 忠实性审计 → 质检补丁 → 确定性装配 PatentContent。"""
    if _is_flash(ctx):
        return {}
    draft = dict(ctx.state.get("draft") or {})
    if not draft:
        raise ValueError("缺少草稿产物，无法进入规则校验")
    gaps = list(ctx.state.get("gaps") or [])
    source_map = list(ctx.state.get("source_map") or [])
    mode = _mode(ctx)

    # 1) 权利要求确定性校验 + REPAIR
    draft, lint_report = await _lint_and_repair(ctx, draft)
    if not lint_report["passed"]:
        gaps.append(
            "【待补充：权利要求仍有未消解的书式问题，建议人工复核后经内容编辑接口修订。】"
        )

    # 2) 忠实性审计（B4b）
    _, _, paper_text = await _load_paper(ctx)
    audit = await faithfulness.audit_faithfulness(
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.faithfulness",
        paper_text=budget_paper_text(paper_text, paper_budget_tokens(output_tokens=4096)),
        source_map=source_map,
        claims=list(draft.get("claims") or []),
        embodiments=str((draft.get("description") or {}).get("embodiments") or ""),
        case_card=_case_card(ctx),
    )
    unsourced_gaps: list[str] = []
    if audit.unsourced:
        if mode == "human_in_loop":
            answer = await ctx.await_user(
                InteractionRequest(
                    kind="form",
                    prompt=(
                        f"忠实性审计发现 {len(audit.unsourced)} 条断言无法落到论文原文。"
                        "请逐条选择处置：delete=删除该表述，placeholder=改为【待补充：…】占位，keep=我确认有据保留。"
                    ),
                    schema={
                        "type": "object",
                        "properties": {
                            "decisions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "location": {"type": "string", "title": "位置"},
                                        "assertion": {"type": "string", "title": "断言"},
                                        "action": {
                                            "type": "string",
                                            "title": "处置",
                                            "enum": ["delete", "placeholder", "keep"],
                                        },
                                        "text": {"type": "string", "title": "替换文字（可选）"},
                                    },
                                },
                            }
                        },
                    },
                    default={
                        "decisions": [
                            {
                                "location": u.location,
                                "assertion": u.assertion,
                                "action": "placeholder",
                                "text": u.suggested_text,
                            }
                            for u in audit.unsourced
                        ]
                    },
                )
            )
            decisions = (answer or {}).get("decisions") if isinstance(answer, dict) else None
            draft, unsourced_gaps = faithfulness.apply_unsourced(
                draft, audit, mode="human_in_loop", decisions=decisions
            )
        else:
            draft, unsourced_gaps = faithfulness.apply_unsourced(draft, audit, mode="direct")
        gaps.extend(unsourced_gaps)

    # 3) 质检（B7）：补丁服务端静默应用
    quality = await faithfulness.audit_quality(
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.quality",
        draft=draft,
        source_map=source_map,
        case_card=_case_card(ctx),
    )
    patch_result = faithfulness.apply_patches(draft, quality.patches)
    draft = patch_result["draft"]
    unresolved = [u.model_dump() for u in quality.unresolved]
    if patch_result["low_rate"]:
        unresolved.append(
            {
                "checklist_item": "质检补丁应用率不足",
                "issue": (
                    f"共 {len(quality.patches)} 条补丁，仅应用 {patch_result['applied']} 条"
                    f"（{patch_result['rate']:.0%}），其余锚点未命中，已丢弃。"
                ),
                "needs": "user_decision",
            }
        )

    # 4) 服务端确定性装配 + pydantic 校验（失败回指对应调用 REPAIR）
    content, assemble_report = await _assemble_content(ctx, draft, gaps)

    artifact = await save_content_artifact(
        ctx.case_id, content, run_group=ctx.run_group, summary="初稿装配"
    )
    await ctx.emit(
        "log",
        {"message": f"专利内容契约已装配并落版本 v{artifact.version}（{artifact.filename}）。"},
    )
    return {
        "draft": draft,
        "content": content,
        "content_artifact_id": artifact.id,
        "gaps": content.get("gaps") or [],
        "lint_report": lint_report,
        "faithfulness": {
            "verdict": audit.verdict,
            "unsourced": [u.model_dump() for u in audit.unsourced],
            "applied_gaps": unsourced_gaps,
        },
        "quality": {
            "verdict": quality.verdict,
            "applied": patch_result["applied"],
            "dropped": len(patch_result["dropped"]),
            "rate": patch_result["rate"],
            "unresolved": unresolved,
        },
        "assemble_report": assemble_report,
    }


def _compose_content(draft: dict[str, Any], gaps: list[str]) -> dict[str, Any]:
    """服务端确定性装配 PatentContent（非 LLM，字段一一映射）。"""
    desc = draft.get("description") or {}
    return {
        "invention_name": str(draft.get("invention_name") or "").strip(),
        "source_title": str(draft.get("source_title") or "").strip(),
        "abstract": str(draft.get("abstract") or "").strip(),
        "abstract_drawing": str(draft.get("abstract_drawing") or "").strip(),
        "claims": [str(c).strip() for c in (draft.get("claims") or []) if str(c).strip()],
        "description": {
            "technical_field": str(desc.get("technical_field") or "").strip(),
            "background": str(desc.get("background") or "").strip(),
            "invention_content": str(desc.get("invention_content") or "").strip(),
            "drawing_description": [
                str(x).strip() for x in (desc.get("drawing_description") or []) if str(x).strip()
            ],
            "embodiments": str(desc.get("embodiments") or "").strip(),
        },
        "drawings": [str(d).strip() for d in (draft.get("drawings") or []) if str(d).strip()],
        "source_figures": list(draft.get("source_figures") or []),
        "drawing_assets": [],
        "image_model_prompts": [],
        "drawing_validation": [],
        "gaps": list(dict.fromkeys(gaps)),
    }


_FIELD_TO_CALL = {
    "invention_name": "claims",
    "claims": "claims",
    "abstract": "abstract",
    "description": "description",
    "drawings": "drawings",
}


async def _assemble_content(
    ctx: Ctx, draft: dict[str, Any], gaps: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """装配 + 校验；校验失败按出错字段回指对应生成调用 REPAIR（≤2 轮）。"""
    errors_log: list[str] = []
    for attempt in range(MAX_ASSEMBLE_REPAIR + 1):
        content = _compose_content(draft, gaps)
        try:
            PatentContent.model_validate(content)
            return content, {"repair_rounds": attempt, "errors": errors_log}
        except ValidationError as exc:
            if attempt == MAX_ASSEMBLE_REPAIR:
                raise ValueError(
                    "专利内容契约两轮修复后仍未通过校验："
                    + "；".join(errors_log + [str(exc)])[:1500]
                ) from exc
            fields = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            errors_log.append(str(exc)[:500])
            await ctx.emit(
                "log", {"message": f"内容契约校验未通过（{'、'.join(sorted(fields))}），正在定向修复。"}
            )
            calls = {_FIELD_TO_CALL.get(f, "description") for f in fields}
            for call in calls:
                draft = await _repair_call(ctx, call, draft, str(exc), attempt + 1)
    raise ValueError("专利内容契约装配失败")  # pragma: no cover —— 循环内必定 return/raise


async def _repair_call(
    ctx: Ctx, call: str, draft: dict[str, Any], error: str, round_no: int
) -> dict[str, Any]:
    """按出错字段回指对应生成调用做 REPAIR，并把结果并回草稿。"""
    common = {
        "case_card": _case_card(ctx),
        "extra": {
            "extraction": ctx.state.get("extraction"),
            "invention_name": draft.get("invention_name"),
            "claims": draft.get("claims"),
        },
    }
    if call == "claims":
        system = assembler.assemble(CLAIMS_PARTS, runtime_ctx=common)
        repaired: DraftClaims = await _call_struct(
            ctx, DraftClaims, tag=f"assemble.claims{round_no}", system=system,
            user=(
                "专利内容契约校验失败，错误如下：\n" + error
                + "\n\n请补全/修正发明名称与权利要求，只输出 "
                '{"invention_name": "…", "claims": [...]}。\n\n当前权利要求：\n'
                + faithfulness.claims_text(draft.get("claims") or [])
            ),
            call_class="REPAIR",
        )
        return {**draft, "invention_name": repaired.invention_name, "claims": list(repaired.claims)}

    if call == "abstract":
        system = assembler.assemble(ABSTRACT_PARTS, runtime_ctx=common)
        repaired_abs: AbstractDraft = await _call_struct(
            ctx, AbstractDraft, tag=f"assemble.abstract{round_no}", system=system,
            user=(
                "专利内容契约校验失败，错误如下：\n" + error
                + '\n\n请重写说明书摘要（≤300 字，名称与权利要求 1 一致），只输出 {"abstract": "…"}。'
            ),
            call_class="REPAIR",
        )
        return {**draft, "abstract": repaired_abs.abstract.strip()}

    if call == "drawings":
        system = assembler.assemble(DRAWING_SPEC_PARTS, runtime_ctx=common)
        repaired_specs: DrawingSpecs = await _call_struct(
            ctx, DrawingSpecs, tag=f"assemble.drawings{round_no}", system=system,
            user=("专利内容契约校验失败，错误如下：\n" + error + "\n\n请重新输出附图规格 JSON。"),
            call_class="REPAIR",
        )
        return {
            **draft,
            "drawings": list(repaired_specs.drawings),
            "abstract_drawing": repaired_specs.abstract_drawing or draft.get("abstract_drawing", ""),
            "source_figures": [f.model_dump() for f in repaired_specs.source_figures]
            or draft.get("source_figures", []),
        }

    system = assembler.assemble(DESCRIPTION_PARTS, runtime_ctx=common)
    repaired_desc: DescriptionSections = await _call_struct(
        ctx, DescriptionSections, tag=f"assemble.description{round_no}", system=system,
        user=(
            "专利内容契约校验失败，错误如下：\n" + error
            + "\n\n请补全说明书缺失的小节，只输出 "
            '{"technical_field","background","invention_content","drawing_description","embodiments"} '
            "五键 JSON（无需修改的键回填原文）。\n\n当前说明书：\n"
            + json.dumps(draft.get("description") or {}, ensure_ascii=False, indent=2)
        ),
        call_class="REPAIR",
    )
    desc = dict(draft.get("description") or {})
    for key in ("technical_field", "background", "invention_content", "embodiments"):
        value = str(getattr(repaired_desc, key) or "").strip()
        if value:
            desc[key] = value
    if repaired_desc.drawing_description:
        desc["drawing_description"] = list(repaired_desc.drawing_description)
    return {**draft, "description": desc}


# ---------------------------------------------------------------------------
# 步骤 5：content_review —— 人工确认模式的内容审阅门控
# ---------------------------------------------------------------------------


async def content_review(ctx: Ctx) -> dict[str, Any]:
    """human-in-loop：把装配好的内容交用户审阅/编辑；direct 与 flash 直接跳过。"""
    if _is_flash(ctx) or _mode(ctx) != "human_in_loop":
        return {}
    content = ctx.state.get("content") or {}
    payload = await ctx.await_user(
        InteractionRequest(
            kind="review",
            prompt=(
                "五部分内容已装配完成，请审阅。可直接确认，或提交编辑后的 content 覆盖当前版本"
                "（提交内容会重新经契约校验与权项校验）。"
            ),
            schema={
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean", "title": "确认无误"},
                    "content": {"type": "object", "title": "编辑后的完整内容（可选）"},
                },
            },
            default={"approved": True, "content": content},
        )
    )
    edited = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(edited, dict) or edited == content:
        return {"review": {"approved": True, "edited": False}}

    validated = PatentContent.model_validate(edited)
    new_content = json.loads(validated.model_dump_json())
    findings = claims_lint.lint_claims(new_content["claims"], new_content["invention_name"])
    artifact = await save_content_artifact(
        ctx.case_id, new_content, run_group=ctx.run_group, summary="用户编辑版本"
    )
    return {
        "content": new_content,
        "content_artifact_id": artifact.id,
        "gaps": new_content.get("gaps") or [],
        "review": {"approved": True, "edited": True, "lint": findings},
    }


# ---------------------------------------------------------------------------
# 步骤 6：drawings —— 附图脚本（validation 不过 → 规格 REPAIR ≤2 → 降级）
# ---------------------------------------------------------------------------


def _figure_numbers(content: dict[str, Any]) -> list[int]:
    nums: list[int] = []
    for line in content.get("drawings") or []:
        m = re.search(r"图\s*(\d+)", str(line))
        if m:
            nums.append(int(m.group(1)))
    return nums


async def _repair_drawing_spec(ctx: Ctx, content: dict[str, Any], figure_no: int, reason: str) -> str:
    """针对单幅图重写附图规格（REPAIR）；返回新规格文字（失败返回空串）。"""
    spec = drawings_service.spec_of(content, figure_no)
    system = assembler.assemble(
        DRAWING_SPEC_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "invention_name": content.get("invention_name"),
                "claims": content.get("claims"),
                "drawing_description": (content.get("description") or {}).get("drawing_description"),
            },
        },
    )
    try:
        repaired: DrawingSpecs = await _call_struct(
            ctx,
            DrawingSpecs,
            tag=f"drawings.repair{figure_no}",
            system=system,
            user=(
                f"图{figure_no} 的附图规格未能通过平台附图生成校验，原因：{reason}\n\n"
                f"当前规格：{spec}\n\n"
                f"请只重写图{figure_no} 的规格并放在 drawings 数组第一项，格式必须可被脚本解析：\n"
                "- 流程图：「图N：……流程图，包含步骤S101，……；S102，……；S103，……。」\n"
                "- 系统框图：「图N：……系统结构示意图，包含A模块、B模块、C模块。」\n"
                "步骤/模块必须全部有说明书支撑，不得新增。"
            ),
            call_class="REPAIR",
        )
    except Exception as exc:  # noqa: BLE001 —— REPAIR 失败即走降级，不炸步骤
        logger.warning("附图规格 REPAIR 调用失败 figure=%s：%s", figure_no, exc)
        return ""
    for line in repaired.drawings:
        if re.search(r"图\s*(\d+)", str(line)):
            return str(line).strip()
    return ""


async def regenerate_drawings(
    ctx: Ctx | None, case_id: str, content: dict[str, Any], *, repair: bool = True
) -> dict[str, Any]:
    """跑附图脚本，必要时 REPAIR 规格；返回 {content, degraded, gaps, assets}。

    ctx 为 None 时（API 手动重跑）跳过 REPAIR，失败图直接降级。
    """
    work: dict[str, Any] = json.loads(json.dumps(content, ensure_ascii=False))
    attempts: dict[int, int] = {}
    degraded: list[int] = []
    gaps: list[str] = []

    async def _try_repair(current: dict[str, Any], figure_no: int, reason: str) -> bool:
        """在 REPAIR 预算内重写该图规格；成功替换返回 True。"""
        attempts[figure_no] = attempts.get(figure_no, 0) + 1
        if ctx is None or not repair or attempts[figure_no] > MAX_DRAWING_REPAIR:
            return False
        new_spec = await _repair_drawing_spec(ctx, current, figure_no, reason)
        return bool(new_spec) and drawings_service.replace_spec(current, figure_no, new_spec)

    # 循环上限 = 每图两次 REPAIR + 首跑 + 收尾，杜绝死循环
    for _round in range(2 * MAX_DRAWING_REPAIR + 2):
        try:
            work = await drawings_service.generate(case_id, work)
        except drawings_service.DrawingGenerationError as exc:
            figure_no = exc.figure_no
            if figure_no is None:
                # 整体失败 = 附图脚本根本跑不起来，所有图都画不出。
                # 这时逐张去问图像模型没有信息量：第一张不成，后面大概率也不成，
                # 而每次最坏要等满 IMAGE_TIMEOUT，串起来能把流水线堵上二十多分钟。
                # 所以只拿第一张探路——通了再继续补，不通就整体降级。
                numbers = _figure_numbers(work)
                channel_ok = True
                for n in numbers:
                    if channel_ok and await drawings_service.try_ai_figure(
                        case_id, work, n, step_key="drawings"
                    ):
                        continue
                    if channel_ok and n == numbers[0]:
                        channel_ok = False
                        if ctx is not None:
                            await ctx.emit(
                                "log",
                                {"message": "图像模型未能补出首图，其余附图直接降级为提示词。"},
                            )
                    gaps.append(drawings_service.degrade_figure(work, n, "附图脚本执行失败"))
                    degraded.append(n)
                break
            if await _try_repair(work, figure_no, str(exc)):
                continue
            if await drawings_service.try_ai_figure(case_id, work, figure_no, step_key="drawings"):
                continue
            gaps.append(
                drawings_service.degrade_figure(work, figure_no, "规格中的步骤/模块不足以成图")
            )
            degraded.append(figure_no)
            if not (work.get("drawings") or []):
                break
            continue

        failed = drawings_service.failed_figures(work)
        if not failed:
            break
        progressed = False
        for position, figure_no in enumerate(failed, start=1):
            if ctx is not None:
                await ctx.progress(
                    "修复未通过校验的附图",
                    index=position, total=len(failed),
                    detail=f"图{figure_no}",
                    waiting_for="模型",
                )
            if await _try_repair(
                work, figure_no, "画布留白过大或图内混入图题，validation.passes=false"
            ):
                progressed = True
            elif await drawings_service.try_ai_figure(
                case_id, work, figure_no, step_key="drawings"
            ):
                progressed = True
            else:
                gaps.append(
                    drawings_service.degrade_figure(work, figure_no, "附图画布校验未通过")
                )
                degraded.append(figure_no)
        if not progressed:
            break

    existing = work.setdefault("gaps", [])
    for gap in gaps:
        if gap not in existing:
            existing.append(gap)
    return {
        "content": work,
        "degraded": sorted(set(degraded)),
        "gaps": gaps,
        "assets": work.get("drawing_assets") or [],
    }


async def drawings(ctx: Ctx) -> dict[str, Any]:
    """B6 前半：生成 SVG/PNG 附图并把资产元数据回写内容契约。"""
    if _is_flash(ctx):
        return {}
    content = dict(ctx.state.get("content") or {})
    if not content:
        raise ValueError("缺少专利内容契约，无法生成附图")

    planned = len(_figure_numbers(content))
    await ctx.progress(
        "生成说明书附图",
        detail=f"共 {planned} 幅待成图" if planned else "",
        waiting_for="附图脚本",
    )
    result = await regenerate_drawings(ctx, ctx.case_id, content)
    content = result["content"]
    assets = result["assets"]

    artifact_ids = await save_figure_artifacts(ctx.case_id, content, run_group=ctx.run_group)
    artifact = await save_content_artifact(
        ctx.case_id, content, run_group=ctx.run_group, summary="附图资产回写"
    )
    await ctx.emit(
        "log",
        {
            "message": (
                f"附图生成完成：成图 {len(assets)} 幅"
                + (f"，降级为提示词 {len(result['degraded'])} 幅" if result["degraded"] else "")
                + "。"
            )
        },
    )
    return {
        "content": content,
        "content_artifact_id": artifact.id,
        "gaps": content.get("gaps") or [],
        "figures": {"assets": assets, "degraded": result["degraded"], "artifacts": artifact_ids},
    }


async def save_figure_artifacts(
    case_id: str, content: dict[str, Any], *, run_group: str | None = None
) -> list[str]:
    """把工作目录里的 SVG/PNG 落成 figure_svg / figure_png 交付物。"""
    ids: list[str] = []
    for item in drawings_service.asset_files(case_id, content):
        for key, kind in (("svg_path", "figure_svg"), ("png_path", "figure_png")):
            path: Path | None = item.get(key)
            if path is None:
                continue
            data = await anyio.to_thread.run_sync(path.read_bytes)
            artifact = await artifacts_service.save_artifact(
                case_id,
                kind,
                data,
                path.suffix.lstrip("."),
                title=f"{content.get('invention_name') or '附图'}_图{item['figure_no']}",
                run_group=run_group,
                iteration_type="initial",
                summary=f"图{item['figure_no']}",
            )
            await _emit_artifact(case_id, artifact)
            ids.append(artifact.id)
    return ids


# ---------------------------------------------------------------------------
# 步骤 7：build —— DOCX / PDF 生成与交付
# ---------------------------------------------------------------------------


def _run_docx_sync(json_path: Path, docx_path: Path, *, require_drawings: bool) -> str:
    """子进程调 generate_patent_docx.py；返回 stdout 摘要，失败抛 BuildError。"""
    args = [str(json_path), "-o", str(docx_path)]
    if require_drawings:
        args.append("--require-drawings")
    try:
        proc = run_tool("generate_patent_docx.py", args, timeout=DOCX_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"DOCX 生成超时（{DOCX_TIMEOUT}s）") from exc
    except OSError as exc:
        raise BuildError(f"无法启动 DOCX 生成脚本：{exc}") from exc
    if proc.returncode != 0 or not docx_path.is_file():
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
        raise BuildError(f"DOCX 生成失败（退出码 {proc.returncode}）：{tail or '无输出'}")
    return (proc.stdout or "").strip()


def _run_pdf_fallback_sync(docx_path: Path, pdf_path: Path, json_path: Path) -> None:
    """Pillow 图片版 PDF 兜底（export_patent_pdf.py --content-json）。"""
    try:
        proc = run_tool(
            "export_patent_pdf.py",
            [str(docx_path), "-o", str(pdf_path), "--content-json", str(json_path)],
            timeout=DOCX_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise BuildError(f"PDF 兜底导出失败：{exc}") from exc
    if proc.returncode != 0 or not pdf_path.is_file():
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]
        raise BuildError(f"PDF 兜底导出失败（退出码 {proc.returncode}）：{tail or '无输出'}")


async def build_files(
    case_id: str, content: dict[str, Any], *, run_group: str | None = None
) -> dict[str, Any]:
    """PatentContent → DOCX（--require-drawings）→ PDF（Word/soffice → Pillow 兜底）。

    返回 {docx, pdf, pdf_engine, pdf_error, docx_warning, placeholders}。
    """
    work = drawings_service.work_dir(case_id)
    json_path = await anyio.to_thread.run_sync(
        lambda: drawings_service.write_content_sync(case_id, content)
    )
    docx_path = work / "patent_application.docx"
    pdf_path = work / "patent_application.pdf"
    title = content.get("invention_name") or None

    docx_warning: str | None = None
    has_assets = bool(content.get("drawing_assets"))
    try:
        await anyio.to_thread.run_sync(
            lambda: _run_docx_sync(json_path, docx_path, require_drawings=has_assets)
        )
    except BuildError as exc:
        if not has_assets:
            raise
        # 嵌图失败时退一步：不强制附图，保证仍有可交付的 Word 文件
        logger.warning("--require-drawings 生成失败，降级为不强制附图：%s", exc)
        docx_warning = f"附图未能嵌入 Word（{exc}）"
        await anyio.to_thread.run_sync(
            lambda: _run_docx_sync(json_path, docx_path, require_drawings=False)
        )

    docx_bytes = await anyio.to_thread.run_sync(docx_path.read_bytes)
    docx_artifact = await artifacts_service.save_artifact(
        case_id, "patent_docx", docx_bytes, "docx",
        title=title, run_group=run_group, iteration_type="initial",
    )
    await _emit_artifact(case_id, docx_artifact)

    pdf_artifact = None
    pdf_engine: str | None = None
    pdf_error: str | None = None
    try:
        pdf_engine = await export_pdf_service.docx_to_pdf(docx_path, pdf_path)
    except export_pdf_service.PdfExportError as exc:
        logger.info("docx→pdf 常规引擎不可用，改用图片版兜底：%s", exc)
        try:
            await anyio.to_thread.run_sync(
                lambda: _run_pdf_fallback_sync(docx_path, pdf_path, json_path)
            )
            pdf_engine = "pillow"
        except BuildError as fallback_exc:
            pdf_error = f"{exc}；兜底同样失败：{fallback_exc}"
    if pdf_error is None and pdf_path.is_file():
        pdf_bytes = await anyio.to_thread.run_sync(pdf_path.read_bytes)
        pdf_artifact = await artifacts_service.save_artifact(
            case_id, "patent_pdf", pdf_bytes, "pdf",
            title=title, run_group=run_group, iteration_type="export",
            source_artifact_id=docx_artifact.id,
        )
        await _emit_artifact(case_id, pdf_artifact)

    downloads = [
        {"kind": docx_artifact.kind, "artifact_id": docx_artifact.id, "filename": docx_artifact.filename}
    ]
    if pdf_artifact is not None:
        downloads.append(
            {"kind": pdf_artifact.kind, "artifact_id": pdf_artifact.id, "filename": pdf_artifact.filename}
        )
    await _emit_doc_version(case_id, docx_artifact, downloads=downloads)

    return {
        "docx": json.loads(docx_artifact.model_dump_json()),
        "pdf": json.loads(pdf_artifact.model_dump_json()) if pdf_artifact else None,
        "pdf_engine": pdf_engine,
        "pdf_error": pdf_error,
        "docx_warning": docx_warning,
        "placeholders": _content_placeholders(content),
    }


def _content_placeholders(content: dict[str, Any]) -> list[str]:
    """内容契约中剩余的【待补充：…】占位符（含 gaps 与正文扫描）。"""
    blob = json.dumps(content, ensure_ascii=False)
    found = _placeholders_in(blob)
    gaps = [g for g in (content.get("gaps") or []) if isinstance(g, str)]
    return list(dict.fromkeys([*found, *gaps]))


async def build(ctx: Ctx) -> dict[str, Any]:
    """B6 后半 + B8：生成 Word/PDF 并给出交付消息（只报文件/占位符/转换限制）。"""
    if _is_flash(ctx):
        return {}
    content = dict(ctx.state.get("content") or {})
    if not content:
        raise ValueError("缺少专利内容契约，无法生成文件")

    result = await build_files(ctx.case_id, content, run_group=ctx.run_group)

    lines = ["专利申请文件已生成："]
    lines.append(f"- Word 文件：{result['docx']['filename']}")
    if result["pdf"]:
        lines.append(f"- PDF 文件：{result['pdf']['filename']}")
    figures = ctx.state.get("figures") or {}
    assets = figures.get("assets") or content.get("drawing_assets") or []
    if assets:
        lines.append(f"- 说明书附图：SVG/PNG 各 {len(assets)} 幅（已嵌入 Word）")

    placeholders = result["placeholders"]
    if placeholders:
        lines.append(f"- 剩余占位符 {len(placeholders)} 处，已汇总在「材料缺口说明」，请补充后定稿：")
        lines.extend(f"  · {p}" for p in placeholders[:10])
    else:
        lines.append("- 无剩余【待补充】占位符。")

    limits: list[str] = []
    if result["pdf_engine"] == "pillow":
        limits.append("PDF 为图片版兜底（未检测到 Word/LibreOffice），Word 文件仍是可编辑主交付物")
    if result["pdf_error"]:
        limits.append(f"PDF 未能生成：{result['pdf_error']}")
    if result["docx_warning"]:
        limits.append(result["docx_warning"])
    degraded = figures.get("degraded") or []
    if degraded:
        limits.append(
            "图 " + "、".join(str(n) for n in degraded) + " 未能自动成图，已改为提供图像模型精修提示词"
        )
    if limits:
        lines.append("- 转换限制：" + "；".join(limits) + "。")

    await ctx.chat_delta("\n".join(lines))
    await ctx.chat_done()
    return {"build": result}


# ---------------------------------------------------------------------------
# 步骤表
# ---------------------------------------------------------------------------


def build_steps(case: dict[str, Any]) -> list[StepDef]:
    """模块 B 的七步流水线（flash 模式下后六步空跑）。"""
    return [
        StepDef(key="input_check", name_zh="输入评估", handler=input_check),
        StepDef(key="extraction", name_zh="深读提取", handler=extraction),
        StepDef(key="draft", name_zh="五部分起草", handler=draft),
        StepDef(key="rules_check", name_zh="规则校验与装配", handler=rules_check),
        StepDef(key="content_review", name_zh="内容确认", handler=content_review, gate="review"),
        StepDef(key="drawings", name_zh="附图生成", handler=drawings),
        StepDef(key="build", name_zh="文件生成与交付", handler=build),
    ]
