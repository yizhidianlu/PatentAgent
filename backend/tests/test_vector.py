"""OA 案例库检索层测试（services/vector）：三级降级链、维度变更重建、元数据预过滤。

不依赖真实 embedding 服务：把 `services/vector.embed` 换成**确定性**的词袋向量
（维度 = 1 个常数分量 + 7 个关键词计数分量），于是同一段文本永远得到同一个向量，
余弦排序完全可预期；其余环节（切块、写 oa_vec_blob、sqlite-vec vec0 KNN、暴搜、
关键词打分、元数据预过滤）全部真跑。

三级降级链（backend-architecture.md §6 风险 4）：

| 条件 | 断言的 `mode` |
|---|---|
| embedding 已配置 + sqlite-vec 可用 + 索引就绪 | `vector` |
| embedding 已配置，但扩展加载失败（monkeypatch `sqlite_vec_status.loaded=False`） | `brute` |
| 未配置 embedding（设置页未启用 / 无模型） | `keyword` |
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 确定性 embedding
# ---------------------------------------------------------------------------

#: 词袋维度：第 0 维恒为 0.5（避免零向量），其余为关键词计数
VOCAB: tuple[str, ...] = (
    "创造性", "新颖性", "超声", "图像", "修改权利要求", "公开不充分", "补正",
)
DIM = len(VOCAB) + 1
DIM2 = DIM * 2  # 「换模型换维度」用的第二套维度


def bag_vector(text: str, dim: int = DIM) -> list[float]:
    """词袋向量：[0.5, 各关键词出现次数…]，右侧补零到 dim（确定性、可人工推演）。"""
    vec = [0.5] + [float((text or "").count(word)) for word in VOCAB]
    if len(vec) < dim:
        vec.extend([0.0] * (dim - len(vec)))
    return vec[:dim]


def make_fake_embed(dim: int = DIM):
    """构造 `vector.embed` 的替身（异步，签名与真身一致）。"""

    async def fake_embed(texts: Sequence[str], **_: Any) -> list[list[float]]:
        return [bag_vector(str(t or ""), dim) for t in texts]

    return fake_embed


# ---------------------------------------------------------------------------
# 案例语料
# ---------------------------------------------------------------------------

NOTE_INVENTIVENESS = """## 通知书要点
审查员依据专利法第22条第3款指出权利要求1相对于对比文件1不具备创造性，
认为区别特征属于本领域惯用技术手段，超声图像的处理方式已被对比文件公开。

## 答复策略
本案采用修改权利要求的方式答复，将说明书中记载的超像素邻接图特征补入权利要求1，
并陈述该区别特征带来的预料不到的技术效果。创造性论辩围绕三步法展开。

## 结果
经修改后授权。
"""

NOTE_CLARITY = """## 通知书要点
审查员依据专利法第26条第4款指出权利要求2的用语不清楚，
所述阈值范围在说明书中缺少对应记载，属于公开不充分的边缘情形。

## 答复策略
本案通过修改权利要求删除不清楚的用语，并补充说明书中已记载的取值依据。

## 结果
经修改后授权。
"""

NOTE_FORMALITY = """## 通知书要点
补正通知书指出说明书附图标记与正文不一致，属于形式缺陷，需要补正。

## 答复策略
提交补正书统一附图标记，未涉及实质性修改。

