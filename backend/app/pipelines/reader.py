"""专利解读流水线（模块 C，prompt-porting-spec.md §2 模块 C / C0–C7）。

五步：

    acquire → claim_tree(review 门控) → claim_deltas → note(3 次 GEN) → lint_deliver

- **启动载荷**：`{pub_no?, file_id?}`。给了 `file_id` 就复用 `services/convert` 已经
  做过的 PDF→md；只给公开号则按 `references/patent_pdf_sources.yaml` 的优先级走
  `services/patent_fetch` 抓全文 PDF（Google Patents CDN > 国知局），**抓取失败不抛**，
  改为 `await_user(kind='form')` 给出可点链接请用户手工上传；
- **判型**：`tools/patent_type.py` 的种类码逻辑为主路径（公开号末位 A/B/C→发明、
  U/Y→实用新型、S→外观设计），判不出时才用 `reader/type_hooks.md` 兜底一次 STRUCT；
- **权要树**：STRUCT 抽取 → **服务端树 lint**（引用号 < 自身权号、无环、多项引用
  权利要求不得再被引用）→ 违规带错 REPAIR ≤2 → 仍有疑难则 `await_user(kind='claim_tree')`
  让用户消歧（默认按 alternative 处理，用户可跳过）；
- **白话增量**：每权 12–40 字，长度越界带错 REPAIR ≤2；
- **报告**：三次 GEN 写 11 节（结构节 / 叙事节 / 附录），doc 通道流式推送；
- **交付**：服务端确定性 lint（`说明书 NNNN` 必须命中段落号索引表、禁裸 `[0002]`、
  mermaid 可渲染）→ `reader/self_check.md` AUDIT 补丁（`services/patches` 应用）
  → 装配 `report.json` + 落 `reader_note_md` 交付物（可经 /artifacts/{id}/export 出 docx）。

纪律：**忠实红线**——本模块不联网检索，任何公开线索一律带「推断自公开文本，未联网核验」
标注且只能出现在第九、十节；`说明书 NNNN` 引用必须命中真实段落号，未命中就地标注。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.reader import (
    CALLOUT_BLOCK_TYPES,
    DELTA_MAX_CHARS,
    DELTA_MIN_CHARS,
    NO_CLUE_NOTE,
    ClaimDeltas,
    ClaimTree,
    Grounding,
    IpcCoordinate,
    PatentTypeGuess,
    ReaderMeta,
    ReaderReport,
    ReaderSelfCheck,
    ReaderStartPayload,
    ReportBlock,
    ReportSection,
)
from ..services import artifacts as artifacts_service
from ..services import assembler, assets_loader, patches, patent_fetch
from ..services import convert as convert_service
from ..services import disclosure_build as build_service
from ..services.sse import hub
from ..tools import patent_type as patent_type_tool
from .engine import Ctx, InteractionRequest, StepDef

logger = logging.getLogger(__name__)

# 文档通道 doc_id（前端右侧文档面板按 doc_id 归集流式正文）
DOC_ID = "reader_note"

# 各类 REPAIR 次数上限（§0.1：REPAIR ≤2 → 降级策略）
MAX_TREE_REPAIR = 2
MAX_DELTA_REPAIR = 2
MAX_SELF_CHECK_ROUNDS = 2

# 注入撰写调用的说明书段落文本上限（§3.6 上下文经济；超出按段落号顺序截断）
PARAGRAPH_BUDGET_CHARS = 20000

# 免责声明（report_template.md 第十一节，逐字）
DISCLAIMER = (
    "本解读仅供技术理解辅助，不构成法律意见；专利保护范围以官方法律文本为准。"
    "重大决策请咨询专利代理师/律师。"
)

# IPC 行业词表（离线；grounding 调用的行业提示来源）
IPC_HINTS_KEY = "references/ipc_application_hints.yaml"


# ---------------------------------------------------------------------------
# prompt 装配清单（§0.2：平台角色头 → 模块 Role → 阶段指令）
# ---------------------------------------------------------------------------

# 注：解读模式**不注入** common/desensitization.md ——脱敏规则要求「公司/产品不出现
# 具体名称」，而解读报告的著录项必须逐字保留申请人；本模块的输入是已公开的专利文本，
# 不存在需要脱敏的内部材料。术语贯穿规则照常注入。
CLAIM_TREE_PARTS = ["common/system_base", "reader/claim_tree"]
CLAIM_DELTAS_PARTS = ["common/system_base", "reader/claim_deltas"]
GROUNDING_PARTS = ["common/system_base", "reader/grounding", "reader/report_style"]
WRITER_PARTS = [
    "common/system_base",
    "reader/report_writer",
    "reader/report_template",
    "reader/report_style",
    "common/terminology_rules",
]
SELF_CHECK_PARTS = ["common/system_base", "reader/self_check", "reader/report_style"]
TYPE_HOOK_PARTS = ["common/system_base", "reader/type_hooks"]


# ---------------------------------------------------------------------------
# 11 节结构（report_template.md 逐字保留的节标题）
# ---------------------------------------------------------------------------

SECTION_SPECS: tuple[tuple[str, str, str], ...] = (
    ("s1", "一", "一、一句话"),
    ("s2", "二", "二、连贯叙事"),
    ("s3", "三", "三、权利要求树"),
    ("s4", "四", "四、独立权利要求精读"),
    ("s5", "五", "五、专利内术语表"),
    ("s6", "六", "六、特征—说明书—附图对照"),
    ("s7", "七", "七、和现有技术的差别"),
    ("s8", "八", "八、阅读建议"),
    ("s9", "九", "九、技术应用场景"),
    ("s10", "十", "十、附录：行业坐标与公开线索"),
    ("s11", "十一", "十一、免责声明"),
)

SECTION_IDS: tuple[str, ...] = tuple(s[0] for s in SECTION_SPECS)
_NUMERAL_TO_ID: dict[str, str] = {s[1]: s[0] for s in SECTION_SPECS}
_ID_TO_TITLE: dict[str, str] = {s[0]: s[2] for s in SECTION_SPECS}

# 三次撰写调用的分工（report_writer.md）
WRITER_CALLS: dict[str, tuple[str, ...]] = {
    "structure": ("s3", "s4", "s5", "s6"),
    "narrative": ("s1", "s2", "s7", "s8", "s9"),
    "appendix": ("s10", "s11"),
}


# ---------------------------------------------------------------------------
# 通用 LLM 调用封装（每次调用都经 assembler 装配 system prompt）
# ---------------------------------------------------------------------------


def _messages(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": str(system)}, {"role": "user", "content": user}]


async def _stream_gen(ctx: Ctx, *, tag: str, system: str, user: str) -> str:
    """GEN 流式调用：逐 token 推 doc 通道 SSE，返回完整文本。"""
    chunks: list[str] = []
    async for delta in ctx.llm.chat_stream(
        _messages(system, user),
        case_id=ctx.case_id,
        step_key=f"{ctx.step_key}.{tag}",
        **assembler.call_params("GEN"),
    ):
        chunks.append(delta)
        await ctx.doc_delta(DOC_ID, delta)
    await ctx.doc_done(DOC_ID)
    return "".join(chunks)


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


async def _emit_artifact(case_id: str, artifact) -> None:
    await hub.emit(case_id, "artifact_created", json.loads(artifact.model_dump_json()))


async def _emit_doc_version(case_id: str, artifact, downloads: list[dict[str, Any]] | None = None) -> None:
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


# ---------------------------------------------------------------------------
# 案件卡片与结构产物的持久化
# ---------------------------------------------------------------------------


def _case_card(ctx: Ctx) -> dict[str, Any]:
    """注入 prompt 的案件卡片。"""
    case = ctx.case or {}
    meta = ctx.state.get("reader_meta") or {}
    return {
        "case_id": ctx.case_id,
        "module": "reader（专利解读）",
        "title": meta.get("title") or case.get("title") or "",
        "pub_no": meta.get("pub_no") or "",
        "patent_type": meta.get("type") or case.get("patent_type") or "",
        "patent_type_label": meta.get("type_label") or "",
        "ipc": meta.get("ipc") or [],
        "perspective": meta.get("perspective") or "研发",
        "evidence_scope": meta.get("evidence_scope") or "full_text",
    }


def _structure_dir(case_id: str) -> Path:
    return get_config().uploads_dir / case_id / "reader"


def _save_structure_sync(case_id: str, structure: patent_fetch.PatentStructure) -> str:
    """把结构切分产物落盘（全文不进 cases.state_json，只在 state 里存路径）。"""
    out_dir = _structure_dir(case_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "structure.json"
    path.write_text(
        json.dumps(structure.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(path)


def _load_structure_sync(path: str) -> patent_fetch.PatentStructure:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"专利结构切分产物已不在磁盘：{path}（请重跑「获取与文本化」步骤）")
    return patent_fetch.PatentStructure.from_dict(json.loads(p.read_text(encoding="utf-8")))


async def _load_structure(ctx: Ctx) -> patent_fetch.PatentStructure:
    path = str(ctx.state.get("reader_structure_path") or "")
    if not path:
        raise ValueError("缺少专利结构切分产物，请先跑「获取与文本化」步骤")
    return await db.arun(_load_structure_sync, path)


def _store_bytes_sync(
    case_id: str, filename: str, payload: bytes, mime: str
) -> tuple[str, str | None]:
    """把抓来的（或用户粘贴的）文件落 uploads/ 并登记 files 行；返回 (file_id, convert_error)。"""
    cfg = get_config()
    case_dir = cfg.uploads_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    stored_path = convert_service.allocate_upload_path(case_dir, filename)
    stored_path.write_bytes(payload)

    result = convert_service.convert_upload(case_id, stored_path)
    md_path = str(result.md_path) if result.md_path is not None else None
    file_id = str(ULID())
    db.execute(
        """
        INSERT INTO files(id, case_id, kind, orig_name, mime, size, stored_path, md_path,
                          meta_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            file_id, case_id, "upload", stored_path.name, mime, len(payload),
            str(stored_path), md_path, json.dumps(result.meta, ensure_ascii=False), db.now_str(),
        ),
    )
    return file_id, result.meta.get("convert_error")


