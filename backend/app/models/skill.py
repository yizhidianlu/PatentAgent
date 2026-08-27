# -*- coding: utf-8 -*-
"""技能库的数据契约。

「技能」= 平台支持的一项可选能力，用户写专利时勾选启用。

**技能定义是代码，不是数据。**
skills 表只存「谁开了什么、配了什么」，技能本身（做什么、怎么调）写在
services/skills/registry.py 的注册表里。这条边界不能松：技能一旦可由用户
上传或编辑，就等于开了一个任意代码执行的口子——而这个平台里跑着别人的
专利交底书。外部开源工程要接进来，走的是「维护端写一个适配器 + 声明许可证」
这条路，不是「用户粘一段脚本」。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 技能分类。面向用户的说法，不是代码模块名。
SkillCategory = Literal[
    "search",      # 检索查新
    "drawing",     # 附图与图形
    "writing",     # 撰写与润色
    "validation",  # 校验与自检
    "export",      # 导出与交付
]

# 技能当前能不能用。
#   available    —— 开箱可用
#   needs_config —— 依赖尚未配置（如未配图像模型），前端要引导去设置页
#   unavailable  —— 本机环境不具备（如没装 Word / 没装 Chrome），且用户自己解决不了
SkillStatus = Literal["available", "needs_config", "unavailable"]

# 适用模块，与 cases.module 取值一致
SkillModule = Literal["disclosure", "paper2patent", "reader", "oa"]

# 适用专利类型，与 cases.patent_type 取值一致
SkillPatentType = Literal["invention", "utility_model", "design"]


class SkillRequirement(BaseModel):
    """技能的一项前置条件，以及它当前满不满足。"""

    model_config = ConfigDict(extra="ignore")

    key: str                      # 机器可读标识，如 image_gen / chrome / winword
    label: str                    # 面向用户的说法，如「已配置图像生成模型」
    satisfied: bool
    hint: str = ""                # 不满足时告诉用户怎么办；能给出设置页路径就给
    settings_path: str | None = None   # 可直接跳转的前端路径，如 /settings


class SkillOut(BaseModel):
    """技能库列表项。"""

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str
    category: SkillCategory
    summary: str                  # 一句话，列表卡片上显示
    description: str = ""         # 详情，展开后显示
    modules: list[SkillModule] = Field(default_factory=list)
    patent_types: list[SkillPatentType] = Field(default_factory=list)

    status: SkillStatus
    requirements: list[SkillRequirement] = Field(default_factory=list)

    # 用户是否启用了它。技能定义里给默认值，用户覆盖后存进 DB。
    enabled: bool = True
    # 能否由用户开关。有些能力是流程骨架的一部分（如权项格式校验），关掉就等于
    # 允许产出不合规文书，那种不给开关。
    toggleable: bool = True

    inputs: str = ""              # 需要用户提供什么
    outputs: str = ""             # 产出什么

    # 外部来源的技能要如实标注，这既是许可证义务也是给用户的信息
    provider: str = "builtin"     # builtin | 具体工程名
    source_url: str | None = None
    license: str | None = None

    # 成本提示：会不会额外花钱/花时间。用户勾选前该知道。
    cost_hint: str = ""


class SkillListOut(BaseModel):
    """GET /skills 的响应。"""

    skills: list[SkillOut]
    # 按分类给出的顺序，前端据此分组，不必自己硬编码
    categories: list[dict[str, str]] = Field(default_factory=list)


class SkillToggleIn(BaseModel):
    """PUT /skills/{key} 请求体。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
