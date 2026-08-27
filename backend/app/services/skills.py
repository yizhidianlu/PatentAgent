# -*- coding: utf-8 -*-
"""技能库：注册表 + 可用性探测 + 启用状态。

技能定义写死在本模块的 REGISTRY 里，**不从数据库读**。
DB 只存「用户把哪些技能关掉了」。这条边界不能松：技能一旦可由用户上传或编辑，
就等于开了一个任意代码执行的口子——而这个平台里跑着别人的专利交底书。

外部开源工程要接进来，走的是「维护端写一个适配器 + 在这里声明来源与许可证」
这条路，不是「用户粘一段脚本」。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..db import database as db
from ..models.skill import (
    SkillCategory,
    SkillModule,
    SkillOut,
    SkillPatentType,
    SkillRequirement,
    SkillStatus,
)

SETTINGS_KEY = "skills"

CATEGORY_LABELS: dict[str, str] = {
    "search": "检索查新",
    "drawing": "附图与图形",
    "writing": "撰写与润色",
    "validation": "校验与自检",
    "export": "导出与交付",
}


@dataclass(frozen=True)
class RequirementDef:
    """一项前置条件：怎么判断满足、不满足时告诉用户什么。"""

    key: str
    label: str
    probe: Callable[[], bool]
    hint: str = ""
    settings_path: str | None = None


@dataclass(frozen=True)
class SkillDef:
    """一项技能的静态定义。"""

    key: str
    name: str
    category: SkillCategory
    summary: str
    description: str = ""
    modules: tuple[SkillModule, ...] = ()
    patent_types: tuple[SkillPatentType, ...] = ()
    requirements: tuple[RequirementDef, ...] = ()
    default_enabled: bool = True
    # 流程骨架的一部分，关掉就等于允许产出不合规文书 → 不给开关
    toggleable: bool = True
    inputs: str = ""
    outputs: str = ""
    provider: str = "builtin"
    source_url: str | None = None
    license: str | None = None
    cost_hint: str = ""
    order: int = 100


# ---------------------------------------------------------------------------
# 前置条件探测
# ---------------------------------------------------------------------------

_CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
_WINWORD_CANDIDATES = [
    Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
    Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE"),
]


def _first_existing(paths: list[Path]) -> bool:
    return any(p.is_file() for p in paths)


def _has_chrome() -> bool:
    return _first_existing(_CHROME_CANDIDATES) or bool(shutil.which("chrome"))


def _has_word() -> bool:
    return _first_existing(_WINWORD_CANDIDATES)


def _has_soffice() -> bool:
    return bool(shutil.which("soffice"))


def _llm_configured() -> bool:
    cfg = db.get_setting_json("llm") or {}
    return bool(cfg.get("model")) and bool(cfg.get("api_key"))


def _embedding_configured() -> bool:
    cfg = db.get_setting_json("embedding") or {}
    return bool(cfg.get("model")) and bool(cfg.get("api_key"))


def _image_gen_configured() -> bool:
    cfg = db.get_setting_json("image_gen") or {}
    return bool(cfg.get("enabled")) and bool(cfg.get("model"))


REQ_LLM = RequirementDef(
    key="llm",
    label="已配置文本模型",
    probe=_llm_configured,
    hint="在「设置 → 模型服务」填写 Base URL、API Key 与模型名。",
    settings_path="/settings",
)
REQ_EMBEDDING = RequirementDef(
    key="embedding",
    label="已配置向量模型",
    probe=_embedding_configured,
    hint="在「设置 → 向量与检索」配置 Embedding 模型，否则只能按关键词粗筛。",
    settings_path="/settings",
)
REQ_IMAGE_GEN = RequirementDef(
    key="image_gen",
    label="已启用图像生成",
    probe=_image_gen_configured,
    hint="在「设置 → 图像生成」配置并开启图像模型。出图较慢且会消耗额度。",
    settings_path="/settings",
)
REQ_CHROME = RequirementDef(
    key="chrome",
    label="本机装有 Chrome",
    probe=_has_chrome,
    hint="国知局检索需要真实浏览器才能通过站点校验，请在服务器上安装 Chrome。",
)
def _cad_libs_available() -> bool:
    """ezdxf 与 matplotlib 是否可用。

    两者都是纯 Python 轮子，Python 3.13 上实测可直接安装——
    注意别装 ezdxf[draw]，那个 extra 会拉 PySide6(Qt)，在未开启长路径的
    Windows 上装不上，而渲染成 PNG 只需要 matplotlib 后端。
    """
    from importlib.util import find_spec

    return find_spec("ezdxf") is not None and find_spec("matplotlib") is not None


REQ_CAD_LIBS = RequirementDef(
    key="cad_libs",
    label="已安装制图库（ezdxf + matplotlib）",
    probe=_cad_libs_available,
    hint="在服务器上执行：pip install ezdxf matplotlib（勿用 ezdxf[draw]，它会拉 Qt）。",
)

REQ_WORD_OR_SOFFICE = RequirementDef(
    key="pdf_engine",
    label="本机可转 PDF（Word 或 LibreOffice）",
    probe=lambda: _has_word() or _has_soffice(),
    hint="装 Microsoft Word 或 LibreOffice 后才能导出 PDF；否则仅交付 .docx。",
)


# ---------------------------------------------------------------------------
# 注册表
#
# 顺序即前端展示顺序。新增技能在这里加一条，前端不需要改。
# ---------------------------------------------------------------------------

REGISTRY: tuple[SkillDef, ...] = (
    SkillDef(
        key="cnipa_search",
        name="国知局联网查新",
        category="search",
        summary="到国家知识产权局公布公告系统检索相似专利，并把命中结果消化进背景技术",
        description=(
            "按案件主题构造 2-8 组语义检索式，driving 真实浏览器访问国知局公布公告系统，"
            "逐条读取命中专利的摘要并改写成「现有技术」段落，同时给出与本方案的区别点。\n\n"
            "检索失败或被站点拦截时不会中断流程：会转为让你手工粘贴对比文件，或如实"
            "声明未检索到相关现有技术。"
        ),
        modules=("disclosure",),
        patent_types=("invention", "utility_model", "design"),
        requirements=(REQ_CHROME, REQ_LLM),
        inputs="案件主题与技术要点（自动从材料中提取）",
        outputs="现有技术段落 + 命中专利清单（公开号、名称、区别点）",
        cost_hint="联网检索，单次约 1-3 分钟",
        order=10,
    ),
    SkillDef(
        key="ai_figure",
        name="附图智能补绘",
        category="drawing",
        summary="内置矢量脚本画不出某张附图时，改由图像模型按规格补一张黑白线条图",
        description=(
            "平台优先用确定性脚本生成框图与流程图——那样产出稳定、可复现。"
            "遇到脚本表达不了的结构（如机械装配关系），会把该图的规格转成"
            "严格约束的提示词交给图像模型：纯黑白线条、白底、无阴影无渲染、"
            "图内不出现图号与标题。\n\n"
            "关掉时，画不出的图只给出精修提示词，留待人工绘制。"
        ),
        modules=("paper2patent",),
        patent_types=("invention", "utility_model", "design"),
        requirements=(REQ_IMAGE_GEN,),
        default_enabled=False,   # 会花钱，默认不替用户做主
        inputs="附图规格（由说明书内容自动推导）",
        outputs="嵌入文档的黑白线条附图",
        cost_hint="每张约 20-60 秒，按图像模型计费；每案最多补 3 张",
        order=20,
    ),
    SkillDef(
        key="cad_lineart",
        name="机械结构线稿",
        category="drawing",
        summary="由结构描述直接生成符合专利规范的黑白线条图，并附可编辑的 DXF 源文件",
        description=(
            "实用新型与外观设计需要机械结构附图，而这类图用框图脚本画不出来。\n\n"
            "这项技能把技术方案里的结构关系转成二维工程图元（轮廓、内腔、剖面线、"
            "零件标号与引出线），直接产出纯黑白线条图：白底、无颜色灰度渐变阴影、"
            "图内只有数字标号不写说明文字——这些都是审查指南对附图的硬性要求。\n\n"
            "同时产出 DXF 矢量源文件。代理人可以拿去任何 CAD 软件里继续修改，"
            "这比只给一张位图有用得多。"
        ),
        modules=("disclosure", "paper2patent"),
        patent_types=("utility_model", "design", "invention"),
        requirements=(REQ_CAD_LIBS, REQ_LLM),
        default_enabled=False,   # 依赖未必装，默认不开免得每次都提示缺环境
        inputs="技术方案中的结构描述（自动提取，也可人工调整图元）",
        outputs="黑白线条 PNG（嵌入文档）+ DXF 矢量源（可二次编辑）",
        provider="ezdxf",
        source_url="https://github.com/mozman/ezdxf",
        license="MIT",
        cost_hint="本地渲染，单张约 1-3 秒，不消耗模型额度",
        order=21,
    ),
    SkillDef(
        key="formula_check",
        name="公式范式校验",
        category="validation",
        summary="公式按范式库选型并做数值复算，杜绝凭空编造的表达式",
        description=(
            "说明书里的公式不允许自由发挥：先从范式库中选定形式，再对给出的"
            "示例数值做一次实际计算复核，符号在各章节之间逐字保持同形。"
            "复算不过的公式会被打回重写。"
        ),
        modules=("disclosure",),
        patent_types=("invention",),
        requirements=(REQ_LLM,),
        toggleable=False,   # 关掉等于允许编造公式
        inputs="技术方案中的计算逻辑",
        outputs="可编辑的 Office 公式 + 符号表",
        order=30,
    ),
    SkillDef(
        key="claims_lint",
        name="权利要求格式校验",
        category="validation",
        summary="按审查标准检查权项：禁用词、引用基础、单句号、名称一致性",
        description=(
            "确定性规则校验，不依赖模型判断：每项权利要求只能有一个句号、"
            "从属权项的引用基础必须存在且不得多引多、技术名称需与说明书一致、"
            "命中禁用词时标记待人工确认（不自动删改，避免误伤「等离子」这类正常术语）。"
        ),
        modules=("paper2patent",),
        patent_types=("invention", "utility_model"),
        requirements=(),
        toggleable=False,   # 关掉等于允许产出不合规权项
        inputs="生成的权利要求书",
        outputs="校验结果与修复建议",
        order=31,
    ),
    SkillDef(
        key="oa_case_retrieval",
        name="历史案例检索",
        category="search",
        summary="从你自建的案例库里找出相似的审查意见与答复，供本次答复参考",
        description=(
            "把审查意见拆成逐条缺陷，按语义相似度从案例库中召回历史答复。"
            "未配置向量模型时自动降级为关键词粗筛，并在界面上标明当前用的是哪种。"
        ),
        modules=("oa",),
        requirements=(REQ_EMBEDDING,),
        inputs="审查意见通知书",
        outputs="相似案例清单 + 可参考的答复思路",
        cost_hint="需先在案例库中录入并确认历史案例",
        order=11,
    ),
    SkillDef(
        key="pdf_export",
        name="PDF 交付",
        category="export",
        summary="把生成的 Word 文档转为 PDF，便于直接提交或存档",
        description=(
            "优先使用本机 Microsoft Word 转换（版式最接近提交要求），"
            "没有 Word 时退回 LibreOffice。两者都没有时仅交付 .docx，"
            "你可以自行另存为 PDF。"
        ),
        modules=("disclosure", "paper2patent", "reader", "oa"),
        requirements=(REQ_WORD_OR_SOFFICE,),
        inputs="已生成的 .docx",
        outputs="同名 .pdf",
        order=40,
    ),
)


# ---------------------------------------------------------------------------
# 启用状态
# ---------------------------------------------------------------------------

def _overrides() -> dict[str, bool]:
    stored = db.get_setting_json(SETTINGS_KEY) or {}
    raw = stored.get("enabled") if isinstance(stored, dict) else None
    return {k: bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def set_enabled(key: str, enabled: bool) -> None:
    """记下用户对某项技能的开关选择。"""
    defn = get_definition(key)
    if defn is None:
        raise KeyError(key)
    if not defn.toggleable:
        raise ValueError(f"技能「{defn.name}」是流程的必要环节，不能关闭")
    stored = db.get_setting_json(SETTINGS_KEY) or {}
    enabled_map = dict(stored.get("enabled") or {})
    enabled_map[key] = bool(enabled)
    stored["enabled"] = enabled_map
    db.set_setting_json(SETTINGS_KEY, stored)


def get_definition(key: str) -> SkillDef | None:
    return next((s for s in REGISTRY if s.key == key), None)


def is_user_enabled(key: str) -> bool:
    """用户有没有把这项技能关掉 —— **只看开关，不看前置条件**。

    与 is_enabled 的区别是刻意的：
    * 「用户明确关掉了」是一个决定，流程必须尊重它，直接不做；
    * 「前置条件没配好」不是决定，那时该走该能力自己的降级路径
      （联网查新失败会给出重试/手工录入/跳过三选项，比直接跳过有用得多）。

    把两者混为一谈会让「没装 Chrome」表现得和「用户不想联网」一模一样，
    而后者需要如实写进文书、前者应该提示用户去装浏览器。
    """
    defn = get_definition(key)
    if defn is None:
        return False
    if not defn.toggleable:
        return True
    return _overrides().get(key, defn.default_enabled)


def is_enabled(key: str) -> bool:
    """技能是否对流水线生效：用户开着 **且** 前置条件都满足。

    两个条件缺一不可——用户开了但没配模型，仍然跑不起来，
    这时应当走降级路径而不是报错。
    """
    defn = get_definition(key)
    if defn is None:
        return False
    if not defn.toggleable:
        return True
    if not _overrides().get(key, defn.default_enabled):
        return False
    return all(req.probe() for req in defn.requirements)


def _resolve(defn: SkillDef, overrides: dict[str, bool]) -> SkillOut:
    reqs = [
        SkillRequirement(
            key=r.key,
            label=r.label,
            satisfied=r.probe(),
            hint=r.hint,
            settings_path=r.settings_path,
        )
        for r in defn.requirements
    ]
    unmet = [r for r in reqs if not r.satisfied]
    status: SkillStatus
    if not unmet:
        status = "available"
    elif all(r.settings_path for r in unmet):
        # 缺的都是「去设置页填一下就有」的，与「本机没装 Word」不是一回事
        status = "needs_config"
    else:
        status = "unavailable"

    return SkillOut(
        key=defn.key,
        name=defn.name,
        category=defn.category,
        summary=defn.summary,
        description=defn.description,
        modules=list(defn.modules),
        patent_types=list(defn.patent_types),
        status=status,
        requirements=reqs,
        enabled=overrides.get(defn.key, defn.default_enabled) if defn.toggleable else True,
        toggleable=defn.toggleable,
        inputs=defn.inputs,
        outputs=defn.outputs,
        provider=defn.provider,
        source_url=defn.source_url,
        license=defn.license,
        cost_hint=defn.cost_hint,
    )


def list_skills() -> list[SkillOut]:
    """列出全部技能，含实时探测出的可用性。"""
    overrides = _overrides()
    return [_resolve(d, overrides) for d in sorted(REGISTRY, key=lambda s: (s.order, s.key))]


def categories() -> list[dict[str, str]]:
    """前端分组用的分类顺序——只给出确实有技能的分类。"""
    seen: list[str] = []
    for d in sorted(REGISTRY, key=lambda s: (s.order, s.key)):
        if d.category not in seen:
            seen.append(d.category)
    return [{"key": c, "label": CATEGORY_LABELS.get(c, c)} for c in seen]