## 结果
补正后继续审查。
"""


def frontmatter(**over: Any) -> dict[str, Any]:
    """一份合法的案例 frontmatter（枚举值取自 oa_case.schema.yaml）。"""
    data: dict[str, Any] = {
        "case_id": "vec-case",
        "title": "向量检索测试案例",
        "patent_type": "invention",
        "statutes": ["专利法第22条第3款"],
        "defect_types": ["inventiveness"],
        "domain": "医学影像",
        "notice_kind": "office_action",
        "outcome": "amended_then_granted",
        "strategy": ["amend_claims"],
        "tags": ["超声", "分割"],
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _wipe_library() -> None:
    """清空案例库与检索层（含 vec0 虚拟表），保证各用例互不干扰。"""
    from app.db import database as db
    from app.services import vector

    if vector.vec_available():
        db.execute(f"DROP TABLE IF EXISTS {vector.VEC_TABLE}")
    db.execute(f"DELETE FROM {vector.BLOB_TABLE}")
    db.execute("DELETE FROM oa_chunks")
    db.execute("DELETE FROM oa_library")
    db.execute("DELETE FROM oa_vec_meta")


def set_embedding(*, enabled: bool, dim: int = DIM, model: str = "fake-embed") -> None:
    """写设置页的 embedding 配置（is_configured 只看 enabled + model）。"""
    from app.db import database as db

    db.set_setting_json(
        "embedding",
        {
            "enabled": enabled, "provider": "custom", "base_url": "http://localhost/v1",
            "api_key": "test", "model": model if enabled else "", "dim": dim,
        },
    )


@pytest.fixture(autouse=True)
def clean_library():
    """每个用例前后清空案例库；用例结束恢复原 embedding 设置。"""
    from app.db import database as db

    db.init_db()
    original = db.get_setting_json("embedding")
    _wipe_library()
    yield
    _wipe_library()
    if original is None:
        db.execute("DELETE FROM settings WHERE key='embedding'")
    else:
        db.set_setting_json("embedding", original)


async def add_case(note_md: str, *, status: str = "confirmed", **fm: Any) -> str:
    """新建一条案例（默认 confirmed → 立即切块 + 向量化）；返回 library_id。"""
    from app.services import oa_library

    item, _notes = await oa_library.create(frontmatter(**fm), note_md, status=status)
    return item.id


def vec_available() -> bool:
    from app.services import vector

    return vector.vec_available()


# ---------------------------------------------------------------------------
# 1. 三级降级链
# ---------------------------------------------------------------------------


async def test_mode_keyword_without_embedding() -> None:
    """未配置 embedding → mode=keyword，仍能靠关键词命中已确认案例。"""
    from app.services import vector

    set_embedding(enabled=False)
    lib_id = await add_case(NOTE_INVENTIVENESS, case_id="kw-inventiveness")

    result = await vector.search("创造性 专利法第22条第3款 区别特征属于惯用手段", k=5)

    assert result["mode"] == "keyword"
    assert "Embedding" in result["message"]          # 明确告知降级原因
    assert result["total_candidates"] >= 1
    assert [h["library_id"] for h in result["hits"]] == [lib_id]
    assert result["hits"][0]["score"] > 0
    assert result["hits"][0]["frontmatter"]["case_id"] == "kw-inventiveness"


async def test_mode_keyword_on_empty_library() -> None:
    """空库不报错：mode=keyword、hits 为空并说明「没有已确认的案例」。"""
    from app.services import vector

    set_embedding(enabled=False)
    result = await vector.search("创造性", k=5)

    assert result["mode"] == "keyword"
    assert result["hits"] == []
    assert "没有已确认的案例" in result["message"]


async def test_mode_vector_with_sqlite_vec(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlite-vec 可用 + 已建索引 → mode=vector，且按余弦相似度排序。"""
    from app.services import vector

    if not vec_available():
        pytest.skip(f"本机 sqlite-vec 扩展不可用：{vector.db.sqlite_vec_status.get('error')}")

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    set_embedding(enabled=True, dim=DIM)

    inventive = await add_case(NOTE_INVENTIVENESS, case_id="vec-inventiveness")
    formal = await add_case(
        NOTE_FORMALITY, case_id="vec-formality",
        statutes=["专利法实施细则第23条"], defect_types=["formality"],
        notice_kind="correction", tags=["补正"],
    )

    assert vector.vec_table_dim() == DIM
    assert vector.get_meta()["dim"] == DIM

    result = await vector.search("创造性 创造性 超声 图像", k=5)

    assert result["mode"] == "vector"
    assert result["hits"], result
    assert result["hits"][0]["library_id"] == inventive     # 创造性案例排第一
    assert formal in {h["library_id"] for h in result["hits"]}
    assert 0.0 <= result["hits"][0]["score"] <= 1.0
    assert result["hits"][0]["score"] > result["hits"][-1]["score"]


