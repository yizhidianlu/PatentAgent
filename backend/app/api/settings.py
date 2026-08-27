"""设置相关 API：GET/PUT llm|embedding|image_gen|general、连接测试与向量索引重建。

掩码约定：GET/PUT 响应中的 api_key 一律掩码（sk-***尾4位）；
PUT 省略或提交空 api_key 表示"不修改已存 key"；
**回传掩码值（含 ***）同样视为"不修改"** —— 前端把 GET 到的掩码原样 PUT 回来
（用户只改了 model 等其它字段）是常态，不能把掩码当成真 key 存进去。
连接测试端点的请求体临时覆盖遵循同一规则。

权限（auth-system.md §4.3）：`llm` / `embedding` / `image_gen` 是**平台级**配置
（API Key 由管理员统一持有，成本由平台承担），整组收紧为管理员专属；
`general`（外观等）保持登录可读、管理员可写。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..db import database as db
from ..models.settings import (
    EmbeddingReindexResult,
    EmbeddingSettings,
    EmbeddingTestRequest,
    EmbeddingTestResult,
    EmbeddingUpdateResult,
    GeneralSettings,
    ImageGenSettings,
    ImageGenTestRequest,
    ImageGenTestResult,
    LlmSettings,
    LlmTestRequest,
    LlmTestResult,
    load_tolerant,
)
from ..services import llm as llm_service
from .deps import current_user, require_admin

router = APIRouter(prefix="/settings", tags=["设置"])

#: 平台级配置（模型服务 / 向量与检索 / 图像生成）统一挂管理员依赖
ADMIN_ONLY = [Depends(require_admin)]


def _load_llm() -> LlmSettings:
    return load_tolerant(LlmSettings, db.get_setting_json("llm"))


def _load_embedding() -> EmbeddingSettings:
    return load_tolerant(EmbeddingSettings, db.get_setting_json("embedding"))


def _load_image_gen() -> ImageGenSettings:
    return load_tolerant(ImageGenSettings, db.get_setting_json("image_gen"))


def _load_general() -> GeneralSettings:
    return load_tolerant(GeneralSettings, db.get_setting_json("general"))


def is_masked_key(value: str | None) -> bool:
    """是否为掩码回传值（mask_api_key 产出的形态一律含 ***）。"""
    return "***" in (value or "")


def _keep_stored_key(submitted: str | None, stored: str) -> str:
    """空值 / 掩码值 = 不修改，回落到已存 key。"""
    if not submitted or is_masked_key(submitted):
        return stored
    return submitted


def _clean_override(
    body: LlmTestRequest | EmbeddingTestRequest | ImageGenTestRequest | None,
) -> dict[str, Any] | None:
    """连接测试请求体 → 临时覆盖 dict：丢掉未填字段与掩码 api_key（回落已存配置）。"""
    if body is None:
        return None
    override = body.model_dump(exclude_none=True)
    if is_masked_key(override.get("api_key")) or override.get("api_key") == "":
        override.pop("api_key", None)
    return override or None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

@router.get("/llm", dependencies=ADMIN_ONLY, response_model=LlmSettings, summary="读取 LLM 配置（api_key 掩码）")
async def get_llm() -> LlmSettings:
    cfg = await db.arun(_load_llm)
    return cfg.masked()


@router.put("/llm", dependencies=ADMIN_ONLY, response_model=LlmSettings,
            summary="保存 LLM 配置（api_key 省略/为空/为掩码 = 不修改）")
async def put_llm(body: LlmSettings) -> LlmSettings:
    def op() -> LlmSettings:
        stored = _load_llm()
        merged = body.model_copy(update={"api_key": _keep_stored_key(body.api_key, stored.api_key)})
        db.set_setting_json("llm", merged.model_dump())
        return merged

    merged = await db.arun(op)
    return merged.masked()


@router.post("/llm/test", dependencies=ADMIN_ONLY, response_model=LlmTestResult,
             summary="用当前（或请求体临时）配置发一次最小 chat 测试连通性")
async def test_llm(body: LlmTestRequest | None = None) -> LlmTestResult:
    return await llm_service.test_llm(_clean_override(body))


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

@router.get("/embedding", dependencies=ADMIN_ONLY, response_model=EmbeddingSettings, summary="读取 Embedding 配置（api_key 掩码）")
async def get_embedding() -> EmbeddingSettings:
    cfg = await db.arun(_load_embedding)
    return cfg.masked()


@router.put("/embedding", dependencies=ADMIN_ONLY, response_model=EmbeddingUpdateResult,
            summary="保存 Embedding 配置（api_key 省略/为空/为掩码 = 不修改）；"
                    "dim 变化时返回 need_rebuild=true 提示需重建向量库")
async def put_embedding(body: EmbeddingSettings) -> EmbeddingUpdateResult:
    def op() -> tuple[EmbeddingSettings, bool]:
        stored = _load_embedding()
        merged = body.model_copy(update={"api_key": _keep_stored_key(body.api_key, stored.api_key)})
        need_rebuild = bool(stored.model) and stored.dim != merged.dim
        db.set_setting_json("embedding", merged.model_dump())
        return merged, need_rebuild

    merged, need_rebuild = await db.arun(op)
    return EmbeddingUpdateResult(settings=merged.masked(), need_rebuild=need_rebuild)


@router.post("/embedding/test", dependencies=ADMIN_ONLY, response_model=EmbeddingTestResult,
             summary="用当前（或临时）配置 embed(\"测试\") 测试连通性并返回维度")
async def test_embedding(body: EmbeddingTestRequest | None = None) -> EmbeddingTestResult:
    return await llm_service.test_embedding(_clean_override(body))


@router.post("/embedding/reindex", dependencies=ADMIN_ONLY, response_model=EmbeddingReindexResult,
             summary="重建向量索引：丢弃 oa_vec 虚拟表并把全部案例标记为待重新向量化")
async def reindex_embedding() -> EmbeddingReindexResult:
    def op() -> EmbeddingReindexResult:
        cfg = _load_embedding()
        vec_available = bool(db.sqlite_vec_status.get("loaded"))
        if vec_available:
            # 维度写死在虚拟表定义里，换维度必须 DROP 重建
            db.execute("DROP TABLE IF EXISTS oa_vec")
        db.execute("UPDATE oa_library SET embedded=0 WHERE embedded<>0")
        total_row = db.query_one("SELECT COUNT(*) AS n FROM oa_library")
        total = int(total_row["n"]) if total_row else 0
        message = (
            f"已标记 {total} 条案例待重新向量化（维度 {cfg.dim}）。"
            if vec_available
            else f"sqlite-vec 扩展不可用（{db.sqlite_vec_status.get('error') or '未加载'}），"
                 f"已标记 {total} 条案例待重新向量化，向量检索将在扩展就绪后生效。"
        )
        return EmbeddingReindexResult(
            ok=True, queued=total, dim=cfg.dim,
            vec_available=vec_available, message=message,
        )

    return await db.arun(op)


# ---------------------------------------------------------------------------
# 图像生成（附图 / 线稿）
# ---------------------------------------------------------------------------

@router.get("/image-gen", dependencies=ADMIN_ONLY, response_model=ImageGenSettings, summary="读取图像生成配置（api_key 掩码）")
async def get_image_gen() -> ImageGenSettings:
    cfg = await db.arun(_load_image_gen)
    return cfg.masked()


@router.put("/image-gen", dependencies=ADMIN_ONLY, response_model=ImageGenSettings,
            summary="保存图像生成配置（api_key 省略/为空/为掩码 = 不修改）")
async def put_image_gen(body: ImageGenSettings) -> ImageGenSettings:
    def op() -> ImageGenSettings:
        stored = _load_image_gen()
        merged = body.model_copy(update={"api_key": _keep_stored_key(body.api_key, stored.api_key)})
        db.set_setting_json("image_gen", merged.model_dump())
        return merged

    merged = await db.arun(op)
    return merged.masked()


@router.post("/image-gen/test", dependencies=ADMIN_ONLY, response_model=ImageGenTestResult,
             summary="用当前（或请求体临时）配置发一次最小 images 请求并回传缩略图")
async def test_image_gen(body: ImageGenTestRequest | None = None) -> ImageGenTestResult:
    return await llm_service.test_image_gen(_clean_override(body))


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------

@router.get("/general", dependencies=[Depends(current_user)], response_model=GeneralSettings, summary="读取通用设置")
async def get_general() -> GeneralSettings:
    return await db.arun(_load_general)


@router.put("/general", dependencies=ADMIN_ONLY, response_model=GeneralSettings, summary="保存通用设置")
async def put_general(body: GeneralSettings) -> GeneralSettings:
    await db.arun(db.set_setting_json, "general", body.model_dump())
    return body