def _read_file_md_sync(case_id: str, file_id: str) -> tuple[dict[str, Any], str]:
    """取该案件下某个文件的转换文本（PDF→md 由 services/convert 完成，此处只读）。"""
    row = db.query_one("SELECT * FROM files WHERE id=? AND case_id=?", (file_id, case_id))
    if row is None:
        raise ValueError(f"文件不存在或不属于本案件：{file_id}")
    record = dict(row)
    md_path = record.get("md_path")
    if not md_path:
        meta = json.loads(record.get("meta_json") or "{}")
        reason = meta.get("convert_error") or "该文件没有可用的文本"
        raise ValueError(f"《{record.get('orig_name')}》无法进入解读流水线：{reason}")
    path = Path(md_path)
    if not path.is_file():
        raise ValueError(f"转换文本已不在磁盘：{md_path}")
    return record, path.read_text(encoding="utf-8", errors="replace")


async def _set_case_meta(ctx: Ctx, title: str, patent_type: str | None) -> None:
    """回填案件标题与专利类型并广播 case_title。"""
    clean = (title or "").strip()[:120]
    sets, params = ["updated_at=?"], [db.now_str()]
    if clean:
        sets.append("title=?")
        params.append(clean)
    if patent_type in ("invention", "utility_model", "design"):
        sets.append("patent_type=?")
        params.append(patent_type)
    if len(sets) == 1:
        return
    await db.aexecute(f"UPDATE cases SET {', '.join(sets)} WHERE id=?", (*params, ctx.case_id))
    await ctx.reload_case()
    if clean:
        await ctx.emit("case_title", {"title": clean})


# ---------------------------------------------------------------------------
# 步骤 1：acquire —— 取证 / 文本化 / 结构切分 / 判型
# ---------------------------------------------------------------------------

_UPLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_id": {"type": "string", "title": "已上传专利 PDF 的文件 ID"},
        "text": {"type": "string", "title": "或直接粘贴专利全文文本"},
        "pub_no": {"type": "string", "title": "公开号（可修正）"},
    },
}


async def _resolve_source(ctx: Ctx, payload: ReaderStartPayload) -> dict[str, Any]:
    """确定专利文本来源：file_id 直用 / pub_no 抓取 / 失败走门控请用户上传。"""
    pub_no = patent_fetch.normalize_pub_no(payload.pub_no)
    file_id = str(payload.file_id or "").strip()
    fetch_report: dict[str, Any] | None = None

    if not file_id and pub_no:
        await ctx.emit("log", {"message": f"正在按取证源优先级获取 {pub_no} 的全文 PDF…"})
        result = await patent_fetch.fetch_patent_pdf(pub_no)
        fetch_report = result.to_dict()
        if result.ok and result.pdf:
            file_id, convert_error = await db.arun(
                _store_bytes_sync, ctx.case_id, f"{pub_no}.pdf", result.pdf, "application/pdf"
            )
            if convert_error:
                await ctx.emit("log", {"message": f"PDF 文本抽取失败：{convert_error}"})
                file_id = ""
            else:
                await ctx.emit(
                    "log",
                    {"message": f"已获取全文 PDF（来源：{result.source_id}，{len(result.pdf)} 字节）"},
                )
        else:
            await ctx.emit("log", {"message": f"自动获取失败：{result.error}"})

    if file_id:
        return {"file_id": file_id, "pub_no": pub_no, "fetch": fetch_report, "manual": False}

    # 取证失败（或压根没给来源）→ 门控请用户手工上传，并给出可点链接
    links = patent_fetch.manual_links(pub_no)
    reason = (fetch_report or {}).get("error") or "未提供公开号或文件"
    prompt = (
        f"未能自动获取专利全文 PDF（{reason}）。"
        "请先在本案件下上传专利 PDF（文件接口 POST /cases/{id}/files），再把返回的文件 ID 填入下方；"
        "也可以直接粘贴专利全文文本。"
    )
    if links:
        prompt += "\n可从以下页面自行下载 PDF：\n" + "\n".join(
            f"- {item['label']}：{item['url']}" for item in links
        )
    answer = await ctx.await_user(
        InteractionRequest(
            kind="form",
            schema=_UPLOAD_SCHEMA,
            prompt=prompt,
            default={"pub_no": pub_no, "file_id": "", "text": "", "links": links},
        )
    )
    data = answer if isinstance(answer, dict) else {}
    pub_no = patent_fetch.normalize_pub_no(data.get("pub_no") or pub_no)
    file_id = str(data.get("file_id") or "").strip()
    pasted = str(data.get("text") or "").strip()
    if not file_id and pasted:
        file_id, _ = await db.arun(
            _store_bytes_sync,
            ctx.case_id,
            f"{pub_no or '专利全文'}.md",
            pasted.encode("utf-8"),
            "text/markdown",
        )
    if not file_id:
        raise ValueError("未获得可解读的专利文本：请上传专利 PDF 或粘贴全文后重试")
    return {"file_id": file_id, "pub_no": pub_no, "fetch": fetch_report, "manual": True}


