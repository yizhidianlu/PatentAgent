# -*- coding: utf-8 -*-
"""embedding 路径的错误翻译与维度指令。

机主在设置页点「测试」，看到的是一句原始的
`Error code: 429 - {'error': {'code': '1113', 'message': '余额不足或无可用资源包,请充值。'}}`
——今天为 chat 做的配额识别与人话翻译，embedding 一条都没沾上（它是独立调用路径）。

而这句原话今天已经被证伪过一次：它的真实含义可能是「走错端点」，也可能是
「订阅套餐不含这个模型」。实测（同一把 key，三个端点 × 两个 embedding 模型全 1113，
而它的对话模型在订阅端点完全正常）指向后者。两种含义处置完全不同，
所以提示里必须把可能性**和判据**一起给出来。
"""
from __future__ import annotations

import pytest

from app.services import vector

ZHIPU_1113 = (
    "Error code: 429 - {'error': {'code': '1113', "
    "'message': '余额不足或无可用资源包,请充值。'}}"
)
ZHIPU_CONCURRENCY = (
    'Error code: 429 - {"error": {"code": "1302", '
    '"message": "您当前使用该API的并发数过高,请降低并发或联系客服增加限额"}}'
)


class Cfg:
    def __init__(self, model: str = "embedding-3", dim: int = 1024):
        self.model = model
        self.dim = dim


# ---------------------------------------------------------------------------
# 错误翻译
# ---------------------------------------------------------------------------


def test_quota_error_names_both_causes_and_the_discriminator() -> None:
    """余额类报错必须给出两种成因**和区分它们的判据**。

    只说「余额不足」会把人送去充值——而真实成因可能是套餐不含该模型，
    充了也没用。今天这个坑已经踩过一次（chat 侧的 1113 其实是走错端点）。
    """
    hint = vector.embedding_error_hint(Cfg(), RuntimeError(ZHIPU_1113))
    assert "embedding-3" in hint, "要说清是哪个模型"
    assert "余额不足或无可用资源包" in hint, "服务端原话必须原样带上"
    assert "套餐" in hint, "必须提出「套餐不含该模型」这种可能"
    assert "判据" in hint, "两种成因处置不同，必须给出区分方法"
    assert "关闭向量检索" in hint, "必须给出一条不花钱的出路（退化为关键词检索）"


def test_rate_limit_is_not_confused_with_quota() -> None:
    """并发超限等一等就过，不该被说成余额问题。"""
    hint = vector.embedding_error_hint(Cfg(), RuntimeError(ZHIPU_CONCURRENCY))
    assert "限流" in hint
    assert "套餐" not in hint


def test_unknown_error_still_names_the_model_and_quotes_the_server() -> None:
    hint = vector.embedding_error_hint(Cfg(), RuntimeError("Connection reset by peer"))
    assert "embedding-3" in hint and "Connection reset" in hint


# ---------------------------------------------------------------------------
# 维度：让设置成为指令，而不是事后断言
# ---------------------------------------------------------------------------


def test_configured_dimension_is_requested_from_the_provider() -> None:
    """此前从不发 dimensions，「向量维度」只是个事后校验值——
    配 1024 而模型默认返回别的维度时，要等真正嵌入那一刻才报错。"""
    assert vector._dimension_kwargs(Cfg(dim=1024)) == {"dimensions": 1024}


def test_zero_dimension_sends_nothing() -> None:
    assert vector._dimension_kwargs(Cfg(dim=0)) == {}


def test_provider_rejecting_dimensions_is_learned_once() -> None:
    """不认这个参数的服务商：摘掉重发一次，并记住，不再每批白试。"""
    cfg = Cfg(model="no-dim-model", dim=1024)
    vector._NO_DIMENSIONS.discard(cfg.model)
    try:
        exc = RuntimeError("400 invalid_request_error: unknown parameter 'dimensions'")
        assert vector._drop_dimensions_on_error(cfg, exc) is True
        assert vector._dimension_kwargs(cfg) == {}, "学到之后不该再发"
        assert vector._drop_dimensions_on_error(cfg, exc) is False, "只该学一次"
    finally:
        vector._NO_DIMENSIONS.discard(cfg.model)


def test_unrelated_error_does_not_disable_dimensions() -> None:
    """余额类错误与 dimensions 无关：摘掉它重发只会白烧一次调用。"""
    cfg = Cfg(model="quota-model", dim=1024)
    vector._NO_DIMENSIONS.discard(cfg.model)
    try:
        assert vector._drop_dimensions_on_error(cfg, RuntimeError(ZHIPU_1113)) is False
        assert vector._dimension_kwargs(cfg) == {"dimensions": 1024}
    finally:
        vector._NO_DIMENSIONS.discard(cfg.model)


