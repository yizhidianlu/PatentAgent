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

from fastapi import APIRouter, Depends, Request, HTTPException

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
    ModelTiersSettings,
    ModelTiersOut,
)
from ..services import llm as llm_service
from ..services import auth as auth_service
from .deps import client_ip, current_user, require_admin

router = APIRouter(prefix="/settings", tags=["设置"])

#: 平台级配置（模型服务 / 向量与检索 / 图像生成）统一挂管理员依赖
ADMIN_ONLY = [Depends(require_admin)]


def _audit_settings(
    admin: dict[str, Any],
    request: Request,
    section: str,
    detail: dict[str, Any],
) -> None:
    """记一条平台级设置变更。

    detail 只放「改成了什么形状」——模型名、开关、引擎选择这类，
    **绝不放 api_key**：审计日志的查看门槛比设置页低（管理员都能翻历史），
    把密钥写进去等于给它开第二个出口。密钥只留「是否被改过」这一个布尔。
    """
    auth_service.audit(
        "settings_updated",
        actor_id=admin["id"],
        actor_name=admin["username"],
        target_type="settings",
        target_id=section,
        detail=detail,
        ip=client_ip(request),
    )


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


async def _audit_test_target(
    admin: dict[str, Any],
    request: Request,
    section: str,
    override: dict[str, Any] | None,
    stored_base_url: str,
) -> None:
    """测试目标与已存地址不同 host 时留一条痕。

    密钥不写（与设置审计同一原则）。这条的作用是让「把密钥往外送」的尝试
    **可见**——否则只能靠代码审计才能发现，而那时东西已经出去了。
    """
    target = str((override or {}).get("base_url") or "").strip()
    if not target or _same_host(target, stored_base_url):
        return
    await db.arun(
        auth_service.audit,
        "settings_test_external",
        actor_id=admin["id"],
        actor_name=admin["username"],
        target_type="settings",
        target_id=section,
        detail={"base_url": target, "stored_base_url": stored_base_url},
        ip=client_ip(request),
    )


def _same_host(a: str, b: str) -> bool:
    """两个 base_url 是不是同一个服务地址（只比 host:port，忽略路径）。

    按 host 而不是完整 URL 判断，是因为改路径是常见的合法操作
    （/v1 → /v1beta、加版本号），而改 host 才意味着「换了收密钥的人」。
    """
    from urllib.parse import urlsplit

    try:
        pa, pb = urlsplit((a or "").strip()), urlsplit((b or "").strip())
    except ValueError:
        return False
    if not pa.netloc or not pb.netloc:
        return False
    return pa.netloc.lower() == pb.netloc.lower()


def _clean_override(
    body: LlmTestRequest | EmbeddingTestRequest | ImageGenTestRequest | None,
    stored_base_url: str = "",
) -> dict[str, Any] | None:
    """连接测试请求体 → 临时覆盖 dict。

    **不变式：库里的密钥只能发往库里的目的地。**

    掩码 / 空 api_key 表示「沿用已存的那把」——这个约定对 PUT 是对的，
    因为 PUT 时目的地也一并落库，密钥与目的地始终配对。但 test 不同：
    目的地是临时的、且完全由请求方决定，配对关系在这里断了。

    于是只要提交 `{base_url: "https://attacker/v1", api_key: "<界面上拿到的掩码>"}`，
    平台就会把真实明文密钥当 Bearer 发给对方——**攻击者不需要构造任何异常输入，
    用界面本身的数据流就能触发**。

    所以：换了 host 就必须自带真实密钥，不允许回落。
    同 host 改路径/端口仍可回落，那是真实存在的用法（同一供应商换版本）。
    """
    if body is None:
        return None
    override = body.model_dump(exclude_none=True)

    supplied = override.get("api_key")
    falls_back = is_masked_key(supplied) or supplied == "" or supplied is None
    if falls_back:
        override.pop("api_key", None)
        target = str(override.get("base_url") or "").strip()
        if target and stored_base_url and not _same_host(target, stored_base_url):
            raise HTTPException(
                status_code=400,
                detail=(
                    "测试新的服务地址时必须同时填写该地址的 API Key。"
                    "已保存的密钥只会发往已保存的服务地址。"
                ),
            )
    return override or None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

@router.get("/llm", dependencies=ADMIN_ONLY, response_model=LlmSettings, summary="读取 LLM 配置（api_key 掩码）")
async def get_llm() -> LlmSettings:
    cfg = await db.arun(_load_llm)
    return cfg.masked()


@router.put("/llm", response_model=LlmSettings,
            summary="保存 LLM 配置（api_key 省略/为空/为掩码 = 不修改）")