async def _resolve_type(
    ctx: Ctx, pub_no: str, declared: str, structure: patent_fetch.PatentStructure
) -> dict[str, Any]:
    """判型：种类码主路径（tools/patent_type.py）；判不出才用 type_hooks 兜底一次。"""
    info = patent_type_tool.resolve_reader_patent_type(
        pub=pub_no or structure.pub_no,
        user_declared=declared or None,
        biblio_text=structure.biblio_text(),
    )
    if info.get("patent_type"):
        return info

    await ctx.emit("log", {"message": "公开号种类码与扉页关键词均未给出专利类型，转由模型兜底判定。"})
    system = assembler.assemble(
        TYPE_HOOK_PARTS, runtime_ctx={"case_card": _case_card(ctx)}
    )
    user = (
        "以下为本案专利公开文本的扉页与权利要求书片段，请按判定顺序输出唯一一个 JSON 对象。\n\n"
        f"===== 扉页 =====\n{structure.biblio_text()[:2000]}\n\n"
        f"===== 权利要求书（节选）=====\n{structure.claims_markdown()[:2000]}"
    )
    guess: PatentTypeGuess = await _call_struct(
        ctx, PatentTypeGuess, tag="type_hook", system=system, user=user
    )
    if guess.patent_type != "unknown":
        info.update(
            {
                "patent_type": guess.patent_type,
                "label_zh": patent_type_tool.TYPE_LABEL_ZH.get(guess.patent_type, ""),
                "source": f"llm_{guess.basis or 'text_inference'}",
                "confidence": guess.confidence,
                "evidence": guess.evidence,
            }
        )
    return info


async def acquire(ctx: Ctx) -> dict[str, Any]:
    """C0+C1：取证 → PDF→md（复用 services/convert）→ 结构切分 → 判型。"""
    payload = ReaderStartPayload.model_validate(ctx.start_payload or {})
    source = await _resolve_source(ctx, payload)

    record, md_text = await db.arun(_read_file_md_sync, ctx.case_id, source["file_id"])
    structure = patent_fetch.parse_patent_md(md_text)
    if not structure.claims and not structure.paragraphs:
        raise ValueError(
            f"《{record.get('orig_name')}》未解析出权利要求书或带段落号的说明书，"
            "可能是扫描件或非专利文档，请更换文件后重试"
        )

    pub_no = source["pub_no"] or structure.pub_no
    type_info = await _resolve_type(
        ctx, pub_no, payload.patent_type or str(ctx.case.get("patent_type") or ""), structure
    )
    patent_type = type_info.get("patent_type") or ""
    structure_path = await db.arun(_save_structure_sync, ctx.case_id, structure)

    meta = ReaderMeta(
        pub_no=pub_no,
        type=patent_type,
        type_label=type_info.get("label_zh") or "",
        ipc=structure.ipc,
        title=structure.title,
        applicants=structure.applicants,
        domain=(structure.ipc[0].split(" ")[0] if structure.ipc else ""),
        pub_date=structure.pub_date,
        read_date=db.now_str()[:10],
        perspective=payload.perspective,
        evidence_scope=structure.evidence_scope(),
    )
    await _set_case_meta(ctx, structure.title or (f"专利解读 {pub_no}" if pub_no else ""), patent_type)
    await ctx.emit(
        "log",
        {
            "message": (
                f"文本化完成：权利要求 {len(structure.claims)} 项、说明书段落 "
                f"{len(structure.paragraphs)} 段、附图 {len(structure.figures)} 幅"
                f"（类型：{meta.type_label or '未定'}）。"
            )
        },
    )
    return {
        "reader_source": {
            "file_id": source["file_id"],
            "orig_name": record.get("orig_name"),
            "manual_upload": source["manual"],
            "fetch": source["fetch"],
        },
        "reader_meta": json.loads(meta.model_dump_json()),
        "reader_type": type_info,
        "reader_structure_path": structure_path,
        "reader_structure": structure.summary(),
    }


# ---------------------------------------------------------------------------
# 步骤 2：claim_tree —— STRUCT 抽取 + 服务端树 lint + 消歧门控
# ---------------------------------------------------------------------------


def lint_claim_tree(tree: ClaimTree) -> list[dict[str, Any]]:
    """权要树的确定性树形合法性校验（claim_tree.md「由平台代码执行」的那一束）。

    规则：
    - 引用号必须**小于**自身权号，且被引用的权利要求必须存在；
    - 无循环引用（用户在门控里编辑过树之后仍要复核）；
    - **多项引用的权利要求不得再被引用**（同时引用多项者互引即为 error）；
    - 独立权不得有引用、从属权必须有引用；编号连续且不重复；特征非空。
    """
    findings: list[dict[str, Any]] = []

    def add(claim: int, rule: str, severity: str, message: str) -> None:
        findings.append({"claim": claim, "rule": rule, "severity": severity, "message": message})

    claims = list(tree.claims)
    if not claims:
        add(0, "empty", "error", "未抽取到任何权利要求")
        return findings

    numbers = [c.no for c in claims]
    seen: set[int] = set()
    for no in numbers:
        if no in seen:
            add(no, "duplicate_no", "error", f"权利要求 {no} 重复出现")
        seen.add(no)
    expected = list(range(1, max(numbers) + 1))
    missing = [n for n in expected if n not in seen]
    if missing:
        add(
            missing[0],
            "numbering",
            "warning",
            "权利要求编号不连续，缺少 " + "、".join(str(n) for n in missing),
        )

    multi_ref_nos = {c.no for c in claims if c.is_multi_ref}
    for claim in claims:
        for ref in claim.refs:
            if ref >= claim.no:
                add(
                    claim.no,
                    "ref_order",
                    "error",
                    f"权利要求 {claim.no} 引用了权利要求 {ref}：引用号必须小于自身权号",
                )
            elif ref not in seen:
                add(
                    claim.no,
                    "ref_missing",
                    "error",
                    f"权利要求 {claim.no} 引用了不存在的权利要求 {ref}",
                )
            if ref in multi_ref_nos and ref != claim.no:
                severity = "error" if claim.is_multi_ref else "warning"
                add(
                    claim.no,
                    "multi_ref_chain",
                    severity,
                    f"权利要求 {ref} 本身是多项引用，不应再被权利要求 {claim.no} 引用",
                )
        if claim.type == "independent" and claim.refs:
            add(claim.no, "independent_ref", "error", f"权利要求 {claim.no} 标为独立权却仍有引用")
        if claim.type == "dependent" and not claim.refs:
            add(claim.no, "dependent_no_ref", "error", f"权利要求 {claim.no} 标为从属权却没有引用")
        if not claim.features:
            add(claim.no, "no_feature", "warning", f"权利要求 {claim.no} 没有拆出任何技术特征")

    for cycle in _find_cycles(claims):
        add(
            cycle[0],
            "cycle",
            "error",
            "存在循环引用：" + " → ".join(str(n) for n in cycle),
        )
    return findings


def _find_cycles(claims: list[Any]) -> list[list[int]]:
    """DFS 找出引用图里的环（返回环上的权号序列，最多 3 条）。"""
    graph = {c.no: [r for r in c.refs] for c in claims}
    cycles: list[list[int]] = []
    state: dict[int, int] = {}          # 0=未访问 1=在栈上 2=已完成
    stack: list[int] = []

    def visit(node: int) -> None:
        if len(cycles) >= 3 or state.get(node, 0) == 2:
            return
        if state.get(node, 0) == 1:
            idx = stack.index(node)
            cycles.append([*stack[idx:], node])
            return
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt in graph:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for no in sorted(graph):
        visit(no)
    return cycles


def _errors(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in findings if f.get("severity") == "error"]


def _findings_text(findings: list[dict[str, Any]]) -> str:
    return "\n".join(f"- 权{f['claim']}｜{f['rule']}｜{f['severity']}：{f['message']}" for f in findings)


_TREE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "权利要求树消歧",
    "properties": {
        "claims": {
            "type": "array",
            "title": "权利要求树（可编辑）",
            "items": {
                "type": "object",
                "properties": {
                    "no": {"type": "integer", "title": "权号"},
                    "type": {
                        "type": "string",
                        "title": "类型",
                        "enum": ["independent", "dependent"],
                    },
                    "refs": {
                        "type": "array",
                        "title": "引用权号",
                        "items": {"type": "integer"},
                    },
                    "ref_mode": {
                        "type": "string",
                        "title": "引用方式",
                        "enum": ["none", "single", "alternative", "range", "multiple"],
                    },
                    "preamble": {"type": "string", "title": "前序部分"},
                },
            },
        },
        "skip": {"type": "boolean", "title": "跳过消歧（按默认处理）"},
    },
}