@pytest.mark.anyio
async def test_embed_without_model_is_still_a_clear_message() -> None:
    from app.db import database as db_mod

    db_mod.init_db()          # load_config 读 settings 表
    with pytest.raises(vector.EmbeddingNotConfiguredError) as info:
        await vector.embed(["x"], override={"model": "", "base_url": "", "api_key": ""})
    assert "设置页" in str(info.value)


# ---------------------------------------------------------------------------
# 测试端点：不能「测试通过」而生产必炸
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_connection_test_fails_when_measured_dim_differs(monkeypatch) -> None:
    """实测维度与设置不符 = 每次入库都会失败，测试就该红，而不是绿着放行。

    embedding-3 默认 2048 而设置里常见 1024。此前测试端点只回传实测维度、
    从不比对，于是「测试通过，2048 维」和存着的 1024 长期并存；真正的失败
    要等第一次入库才炸，而检索侧又把它静默降级成关键词——一个配置错误
    被伪装成两种互不相干的现象。
    """
    from app.db import database as db_mod
    from app.services import llm

    db_mod.init_db()

    class FakeEmbeddings:
        async def create(self, **kw):
            assert kw.get("dimensions") == 1024, "测试端点也得按设置索要维度"

            class D:
                embedding = [0.0] * 2048   # 服务商不理会 dimensions，仍返回默认维度

            class R:
                data = [D()]

            return R()

    class FakeClient:
        embeddings = FakeEmbeddings()

    monkeypatch.setattr(llm, "_client", lambda *a, **k: FakeClient())
    got = await llm.test_embedding(
        {"model": "embedding-3", "base_url": "https://x/v4", "api_key": "k"}
    )
    assert got.ok is False, "维度不符却报测试通过，会把排查引向完全错误的方向"
    assert got.dim == 2048
    assert "2048" in (got.error or "") and "1024" in (got.error or "")
    assert "重建" in (got.error or ""), "改维度要重建向量库，必须提醒"


@pytest.mark.anyio
async def test_connection_test_translates_quota_error(monkeypatch) -> None:
    """机主截图里那句原始 429 —— 测试端点是他真正会点的那个按钮。"""
    from app.db import database as db_mod
    from app.services import llm

    db_mod.init_db()

    class FakeEmbeddings:
        async def create(self, **kw):
            raise RuntimeError(ZHIPU_1113)

    class FakeClient:
        embeddings = FakeEmbeddings()

    monkeypatch.setattr(llm, "_client", lambda *a, **k: FakeClient())
    got = await llm.test_embedding(
        {"model": "embedding-3", "base_url": "https://x/v4", "api_key": "k"}
    )
    assert got.ok is False
    assert "套餐" in (got.error or ""), "原始 Error code 对用户毫无用处"


# ---------------------------------------------------------------------------
# base_url：照抄文档 curl 的完整 URL 不该换来一个无线索的 404
# ---------------------------------------------------------------------------


def test_embeddings_suffix_is_stripped_from_base_url() -> None:
    """SDK 总会自己拼 /embeddings，照抄完整 URL 会得到 /embeddings/embeddings。"""
    from app.models.settings import EmbeddingSettings

    for raw in (
        "https://open.bigmodel.cn/api/paas/v4/embeddings",
        "https://open.bigmodel.cn/api/paas/v4/embeddings/",
        "  https://open.bigmodel.cn/api/paas/v4/  ",
    ):
        assert (
            EmbeddingSettings(base_url=raw).base_url
            == "https://open.bigmodel.cn/api/paas/v4"
        ), raw


def test_normal_base_url_is_untouched() -> None:
    from app.models.settings import EmbeddingSettings

    assert EmbeddingSettings(base_url="https://x/v1").base_url == "https://x/v1"
    assert EmbeddingSettings().base_url == ""


# ---------------------------------------------------------------------------
# 长文本保护
# ---------------------------------------------------------------------------


def test_long_query_is_truncated_to_the_documented_limit() -> None:
    """embedding-3 单条上限 3072 tokens；入库分块本就短，长 query 此前无保护。"""
    assert vector.EMBED_MAX_CHARS <= 3072
    assert vector.EMBED_BATCH <= 64, "智谱单次数组上限 64 条"


def test_test_connection_validates_the_value_being_tested() -> None:
    """「测试连接」传的是表单里刚敲进去、还没保存的值——校验它正是这个按钮的意义。

    `model_copy(update=...)` 不跑校验器，于是后缀剥离曾经只在存盘后生效、
    在测试时不生效，恰好反了：照抄文档 curl 的人先点测试，拿到一个 404。
    """
    from app.db import database as db_mod
    from app.services import llm

    db_mod.init_db()
    cfg = llm.load_embedding_settings(
        {"base_url": "https://open.bigmodel.cn/api/paas/v4/embeddings"}
    )
    assert cfg.base_url == "https://open.bigmodel.cn/api/paas/v4"
