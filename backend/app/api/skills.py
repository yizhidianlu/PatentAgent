# -*- coding: utf-8 -*-
"""技能库 API。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/skills` | 全部技能 + 实时可用性 + 当前启用状态 |
| PUT | `/skills/{key}` | 开关某项技能 |

权限：技能开关影响的是**平台行为**（会不会联网检索、会不会调图像模型花钱），
与模型配置同属平台级设置，因此写操作要求管理员；读取对所有登录用户开放——
普通用户需要知道自己能用什么、为什么某项不可用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..db import database as db
from ..models.skill import SkillListOut, SkillOut, SkillToggleIn
from ..services import auth as auth_service
from ..services import skills as skills_service
from .deps import client_ip, current_user, require_admin

router = APIRouter(prefix="/skills", tags=["技能库"])


@router.get("", response_model=SkillListOut, summary="技能库列表")
async def list_skills(
    user: dict[str, Any] = Depends(current_user),
) -> SkillListOut:
    """列出全部技能。

    每次都实时探测前置条件（是否配了模型、本机有没有 Chrome/Word），
    不做缓存——探测都是本地文件与 DB 读取，开销可忽略，而缓存会让用户
    「刚在设置页配好模型、回到技能库还显示不可用」。
    """
    def _run() -> SkillListOut:
        return SkillListOut(
            skills=skills_service.list_skills(),
            categories=skills_service.categories(),
        )

    return await db.arun(_run)


@router.put("/{key}", response_model=SkillOut, summary="开关技能")
async def toggle_skill(
    key: str,
    body: SkillToggleIn,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> SkillOut:
    def _run() -> SkillOut:
        try:
            skills_service.set_enabled(key, body.enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"技能「{key}」不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        auth_service.audit(
            "skill_toggled",
            actor_id=admin["id"], actor_name=admin["username"],
            detail={"skill": key, "enabled": body.enabled},
            ip=client_ip(request),
        )
        updated = next((s for s in skills_service.list_skills() if s.key == key), None)
        if updated is None:  # 理论上不可达：set_enabled 已校验过 key
            raise HTTPException(status_code=404, detail=f"技能「{key}」不存在")
        return updated

    return await db.arun(_run)