def _merge_user_tree(tree: ClaimTree, answer: Any) -> tuple[ClaimTree, bool]:
    """把用户在门控里编辑的树并回；跳过（或空提交）时按 alternative 默认处理。

    返回 (新树, 用户是否真的做了消歧)。
    """
    data = answer if isinstance(answer, dict) else {}
    edited = data.get("claims")
    skipped = bool(data.get("skip")) or not isinstance(edited, list) or not edited
    if not skipped:
        patch_by_no = {}
        for item in edited:
            if isinstance(item, dict) and item.get("no") is not None:
                try:
                    patch_by_no[int(item["no"])] = item
                except (TypeError, ValueError):
                    continue
        raw = json.loads(tree.model_dump_json())
        for claim in raw.get("claims", []):
            patch = patch_by_no.get(int(claim["no"]))
            if not patch:
                continue
            for key in ("type", "refs", "ref_mode", "preamble"):
                if patch.get(key) not in (None, ""):
                    claim[key] = patch[key]
        return ClaimTree.model_validate(raw), True

    # 默认策略：把仍存疑的多引用按 alternative 处理（claim_tree.md 消歧规则的保守解）
    raw = json.loads(tree.model_dump_json())
    ambiguous = {a.no for a in tree.ambiguities if a.kind in ("multi_ref", "other", "")}
    for claim in raw.get("claims", []):
        if int(claim["no"]) in ambiguous and len(claim.get("refs") or []) > 1:
            claim["ref_mode"] = "alternative"
    return ClaimTree.model_validate(raw), False


async def claim_tree(ctx: Ctx) -> dict[str, Any]:
    """C2 权要树：STRUCT 抽取 → 树 lint（带错 REPAIR ≤2）→ 疑难时门控消歧。"""
    structure = await _load_structure(ctx)
    system = assembler.assemble(
        CLAIM_TREE_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {"claim_count_hint": len(structure.claims)},
        },
    )
    user = (
        "以下是本案权利要求书全文（服务端已按项切分并保留原文标点）。"
        "请逐项解析为权要树，只输出一个 JSON 对象。\n\n"
        f"===== 权利要求书 =====\n{structure.claims_markdown()}"
    )
    tree: ClaimTree = await _call_struct(ctx, ClaimTree, tag="tree", system=system, user=user)
    findings = lint_claim_tree(tree)

    attempts = 0
    while _errors(findings) and attempts < MAX_TREE_REPAIR:
        attempts += 1
        await ctx.emit(
            "log",
            {"message": f"权要树校验发现 {len(_errors(findings))} 处违规，正在第 {attempts} 次修复。"},
        )
        tree = await _call_struct(
            ctx,
            ClaimTree,
            tag=f"tree.repair{attempts}",
            system=system,
            user=(
                "以下权要树未通过平台的树形合法性校验，请逐条修正后重新输出完整 JSON。\n\n"
                f"违规清单：\n{_findings_text(_errors(findings))}\n\n"
                f"当前权要树：\n{tree.model_dump_json(indent=2)}\n\n"
                f"===== 权利要求书原文 =====\n{structure.claims_markdown()}"
            ),
            call_class="REPAIR",
        )
        findings = lint_claim_tree(tree)

    resolved = False
    if tree.ambiguities or _errors(findings):
        issues = [
            {"no": a.no, "kind": a.kind, "issue": a.issue, "candidates": a.candidates,
             "chosen": a.chosen, "reason": a.reason}
            for a in tree.ambiguities
        ]
        lines = [f"权{i['no']}：{i['issue']}" for i in issues if i.get("issue")]
        lines += [f"权{f['claim']}：{f['message']}" for f in _errors(findings)]
        answer = await ctx.await_user(
            InteractionRequest(
                kind="claim_tree",
                schema=_TREE_SCHEMA,
                prompt=(
                    "权利要求树中有以下待确认之处，请核对后提交（可直接跳过，"
                    "平台将把仍存疑的多引用按「或」的择一引用处理）：\n"
                    + "\n".join(f"- {line}" for line in lines)
                ),
                default={
                    "claims": json.loads(tree.model_dump_json())["claims"],
                    "ambiguities": issues,
                    "lint": findings,
                    "skip": False,
                },
            )
        )
        tree, resolved = _merge_user_tree(tree, answer)
        findings = lint_claim_tree(tree)

    await ctx.emit("log", {"message": f"权要树已确认：{tree.summary_line()}"})
    return {
        "claim_tree": json.loads(tree.model_dump_json()),
        "claim_tree_lint": findings,
        "claim_tree_repairs": attempts,
        "claim_tree_resolved": resolved,
    }


def _tree_from_state(ctx: Ctx) -> ClaimTree:
    data = ctx.state.get("claim_tree")
    if not isinstance(data, dict) or not data.get("claims"):
        raise ValueError("缺少已确认的权利要求树，请先跑「权利要求树」步骤")
    return ClaimTree.model_validate(data)


# ---------------------------------------------------------------------------
# 步骤 3：claim_deltas —— 每权 12–40 字白话增量（长度越界 REPAIR ≤2）
# ---------------------------------------------------------------------------


def lint_claim_deltas(tree: ClaimTree, deltas: ClaimDeltas) -> list[dict[str, Any]]:
    """白话增量的确定性校验：覆盖全部权项 + 12–40 字 + 无「如权利要求…所述」套话。"""
    findings: list[dict[str, Any]] = []
    by_claim = {d.claim: d for d in deltas.deltas}
    for claim in tree.claims:
        item = by_claim.get(claim.no)
        if item is None or not item.delta.strip():
            findings.append(
                {
                    "claim": claim.no,
                    "rule": "missing",
                    "severity": "error",
                    "message": f"权利要求 {claim.no} 缺少白话增量",
                }
            )
            continue
        length = item.length
        if length < DELTA_MIN_CHARS:
            findings.append(
                {
                    "claim": claim.no,
                    "rule": "too_short",
                    "severity": "error",
                    "message": (
                        f"权利要求 {claim.no} 的白话增量只有 {length} 字，"
                        f"少于 {DELTA_MIN_CHARS} 字：「{item.delta}」"
                    ),
                }
            )
        elif length > DELTA_MAX_CHARS:
            findings.append(
                {
                    "claim": claim.no,
                    "rule": "too_long",
                    "severity": "error",
                    "message": (
                        f"权利要求 {claim.no} 的白话增量有 {length} 字，"
                        f"超过 {DELTA_MAX_CHARS} 字：「{item.delta}」"
                    ),
                }
            )
        for cliche in ("如权利要求", "根据权利要求", "其特征在于"):
            if cliche in item.delta:
                findings.append(
                    {
                        "claim": claim.no,
                        "rule": "cliche",
                        "severity": "error",
                        "message": f"权利要求 {claim.no} 的白话增量出现套话「{cliche}」，请改写为增量本身",
                    }
                )
                break
    extra = [d.claim for d in deltas.deltas if d.claim not in {c.no for c in tree.claims}]
    for no in extra:
        findings.append(
            {
                "claim": no,
                "rule": "unknown_claim",
                "severity": "warning",
                "message": f"白话增量里出现了权要树中不存在的权利要求 {no}",
            }
        )
    return findings