async def test_mode_brute_when_extension_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlite-vec 加载失败 → 向量仍写 oa_vec_blob，检索降级为纯 Python 余弦暴搜。"""
    from app.db import database as db
    from app.services import vector

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    monkeypatch.setitem(db.sqlite_vec_status, "loaded", False)
    monkeypatch.setitem(db.sqlite_vec_status, "error", "扩展加载失败: 测试模拟")
    set_embedding(enabled=True, dim=DIM)

    inventive = await add_case(NOTE_INVENTIVENESS, case_id="brute-inventiveness")
    await add_case(
        NOTE_FORMALITY, case_id="brute-formality",
        statutes=["专利法实施细则第23条"], defect_types=["formality"],
        notice_kind="correction",
    )

    assert vector.vec_table_dim() is None                   # 扩展不可用 → 没有 vec0 表
    blobs = db.query_all(f"SELECT chunk_id, dim FROM {vector.BLOB_TABLE}")
    assert blobs and all(int(r["dim"]) == DIM for r in blobs)   # 向量不因扩展缺失而丢失

    result = await vector.search("创造性 创造性 超声 图像", k=5)

    assert result["mode"] == "brute"
    assert "暴搜" in result["message"]
    assert result["hits"][0]["library_id"] == inventive
    assert 0.0 < result["hits"][0]["score"] <= 1.0


async def test_mode_keyword_when_embed_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding 接口报错 → 不抛异常，降级关键词并把错误如实透出。"""
    from app.services import vector

    set_embedding(enabled=True, dim=DIM)
    lib_id = await add_case(NOTE_INVENTIVENESS, case_id="fallback-case", status="confirmed")

    async def boom(_texts: Sequence[str], **_: Any) -> list[list[float]]:
        raise vector.EmbeddingCallError("Embedding 调用失败（模型 fake-embed）：连接被拒绝")

    monkeypatch.setattr(vector, "embed", boom)
    result = await vector.search("创造性 超声 图像", k=5)

    assert result["mode"] == "keyword"
    assert "连接被拒绝" in result["error"]
    assert [h["library_id"] for h in result["hits"]] == [lib_id]


# ---------------------------------------------------------------------------
# 2. 维度变更与重建
# ---------------------------------------------------------------------------


async def test_dim_change_degrades_then_rebuild_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    """维度变更：旧向量作废 → 先降级关键词并提示 rebuild → rebuild 后恢复向量检索。"""
    from app.db import database as db
    from app.services import vector

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    set_embedding(enabled=True, dim=DIM)
    lib_id = await add_case(NOTE_INVENTIVENESS, case_id="dim-case")
    expected_mode = "vector" if vec_available() else "brute"
    assert (await vector.search("创造性 超声", k=3))["mode"] == expected_mode

    # 换模型 → 换维度：旧 blob 维度不匹配，本次检索必须降级且明确提示重建
    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM2))
    set_embedding(enabled=True, dim=DIM2, model="fake-embed-large")

    degraded = await vector.search("创造性 超声", k=3)
    assert degraded["mode"] == "keyword"
    assert "rebuild" in degraded["message"]

    report = await vector.rebuild()

    assert report["ok"] is True
    assert report["cases"] == 1
    assert report["chunks"] >= 1
    assert report["dim"] == DIM2
    assert report["mode"] == expected_mode
    if vec_available():
        assert vector.vec_table_dim() == DIM2
    blobs = db.query_all(f"SELECT dim FROM {vector.BLOB_TABLE}")
    assert blobs and all(int(r["dim"]) == DIM2 for r in blobs)

    restored = await vector.search("创造性 超声", k=3)
    assert restored["mode"] == expected_mode
    assert [h["library_id"] for h in restored["hits"]] == [lib_id]


