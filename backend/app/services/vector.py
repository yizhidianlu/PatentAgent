"""OA 案例库检索层（sqlite-vec 封装 + 三级降级链）。

三级降级链（backend-architecture.md §6 风险 4 / guardrails.md「配置适配说明」）：

| 条件 | `retrieval_mode` | 实现 |
|---|---|---|
| embedding 已配置且启用 + sqlite-vec 扩展可用 + 已建索引 | `vector` | vec0 虚拟表 KNN（`chunk_id IN (…)` 元数据预过滤） |
| embedding 已配置且启用，但扩展不可用 / 索引未建 | `brute` | 读 `oa_vec_blob` 的 float32 向量，纯 Python 余弦暴搜 |
| 未配置 embedding（或设置页未启用） | `keyword` | `oa_chunks.text` 的中文 2-gram / 英数词命中打分 |

要点：
- **元数据预过滤先于向量检索**：先按 `statutes / defect_types / patent_type / tags`
  过滤出候选 `chunk_id` 集合，再把该集合作为 KNN 的 `chunk_id IN (…)` 条件下推
  （sqlite-vec v0.1.9 实测支持），而不是「先 KNN 再筛」；
- **向量表维度写死在 DDL**（`embedding float[{dim}]`），维度来自设置页 embedding 配置；
  维度变更必须 DROP 重建（`ensure_vec_table` 自动识别 + `rebuild()` 全量回填）；
- **向量原始数据存 `oa_vec_blob`**（可移植的 float32 小端字节串，与 vec0 输入同格式），
  扩展缺失时不丢数据、就绪后 rebuild 即可回填；
- 入库向量一律 **L2 归一化**，于是 vec0 的 L2 距离 d 与余弦相似度 cos 满足
  `cos = 1 - d²/2`，两条向量路径（vector / brute）的 score 口径一致（[0,1]）。

只有 `oa_library.status='confirmed'`（人审闸门通过）的案例才进检索库。
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import struct
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..db import database as db
from ..models.oa import DEFECT_TYPE_TO_BASE
from ..models.settings import EmbeddingSettings
from . import llm

logger = logging.getLogger(__name__)

VEC_TABLE = "oa_vec"
BLOB_TABLE = "oa_vec_blob"

#: embeddings 接口单批条数（多数兼容端点对 input 数组长度有限制）
EMBED_BATCH = 16       # 智谱 embedding-3 单次数组上限 64，留足余量
#: 单条文本上限。embedding-3 标称 3072 tokens；中文约 1 字 ≈ 1 token，故按字符保守取值。
#: 入库分块 ≤600 字符本就安全，这道保护是给检索时的长 query 用的。
EMBED_MAX_CHARS = 3000
#: embeddings 请求超时（秒）
EMBED_TIMEOUT = 60
#: `chunk_id IN (…)` 单批参数上限（避开 SQLITE_MAX_VARIABLE_NUMBER）
IN_BATCH = 500
#: 切块长度区间（案例笔记按小节切，每块 300–600 字）
MIN_CHUNK_CHARS = 300
MAX_CHUNK_CHARS = 600


class EmbeddingNotConfiguredError(RuntimeError):
    """尚未配置（或未启用）Embedding —— 案例库只能走关键词检索。"""


class EmbeddingCallError(RuntimeError):
    """Embedding 接口调用失败或返回不合契约（维度不符等）。"""


# ---------------------------------------------------------------------------
# 配置与能力探测
# ---------------------------------------------------------------------------


def load_config(override: dict[str, Any] | None = None) -> EmbeddingSettings:
    """读设置页的 embedding 配置（base_url / api_key / model / dim / enabled）。"""
    return llm.load_embedding_settings(override)


def is_configured(cfg: EmbeddingSettings | None = None) -> bool:
    """向量检索是否可用：设置页已启用且填了模型。"""
    cfg = cfg or load_config()
    return bool(cfg.enabled and cfg.model)


def vec_available() -> bool:
    """sqlite-vec 扩展是否加载成功（Windows/Py3.13 下可能失败，届时降级暴搜）。"""
    return bool(db.sqlite_vec_status.get("loaded"))


def not_configured_message(cfg: EmbeddingSettings | None = None) -> str:
    """未启用向量检索时呈现给用户的说明。"""
    cfg = cfg or load_config()
    if not cfg.model:
        return "尚未配置 Embedding 模型（设置页 → Embedding），案例检索已降级为关键词匹配。"
    return "设置页未启用向量检索（Embedding.enabled=false），案例检索已降级为关键词匹配。"


# ---------------------------------------------------------------------------
# embed()：OpenAI 兼容 embeddings 接口
# ---------------------------------------------------------------------------


#: 已确认不接受 `dimensions` 的模型（进程内记忆，避免每批都白试一次）。
_NO_DIMENSIONS: set[str] = set()


def _dimension_kwargs(cfg: Any) -> dict[str, Any]:
    """按设置里的维度向服务商索要向量长度。

    此前从不发这个参数，于是「向量维度」只是个**事后校验值**：配 1024 而模型
    默认返回 2048 时，要等到真正嵌入的那一刻才报「维度不一致」。
    支持该参数的服务商应当直接按配置值出向量——让设置成为指令，而不是断言。
    """
    dim = int(getattr(cfg, "dim", 0) or 0)
    if dim <= 0 or str(getattr(cfg, "model", "")) in _NO_DIMENSIONS:
        return {}
    return {"dimensions": dim}


def _drop_dimensions_on_error(cfg: Any, exc: Exception) -> bool:
    """报错是否因为不认 `dimensions`；是则记下并让调用方摘掉重发一次。"""
    model = str(getattr(cfg, "model", ""))
    if not model or model in _NO_DIMENSIONS:
        return False
    if int(getattr(cfg, "dim", 0) or 0) <= 0:
        return False
    message = str(exc).lower()
    if "dimensions" not in message:
        return False
    _NO_DIMENSIONS.add(model)
    logger.info("模型 %s 不接受 dimensions 参数，后续按其默认维度返回", model)
    return True


def embedding_error_hint(cfg: Any, exc: Exception) -> str:
    """把服务商原话翻译成「这是什么问题、该去做什么」。

    机主看到的是一句原始的 `Error code: 429 - {...'1113'...余额不足或无可用资源包}`。
    今天已经证明这句话的真实含义可能是「订阅套餐不含这个模型」而不是「没钱」——
    实测：同一把 key 三个端点两个 embedding 模型全 1113，而它的 chat 在订阅端点正常。
    两种含义的处置完全不同，所以必须把可能性和判据一起说出来。
    """
    model = str(getattr(cfg, "model", "") or "该模型")
    detail = llm._server_message(exc)
    if llm._is_quota_exhausted(exc):
        return (
            f"Embedding 服务返回配额/余额受限（模型 {model}）：{detail}\n"
            "常见成因有两种，处置不同：\n"
            "① 该模型不在当前套餐内 —— 订阅制套餐（如 GLM Coding Plan）通常只含对话模型，"
            "embedding 需要按量计费余额；\n"
            "② 账户确实欠费。\n"
            "判据：若同一把 Key 的对话模型可用而 embedding 报此错，多半是 ①。"
            "此时可为账户充值按量余额、换一家 embedding 服务商，"
            "或关闭向量检索（案例库会退化为关键词检索，功能仍可用）。"
        )
    if llm._is_rate_limited(exc):
        return f"Embedding 服务触发限流（模型 {model}）：{detail}。请稍后重试或降低并发。"
    if llm._is_context_overflow(exc):
        return (
            f"送入 {model} 的文本超出其单条上限：{detail}。"
            "请缩短案例笔记的分块长度后重建索引。"
        )
    return f"Embedding 调用失败（模型 {model}）：{detail}"


async def embed(
    texts: Sequence[str], *, override: dict[str, Any] | None = None
) -> list[list[float]]:
    """把文本批量转成向量（顺序与入参一致）。

    未配置模型抛 `EmbeddingNotConfiguredError`；调用失败或维度与设置不符抛
    `EmbeddingCallError`（两者的消息都直接可呈现给用户）。
    """
    cfg = load_config(override)
    if not cfg.model:
        raise EmbeddingNotConfiguredError(
            "尚未配置 Embedding 模型：请在设置页填写 base_url / 模型 / 维度并测试连通后重试"
        )
    items = [str(t or "")[:EMBED_MAX_CHARS] for t in texts]
    if not items:
        return []

    client = llm._client(cfg.base_url, cfg.api_key, timeout=EMBED_TIMEOUT)
    vectors: list[list[float]] = []
    for start in range(0, len(items), EMBED_BATCH):
        batch = items[start : start + EMBED_BATCH]
        # 走 chat 那道并发闸：它的设计前提是「上游看到的并发不超上限」，
        # 而 embedding 此前是闸外流量——两者常指向同一个服务商（现场即如此）。
        slot = await llm._acquire_slot(
            cfg.base_url, case_id=None, step_key=None, model=cfg.model
        )
        token = llm._track_call(cfg.model, None)
        try:
            resp = await client.embeddings.create(
                model=cfg.model, input=batch, **_dimension_kwargs(cfg)
            )
        except Exception as exc:  # noqa: BLE001 —— 统一转成可呈现的领域异常
            if _drop_dimensions_on_error(cfg, exc):
                # 该服务商不认 dimensions：摘掉重发一次，而不是让整次嵌入失败
                resp = await client.embeddings.create(model=cfg.model, input=batch)
            else:
                raise EmbeddingCallError(embedding_error_hint(cfg, exc)) from exc
        finally:
            llm._untrack_call(token)
            if slot is not None:
                slot.release()
        data = sorted(resp.data or [], key=lambda d: int(getattr(d, "index", 0) or 0))
        if len(data) != len(batch):
            raise EmbeddingCallError(
                f"Embedding 返回条数 {len(data)} 与请求条数 {len(batch)} 不一致"
            )
        for item in data:
            vec = [float(x) for x in item.embedding]
            if cfg.dim and len(vec) != cfg.dim:
                raise EmbeddingCallError(
                    f"Embedding 实际维度 {len(vec)} 与设置的维度 {cfg.dim} 不一致："
                    "请在设置页更正维度后重建向量库（POST /oa/library/rebuild）"
                )
            vectors.append(vec)
    return vectors


# ---------------------------------------------------------------------------
# 向量打包 / 归一化
# ---------------------------------------------------------------------------


def normalize_vector(vec: Sequence[float]) -> list[float]:
    """L2 归一化（零向量原样返回）。"""
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm <= 0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]


def pack_vector(vec: Sequence[float]) -> bytes:
    """float32 小端字节串（vec0 与 oa_vec_blob 共用的存储格式）。"""
    return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    """反序列化 float32 字节串。"""
    return list(struct.unpack(f"<{dim}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（长度不等时按较短的截断）。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    na = math.sqrt(sum(float(a[i]) ** 2 for i in range(n)))
    nb = math.sqrt(sum(float(b[i]) ** 2 for i in range(n)))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _distance_to_score(distance: float) -> float:
    """归一化向量的 L2 距离 → 余弦相似度（cos = 1 - d²/2），裁剪到 [0,1]。"""
    score = 1.0 - (float(distance) ** 2) / 2.0
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# 向量表管理（维度写死在 DDL → 维度变更必须 DROP 重建）
# ---------------------------------------------------------------------------


def vec_table_dim() -> int | None:
    """当前 oa_vec 虚拟表的维度；表不存在返回 None。"""
    row = db.query_one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (VEC_TABLE,)
    )
    if row is None:
        return None
    m = re.search(r"float\s*\[\s*(\d+)\s*\]", str(row["sql"] or ""))
    return int(m.group(1)) if m else None


def get_meta() -> dict[str, Any]:
    """当前索引元信息 `{dim, model, updated_at}`（未建索引时为空 dict）。"""
    row = db.query_one("SELECT dim, model, updated_at FROM oa_vec_meta WHERE id=1")
    return dict(row) if row is not None else {}


def set_meta(dim: int, model: str) -> None:
    """记录当前索引的维度与模型。"""
    db.execute(
        """
        INSERT INTO oa_vec_meta(id, dim, model, updated_at) VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET dim=excluded.dim, model=excluded.model,
                                      updated_at=excluded.updated_at
        """,
        (int(dim), str(model or ""), db.now_str()),
    )


def drop_vec_table() -> None:
    """丢弃 oa_vec 虚拟表（维度变更 / rebuild 用）。"""
    if not vec_available():
        return
    db.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")


def ensure_vec_table(dim: int) -> bool:
    """确保 oa_vec 存在且维度 = dim；维度不符时 DROP 重建并作废旧向量。

    返回 True 表示 vec0 索引可用（sqlite-vec 已加载且表已就绪）。
    """
    if dim <= 0 or not vec_available():
        return False
    current = vec_table_dim()
    if current == dim:
        return True
    if current is not None:
        logger.info("oa_vec 维度由 %s 变更为 %s，DROP 重建并作废旧向量", current, dim)
        db.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")
    try:
        db.execute(
            f"CREATE VIRTUAL TABLE {VEC_TABLE} USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding float[{int(dim)}])"
        )
    except sqlite3.Error as exc:
        logger.warning("创建 oa_vec 虚拟表失败（降级暴搜）：%s", exc)
        return False
    if current is not None and current != dim:
        # 维度变更：旧维度的 blob 全部作废，等待 rebuild 重新嵌入
        db.execute(f"DELETE FROM {BLOB_TABLE} WHERE dim<>?", (int(dim),))
        db.execute("UPDATE oa_library SET embedded=0 WHERE embedded<>0")
    return True


# ---------------------------------------------------------------------------
# 切块（案例笔记按小节切，每块 300–600 字）
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_NAV_RE = re.compile(r"^\s*>\s*导航[:：].*$", re.MULTILINE)


def _strip_note(note_md: str) -> str:
    """去掉 frontmatter 与导航行，只留正文。"""
    text = _FRONTMATTER_RE.sub("", note_md or "", count=1)
    return _NAV_RE.sub("", text)


def _split_sentences(text: str) -> list[str]:
    """按中文/英文句末标点与换行切句（保留标点）。"""
    parts = re.split(r"(?<=[。！？!?；;])|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def split_note(note_md: str) -> list[str]:
    """案例笔记 → 检索块：先按 `##` 小节切，再在小节内按句聚成 300–600 字的块。

    每块带上小节标题（`【通知书要点】…`），保证块脱离上下文后仍可读。
    """
    body = _strip_note(note_md)
    sections: list[tuple[str, list[str]]] = []
    title = ""
    buf: list[str] = []

    def flush() -> None:
        if buf and any(line.strip() for line in buf):
            sections.append((title, list(buf)))
        buf.clear()

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            title = stripped[3:].strip()
        elif stripped.startswith("# "):
            flush()
            title = stripped[2:].strip()
        else:
            buf.append(line)
    flush()

    chunks: list[str] = []
    for sec_title, lines in sections:
        text = " ".join(x.strip() for x in lines if x.strip())
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        prefix = f"【{sec_title}】" if sec_title else ""
        budget = max(120, MAX_CHUNK_CHARS - len(prefix))
        current = ""
        for sentence in _split_sentences(text):
            while len(sentence) > budget:              # 超长单句硬切
                if current:
                    chunks.append(prefix + current)
                    current = ""
                chunks.append(prefix + sentence[:budget])
                sentence = sentence[budget:]
            if not sentence:
                continue
            if current and len(current) + len(sentence) > budget:
                chunks.append(prefix + current)
                current = sentence
            else:
                current = f"{current}{sentence}" if current else sentence
        if current:
            chunks.append(prefix + current)

    # 相邻过短块合并（避免「## 结果」之类一句话小节单独成块）
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) < MIN_CHUNK_CHARS and \
                len(merged[-1]) + len(chunk) <= MAX_CHUNK_CHARS + MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]}\n{chunk}"
        else:
            merged.append(chunk)
    return [c.strip() for c in merged if c.strip()]


# ---------------------------------------------------------------------------
# 元数据预过滤
# ---------------------------------------------------------------------------


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip() for v in value if str(v).strip()}
    return set()


def _frontmatter_of(row: Mapping[str, Any]) -> dict[str, Any]:
    import json

    try:
        data = json.loads(row["frontmatter_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def match_filters(frontmatter: Mapping[str, Any], filters: Mapping[str, Any] | None) -> bool:
    """案例 frontmatter 是否满足元数据过滤条件（空条件 = 不约束）。

    列表型条件（statutes / defect_types / tags）取「交集非空」；`defect_types` 额外
    容忍平台扩充值经 `DEFECT_TYPE_TO_BASE` 归并后的匹配（notice_struct.md 注记）。
    """
    if not filters:
        return True

    want_type = str(filters.get("patent_type") or "").strip()
    if want_type and str(frontmatter.get("patent_type") or "").strip() != want_type:
        return False

    for key in ("statutes", "tags"):
        want = _as_set(filters.get(key))
        if want and not (want & _as_set(frontmatter.get(key))):
            return False

    want_defects = _as_set(filters.get("defect_types"))
    if want_defects:
        have = _as_set(frontmatter.get("defect_types"))
        expanded = set(want_defects)
        for value in want_defects:
            base = DEFECT_TYPE_TO_BASE.get(value)
            if base:
                expanded.add(base)
        if not (expanded & have):
            return False

    exclude = _as_set(filters.get("exclude_case_ids"))
    if exclude and str(frontmatter.get("case_id") or "") in exclude:
        return False
    return True


def _candidate_chunks_sync(
    filters: Mapping[str, Any] | None,
    viewer: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """元数据预过滤 → 候选检索块（只取 status='confirmed' 的案例）。

    `viewer` 给定时先按属主 + visibility 做数据隔离（M8）：普通用户只能检索
    自己的案例与被标为 shared 的案例；None = 服务层内部调用，不过滤。
    """
    from . import oa_library  # 延迟导入：oa_library 依赖 vector，避免循环

    lib_rows = db.query_all(
        "SELECT id, status, frontmatter_json, embedded, user_id, visibility "
        "FROM oa_library WHERE status='confirmed'"
    )
    keep: dict[str, dict[str, Any]] = {}
    exclude_ids = _as_set((filters or {}).get("exclude_library_ids"))
    for row in lib_rows:
        if str(row["id"]) in exclude_ids:
            continue
        if not oa_library.visible_to(row, viewer):
            continue
        fm = _frontmatter_of(row)
        if not match_filters(fm, filters):
            continue
        keep[str(row["id"])] = fm
    if not keep:
        return []
    placeholders = ",".join("?" for _ in keep)
    chunk_rows = db.query_all(
        f"SELECT id, library_id, chunk_index, text FROM oa_chunks "
        f"WHERE library_id IN ({placeholders}) ORDER BY library_id, chunk_index",
        tuple(keep),
    )
    return [
        {
            "chunk_id": int(r["id"]),
            "library_id": str(r["library_id"]),
            "chunk_index": int(r["chunk_index"]),
            "text": str(r["text"] or ""),
            "frontmatter": keep[str(r["library_id"])],
        }
        for r in chunk_rows
    ]


# ---------------------------------------------------------------------------
# 关键词降级检索
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[一-鿿]{2,}")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}|\d{2,}")
_STOP = {
    "the", "and", "for", "with", "that", "this",
}


def keyword_terms(query: str) -> list[str]:
    """查询 → 关键词集合：中文串取 2-gram，英数取整词（去重、去停用词）。"""
    terms: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if len(value) >= 2 and value.lower() not in _STOP and value not in terms:
            terms.append(value)

    for run in _CJK_RE.findall(query or ""):
        add(run)
        for i in range(len(run) - 1):
            add(run[i : i + 2])
    for word in _WORD_RE.findall(query or ""):
        add(word.lower())
    return terms


def keyword_score(text: str, terms: Sequence[str]) -> float:
    """命中率打分：命中词数 / 总词数，长词命中加权。"""
    if not terms:
        return 0.0
    body = (text or "").lower()
    total = 0.0
    hit = 0.0
    for term in terms:
        weight = 1.0 + 0.5 * (len(term) > 2)
        total += weight
        if term.lower() in body:
            hit += weight
    return hit / total if total else 0.0


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def _hit(item: Mapping[str, Any], score: float) -> dict[str, Any]:
    fm = dict(item.get("frontmatter") or {})
    return {
        "library_id": item["library_id"],
        "case_id": str(fm.get("case_id") or ""),
        "title": str(fm.get("title") or ""),
        "chunk_id": int(item["chunk_id"]),
        "chunk_index": int(item["chunk_index"]),
        "chunk_text": str(item["text"]),
        "score": round(float(score), 6),
        "frontmatter": fm,
    }


def _vector_search_sync(
    qvec: Sequence[float], candidates: Sequence[Mapping[str, Any]], k: int, dim: int
) -> list[tuple[int, float]] | None:
    """vec0 KNN（元数据候选集下推为 `chunk_id IN (…)`）；不可用返回 None（→ 暴搜）。

    这里**不**建表：索引缺失或维度不符时一律退回暴搜（`oa_vec_blob` 是向量的
    权威存储），避免在检索路径上顺手 DROP 重建出一张空表、把命中吃掉。
    """
    if not vec_available() or vec_table_dim() != dim:
        return None
    ids = [int(c["chunk_id"]) for c in candidates]
    blob = pack_vector(normalize_vector(qvec))
    found: list[tuple[int, float]] = []
    try:
        for start in range(0, len(ids), IN_BATCH):
            batch = ids[start : start + IN_BATCH]
            placeholders = ",".join("?" for _ in batch)
            rows = db.query_all(
                f"SELECT chunk_id, distance FROM {VEC_TABLE} "
                f"WHERE embedding MATCH ? AND k = ? AND chunk_id IN ({placeholders}) "
                f"ORDER BY distance",
                (blob, int(k), *batch),
            )
            found.extend((int(r["chunk_id"]), float(r["distance"])) for r in rows)
    except sqlite3.Error as exc:
        logger.warning("sqlite-vec KNN 失败（降级暴搜）：%s", exc)
        return None
    if not found:
        # 候选块有向量（调用方已按 oa_vec_blob 过滤）却在索引里查不到 → 索引不同步
        return None
    found.sort(key=lambda x: x[1])
    return found[:k]


def _brute_search_sync(
    qvec: Sequence[float], candidates: Sequence[Mapping[str, Any]], k: int, dim: int
) -> list[tuple[int, float]]:
    """纯 Python 余弦暴搜（读 oa_vec_blob；本地小语料可接受）。"""
    ids = [int(c["chunk_id"]) for c in candidates]
    scored: list[tuple[int, float]] = []
    for start in range(0, len(ids), IN_BATCH):
        batch = ids[start : start + IN_BATCH]
        placeholders = ",".join("?" for _ in batch)
        rows = db.query_all(
            f"SELECT chunk_id, dim, embedding FROM {BLOB_TABLE} "
            f"WHERE chunk_id IN ({placeholders}) AND dim=?",
            (*batch, int(dim)),
        )
        for row in rows:
            vec = unpack_vector(row["embedding"], int(row["dim"]))
            scored.append((int(row["chunk_id"]), cosine(qvec, vec)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def _embedded_chunk_ids_sync(candidates: Sequence[Mapping[str, Any]], dim: int) -> set[int]:
    """候选块里已存在指定维度向量的那部分。"""
    ids = [int(c["chunk_id"]) for c in candidates]
    found: set[int] = set()
    for start in range(0, len(ids), IN_BATCH):
        batch = ids[start : start + IN_BATCH]
        placeholders = ",".join("?" for _ in batch)
        rows = db.query_all(
            f"SELECT chunk_id FROM {BLOB_TABLE} WHERE chunk_id IN ({placeholders}) AND dim=?",
            (*batch, int(dim)),
        )
        found.update(int(r["chunk_id"]) for r in rows)
    return found


async def search(
    query: str,
    k: int = 5,
    filters: Mapping[str, Any] | None = None,
    viewer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """案例检索（三级降级链）。

    返回 `{mode, query, k, filters, total_candidates, hits, message, error}`；
    `mode` ∈ vector | brute | keyword —— **必须**透出给前端明示检索方式。
    `viewer`（{id, role}）给定时按案例属主 + visibility 做数据隔离。
    """
    k = max(1, int(k or 5))
    filters = dict(filters or {})
    cfg = load_config()
    candidates = await db.arun(_candidate_chunks_sync, filters, viewer)
    base: dict[str, Any] = {
        "query": query or "",
        "k": k,
        "filters": filters,
        "total_candidates": len(candidates),
        "hits": [],
        "message": "",
        "error": "",
    }

    if not is_configured(cfg):
        base["mode"] = "keyword"
        base["message"] = not_configured_message(cfg)
        if candidates:
            base["hits"] = _keyword_hits(query, candidates, k)
        else:
            base["message"] += "（案例库中没有已确认的案例）"
        return base

    if not candidates:
        base["mode"] = "vector" if vec_available() else "brute"
        base["message"] = "元数据预过滤后没有候选案例（案例库为空或过滤条件过严）。"
        return base

    try:
        qvec = (await embed([query or ""]))[0]
    except (EmbeddingNotConfiguredError, EmbeddingCallError) as exc:
        base["mode"] = "keyword"
        base["error"] = str(exc)
        base["message"] = f"向量检索不可用，已降级为关键词检索：{exc}"
        base["hits"] = _keyword_hits(query, candidates, k)
        return base

    dim = len(qvec)
    embedded = await db.arun(_embedded_chunk_ids_sync, candidates, dim)
    usable = [c for c in candidates if int(c["chunk_id"]) in embedded]
    if not usable:
        base["mode"] = "keyword"
        base["message"] = (
            "候选案例尚未完成向量化（或维度已变更），本次降级为关键词检索；"
            "可执行 POST /oa/library/rebuild 重建向量库。"
        )
        base["hits"] = _keyword_hits(query, candidates, k)
        return base

    by_id = {int(c["chunk_id"]): c for c in usable}
    ranked = await db.arun(_vector_search_sync, qvec, usable, k, dim)
    if ranked is not None:
        base["mode"] = "vector"
        base["hits"] = [
            _hit(by_id[cid], _distance_to_score(dist)) for cid, dist in ranked if cid in by_id
        ]
        return base

    scored = await db.arun(_brute_search_sync, qvec, usable, k, dim)
    base["mode"] = "brute"
    base["message"] = (
        "sqlite-vec 扩展不可用，已降级为纯 Python 余弦暴搜"
        f"（{db.sqlite_vec_status.get('error') or '索引未就绪'}）。"
    )
    base["hits"] = [_hit(by_id[cid], score) for cid, score in scored if cid in by_id]
    return base


def _keyword_hits(
    query: str, candidates: Sequence[Mapping[str, Any]], k: int
) -> list[dict[str, Any]]:
    """关键词命中打分 → 前 k 条（含 frontmatter 文本，便于命中标题/领域标签）。"""
    terms = keyword_terms(query)
    scored: list[tuple[float, Mapping[str, Any]]] = []
    for item in candidates:
        fm = item.get("frontmatter") or {}
        haystack = " ".join(
            [
                str(item.get("text") or ""),
                str(fm.get("title") or ""),
                str(fm.get("domain") or ""),
                " ".join(str(x) for x in (fm.get("statutes") or [])),
                " ".join(str(x) for x in (fm.get("tags") or [])),
            ]
        )
        score = keyword_score(haystack, terms)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1]["library_id"], x[1]["chunk_index"]))
    return [_hit(item, score) for score, item in scored[:k]]


# ---------------------------------------------------------------------------
# 写入：upsert / delete / rebuild
# ---------------------------------------------------------------------------


def _replace_chunks_sync(library_id: str, chunks: Sequence[str]) -> list[int]:
    """重写某案例的检索块（先删旧块与旧向量，再插新块）；返回新块 id 列表。"""
    def op(conn: sqlite3.Connection) -> list[int]:
        old = [int(r[0]) for r in conn.execute(
            "SELECT id FROM oa_chunks WHERE library_id=?", (library_id,)
        ).fetchall()]
        if old and vec_available() and vec_table_dim() is not None:
            placeholders = ",".join("?" for _ in old)
            try:
                conn.execute(
                    f"DELETE FROM {VEC_TABLE} WHERE chunk_id IN ({placeholders})", tuple(old)
                )
            except sqlite3.Error as exc:  # 表可能刚被 DROP
                logger.debug("清理 oa_vec 旧向量失败（忽略）：%s", exc)
        conn.execute(f"DELETE FROM {BLOB_TABLE} WHERE library_id=?", (library_id,))
        conn.execute("DELETE FROM oa_chunks WHERE library_id=?", (library_id,))
        ids: list[int] = []
        for index, text in enumerate(chunks):
            cur = conn.execute(
                "INSERT INTO oa_chunks(library_id, chunk_index, text) VALUES (?,?,?)",
                (library_id, index, text),
            )
            ids.append(int(cur.lastrowid))
        return ids

    return db.transaction(op)


def _store_vectors_sync(
    library_id: str, pairs: Sequence[tuple[int, Sequence[float]]], dim: int, model: str
) -> int:
    """写入向量（oa_vec_blob 必写；oa_vec 在扩展可用时同步写）。"""
    use_vec = ensure_vec_table(dim)
    now = db.now_str()

    def op(conn: sqlite3.Connection) -> int:
        count = 0
        for chunk_id, vec in pairs:
            blob = pack_vector(normalize_vector(vec))
            conn.execute(
                f"INSERT INTO {BLOB_TABLE}(chunk_id, library_id, dim, embedding, model, created_at) "
                f"VALUES (?,?,?,?,?,?) "
                f"ON CONFLICT(chunk_id) DO UPDATE SET library_id=excluded.library_id, "
                f"dim=excluded.dim, embedding=excluded.embedding, model=excluded.model, "
                f"created_at=excluded.created_at",
                (int(chunk_id), library_id, int(dim), blob, model, now),
            )
            if use_vec:
                conn.execute(f"DELETE FROM {VEC_TABLE} WHERE chunk_id=?", (int(chunk_id),))
                conn.execute(
                    f"INSERT INTO {VEC_TABLE}(chunk_id, embedding) VALUES (?,?)",
                    (int(chunk_id), blob),
                )
            count += 1
        return count

    stored = db.transaction(op)
    if stored:
        set_meta(dim, model)
    return stored


def _set_embedded_sync(library_id: str, embedded: int) -> None:
    db.execute(
        "UPDATE oa_library SET embedded=?, updated_at=? WHERE id=?",
        (int(embedded), db.now_str(), library_id),
    )


def _library_row_sync(library_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM oa_library WHERE id=?", (library_id,))
    return dict(row) if row is not None else None


async def upsert(library_id: str) -> dict[str, Any]:
    """（重新）切块并向量化某案例。

    - 案例未 `confirmed`：清空其检索块（人审闸门：只有确认过的案例进检索库）；
    - embedding 未配置/未启用：只切块（关键词检索可用），`embedded=0`；
    - embedding 可用：切块 + 嵌入 + 写 oa_vec_blob（扩展可用时同步写 oa_vec），`embedded=1`。
    """
    row = await db.arun(_library_row_sync, library_id)
    if row is None:
        raise KeyError(f"案例不存在：{library_id}")
    if str(row.get("status")) != "confirmed":
        await db.arun(_replace_chunks_sync, library_id, [])
        await db.arun(_set_embedded_sync, library_id, 0)
        return {"library_id": library_id, "chunks": 0, "embedded": 0, "mode": "skipped",
                "message": "案例尚未通过人审确认（status≠confirmed），不进检索库"}

    chunks = split_note(str(row.get("case_note_md") or ""))
    chunk_ids = await db.arun(_replace_chunks_sync, library_id, chunks)

    cfg = load_config()
    if not is_configured(cfg) or not chunk_ids:
        await db.arun(_set_embedded_sync, library_id, 0)
        return {
            "library_id": library_id, "chunks": len(chunk_ids), "embedded": 0,
            "mode": "keyword", "message": not_configured_message(cfg) if chunk_ids else "笔记正文为空",
        }

    vectors = await embed([str(row_text) for row_text in chunks])
    dim = len(vectors[0]) if vectors else int(cfg.dim or 0)
    stored = await db.arun(
        _store_vectors_sync, library_id, list(zip(chunk_ids, vectors)), dim, cfg.model
    )
    await db.arun(_set_embedded_sync, library_id, 1 if stored else 0)
    return {
        "library_id": library_id,
        "chunks": len(chunk_ids),
        "embedded": stored,
        "dim": dim,
        "mode": "vector" if vec_available() and vec_table_dim() == dim else "brute",
        "message": "",
    }


def delete_sync(library_id: str) -> None:
    """删除某案例的检索块与向量（案例行本身由 oa_library 负责）。"""
    _replace_chunks_sync(library_id, [])
    db.execute(f"DELETE FROM {BLOB_TABLE} WHERE library_id=?", (library_id,))


async def delete(library_id: str) -> None:
    """delete_sync 的异步包装。"""
    await db.arun(delete_sync, library_id)


def _reset_index_sync() -> int:
    """丢弃全部向量与索引（返回被标记为待重嵌的案例数）。"""
    drop_vec_table()
    db.execute(f"DELETE FROM {BLOB_TABLE}")
    db.execute("UPDATE oa_library SET embedded=0 WHERE embedded<>0")
    row = db.query_one("SELECT COUNT(*) AS n FROM oa_library WHERE status='confirmed'")
    return int(row["n"]) if row else 0


def _confirmed_ids_sync() -> list[str]:
    rows = db.query_all(
        "SELECT id FROM oa_library WHERE status='confirmed' ORDER BY created_at ASC"
    )
    return [str(r["id"]) for r in rows]


async def rebuild() -> dict[str, Any]:
    """全量重建：DROP 向量表 → 清空向量 → 逐案例重新切块与嵌入。

    维度变更后必须调用（虚拟表的维度写死在 DDL）。单个案例失败不中断整体。
    """
    total = await db.arun(_reset_index_sync)
    cfg = load_config()
    ids = await db.arun(_confirmed_ids_sync)
    if is_configured(cfg):
        ensure_ok = await db.arun(ensure_vec_table, int(cfg.dim or 0))
    else:
        ensure_ok = False

    ok = 0
    chunks = 0
    errors: list[str] = []
    for library_id in ids:
        try:
            result = await upsert(library_id)
        except Exception as exc:  # noqa: BLE001 —— 单案例失败不阻断重建
            logger.warning("案例 %s 重建失败：%s", library_id, exc)
            errors.append(f"{library_id}: {exc}")
            continue
        chunks += int(result.get("chunks") or 0)
        if result.get("embedded"):
            ok += 1

    if not is_configured(cfg):
        mode = "keyword"
        message = not_configured_message(cfg) + f"已重新切块 {chunks} 块，可用关键词检索。"
    elif ensure_ok and vec_table_dim():
        mode = "vector"
        message = f"向量库重建完成：{ok}/{len(ids)} 个案例、{chunks} 个检索块（维度 {cfg.dim}）。"
    else:
        mode = "brute"
        message = (
            f"sqlite-vec 不可用（{db.sqlite_vec_status.get('error') or '未加载'}），"
            f"已重建 {chunks} 个检索块的向量并使用纯 Python 余弦暴搜。"
        )
    return {
        "ok": not errors,
        "total": total,
        "cases": len(ids),
        "embedded_cases": ok,
        "chunks": chunks,
        "dim": int(cfg.dim or 0),
        "mode": mode,
        "vec_available": vec_available(),
        "errors": errors,
        "message": message,
    }


def stats_sync() -> dict[str, Any]:
    """索引状态摘要（/system/env 与前端诊断用）。"""
    def count(sql: str, params: Iterable[Any] = ()) -> int:
        row = db.query_one(sql, tuple(params))
        return int(row[0]) if row else 0

    return {
        "cases": count("SELECT COUNT(*) FROM oa_library"),
        "confirmed": count("SELECT COUNT(*) FROM oa_library WHERE status='confirmed'"),
        "chunks": count("SELECT COUNT(*) FROM oa_chunks"),
        "vectors": count(f"SELECT COUNT(*) FROM {BLOB_TABLE}"),
        "vec_available": vec_available(),
        "vec_table_dim": vec_table_dim(),
        "meta": get_meta(),
    }