async def claim_deltas(ctx: Ctx) -> dict[str, Any]:
    """C3 白话增量：STRUCT → 服务端长度校验 → 越界带错 REPAIR ≤2。"""
    structure = await _load_structure(ctx)
    tree = _tree_from_state(ctx)
    system = assembler.assemble(
        CLAIM_DELTAS_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {"claim_tree": json.loads(tree.model_dump_json())},
        },
    )
    user = (
        f"请为下列每一项权利要求写一句 {DELTA_MIN_CHARS}～{DELTA_MAX_CHARS} 字的白话增量，"
        "只输出一个 JSON 对象。\n\n"
        f"===== 权利要求书原文 =====\n{structure.claims_markdown()}"
    )
    deltas: ClaimDeltas = await _call_struct(
        ctx, ClaimDeltas, tag="deltas", system=system, user=user
    )
    findings = lint_claim_deltas(tree, deltas)

    attempts = 0
    while _errors(findings) and attempts < MAX_DELTA_REPAIR:
        attempts += 1
        await ctx.emit(
            "log",
            {
                "message": (
                    f"白话增量有 {len(_errors(findings))} 条不符合 "
                    f"{DELTA_MIN_CHARS}–{DELTA_MAX_CHARS} 字要求，正在第 {attempts} 次修复。"
                )
            },
        )
        deltas = await _call_struct(
            ctx,
            ClaimDeltas,
            tag=f"deltas.repair{attempts}",
            system=system,
            user=(
                "以下白话增量未通过平台校验，请按问题清单重写（**每项都要重新给出**，"
                "长度务必落在 "
                f"{DELTA_MIN_CHARS}～{DELTA_MAX_CHARS} 字之间），只输出一个 JSON 对象。\n\n"
                f"问题清单：\n{_findings_text(_errors(findings))}\n\n"
                f"当前产物：\n{deltas.model_dump_json(indent=2)}\n\n"
                f"===== 权利要求书原文 =====\n{structure.claims_markdown()}"
            ),
            call_class="REPAIR",
        )
        findings = lint_claim_deltas(tree, deltas)

    if _errors(findings):
        await ctx.emit(
            "log",
            {"message": f"仍有 {len(_errors(findings))} 条白话增量未达标，已如实记录并继续成文。"},
        )
    return {
        "claim_deltas": json.loads(deltas.model_dump_json()),
        "claim_deltas_lint": findings,
        "claim_deltas_repairs": attempts,
    }


def _deltas_from_state(ctx: Ctx) -> ClaimDeltas:
    data = ctx.state.get("claim_deltas")
    if isinstance(data, dict):
        return ClaimDeltas.model_validate(data)
    return ClaimDeltas()


# ---------------------------------------------------------------------------
# 步骤 4：note —— 落地线索 + 三次 GEN 撰写 11 节
# ---------------------------------------------------------------------------


def load_ipc_hints() -> list[dict[str, Any]]:
    """离线 IPC 行业词表（ipc_application_hints.yaml）。"""
    try:
        data = yaml.safe_load(assets_loader.get_raw(IPC_HINTS_KEY))
    except (KeyError, yaml.YAMLError) as exc:  # pragma: no cover —— 资产缺失属部署问题
        logger.warning("IPC 行业词表不可用：%s", exc)
        return []
    hints = (data or {}).get("hints") if isinstance(data, dict) else None
    return [h for h in (hints or []) if isinstance(h, dict)]


def match_ipc_hints(ipc_codes: list[str], text: str, limit: int = 3) -> list[dict[str, Any]]:
    """按 IPC 前缀（长前缀优先）匹配行业提示；未命中退回关键词，再退回 DEFAULT。"""
    hints = load_ipc_hints()
    if not hints:
        return []
    codes = [re.sub(r"\s+", "", c).upper() for c in (ipc_codes or [])]
    matched: list[dict[str, Any]] = []
    for hint in hints:
        prefix = str(hint.get("ipc_prefix") or "").upper()
        if not prefix or prefix == "DEFAULT":
            continue
        if any(code.startswith(prefix) for code in codes):
            matched.append(hint)
    if matched:
        matched.sort(key=lambda h: len(str(h.get("ipc_prefix") or "")), reverse=True)
        return matched[:limit]

    body = text or ""
    for hint in hints:
        if str(hint.get("ipc_prefix") or "").upper() == "DEFAULT":
            continue
        if any(str(kw) and str(kw) in body for kw in (hint.get("keywords") or [])):
            matched.append(hint)
    if matched:
        return matched[:limit]

    default = next((h for h in hints if str(h.get("ipc_prefix") or "").upper() == "DEFAULT"), None)
    return [default] if default else []


def _paragraph_context(structure: patent_fetch.PatentStructure, budget: int) -> str:
    """段落号索引表文本（超预算时按段落号顺序截断并注明）。"""
    text = structure.paragraph_markdown()
    if len(text) <= budget:
        return text
    return (
        text[:budget].rstrip()
        + "\n（注：说明书段落超出本次调用的上下文预算，已按段落号顺序保留上述部分；"
        "引用段落号时只能引用上面出现过的编号。）"
    )


def _writer_context(
    ctx: Ctx,
    structure: patent_fetch.PatentStructure,
    tree: ClaimTree,
    deltas: ClaimDeltas,
) -> dict[str, Any]:
    """三次撰写调用共享的运行时上下文。"""
    return {
        "case_card": _case_card(ctx),
        "extra": {
            "report_meta": ctx.state.get("reader_meta") or {},
            "claim_tree": json.loads(tree.model_dump_json()),
            "claim_deltas": deltas.by_claim(),
            "claim_tree_summary": tree.summary_line(),
            "figures": structure.figures,
            "abstract": structure.abstract,
        },
    }


async def _grounding(
    ctx: Ctx, structure: patent_fetch.PatentStructure, tree: ClaimTree, deltas: ClaimDeltas
) -> tuple[Grounding, list[dict[str, Any]]]:
    """C4 落地线索（离线）：服务端匹配 IPC 行业提示后交 STRUCT 产 anchor_fits。"""
    hint_text = " ".join(
        [structure.title, structure.abstract, structure.claims_markdown()[:2000]]
    )
    hints = match_ipc_hints(structure.ipc, hint_text)
    system = assembler.assemble(
        GROUNDING_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "ipc_application_hints": hints,
                "claim_tree": json.loads(tree.model_dump_json()),
                "claim_deltas": deltas.by_claim(),
            },
        },
    )
    user = (
        "以下是本案说明书的分段文本（四位段落号索引）。请按 anchor_fits 硬性规则输出"
        "行业坐标与应用场景线索，只输出一个 JSON 对象。\n\n"
        f"===== 说明书分段 =====\n{_paragraph_context(structure, PARAGRAPH_BUDGET_CHARS)}"
    )
    grounding: Grounding = await _call_struct(
        ctx, Grounding, tag="grounding", system=system, user=user
    )

    # 服务端校验：anchor_fits 的段落号必须真实存在（grounding.md「编号不存在即判违规」）
    findings: list[dict[str, Any]] = []
    kept = []
    for clue in grounding.clues:
        bad = [
            fit.paragraph
            for fit in clue.anchor_fits
            if not _paragraph_exists(fit.paragraph, structure.paragraphs)
        ]
        if bad or not clue.anchor_fits:
            findings.append(
                {
                    "claim": 0,
                    "rule": "grounding_anchor",
                    "severity": "warning",
                    "message": (
                        f"线索「{clue.scene}」的锚定段落 {('、'.join(bad) or '缺失')} "
                        "未命中说明书段落号索引，已丢弃该线索"
                    ),
                }
            )
            continue
        kept.append(clue)
    grounding.clues = kept
    if not grounding.clues and not grounding.no_clue_note:
        grounding.no_clue_note = NO_CLUE_NOTE
    if not grounding.ipc_coordinates and hints:
        # 模型漏写行业坐标时用服务端的词表命中结果兜底（basis 如实标 hints_match）
        grounding.ipc_coordinates = [
            IpcCoordinate(
                ipc_prefix=str(h.get("ipc_prefix") or ""),
                industry=str(h.get("industry") or ""),
                basis="hints_match",
            )
            for h in hints
        ]
    return grounding, findings


def _paragraph_exists(ref: str, paragraphs: dict[str, str]) -> bool:
    """段落号（含 `0056-0058` 区间写法）是否命中索引表。"""
    raw = re.sub(r"\s+", "", str(ref or ""))
    if not raw:
        return False
    parts = re.split(r"[–—\-~至]", raw)
    return all(part in paragraphs for part in parts if part)