def test_ensure_vec_table_recreates_on_dim_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_vec_table：维度不同即 DROP 重建，并作废旧维度向量、复位 embedded 标记。"""
    from app.db import database as db
    from app.services import vector

    if not vec_available():
        pytest.skip("本机 sqlite-vec 扩展不可用")

    assert vector.ensure_vec_table(DIM) is True
    assert vector.vec_table_dim() == DIM

    now = db.now_str()
    db.execute("INSERT INTO oa_library(id, case_note_md, frontmatter_json, status, embedded, "
               "created_at, updated_at) VALUES ('L1','正文','{}','confirmed',1,?,?)", (now, now))
    db.execute("INSERT INTO oa_chunks(id, library_id, chunk_index, text) VALUES (1,'L1',0,'块')")
    db.execute(
        f"INSERT INTO {vector.BLOB_TABLE}(chunk_id, library_id, dim, embedding, model, created_at) "
        f"VALUES (1,'L1',?,?,'m',?)",
        (DIM, vector.pack_vector([0.0] * DIM), now),
    )

    assert vector.ensure_vec_table(DIM2) is True
    assert vector.vec_table_dim() == DIM2
    assert db.query_all(f"SELECT chunk_id FROM {vector.BLOB_TABLE} WHERE dim=?", (DIM,)) == []
    assert int(db.query_one("SELECT embedded FROM oa_library WHERE id='L1'")["embedded"]) == 0


# ---------------------------------------------------------------------------
# 3. 元数据预过滤
# ---------------------------------------------------------------------------


async def test_metadata_prefilter(monkeypatch: pytest.MonkeyPatch) -> None:
    """statutes / defect_types / patent_type / tags 预过滤先于向量检索生效。"""
    from app.services import vector

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    set_embedding(enabled=True, dim=DIM)

    inventive = await add_case(NOTE_INVENTIVENESS, case_id="filter-inventive")
    clarity = await add_case(
        NOTE_CLARITY, case_id="filter-clarity",
        statutes=["专利法第26条第4款"], defect_types=["support"], tags=["权项"],
    )
    utility = await add_case(
        NOTE_FORMALITY, case_id="filter-utility", patent_type="utility_model",
        statutes=["专利法实施细则第23条"], defect_types=["formality"], tags=["补正"],
    )
    query = "创造性 超声 图像 修改权利要求"

    def ids(result: dict[str, Any]) -> set[str]:
        return {h["library_id"] for h in result["hits"]}

    assert ids(await vector.search(query, k=10)) == {inventive, clarity, utility}

    only_statute = await vector.search(query, k=10, filters={"statutes": ["专利法第26条第4款"]})
    assert ids(only_statute) == {clarity}

    only_defect = await vector.search(query, k=10, filters={"defect_types": ["inventiveness"]})
    assert ids(only_defect) == {inventive}

    only_invention = await vector.search(query, k=10, filters={"patent_type": "invention"})
    assert ids(only_invention) == {inventive, clarity}
    assert only_invention["total_candidates"] < (await vector.search(query, k=10))["total_candidates"]

    only_tag = await vector.search(query, k=10, filters={"tags": ["补正"]})
    assert ids(only_tag) == {utility}

    # 过滤条件互斥 → 零候选，但接口照常返回（mode 仍明示，不抛异常）
    none_hit = await vector.search(
        query, k=10, filters={"statutes": ["专利法第26条第4款"], "defect_types": ["inventiveness"]}
    )
    assert none_hit["hits"] == []
    assert none_hit["total_candidates"] == 0
    assert none_hit["mode"] in ("vector", "brute")


async def test_prefilter_expands_extended_defect_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台扩充缺陷类型（added_matter…）经 DEFECT_TYPE_TO_BASE 归并后仍能命中 other 案例。"""
    from app.services import vector

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    set_embedding(enabled=True, dim=DIM)
    other_case = await add_case(NOTE_CLARITY, case_id="filter-other", defect_types=["other"])

    hit = await vector.search("修改超范围 修改权利要求", k=5, filters={"defect_types": ["added_matter"]})
    assert {h["library_id"] for h in hit["hits"]} == {other_case}

    miss = await vector.search("新颖性", k=5, filters={"defect_types": ["novelty"]})
    assert miss["hits"] == []