async def put_llm(
    body: LlmSettings,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> LlmSettings:
    stored_key_before = (await db.arun(_load_llm)).api_key

    def op() -> LlmSettings:
        stored = _load_llm()
        merged = body.model_copy(update={"api_key": _keep_stored_key(body.api_key, stored.api_key)})
        db.set_setting_json("llm", merged.model_dump())
        return merged

    merged = await db.arun(op)
    # 只留「密钥是否被改过」，不留密钥本身
    await db.arun(
        _audit_settings, admin, request, "llm",
        {
            "model": merged.model,
            "base_url": merged.base_url,
            "api_key_changed": bool(body.api_key and body.api_key != stored_key_before),
            "supports_json_mode": merged.supports_json_mode,
        },
    )
    return merged.masked()


@router.post("/llm/test", dependencies=ADMIN_ONLY, response_model=LlmTestResult,
             summary="用当前（或请求体临时）配置发一次最小 chat 测试连通性")
async def test_llm(
    request: Request,
    body: LlmTestRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> LlmTestResult:
    stored = await db.arun(_load_llm)
    override = _clean_override(body, stored.base_url)
    await _audit_test_target(admin, request, "llm", override, stored.base_url)
    return await llm_service.test_llm(override)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

@router.get("/embedding", dependencies=ADMIN_ONLY, response_model=EmbeddingSettings, summary="读取 Embedding 配置（api_key 掩码）")
async def get_embedding() -> EmbeddingSettings:
    cfg = await db.arun(_load_embedding)
    return cfg.masked()


@router.put("/embedding", response_model=EmbeddingUpdateResult,
            summary="保存 Embedding 配置（api_key 省略/为空/为掩码 = 不修改）；"
                    "dim 变化时返回 need_rebuild=true 提示需重建向量库")
async def put_embedding(
    body: EmbeddingSettings,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> EmbeddingUpdateResult:
    def op() -> tuple[EmbeddingSettings, bool]:
        stored = _load_embedding()
        merged = body.model_copy(update={"api_key": _keep_stored_key(body.api_key, stored.api_key)})
        need_rebuild = bool(stored.model) and stored.dim != merged.dim
        db.set_setting_json("embedding", merged.model_dump())
        return merged, need_rebuild

    merged, need_rebuild = await db.arun(op)
    await db.arun(
        _audit_settings, admin, request, "embedding",
        {"model": merged.model, "enabled": merged.enabled,
         "dim": merged.dim, "need_rebuild": need_rebuild},
    )
    return EmbeddingUpdateResult(settings=merged.masked(), need_rebuild=need_rebuild)


@router.post("/embedding/test", dependencies=ADMIN_ONLY, response_model=EmbeddingTestResult,
             summary="用当前（或临时）配置 embed(\"测试\") 测试连通性并返回维度")
async def test_embedding(
    request: Request,
    body: EmbeddingTestRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> EmbeddingTestResult:
    stored = await db.arun(_load_embedding)
    override = _clean_override(body, stored.base_url)
    await _audit_test_target(admin, request, "embedding", override, stored.base_url)
    return await llm_service.test_embedding(override)


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


@router.put("/image-gen", response_model=ImageGenSettings,
            summary="保存图像生成配置（api_key 省略/为空/为掩码 = 不修改）")
async def put_image_gen(
    body: ImageGenSettings,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> ImageGenSettings:
    def op() -> ImageGenSettings:
        stored = _load_image_gen()
        merged = body.model_copy(update={"api_key": _keep_stored_key(body.api_key, stored.api_key)})
        db.set_setting_json("image_gen", merged.model_dump())
        return merged

    merged = await db.arun(op)
    await db.arun(
        _audit_settings, admin, request, "image_gen",
        {"enabled": merged.enabled, "model": merged.model,
         "provider": merged.provider, "size": merged.size},
    )
    return merged.masked()


@router.post("/image-gen/test", dependencies=ADMIN_ONLY, response_model=ImageGenTestResult,
             summary="用当前（或请求体临时）配置发一次最小 images 请求并回传缩略图")
async def test_image_gen(
    request: Request,
    body: ImageGenTestRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> ImageGenTestResult:
    stored = await db.arun(_load_image_gen)
    override = _clean_override(body, stored.base_url)
    await _audit_test_target(admin, request, "image_gen", override, stored.base_url)
    return await llm_service.test_image_gen(override)


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 模型档位（快速 / 深度思考）
# ---------------------------------------------------------------------------

def _load_model_tiers() -> ModelTiersSettings:
    return load_tolerant(ModelTiersSettings, db.get_setting_json("model_tiers") or {})


@router.get("/model-tiers", dependencies=[Depends(current_user)],
            response_model=ModelTiersOut, summary="读取两档模型配置（含各档实际生效的模型名）")
async def get_model_tiers() -> ModelTiersOut:
    def op() -> ModelTiersOut:
        tiers = _load_model_tiers()
        base = load_tolerant(LlmSettings, db.get_setting_json("llm") or {})
        # 把「实际会用哪个模型」一并回出去：档位留空时回落主配置，
        # 界面若只显示用户填的空值，用户会以为这一档没生效
        return ModelTiersOut(
            fast=tiers.fast,
            deep=tiers.deep,
            default_tier=tiers.default_tier,
            base_model=base.model,
            effective={
                "fast": tiers.fast.model.strip() or base.model,
                "deep": tiers.deep.model.strip() or base.model,
            },
        )

    return await db.arun(op)


@router.put("/model-tiers", response_model=ModelTiersOut,
            summary="保存两档模型配置（留空的字段沿用主配置）")
async def put_model_tiers(
    body: ModelTiersSettings,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> ModelTiersOut:
    await db.arun(db.set_setting_json, "model_tiers", body.model_dump())
    await db.arun(
        _audit_settings, admin, request, "model_tiers",
        {
            "fast_model": body.fast.model,
            "deep_model": body.deep.model,
            "default_tier": body.default_tier,
        },
    )
    return await get_model_tiers()

@router.get("/general", dependencies=[Depends(current_user)], response_model=GeneralSettings, summary="读取通用设置")
async def get_general() -> GeneralSettings:
    return await db.arun(_load_general)


@router.put("/general", response_model=GeneralSettings, summary="保存通用设置")
async def put_general(
    body: GeneralSettings,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> GeneralSettings:
    await db.arun(db.set_setting_json, "general", body.model_dump())
    await db.arun(
        _audit_settings, admin, request, "general",
        {k: v for k, v in body.model_dump().items() if k != "api_key"},
    )
    return body