async def note(ctx: Ctx) -> dict[str, Any]:
    """C4+C5：落地线索 STRUCT → 三次 GEN 写 11 节（doc 通道流式）。"""
    structure = await _load_structure(ctx)
    tree = _tree_from_state(ctx)
    deltas = _deltas_from_state(ctx)

    grounding, grounding_findings = await _grounding(ctx, structure, tree, deltas)
    base_ctx = _writer_context(ctx, structure, tree, deltas)
    paragraphs = _paragraph_context(structure, PARAGRAPH_BUDGET_CHARS)

    # ① 结构节（三、四、五、六）
    structure_system = assembler.assemble(WRITER_PARTS, runtime_ctx=base_ctx)
    structure_md = await _stream_gen(
        ctx,
        tag="structure",
        system=structure_system,
        user=(
            "本次是**调用①：结构节**，只写第三、四、五、六节，从 `## 三、权利要求树` 开始。\n"
            "- 第三节只写导语「" + tree.summary_line() + "」，树形表由平台装配，不要自己画表；\n"
            "- 第四节每项独立权利要求一个 `> [!patent-claim]` 卡片 + 原文引用块 + "
            "「特征 | 大白话 | 说明书依据」三列表；\n"
            "- 第五、六节按模板给表；「说明书依据」「说明书位置」列一律写 `说明书 0006` / "
            "`说明书 0058–0061` 格式，段落号必须来自下方索引，禁止写裸 `[0006]`。\n\n"
            f"===== 权利要求书原文 =====\n{structure.claims_markdown()}\n\n"
            f"===== 附图清单 =====\n{structure.figures_markdown()}\n\n"
            f"===== 说明书分段（四位段落号索引）=====\n{paragraphs}"
        ),
    )

    # ② 叙事节（一、二、七、八、九）
    narrative_ctx = {
        **base_ctx,
        "extra": {
            **base_ctx["extra"],
            "grounding": json.loads(grounding.model_dump_json()),
            "structure_digest": build_service.text_digest(structure_md, 600),
        },
    }
    narrative_system = assembler.assemble(WRITER_PARTS, runtime_ctx=narrative_ctx)
    narrative_md = await _stream_gen(
        ctx,
        tag="narrative",
        system=narrative_system,
        user=(
            "本次是**调用②：叙事节**，只写第一、二、七、八、九节，从 `## 一、一句话` 开始。\n"
            "- 第九节用 `> [!grounding]` 卡片包住「场景/模块 | 大白话 | 专利内依据」表，"
            "依据列只能写 `说明书 0056` 或实施例编号，**本节不得出现任何 URL 或联网推测**；\n"
            "- 一至八节只写专利文本内读得出的结论，读不出就如实说明。\n\n"
            f"===== 摘要 =====\n{structure.abstract or '（扉页未解析出摘要）'}\n\n"
            f"===== 说明书分段（四位段落号索引）=====\n{paragraphs}"
        ),
    )

    # ③ 附录（十、十一）
    appendix_ctx = {
        **base_ctx,
        "extra": {
            **base_ctx["extra"],
            "grounding": json.loads(grounding.model_dump_json()),
            "narrative_digest": build_service.text_digest(narrative_md, 600),
        },
    }
    appendix_system = assembler.assemble(WRITER_PARTS, runtime_ctx=appendix_ctx)
    appendix_md = await _stream_gen(
        ctx,
        tag="appendix",
        system=appendix_system,
        user=(
            "本次是**调用③：附录**，只写第十、十一节，从 `## 十、附录：行业坐标与公开线索` 开始。\n"
            "- 十·A 行业坐标按注入的落地线索产物写，「来源」只写「离线 IPC 行业词表」这类自然语言；\n"
            "- 十·B 公开检索线索置于 `> [!warning]-` 卡片内，逐条带「推断自公开文本，未联网核验」标注，"
            f"无可靠线索时写「{NO_CLUE_NOTE}」；禁止虚构任何 URL 或公开号；\n"
            f"- 第十一节照模板逐字输出：{DISCLAIMER}\n\n"
            f"===== 说明书分段（四位段落号索引）=====\n{paragraphs}"
        ),
    )

    sections = _collect_sections(
        {"structure": structure_md, "narrative": narrative_md, "appendix": appendix_md}
    )
    missing = [sid for sid in SECTION_IDS if not sections.get(sid, "").strip()]
    if missing:
        await ctx.emit(
            "log",
            {
                "message": "以下章节未从撰写调用中解析到，已按模板补占位："
                + "、".join(_ID_TO_TITLE[sid] for sid in missing)
            },
        )
    return {
        "grounding": json.loads(grounding.model_dump_json()),
        "grounding_lint": grounding_findings,
        "note_sections": sections,
        "note_missing": missing,
    }


# 撰写调用的节标题：`## 三、权利要求树`（容忍井号数量、序号后的顿号与空格）
_SECTION_HEADING_RE = re.compile(
    r"(?m)^[ \t]*#{1,4}[ \t]*(十一|十|一|二|三|四|五|六|七|八|九)[ \t]*、[ \t]*(.*)$"
)