async def test_only_confirmed_cases_are_searchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """人审闸门：draft 案例不切块、不进检索库；确认后才可检索，回落 draft 再次清空。"""
    from app.db import database as db
    from app.services import oa_library, vector

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    set_embedding(enabled=True, dim=DIM)

    draft_id = await add_case(NOTE_INVENTIVENESS, status="draft", case_id="gate-case")
    assert db.query_all("SELECT id FROM oa_chunks WHERE library_id=?", (draft_id,)) == []
    assert (await vector.search("创造性 超声", k=5))["hits"] == []

    await oa_library.update(draft_id, status="confirmed")
    confirmed = await vector.search("创造性 超声", k=5)
    assert {h["library_id"] for h in confirmed["hits"]} == {draft_id}
    assert (await oa_library.get(draft_id)).embedded is True

    await oa_library.update(draft_id, status="draft")
    assert db.query_all("SELECT id FROM oa_chunks WHERE library_id=?", (draft_id,)) == []
    assert (await vector.search("创造性 超声", k=5))["hits"] == []


async def test_delete_clears_chunks_and_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """删除案例连带清理检索块与向量。"""
    from app.db import database as db
    from app.services import oa_library, vector

    monkeypatch.setattr(vector, "embed", make_fake_embed(DIM))
    set_embedding(enabled=True, dim=DIM)
    lib_id = await add_case(NOTE_INVENTIVENESS, case_id="del-case")
    assert db.query_all("SELECT id FROM oa_chunks WHERE library_id=?", (lib_id,))

    await oa_library.delete(lib_id)

    assert db.query_all("SELECT id FROM oa_chunks WHERE library_id=?", (lib_id,)) == []
    assert db.query_all(f"SELECT chunk_id FROM {vector.BLOB_TABLE} WHERE library_id=?", (lib_id,)) == []
    assert db.query_one("SELECT id FROM oa_library WHERE id=?", (lib_id,)) is None


# ---------------------------------------------------------------------------
# 4. 切块与打分的确定性部件
# ---------------------------------------------------------------------------


def test_split_note_drops_frontmatter_and_keeps_section_titles() -> None:
    """切块：剥 frontmatter 与导航行、块内带小节标题、长度落在约定区间。"""
    from app.services import vector

    note = "---\ncase_id: x\ntitle: y\n---\n\n> 导航：返回案例库\n\n" + NOTE_INVENTIVENESS
    chunks = vector.split_note(note)

    assert chunks
    assert all("case_id: x" not in c for c in chunks)
    assert all("导航" not in c for c in chunks)
    assert any("【通知书要点】" in c for c in chunks)
    assert all(len(c) <= vector.MAX_CHUNK_CHARS + vector.MIN_CHUNK_CHARS for c in chunks)


def test_keyword_scoring_prefers_more_hits() -> None:
    """关键词打分：命中词更多的文本得分更高，无命中为 0。"""
    from app.services import vector

    terms = vector.keyword_terms("创造性 专利法第22条第3款")
    assert "创造性" in terms
    assert vector.keyword_score("本案争议焦点为创造性，涉及专利法第22条第3款", terms) > \
        vector.keyword_score("本案涉及创造性判断", terms)
    assert vector.keyword_score("与本案无关的文本", terms) == 0.0


def test_normalized_vector_score_matches_cosine() -> None:
    """归一化后 L2 距离 → 余弦的换算口径（vector / brute 两条路径打分一致）。"""
    import math

    from app.services import vector

    a = vector.normalize_vector([1.0, 2.0, 3.0, 4.0])
    b = vector.normalize_vector([1.0, 2.0, 3.0, 5.0])
    distance = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    assert vector._distance_to_score(distance) == pytest.approx(vector.cosine(a, b), abs=1e-6)
    assert vector.unpack_vector(vector.pack_vector(a), len(a)) == pytest.approx(a, abs=1e-6)