def split_writer_sections(markdown: str) -> dict[str, str]:
    """把一次撰写调用的输出按 `## 序号、名称` 切成 `{section_id: 正文}`。"""
    text = markdown or ""
    matches = list(_SECTION_HEADING_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        sid = _NUMERAL_TO_ID.get(m.group(1))
        if sid is None:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body and len(body) > len(out.get(sid, "")):
            out[sid] = body
    return out


def _collect_sections(outputs: dict[str, str]) -> dict[str, str]:
    """三次调用的输出合并为 11 节正文（各调用只认自己分工内的节）。"""
    sections: dict[str, str] = {sid: "" for sid in SECTION_IDS}
    for call, markdown in outputs.items():
        allowed = WRITER_CALLS.get(call, SECTION_IDS)
        for sid, body in split_writer_sections(markdown).items():
            if sid in allowed and body.strip():
                sections[sid] = body.strip()
    return sections


# ---------------------------------------------------------------------------
# 步骤 5：lint_deliver —— 确定性 lint + AUDIT 补丁 + report.json + 交付物
# ---------------------------------------------------------------------------

# 说明书段落引用：`说明书 0002` / `说明书 0002–0004`
_SPEC_REF_RE = re.compile(r"说明书\s*(\d{4})(?:\s*([–—\-~至])\s*(\d{4}))?")
# 裸段落号 `[0002]`（report_style.md 硬性禁止）
_BARE_PARA_RE = re.compile(r"\[(\d{4})\]")
# 已经打过的未命中标注（避免重跑时重复标注）
_MISS_NOTE = "（该段落号未在本案说明书中检索到）"


def lint_spec_references(text: str, paragraphs: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """说明书引用 lint：裸 `[0002]` 改写、区间归一为 en-dash、未命中段落号就地标注。"""
    findings: list[dict[str, Any]] = []
    body = text or ""

    def _bare(match: re.Match[str]) -> str:
        no = match.group(1)
        findings.append(
            {
                "rule": "bare_paragraph",
                "severity": "error",
                "message": f"正文出现裸段落号 [{no}]，已改写为「说明书 {no}」",
            }
        )
        return f"说明书 {no}"

    body = _BARE_PARA_RE.sub(_bare, body)

    def _ref(match: re.Match[str]) -> str:
        start, dash, end = match.group(1), match.group(2), match.group(3)
        missing = [n for n in (start, end) if n and n not in paragraphs]
        canonical = f"说明书 {start}" if not end else f"说明书 {start}–{end}"
        if dash and dash != "–":
            findings.append(
                {
                    "rule": "range_dash",
                    "severity": "info",
                    "message": f"段落区间分隔符已归一为 en-dash：说明书 {start}–{end}",
                }
            )
        if missing:
            findings.append(
                {
                    "rule": "paragraph_missing",
                    "severity": "error",
                    "message": (
                        "引用的说明书段落 " + "、".join(missing) + " 不在本案段落号索引表中，已就地标注"
                    ),
                }
            )
            return canonical + _MISS_NOTE
        return canonical

    body = _SPEC_REF_RE.sub(_ref, body)
    # 修复重跑导致的重复标注
    body = re.sub(re.escape(_MISS_NOTE) + r"(?:" + re.escape(_MISS_NOTE) + r")+", _MISS_NOTE, body)
    return body, findings


def _claim_structure_glyphs(tree: ClaimTree) -> dict[int, str]:
    """第三节树形表的「结构」列：独立权 `◆`，从属权按同父兄弟位置给 `├─` / `└─`。"""
    by_no = tree.by_no()
    children: dict[int, list[int]] = {}
    for claim in tree.claims:
        parent = claim.refs[0] if claim.refs else 0
        children.setdefault(parent, []).append(claim.no)

    def depth(no: int, seen: set[int] | None = None) -> int:
        seen = seen or set()
        node = by_no.get(no)
        if node is None or not node.refs or no in seen:
            return 0
        seen.add(no)
        return 1 + depth(node.refs[0], seen)

    glyphs: dict[int, str] = {}
    for claim in tree.claims:
        if claim.type == "independent" or not claim.refs:
            glyphs[claim.no] = "◆"
            continue
        siblings = children.get(claim.refs[0], [])
        branch = "└─" if siblings and claim.no == siblings[-1] else "├─"
        glyphs[claim.no] = "│  " * max(depth(claim.no) - 1, 0) + branch
    return glyphs


def build_claim_tree_block(tree: ClaimTree, deltas: ClaimDeltas) -> ReportBlock:
    """第三节的单一主展示：由平台按 claim_tree + claim_deltas 装配的树形表。"""
    glyphs = _claim_structure_glyphs(tree)
    delta_by = deltas.by_claim()
    rows: list[dict[str, Any]] = []
    lines = ["| 结构 | 权 | 本项新增 |", "| --- | ---: | --- |"]
    for claim in tree.claims:
        glyph = glyphs.get(claim.no, "◆")
        delta = delta_by.get(claim.no, "")
        rows.append(
            {
                "structure": glyph,
                "no": claim.no,
                "type": claim.type,
                "refs": claim.refs,
                "ref_mode": claim.ref_mode,
                "delta": delta,
            }
        )
        lines.append(f"| `{glyph}` | {claim.no} | {delta} |")
    return ReportBlock(
        type="claim_tree",
        content={"summary": tree.summary_line(), "rows": rows, "markdown": "\n".join(lines)},
    )


# 平台装配的树形表表头（撰写调用若自己也画了一张，按「不得双份主展示」剔除）
_TREE_TABLE_HEADER = "| 结构 | 权 | 本项新增 |"


def _is_tree_table(markdown: str) -> bool:
    head = (markdown or "").splitlines()[0] if markdown else ""
    cells = [c.strip() for c in head.strip().strip("|").split("|")]
    return cells == ["结构", "权", "本项新增"]


def strip_tree_tables(markdown: str) -> str:
    """剔除撰写调用自行画的权要树表（第三节的树形表由平台唯一装配）。"""
    lines = (markdown or "").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            table: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i].strip())
                i += 1
            if not _is_tree_table("\n".join(table)):
                out.extend(table)
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip()


async def lint_mermaid(markdown: str) -> tuple[str, int, list[dict[str, Any]]]:
    """mermaid 门禁：逐块真渲染，渲染不过的降级为编号文字清单（R2）。

    正文没有 mermaid 围栏时直接返回（解读报告的主展示是树形表，图是可选的）。
    """
    if "```mermaid" not in (markdown or ""):
        return markdown, 0, []
    text, degraded = await build_service.degrade_mermaid(markdown)
    findings: list[dict[str, Any]] = []
    if degraded:
        findings.append(
            {
                "rule": "mermaid",
                "severity": "warning",
                "message": f"{degraded} 个 mermaid 图渲染未通过，已降级为编号文字清单",
            }
        )
    return text, degraded, findings


def _meta_callout(meta: ReaderMeta, run_group: str) -> str:
    """著录项卡片（服务端确定性装配，撰写调用不碰）。"""
    scope_label = {"full_text": "全文", "abstract_only": "仅摘要", "partial": "部分"}
    lines = [
        "> [!patent-meta] 著录项",
        f"> - **公开号**：{meta.pub_no or '未知'}",
        f"> - **名称**：{meta.title or '未知'}",
        f"> - **类型**：{meta.type_label or '未定'}",
        f"> - **领域**：{meta.domain or '未标注'}",
        f"> - **IPC**：{'、'.join(meta.ipc) if meta.ipc else '未标注'}",
        f"> - **申请人**：{'、'.join(meta.applicants) if meta.applicants else '未标注'}",
        f"> - **视角**：{meta.perspective}",
        f"> - **证据范围**：{scope_label.get(meta.evidence_scope, meta.evidence_scope)}",
        f"> - **运行 ID**：{run_group}",
    ]
    return "\n".join(lines)


def assemble_markdown(
    meta: ReaderMeta,
    sections: dict[str, str],
    tree: ClaimTree,
    deltas: ClaimDeltas,
    *,
    run_group: str,
) -> str:
    """把三次撰写调用的产物 + 平台装配块拼成解读报告 markdown（11 节齐全）。"""
    title = meta.title or meta.pub_no or "未命名专利"
    parts = [f"# 专利解读：{title}", "", _meta_callout(meta, run_group), ""]
    tree_block = build_claim_tree_block(tree, deltas)
    for sid, _numeral, heading in SECTION_SPECS:
        body = (sections.get(sid) or "").strip()
        if sid == "s11":
            # 免责声明照模板逐字输出（撰写调用漏写或改写时由平台补正）
            if DISCLAIMER not in body:
                body = DISCLAIMER
        elif sid == "s3":
            # 树形表是第三节的单一主展示：撰写调用自己画的那份一律剔除
            body = strip_tree_tables(body) or tree.summary_line()
        if not body:
            body = "（本节未生成，请重跑「报告撰写」步骤。）"
        parts.append(f"## {heading}")
        parts.append("")
        parts.append(body)
        if sid == "s3":
            parts.append("")
            parts.append(tree_block.content["markdown"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ---- markdown → report.json blocks ----------------------------------------

_CALLOUT_RE = re.compile(r"^>\s*\[!([A-Za-z-]+)\][-+]?\s*(.*)$")
_MERMAID_OPEN_RE = re.compile(r"^```mermaid\s*$", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")


def _table_block(lines: list[str]) -> ReportBlock:
    """markdown 表格 → table block（headers + rows + 原始 markdown）。"""
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    headers = cells(lines[0]) if lines else []
    body = [cells(ln) for ln in lines[2:]] if len(lines) > 2 else []
    return ReportBlock(
        type="table",
        content={"headers": headers, "rows": body, "markdown": "\n".join(lines)},
    )


def markdown_to_blocks(markdown: str) -> list[ReportBlock]:
    """把一节 markdown 拆成站内 block 序列（callout / table / mermaid / markdown）。"""
    blocks: list[ReportBlock] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if text:
            blocks.append(ReportBlock(type="markdown", content=text))

    lines = (markdown or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if _MERMAID_OPEN_RE.match(stripped):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not _FENCE_CLOSE_RE.match(lines[i].strip()):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(ReportBlock(type="mermaid", content="\n".join(code).strip()))
            continue

        callout = _CALLOUT_RE.match(stripped)
        if callout:
            flush()
            kind = CALLOUT_BLOCK_TYPES.get(callout.group(1).lower())
            title = callout.group(2).strip()
            i += 1
            inner: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                inner.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            body = "\n".join(inner).strip()
            if kind is None:
                # 未映射的 callout（如 [!figure]）按普通 markdown 呈现，不丢内容
                blocks.append(
                    ReportBlock(type="markdown", content="\n".join(x for x in [title, body] if x))
                )
            else:
                blocks.append(ReportBlock(type=kind, content={"title": title, "markdown": body}))
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush()
            table: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i].strip())
                i += 1
            blocks.append(_table_block(table))
            continue

        buffer.append(line)
        i += 1

    flush()
    return blocks


def build_report(
    markdown: str, meta: ReaderMeta, tree: ClaimTree, deltas: ClaimDeltas
) -> ReaderReport:
    """终稿 markdown → `report.json`（meta + 11 节 sections[blocks]）。"""
    bodies = split_writer_sections(markdown)
    head = markdown.split("## ", 1)[0]
    sections: list[ReportSection] = []
    for sid, _numeral, heading in SECTION_SPECS:
        blocks = markdown_to_blocks(bodies.get(sid, ""))
        if sid == "s1":
            # 著录项卡片置于报告首节之前（站内渲染时作为首块）
            meta_blocks = [b for b in markdown_to_blocks(head) if b.type == "callout-meta"]
            blocks = [*meta_blocks, *blocks]
        if sid == "s3":
            # 树形表由平台装配：把 markdown 里的那张树形表换成 claim_tree block
            # （只剔除树形表本身，第三节若另有别的表照常保留）
            rebuilt = build_claim_tree_block(tree, deltas)
            blocks = [
                b
                for b in blocks
                if not (b.type == "table" and _is_tree_table(b.content.get("markdown", "")))
            ]
            blocks.append(rebuilt)
        sections.append(ReportSection(id=sid, title=heading, blocks=blocks))
    return ReaderReport(meta=meta, sections=sections)


# ---- 自检与交付 ------------------------------------------------------------


async def _self_check(
    ctx: Ctx, markdown: str, structure: patent_fetch.PatentStructure, tree: ClaimTree
) -> tuple[str, dict[str, Any]]:
    """C7 自检 AUDIT：补丁经 services/patches 确定性应用（≤2 轮）。"""
    system = assembler.assemble(
        SELF_CHECK_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "claim_tree_summary": tree.summary_line(),
                "figures": structure.figures,
                "paragraph_numbers": list(structure.paragraphs)[:400],
            },
        },
    )
    text = markdown
    applied_total = 0
    unresolved: list[str] = []
    verdicts: list[str] = []
    rounds = 0
    for rounds in range(1, MAX_SELF_CHECK_ROUNDS + 1):
        report: ReaderSelfCheck = await _call_struct(
            ctx,
            ReaderSelfCheck,
            tag=f"self_check{rounds}",
            system=system,
            user=(
                "以下为装配后的解读报告全文，请按核对清单输出补丁清单 JSON。\n\n"
                "===== 解读报告 =====\n" + text
            ),
            call_class="AUDIT",
        )
        verdicts.append(report.verdict)
        unresolved.extend(u.as_text() for u in report.unresolved if u.as_text())
        if not report.patches:
            break
        result = patches.apply_patches(text, [p.model_dump() for p in report.patches])
        text = result["text"]
        applied_total += result["applied"]
        unresolved.extend(result["unresolved"])
        await ctx.emit(
            "log",
            {
                "message": (
                    f"自检第 {rounds} 轮：补丁 {result['total']} 条，应用 {result['applied']} 条"
                    f"（{result['rate']:.0%}）。"
                )
            },
        )
        if result["applied"] == 0:
            break
    return text, {
        "rounds": rounds,
        "applied": applied_total,
        "verdicts": verdicts,
        "unresolved": list(dict.fromkeys(unresolved)),
    }


async def lint_deliver(ctx: Ctx) -> dict[str, Any]:
    """C6+C7：装配 → 确定性 lint → 自检补丁 → report.json → 落 reader_note_md。"""
    structure = await _load_structure(ctx)
    tree = _tree_from_state(ctx)
    deltas = _deltas_from_state(ctx)
    meta = ReaderMeta.model_validate(ctx.state.get("reader_meta") or {})
    sections = dict(ctx.state.get("note_sections") or {})
    if not any((sections.get(sid) or "").strip() for sid in SECTION_IDS):
        raise ValueError("缺少报告正文，请先跑「报告撰写」步骤")

    markdown = assemble_markdown(meta, sections, tree, deltas, run_group=ctx.run_group)

    # ---- 确定性 lint 束（段落引用 / 裸编号 / 附图 / mermaid 可渲染）----
    markdown, ref_findings = lint_spec_references(markdown, structure.paragraphs)
    fig_findings = lint_figure_references(markdown, structure.figures)
    markdown, mermaid_degraded, mermaid_findings = await lint_mermaid(markdown)
    lint_findings = [
        *ref_findings,
        *fig_findings,
        *mermaid_findings,
        *(ctx.state.get("grounding_lint") or []),
    ]

    # ---- AUDIT 自检补丁 ----
    markdown, audit = await _self_check(ctx, markdown, structure, tree)
    # 补丁可能引入新的段落引用问题，收尾再过一次引用 lint
    markdown, post_findings = lint_spec_references(markdown, structure.paragraphs)
    lint_findings.extend(post_findings)

    # ---- report.json + 交付物 ----
    report = build_report(markdown, meta, tree, deltas)
    artifact = await artifacts_service.save_artifact(
        ctx.case_id,
        "reader_note_md",
        markdown,
        "md",
        title=meta.title or meta.pub_no or None,
        run_group=ctx.run_group,
        iteration_type="initial",
        summary=f"专利解读报告（{meta.pub_no or '未知公开号'}）",
    )
    await _emit_artifact(ctx.case_id, artifact)
    await _emit_doc_version(ctx.case_id, artifact)

    lines = [
        "解读报告已生成：",
        f"- 报告文件：{artifact.filename}（11 节，可导出 Word）",
        f"- 权利要求：{tree.summary_line()}",
        f"- 说明书段落索引：{len(structure.paragraphs)} 段；附图：{len(structure.figures)} 幅",
    ]
    errors = [f for f in lint_findings if f.get("severity") == "error"]
    if errors:
        lines.append(f"- 引用核对：{len(errors)} 处需要注意，已在正文就地标注")
    if audit["unresolved"]:
        lines.append(f"- 自检未决事项 {len(audit['unresolved'])} 条（不入正文，见下方清单）")
        lines.extend(f"  · {item}" for item in audit["unresolved"][:5])
    await ctx.chat_delta("\n".join(lines))
    await ctx.chat_done()

    return {
        "report": json.loads(report.model_dump_json()),
        "report_markdown": markdown,
        "report_artifact_id": artifact.id,
        "report_lint": lint_findings,
        "self_check": audit,
        "mermaid_degraded": mermaid_degraded,
    }


_FIG_REF_RE = re.compile(r"图\s*(\d{1,2})")


def lint_figure_references(text: str, figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """附图引用 lint：正文里的「图 N」必须存在于附图清单（清单为空时不判违规）。"""
    if not figures:
        return []
    known = {int(f.get("no")) for f in figures if str(f.get("no", "")).isdigit()}
    unknown = sorted({int(m.group(1)) for m in _FIG_REF_RE.finditer(text or "")} - known)
    if not unknown:
        return []
    return [
        {
            "rule": "figure_missing",
            "severity": "warning",
            "message": "正文引用了附图清单中不存在的 " + "、".join(f"图{n}" for n in unknown),
        }
    ]


# ---------------------------------------------------------------------------
# 步骤表
# ---------------------------------------------------------------------------


def build_steps(case: dict[str, Any]) -> list[StepDef]:
    """模块 C 的五步流水线。"""
    return [
        StepDef(key="acquire", name_zh="获取与文本化", handler=acquire),
        StepDef(key="claim_tree", name_zh="权利要求树", handler=claim_tree, gate="review"),
        StepDef(key="claim_deltas", name_zh="白话增量", handler=claim_deltas),
        StepDef(key="note", name_zh="报告撰写", handler=note),
        StepDef(key="lint_deliver", name_zh="校对与交付", handler=lint_deliver),
    ]
