"""模块 A（专利交底书 · 发明分支）端到端测试。

不依赖真实 LLM 与真实网络：

- `FakeLLM` 按 `step_key` 后缀脚本化回放预置产物（chat_stream / chat / structured 三入口）；
- `cnipa.search` 被替换为返回预置命中的测试桩（命中行经 `add_manual_hits` 真实落库，
  故 `set_digest` / `set_selected` 走真实 DB 路径）；
- 其余环节**全部真跑**：mermaid 真渲染做语法门禁与 PNG 交付、`check_formula_plan --eval`
  真复算、补丁定位真应用、md→docx 真子进程、交付物真落盘。

覆盖的四条 REPAIR 路径（各触发一次）：

1. G2 首版 3.2 mermaid 语法错误 → 渲染门禁不过 → `g2.repair1` 修正；
2. G4 首版 formula_plan 数值例复算不符 → `g4.repair1` 修正；
3. A7 自检返回 2 条补丁 → 服务端确定性应用（应用率 100%）；
4. A8 claim_bias 首版 `basis_quote` 不是终稿子串 → `claim_bias.repair1` 修正。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 案件设定与脚本化产物
# ---------------------------------------------------------------------------

CASE_TITLE = "一种基于负载画像的批量作业调度方法及系统"
WORKING_TITLE = "基于负载画像的批量作业调度"
TITLE_TERMS = ["负载画像", "批量作业调度"]
MODULES = ["负载画像采集模块", "批量作业调度决策模块", "限频控制模块", "执行反馈模块"]
SENSITIVE = ["引途医疗科技有限公司", "北方某三甲医院", "128 万元"]

URL_1 = "https://epub.cnipa.gov.cn/patent/CN114000001A"
URL_2 = "https://epub.cnipa.gov.cn/patent/CN114000002A"

MATERIAL_MD = """# 批量作业调度平台设计说明

本文档由引途医疗科技有限公司算力平台组编写，部署于北方某三甲医院的影像后处理集群，
一期预算 128 万元。

## 1. 现状与问题
现有派发按固定权重轮询，节点资源紧张时仍继续派发，排队时延抖动明显。

## 2. 设计要点
- 每 30 秒采集各计算节点的算力与内存占用，按滑动窗口平滑为负载画像；
- 对待执行作业与候选节点逐对计算匹配分，按匹配分从高到低派发；
- 以最小派发间隔与画像变化幅度两个条件裁决派发时机；
- 作业执行结束后回写耗时与资源占用，修正负载画像。
"""

MATERIAL_DIGEST = {
    "file": "material.md",
    "priority": 2,
    "summary_zh": "该材料给出批量作业调度平台的现状问题与设计要点，涵盖负载画像采集、匹配打分、限频裁决与执行反馈四个环节。",
    "tech_points": [
        "滑动窗口平滑得到多维资源的负载画像",
        "作业与节点逐对计算匹配分",
        "最小派发间隔与变化幅度双条件限频",
        "执行反馈回写修正负载画像",
    ],
    "components": ["负载画像采集", "调度决策", "限频控制", "执行反馈"],
    "figures_mentioned": [],
    "sensitive_hits": SENSITIVE,
    "type_signals": {
        "invention": 0.9,
        "utility_model": 0.1,
        "design": 0.0,
        "evidence": ["材料以方法流程与打分策略为主"],
    },
}

POINTS_MD = """### 候选专利点分析

**P1 基于负载画像的批量作业调度**
- 技术背景：集群按固定权重轮询派发，节点资源紧张时仍继续派发。
- 创新点：以滑动窗口平滑得到的负载画像参与匹配打分，并以双条件限频裁决派发时机。
- 与现有技术区别：现有方案的权重不随节点实时资源变化。
- 可实施性：采集、打分、裁决、回写四个环节均可在现有调度框架内实现。

**P2 执行反馈闭环修正**
- 技术背景：派发后缺少对预估偏差的修正。
- 创新点：以执行反馈回写修正负载画像。
- 与现有技术区别：现有方案不做回写。
- 可实施性：可复用现有作业状态上报通道。

**P3 多维资源归一化比较**
- 技术背景：算力与内存量纲不同难以直接比较。
- 创新点：把多维占用归一化到同一区间后加权。
- 与现有技术区别：现有方案只看单一维度。
- 可实施性：归一化规则简单可实现。

```json
{
  "candidates": [
    {"id": "P1", "title": "基于负载画像的批量作业调度",
     "background": "集群按固定权重轮询派发。", "innovation": "负载画像参与匹配打分并双条件限频。",
     "distinction": "现有方案权重不随实时资源变化。", "feasibility": "四个环节均可在现有框架内实现。",
     "score": 88},
    {"id": "P2", "title": "执行反馈闭环修正",
     "background": "派发后缺少偏差修正。", "innovation": "执行反馈回写修正负载画像。",
     "distinction": "现有方案不做回写。", "feasibility": "复用作业状态上报通道。", "score": 74},
    {"id": "P3", "title": "多维资源归一化比较",
     "background": "算力与内存量纲不同。", "innovation": "归一化后加权比较。",
     "distinction": "现有方案只看单一维度。", "feasibility": "归一化规则简单。", "score": 66}
  ],
  "recommended": "P1",
  "fusion_note": "建议以 P1 为主线融合 P2 的执行反馈闭环与 P3 的归一化比较。"
}
```
"""

SEARCH_TERMS = {
    "blocks": ["批量作业调度", "负载画像", "限频控制", "异构集群调度"],
    "type_param": "invention",
    "rationale": "覆盖本案的调度主线、画像刻画与限频裁决三个核心方案要素。",
}

PRESET_HITS = [
    {
        "url": URL_1,
        "pub_no": "CN114000001A",
        "title": "一种基于固定权重的作业派发方法",
        "applicant": "某调度技术公司",
        "abstract": "本发明公开一种基于固定权重的作业派发方法，按预先设定的权重对作业排序后轮询派发到计算节点。",
    },
    {
        "url": URL_2,
        "pub_no": "CN114000002A",
        "title": "一种基于单一阈值的派发限流方法",
        "applicant": "某云计算研究院",
        "abstract": "本发明公开一种基于单一阈值的派发限流方法，节点占用超过阈值即暂停派发，低于阈值即恢复派发。",
    },
]

ABSTRACT_DIGESTS = {
    "digests": [
        {
            "pub_no": "CN114000001A",
            "title": "一种基于固定权重的作业派发方法",
            "applicant": "某调度技术公司",
            "url": URL_1,
            "scheme_summary": "按预先设定的固定权重对待执行作业排序，再按轮询顺序把作业派发到计算节点。",
            "application": "作业量平稳、节点规格一致的集群。",
            "limitation": "权重不随节点实际资源变化调整，节点资源紧张时仍会继续派发，导致排队时延抖动。",
            "abstract_used": True,
            "note": "",
        },
        {
            "pub_no": "CN114000002A",
            "title": "一种基于单一阈值的派发限流方法",
            "applicant": "某云计算研究院",
            "url": URL_2,
            "scheme_summary": "为节点设定单一阈值，超过阈值即暂停派发，低于阈值即恢复派发。",
            "application": "单一资源维度受限的场景。",
            "limitation": "单阈值在阈值附近反复触发，派发与暂停频繁切换，且未结合多维资源的负载画像。",
            "abstract_used": True,
            "note": "",
        },
    ]
}

PREVIEW_MD = """### 交底书摘要预览

- **选定名称**：一种基于负载画像的批量作业调度方法及系统
- **专利类型**：发明
- **解决的技术问题**：固定权重不感知节点实时资源；单阈值反复触发导致派发抖动；缺少多维资源的统一刻画。
- **核心创新模块或步骤**：负载画像采集；匹配分计算；双条件限频裁决；执行反馈回写。
- **与最相近现有技术的区别**：以滑动窗口平滑得到的负载画像参与匹配打分，并以双阈值限频抑制派发抖动。
"""

SKELETON = {
    "case_title": CASE_TITLE,
    "title_core_terms": TITLE_TERMS,
    "modules": MODULES,
    "steps": [
        {"sid": "S1", "label": "采集节点负载画像"},
        {"sid": "S2", "label": "计算批量作业调度匹配分"},
        {"sid": "S3", "label": "判定限频阈值"},
        {"sid": "S4", "label": "派发作业并回写执行反馈"},
    ],
    "chapter_outline": [
        "一、现有技术与缺点",
        "二、所要解决的技术问题",
        "三、技术方案详细阐述",
        "四、与现有技术相比的优点",
        "五、技术关键点和欲保护点",
        "六、实施例与技术效果",
    ],
    "terms_init": [
        {
            "term": "负载画像",
            "definition": "计算节点在滑动窗口内经平滑得到的多维资源可用比例",
            "forbidden_variants": ["水位"],
            "source_section": "3.1",
        },
        {
            "term": "批量作业调度",
            "definition": "把一批待执行作业按匹配分派发到计算节点的过程",
            "source_section": "3.1",
        },
    ],
}

TERMS_DELTA = """```json
{"terms_delta": {"add": [{"term": "匹配分", "definition": "待执行作业与计算节点的适配程度打分", "source_section": "3.4"}], "update": []}}
```"""

CH1 = f"""## 一、介绍相关技术背景，描述与本发明技术最相近的现有技术，并说明该现有技术存在的缺点

### 1.1 现有技术

检索说明：在国家知识产权局专利公布公告系统中，以「批量作业调度」「负载画像」「限频控制」「异构集群调度」
等为检索词进行检索，命中条目的著录项与公开文本以公布公告页面为准。

**方向一：固定权重的批量作业调度**

- 专利号：CN114000001A；申请方：某调度技术公司
- 技术方案：按预先设定的固定权重对待执行作业排序，再按轮询顺序把作业派发到计算节点。
- 应用场景：作业量平稳、节点规格一致的集群。
- 局限性：权重不随节点实际资源变化调整，节点资源紧张时仍会继续派发，导致排队时延抖动。
- 来源链接：{URL_1}

**方向二：单阈值触发的限频控制**

- 专利号：CN114000002A；申请方：某云计算研究院
- 技术方案：为节点设定单一阈值，超过阈值即暂停派发，低于阈值即恢复派发。
- 应用场景：单一资源维度受限的场景。
- 局限性：单阈值在阈值附近反复触发，派发与暂停频繁切换，且未结合多维资源的负载画像。
- 来源链接：{URL_2}

检索总结：上述现有技术均未把多维资源的负载画像与批量作业调度的匹配打分结合，也未给出抑制反复触发的
双条件限频机制。本发明与现有技术的本质区别在于：以滑动窗口平滑得到的负载画像参与匹配打分，并以最小
派发间隔与画像变化幅度两个条件共同裁决派发时机。

### 1.2 现有技术存在的缺点

- 固定权重不感知节点实时资源，批量作业调度的匹配结果与节点真实承载情况脱节；
- 单阈值在阈值附近反复触发，派发与暂停频繁切换，排队时延抖动明显；
- 缺少对多维资源的统一刻画，无法在算力与内存之间做出权衡。

## 二、针对上述缺点，说明本发明所要解决的技术问题

- 如何以可平滑、可比较的负载画像刻画节点的多维资源可用程度，使批量作业调度的匹配结果贴合节点实际情况；
- 如何在阈值附近抑制派发与暂停的反复切换，降低排队时延抖动；
- 如何把作业执行结束后的耗时与资源占用回写到负载画像，形成闭环。

{TERMS_DELTA}
"""

_CH3_HEAD = """## 三、本发明技术方案的详细阐述

### 3.1 背景

在计算集群的日常运维中，运维人员与作业提交方共同完成一批待执行作业的派发：作业提交方按业务节奏提交
批量作业，集群侧按各计算节点的实际可用资源决定派发顺序与派发时机。

批量作业调度在本方案中的进入方式是：作业进入待调度队列后，由调度决策环节读取各计算节点最近一个窗口的
负载画像，再决定把哪一个作业派发到哪一个节点。

后文高频出现的领域词定义如下：负载画像，指计算节点在滑动窗口内经平滑得到的多维资源可用比例（按窗口内
采样值平滑记录）；批量作业调度，指把一批待执行作业按匹配分派发到计算节点的过程；限频控制，指两次派发
之间须保持的最小间隔与画像变化幅度约束；执行反馈，指作业执行结束后回写的耗时与资源占用记录。

### 3.2 系统框图

"""

_CH3_TAIL = """

### 3.3 模块功能说明

- **负载画像采集模块**：作用是按固定周期采集各计算节点的算力与内存占用并平滑为负载画像；关联关系上，
  它向批量作业调度决策模块提供打分依据，并接收执行反馈模块回写的占用记录。
- **批量作业调度决策模块**：作用是对待执行作业与候选节点逐对计算匹配分并选出派发目标；关联关系上，
  它以负载画像采集模块的输出为输入，把派发意图交给限频控制模块裁决。
- **限频控制模块**：作用是按最小派发间隔与画像变化幅度两个条件裁决派发意图是否立即执行；关联关系上，
  它拦截批量作业调度决策模块的派发意图，放行后交由执行反馈模块跟踪。
- **执行反馈模块**：作用是记录作业执行的耗时与资源占用并回写；关联关系上，它把回写结果交给负载画像
  采集模块，形成闭环。

""" + TERMS_DELTA + "\n"

# 首版 3.2 的 mermaid 故意写错（方括号未闭合）→ 渲染门禁不过 → REPAIR
CH3_BAD = _CH3_HEAD + """```mermaid
flowchart TB
  A["负载画像采集模块"] --> B["批量作业调度决策模块"
  B --> C["限频控制模块"
  C --> D["执行反馈模块"]
```""" + _CH3_TAIL

CH3_FIXED = _CH3_HEAD + """```mermaid
flowchart TB
  subgraph collect[" "]
    direction LR
    A["负载画像采集模块"]
  end
  subgraph decide[" "]
    direction LR
    B["批量作业调度决策模块"]
    C["限频控制模块"]
  end
  D["执行反馈模块"]
  A --> B
  B --> C
  C --> D
  D --> A
```""" + _CH3_TAIL

CH34 = """### 3.4 系统流程说明

```mermaid
flowchart TD
  S1["S1 采集节点负载画像"] --> S2["S2 计算批量作业调度匹配分"]
  S2 --> S3{"S3 判定限频阈值"}
  S3 -->|满足| S4["S4 派发作业并回写执行反馈"]
  S3 -->|不满足| S1
```

S1，采集节点负载画像。负载画像采集模块按固定周期读取各计算节点的算力与内存占用，并按滑动窗口平滑为
本轮负载画像。

S2，计算批量作业调度匹配分。批量作业调度决策模块对待执行作业与候选节点逐对计算匹配分 \\(s_{ij}\\)，
权重取值与阈值区间详见 3.4.1 的公式与 3.5 的参数表。

S3，判定限频阈值。限频控制模块按最小派发间隔与画像变化幅度两个条件裁决本次派发意图：两个条件同时满足
才放行，否则回到 S1 等待下一轮负载画像。

S4，派发作业并回写执行反馈。放行后把作业派发到目标节点，执行反馈模块在作业结束后回写耗时与资源占用，
供下一轮负载画像修正使用。

""" + TERMS_DELTA + "\n"

CH341 = """#### 3.4.1 符号与公式

| 符号 | 含义 | 下标/量纲 |
|------|------|-----------|
| \\(s_{ij}\\) | 作业 i 与节点 j 的匹配分 | 无量纲，取值 [0,1] |
| \\(b\\) | 作业的算力需求权重 | 无量纲，取值 [0,1] |
| \\(g\\) | 节点的内存可用比例 | 无量纲，取值 [0,1] |

匹配分按下式计算：

\\[ s_{ij} = 0.6 b + 0.4 g \\]

式中算力维度权重取 0.6、内存维度权重取 0.4，两项均已归一化到同一区间后再加权，
计算结果直接用于 3.4 中 S2 的派发排序。

### 3.5 关键技术参数

| 参数 | 符号 | 含义 | 取值范围 |
|------|------|------|----------|
| 匹配分 | \\(s_{ij}\\) | 作业与节点的适配程度 | [0,1] |
| 算力需求权重 | \\(b\\) | 作业对算力的需求强度 | [0,1] |
| 内存可用比例 | \\(g\\) | 节点内存的可用程度 | [0,1] |
| 最小派发间隔 | 无 | 两次派发之间的最小时间间隔 | 3～10 秒 |

""" + TERMS_DELTA + "\n"

CH45 = """## 四、与现有技术相比，本发明具有哪些优点？

本发明的核心优点在于把节点的实时承载情况引入派发决策，并以双条件限频抑制派发抖动。

- 匹配结果贴合节点实际可用资源：负载画像按滑动窗口平滑，避免单次采样抖动带来的误判；
- 排队时延抖动明显下降：最小派发间隔与画像变化幅度共同裁决，抑制阈值附近的反复切换；
- 决策闭环可持续：执行反馈回写使下一轮负载画像贴近真实占用。

## 五、本发明的技术关键点和欲保护点是什么？

- 以滑动窗口对多维资源采样值做平滑，得到可比较的负载画像，并将其作为派发决策的输入；
- 以加权求和方式对待执行作业与候选节点逐对计算匹配分，按匹配分从高到低选择派发目标；
- 以最小派发间隔与画像变化幅度两个条件共同裁决派发时机，两者同时满足才放行本次派发；
- 以作业结束后回写的耗时与资源占用修正负载画像，使下一轮批量作业调度使用最新记录。

""" + TERMS_DELTA + "\n"

CH6 = """## 六、其它

### 实施例

**实施例一：批量作业调度的常规派发**

本实施例中，负载画像采集模块每 30 秒采集一次各计算节点的算力与内存占用并平滑为负载画像；批量作业调度
决策模块按 S1 至 S2 得到匹配分并排序；限频控制模块在 S3 判定两个条件均满足后放行；S4 派发作业并在作业
结束后由执行反馈模块回写耗时与资源占用。四个角色的交付物依次为负载画像、匹配分排序表、派发放行结论与
执行反馈记录。

**实施例二：限频条件不满足时的回退**

本实施例走 3.4 中 S3 判定不通过的分支：当距上次派发的间隔不足或画像变化幅度过小时，限频控制模块拒绝
放行，流程回到 S1 等待下一轮负载画像；待间隔与幅度同时满足后再进入 S4 派发。该分支保证阈值附近不会
出现派发与暂停的反复切换。

### 技术效果

- 排队时延抖动下降，派发结果与节点实际可用资源一致；
- 反复切换被抑制，单位时间内的派发次数趋于平稳。

### 参数设置示例

以下参数仅为示例，不作为权利要求限制：算力维度权重取 0.6、内存维度权重取 0.4；
给定 \\(b\\) 为 0.8、\\(g\\) 为 0.5 时匹配分为 0.68，与 3.5 关键技术参数一致。

""" + TERMS_DELTA + "\n"

# G4：首版数值例复算不符（0.6×0.8+0.4×0.5=0.68，却写成 0.90）→ 门禁打回
FORMULA_PLAN_BAD = {
    "theme_summary": "批量作业调度的匹配打分与限频触发",
    "paradigm_ids": ["weighted_sum", "dual_threshold"],
    "plain_zh": "匹配分 = 0.6×算力适配 + 0.4×内存适配",
    "equations": [
        {"tag": 1, "paradigm_id": "weighted_sum", "latex": "s_{ij} = 0.6 b + 0.4 g", "role": "score"},
        {
            "tag": 2,
            "paradigm_id": "dual_threshold",
            "latex": "\\Delta t \\ge T_r ; |\\sigma - \\sigma'| \\ge \\Delta s",
            "role": "trigger",
        },
    ],
    "symbols": [
        {"symbol": "s_{ij}", "meaning_zh": "作业 i 与节点 j 的匹配分", "unit_zh": "无量纲"},
        {"symbol": "b", "meaning_zh": "作业的算力需求权重", "unit_zh": "[0,1]"},
        {"symbol": "g", "meaning_zh": "节点的内存可用比例", "unit_zh": "[0,1]"},
    ],
    "numeric_example": {
        "given": {"b": 0.8, "g": 0.5},
        "compute_zh": "0.6×0.8 + 0.4×0.5",
        "result": {"s_{ij}": 0.90},
    },
    "notes_zh": "",
}

FORMULA_PLAN_FIXED = json.loads(json.dumps(FORMULA_PLAN_BAD))
FORMULA_PLAN_FIXED["numeric_example"]["compute_zh"] = "0.6×0.8 + 0.4×0.5 = 0.68"
FORMULA_PLAN_FIXED["numeric_example"]["result"] = {"s_{ij}": 0.68}

# A7 自检：两条补丁（公式类 + 术语类），锚点与摘录均为终稿逐字子串
PATCH_FORMULA_OLD = "按匹配分从高到低选择派发目标"
PATCH_FORMULA_NEW = "按匹配分 \\(s_{ij}\\) 从高到低选择派发目标"
PATCH_TERM_OLD = "把节点的实时承载情况引入派发决策"
PATCH_TERM_NEW = "把节点的负载画像引入派发决策"

SELF_CHECK_REPORT = {
    "verdict": "patched",
    "patches": [
        {
            "checklist_item": "8.2 公式表述一致",
            "anchor_before": "以加权求和方式对待执行作业与候选节点逐对计算匹配分，",
            "original_excerpt": PATCH_FORMULA_OLD,
            "replacement": PATCH_FORMULA_NEW,
            "reason": "第五章保护点须与式 (1) 的符号同形。",
        },
        {
            "checklist_item": "8.1 术语贴合话题",
            "anchor_before": "本发明的核心优点在于",
            "original_excerpt": PATCH_TERM_OLD,
            "replacement": PATCH_TERM_NEW,
            "reason": "第四章应使用 3.1 已定义的领域词「负载画像」。",
        },
    ],
    "unresolved": [],
}

SELF_CHECK_PASS = {"verdict": "pass", "patches": [], "unresolved": []}

# A8 偏向点：首版 basis_quote 不是终稿子串 → REPAIR
CLAIM_BIAS_BAD = {
    "groups": [
        {
            "axis": "方法流程侧重 vs 装置模块侧重",
            "option_a": {"label": "偏方法流程", "basis_quote": "本发明以强化学习在线调整派发权重"},
            "option_b": {"label": "偏装置模块", "basis_quote": "本发明部署于专用调度硬件板卡"},
        }
    ]
}

CLAIM_BIAS_FIXED = {
    "groups": [
        {
            "axis": "方法流程侧重 vs 装置模块侧重",
            "option_a": {
                "label": "更偏方法流程：以采集、打分、裁决、回写四步的时序关系为保护重心",
                "basis_quote": "以加权求和方式对待执行作业与候选节点逐对计算匹配分",
            },
            "option_b": {
                "label": "更偏装置模块：以四个模块的连接与协作关系为保护重心",
                "basis_quote": "以作业结束后回写的耗时与资源占用修正负载画像",
            },
        }
    ]
}


# ---------------------------------------------------------------------------
# 脚本化 FakeLLM
# ---------------------------------------------------------------------------

STREAM_SCRIPT: dict[str, str] = {
    "recap": "- 技术主题：批量作业调度\n- 专利类型：发明\n- 技术联系人：张三 / 13800000000 / zhangsan@example.com",
    "points": POINTS_MD,
    "preview": PREVIEW_MD,
    "g1": CH1,
    "g2": CH3_BAD,
    "g3": CH34,
    "g5": CH341,
    "g6": CH45,
    "g7": CH6,
}

CHAT_SCRIPT: dict[str, str] = {
    "g2.repair1": CH3_FIXED,
}

STRUCT_SCRIPT: dict[str, Any] = {
    "digest.0": MATERIAL_DIGEST,
    "terms": SEARCH_TERMS,
    "digest.batch1": ABSTRACT_DIGESTS,
    "g0": SKELETON,
    "g4": FORMULA_PLAN_BAD,
    "g4.repair1": FORMULA_PLAN_FIXED,
    "audit": SELF_CHECK_REPORT,
    "audit2": SELF_CHECK_PASS,
    "claim_bias": CLAIM_BIAS_BAD,
    "claim_bias.repair1": CLAIM_BIAS_FIXED,
}


class FakeLLM:
    """按 step_key 后缀回放预置产物；未脚本化的调用直接抛错，便于暴露流程变化。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stream_script = dict(STREAM_SCRIPT)
        self.chat_script = dict(CHAT_SCRIPT)
        self.struct_script = dict(STRUCT_SCRIPT)

    @staticmethod
    def _tag(kwargs: dict[str, Any]) -> str:
        step_key = str(kwargs.get("step_key") or "")
        return step_key.split(".", 1)[1] if "." in step_key else step_key

    async def chat(self, messages, **kwargs) -> str:
        tag = self._tag(kwargs)
        self.calls.append(f"chat:{tag}")
        if tag in self.chat_script:
            return self.chat_script[tag]
        if tag in self.stream_script:
            return self.stream_script[tag]
        raise AssertionError(f"FakeLLM 未脚本化的 chat 调用：{tag}")

    async def chat_stream(self, messages, **kwargs):
        tag = self._tag(kwargs)
        self.calls.append(f"stream:{tag}")
        text = self.stream_script.get(tag)
        if text is None:
            raise AssertionError(f"FakeLLM 未脚本化的流式调用：{tag}")
        for i in range(0, len(text), 64):
            yield text[i : i + 64]

    async def structured(self, messages, model_cls, **kwargs):
        tag = self._tag(kwargs)
        self.calls.append(f"struct:{tag}")
        data = self.struct_script.get(tag)
        if data is None:
            raise AssertionError(f"FakeLLM 未脚本化的结构化调用：{tag}（模型 {model_cls.__name__}）")
        return model_cls.model_validate(data)


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    from app.services import llm

    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat", fake.chat)
    monkeypatch.setattr(llm, "chat_stream", fake.chat_stream)
    monkeypatch.setattr(llm, "structured", fake.structured)
    return fake


def _install_search(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True) -> None:
    """把 cnipa.search 换成测试桩（命中行真实落库，便于走 set_digest/set_selected）。"""
    import inspect as _inspect

    from app.models.search import SearchResult
    from app.services import cnipa

    async def fake_search(case_id: str, terms, patent_type: str = "invention", **kwargs):
        on_progress = kwargs.get("on_progress")
        norm = cnipa.normalize_terms(terms)
        if not ok:
            if on_progress is not None:
                result = on_progress("failed", "浏览器不可用（测试桩）")
                if _inspect.isawaitable(result):
                    await result
            return SearchResult(
                status="failed", hits=[], error="浏览器不可用（测试桩）",
                terms=norm, patent_type=patent_type,
            )
        hits = await cnipa.add_manual_hits(case_id, PRESET_HITS, note="测试桩命中")
        if on_progress is not None:
            result = on_progress("done", f"检索完成，命中 {len(hits)} 条（测试桩）")
            if _inspect.isawaitable(result):
                await result
        return SearchResult(
            status="done", hits=hits, error=None,
            query_id=hits[0].query_id if hits else None,
            terms=norm, patent_type=patent_type,
        )

    monkeypatch.setattr(cnipa, "search", fake_search)


# ---------------------------------------------------------------------------
# 夹具与驱动工具
# ---------------------------------------------------------------------------



def _new_case(client: TestClient, title: str, patent_type: str | None = None) -> str:
    body: dict[str, Any] = {"module": "disclosure", "title": title}
    if patent_type:
        body["patent_type"] = patent_type
    resp = client.post("/api/v1/cases", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload_material(client: TestClient, case_id: str) -> str:
    resp = client.post(
        f"/api/v1/cases/{case_id}/files",
        files={"files": ("material.md", MATERIAL_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()[0]["file"]["id"]


def _steps(case_id: str):
    from app.db import database as db
    from app.pipelines import registry

    row = db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))
    return registry.build_steps(dict(row))


def _case_row(case_id: str) -> dict[str, Any]:
    from app.db import database as db

    return dict(db.query_one("SELECT * FROM cases WHERE id=?", (case_id,)))


def _case_state(case_id: str) -> dict[str, Any]:
    return json.loads(_case_row(case_id)["state_json"] or "{}")


def _failures(case_id: str) -> str:
    from app.db import database as db

    rows = db.query_all(
        "SELECT step_key, status, error FROM pipeline_runs WHERE case_id=? AND status='failed'",
        (case_id,),
    )
    return "；".join(f"{r['step_key']}: {r['error']}" for r in rows) or "无失败步骤记录"


def _artifacts(case_id: str, kind: str) -> list[dict[str, Any]]:
    from app.db import database as db

    rows = db.query_all(
        "SELECT * FROM artifacts WHERE case_id=? AND kind=? ORDER BY version ASC", (case_id, kind)
    )
    return [dict(r) for r in rows]


async def _drive(case_id: str, task: asyncio.Task, answers: dict[str, Any]) -> list[str]:
    """轮询门控并按 step_key 提交预置答复，直到流水线结束；返回遇到的门控顺序。"""
    from app.pipelines import engine

    seen: list[str] = []
    while not task.done():
        pending = engine.get_pending(case_id)
        if pending is not None:
            step_key = pending["step_key"]
            if step_key not in seen:
                seen.append(step_key)
            try:
                engine.submit_input(case_id, step_key, answers.get(step_key, {}))
            except engine.PipelineConflictError:
                pass
        await asyncio.sleep(0.05)
    await task
    return seen


ANSWERS: dict[str, Any] = {
    "intake": {
        "topic": "计算集群的批量作业调度与限频控制",
        "patent_type": "invention",
        "contact": {"name": "张三", "phone": "13800000000", "email": "zhangsan@example.com"},
        "notes": "",
    },
    "points_mining": {
        "selected_ids": ["P1", "P2"],
        "working_title": WORKING_TITLE,
        "instruction": "以 P1 为主线融合 P2 的执行反馈闭环。",
    },
    "prior_art_search": {},          # 不传 selected_ids = 沿用默认全选
    "preview": {"action": "confirm", "feedback": ""},
    "deliver": {"skip": False, "choices": [], "apply": False, "note": ""},
}


# ---------------------------------------------------------------------------
# 1. 发明分支全流水线
# ---------------------------------------------------------------------------

_run: dict[str, Any] = {}


async def test_invention_pipeline_runs_to_deliver(
    client: TestClient, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """八步全跑通：intake → … → deliver，且四条 REPAIR 路径各触发一次。"""
    from app.pipelines import engine

    _install_search(monkeypatch)
    case_id = _new_case(client, "交底书发明测试")
    file_id = _upload_material(client, case_id)

    task = engine.start(
        case_id,
        _steps(case_id),
        run_group="initial",
        start_payload={"file_ids": [file_id], "instruction": "批量作业调度", "patent_type": "invention"},
    )
    seen = await asyncio.wait_for(_drive(case_id, task, ANSWERS), timeout=1200)

    assert _case_row(case_id)["status"] == "completed", _failures(case_id)
    _run["case_id"] = case_id
    _run["state"] = _case_state(case_id)
    _run["calls"] = list(fake_llm.calls)

    # 五个门控都出现过
    for gate in ("intake", "points_mining", "prior_art_search", "preview", "deliver"):
        assert gate in seen, f"未经过门控 {gate}：{seen}"

    # 四条 REPAIR 路径
    assert "chat:g2.repair1" in fake_llm.calls, "3.2 mermaid 语法错误未触发章节 REPAIR"
    assert "struct:g4.repair1" in fake_llm.calls, "formula_plan 数值例复算失败未触发 REPAIR"
    assert "struct:claim_bias.repair1" in fake_llm.calls, "basis_quote 校验未触发 REPAIR"
    assert "struct:audit" in fake_llm.calls


def test_case_title_and_terminology() -> None:
    """G0 骨架回填案件名称；术语表非空且含标题实词。"""
    state = _run["state"]
    assert _case_row(_run["case_id"])["title"] == CASE_TITLE
    assert state["case_title"] == CASE_TITLE

    sheet = state["terminology_sheet"]
    assert sheet["title_core_terms"] == TITLE_TERMS
    canon = [t["canonical"] for t in sheet["term_family"]]
    assert "负载画像" in canon and "批量作业调度" in canon
    assert "匹配分" in canon, "各章 terms_delta 未被剥离合并进术语表"


def test_prior_art_notes_from_search() -> None:
    """查新命中经消化改写入 prior_art_notes，URL 全部来自命中链接集合。"""
    state = _run["state"]
    notes = state["prior_art_notes"]
    assert len(notes) == 2
    assert {n["url"] for n in notes} == {URL_1, URL_2}
    assert all(n["scheme_summary"] for n in notes)
    assert state["prior_art"]["searched"] is True
    assert state["prior_art"]["selected_count"] == 2


def test_final_markdown_structure() -> None:
    """终稿：文头三条注意事项 + 六章齐全 + 无元信息禁词 + 补丁已应用。"""
    from app.services import disclosure_build as build_service

    text = _run["state"]["final_markdown"]

    # 文头（逐字取自 template_reference.md）
    assert text.lstrip().startswith("# 技术交底书")
    assert f"**案件名称**：{CASE_TITLE}" in text
    assert "**专利类型**：发明" in text
    assert "## 注意事项" in text
    for lead in ("（1）交底书应使代理人能看懂", "（2）技术的公开程度", "（3）在与代理人沟通时"):
        assert lead in text, f"文头注意事项缺条目：{lead}"
    assert "- 姓名：张三" in text

    # 六章齐全
    for heading in ("一、", "二、", "三、", "四、", "五、", "六、"):
        assert f"## {heading}" in text, f"终稿缺少章节：{heading}"
    for section in ("### 1.1 现有技术", "### 3.1 背景", "### 3.2 系统框图",
                    "### 3.3 模块功能说明", "### 3.4 系统流程说明",
                    "#### 3.4.1 符号与公式", "### 3.5 关键技术参数"):
        assert section in text, f"终稿缺少小节：{section}"

    # 自检补丁确定性应用（公式类 + 术语类各一条）
    assert PATCH_FORMULA_NEW in text and PATCH_FORMULA_OLD not in text
    assert PATCH_TERM_NEW in text and PATCH_TERM_OLD not in text

    # 元信息禁词与敏感词
    assert build_service.meta_leak_lint(text) == []
    for term in SENSITIVE:
        assert term not in text, f"终稿泄漏敏感词：{term}"

    # terms_delta 围栏块不入正文
    assert "terms_delta" not in text
    # 1.1 的链接全部来自命中集合
    assert build_service.url_lint(text, {URL_1, URL_2}) == []


def test_self_check_report() -> None:
    """自检：2 条补丁 100% 应用；公式类触发门禁复跑、术语类触发族替换。"""
    report = _run["state"]["self_check"]
    first = report["rounds"][0]
    assert first["applied"] == 2 and first["dropped"] == 0
    assert first["rate"] == 1.0 and first["low_rate"] is False
    assert set(first["kinds"]) == {"formula", "terminology"}
    assert report["formula_recheck"] is not None
    assert report["formula_recheck"]["ok"] is True
    assert report["rewritten"] == []
    assert report["mermaid_degraded"] == 0


def test_formula_gate_passed_after_repair() -> None:
    """G4：首版数值例复算不符 → REPAIR 后通过门禁，3.5 符号与计划逐字同形。"""
    from app.services import disclosure_build as build_service

    state = _run["state"]
    gate = state["formula_gate"]
    assert gate["needed"] is True
    assert gate["repairs"] == 1
    assert gate["ok"] is True and gate["errors"] == []
    assert gate["evaluated"]["mismatch"] == 0

    plan = state["formula_plan"]
    assert plan["numeric_example"]["result"]["s_{ij}"] == 0.68
    assert build_service.symbols_lint(state["chapters"]["g5"], plan) == []


def test_build_report_repairs() -> None:
    """章节报告：仅 3.2 触发一次 REPAIR，且没有遗留未解问题与降级图。"""
    report = _run["state"]["build_report"]
    by_key = {r["key"]: r for r in report["chapters"]}
    assert by_key["g2"]["repairs"] == 1
    assert all(r["repairs"] == 0 for k, r in by_key.items() if k != "g2")
    assert report["mermaid_degraded"] == 0
    assert report["unresolved"] == []
    assert report["skeleton"]["repairs"] == 0


def test_mermaid_png_artifacts() -> None:
    """两幅 mermaid 图真实渲染为 PNG 交付物。"""
    case_id = _run["case_id"]
    pngs = _artifacts(case_id, "mermaid_png")
    if not pngs:
        pytest.skip("mermaid 渲染不可用（本机浏览器/Playwright 问题）")
    assert len(pngs) == 2, "3.2 系统框图与 3.4 流程图应各出一幅 PNG"
    for item in pngs:
        assert Path(item["stored_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_md_and_docx_artifacts() -> None:
    """交付物：disclosure_md / disclosure_docx 各一版（version=1），Word 可打开且含六章与嵌图。"""
    from docx import Document

    case_id = _run["case_id"]
    mds = _artifacts(case_id, "disclosure_md")
    assert len(mds) == 1 and mds[0]["version"] == 1
    assert mds[0]["filename"].startswith(CASE_TITLE)
    saved = Path(mds[0]["stored_path"]).read_text(encoding="utf-8")
    assert saved == _run["state"]["final_markdown"]

    docxs = _artifacts(case_id, "disclosure_docx")
    assert len(docxs) == 1 and docxs[0]["version"] == 1
    path = Path(docxs[0]["stored_path"])

    doc = Document(str(path))
    texts = [p.text.strip() for p in doc.paragraphs]
    blob = "\n".join(texts)
    assert "技术交底书" in blob
    for heading in ("一、介绍相关技术背景", "二、针对上述缺点", "三、本发明技术方案的详细阐述",
                    "四、与现有技术相比", "五、本发明的技术关键点", "六、其它"):
        assert heading in blob, f"DOCX 缺少章节：{heading}"

    with zipfile.ZipFile(path) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/")]
    if not media:
        pytest.skip("Word 内没有嵌图（mermaid 预渲染不可用）")
    assert len(media) >= 2


def test_pdf_artifact_optional() -> None:
    """PDF 为可选同步产出：引擎可用时落 disclosure_pdf 交付物，否则明确 skip。"""
    case_id = _run["case_id"]
    pdfs = _artifacts(case_id, "disclosure_pdf")
    files = _run["state"]["deliver"]["files"]
    if not pdfs:
        pytest.skip(f"docx→pdf 引擎不可用：{files.get('pdf_error')}")
    assert Path(pdfs[-1]["stored_path"]).read_bytes()[:5] == b"%PDF-"
    assert files.get("pdf_engine") in ("word", "soffice")


def test_claim_bias_kept_after_repair() -> None:
    """偏向点：首版 basis_quote 落空 → REPAIR 后两个引用均为终稿逐字子串，未丢弃任何组。"""
    deliver = _run["state"]["deliver"]
    bias = deliver["claim_bias"]
    assert bias["repairs"] == 1 and bias["dropped"] == 0
    assert len(bias["groups"]) == 1

    text = _run["state"]["final_markdown"]
    for option in ("option_a", "option_b"):
        quote = bias["groups"][0][option]["basis_quote"]
        assert quote in text, f"{option} 的 basis_quote 不是终稿子串"
    # 用户选择仅存记录，不入正文
    assert deliver["claim_bias_selection"]["applied"] is False
    assert bias["groups"][0]["axis"] not in text


# ---------------------------------------------------------------------------
# 2. 查新失败 → 三选项门控 → 跳过（截断到前四步，避免重复跑成文）
# ---------------------------------------------------------------------------


async def test_prior_art_failure_gate_skip(
    client: TestClient, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检索失败时门控三选项；选择「跳过」后如实记录未检索，且不产生任何查新笔记。"""
    from app.pipelines import engine

    _install_search(monkeypatch, ok=False)
    case_id = _new_case(client, "交底书查新失败测试")
    file_id = _upload_material(client, case_id)

    steps = _steps(case_id)[:4]          # intake → material_scan → points_mining → prior_art_search
    task = engine.start(
        case_id, steps, run_group="initial", start_payload={"file_ids": [file_id]}
    )
    answers = {
        **ANSWERS,
        "prior_art_search": {"action": "skip", "reason": "本次先出稿，检索另行安排"},
    }
    seen = await asyncio.wait_for(_drive(case_id, task, answers), timeout=300)

    assert "prior_art_search" in seen
    state = _case_state(case_id)
    assert state["prior_art"]["skipped"] is True
    assert state["prior_art"]["searched"] is False
    assert state["prior_art"]["skip_reason"] == "本次先出稿，检索另行安排"
    assert state["prior_art_notes"] == []

    # 服务端如实落一条 manual_pending 查新记录（禁止编造检索结果）
    from app.services import cnipa

    latest = await cnipa.latest_query(case_id)
    assert latest is not None and latest.status == "manual_pending" and latest.skipped is True


# ---------------------------------------------------------------------------
# 3. 类型改判反问（材料信号偏实用新型）
# ---------------------------------------------------------------------------


async def test_type_suggest_gate(
    client: TestClient, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """材料信号明显偏结构时反问一次；用户维持发明则类型不变。"""
    from app.pipelines import engine

    digest = json.loads(json.dumps(MATERIAL_DIGEST))
    digest["type_signals"] = {"invention": 0.2, "utility_model": 0.9, "design": 0.0, "evidence": []}
    fake_llm.struct_script["digest.0"] = digest

    case_id = _new_case(client, "交底书类型改判测试")
    file_id = _upload_material(client, case_id)

    steps = _steps(case_id)[:2]          # intake → material_scan
    task = engine.start(case_id, steps, run_group="initial", start_payload={"file_ids": [file_id]})
    answers = {**ANSWERS, "material_scan": {"patent_type": "invention", "note": "确为方法类方案"}}
    seen = await asyncio.wait_for(_drive(case_id, task, answers), timeout=300)

    assert "material_scan" in seen, "类型信号不符时应反问一次"
    state = _case_state(case_id)
    assert state["type_suggest"]["suggested"] == "utility_model"
    assert state["type_suggest"]["changed"] is False
    assert _case_row(case_id)["patent_type"] == "invention"
    assert state["sensitive_terms"] == SENSITIVE


# ---------------------------------------------------------------------------
# 4. 单元级：类型分派、prompt 装配与各 lint
# ---------------------------------------------------------------------------


def test_build_steps_dispatch(client: TestClient) -> None:
    """步骤工厂按 patent_type 分派：发明八步；实用新型/外观九步（多一个 schema_fill）。"""
    from app.pipelines import registry

    steps = registry.build_steps({"module": "disclosure", "patent_type": None})
    assert [s.key for s in steps] == [
        "intake", "material_scan", "points_mining", "prior_art_search",
        "preview", "build", "self_check", "deliver",
    ]
    assert [s.gate for s in steps] == [
        "intake", None, "patent_points", "prior_art", "preview_confirm", None, None, "claim_bias",
    ]

    for ptype in ("utility_model", "design"):
        typed = registry.build_steps({"module": "disclosure", "patent_type": ptype})
        assert [s.key for s in typed] == [
            "intake", "material_scan", "points_mining", "schema_fill", "prior_art_search",
            "preview", "build", "self_check", "deliver",
        ], f"{ptype} 分支步骤表不符"
        assert typed[3].key == "schema_fill" and typed[3].gate == "schema_fill"
        assert typed[3].name_zh == "填表与线稿"
        assert [s.gate for s in typed] == [
            "intake", None, "patent_points", "schema_fill", "prior_art",
            "preview_confirm", None, None, "claim_bias",
        ], f"{ptype} 分支门控序列不符"


def test_iteration_still_invention_only() -> None:
    """迭代（合并/纠正）仍只支持发明：非发明类型触发 _require_invention 守卫。"""
    from types import SimpleNamespace

    from app.pipelines import disclosure_iterate
    from app.pipelines.disclosure import PatentTypeNotSupportedError, _require_invention

    for ptype, label in (("utility_model", "实用新型"), ("design", "外观设计")):
        with pytest.raises(PatentTypeNotSupportedError) as exc:
            disclosure_iterate.build_iteration_steps({"module": "disclosure", "patent_type": ptype})
        assert label in str(exc.value)

        with pytest.raises(PatentTypeNotSupportedError):
            _require_invention(SimpleNamespace(case={"patent_type": ptype}))

    # 发明不受影响
    assert len(disclosure_iterate.build_iteration_steps({"patent_type": "invention"})) == 5
    _require_invention(SimpleNamespace(case={"patent_type": "invention"}))


def test_prompt_parts_assemble() -> None:
    """模块 A 用到的所有 prompt 资产都能装配（缺文件会抛 KeyError）。"""
    from app.pipelines import disclosure as pipeline
    from app.services import assembler
    from app.services import disclosure_build as build_service

    for parts in (
        pipeline.INTAKE_PARTS, pipeline.DIGEST_PARTS, pipeline.POINTS_PARTS,
        pipeline.SEARCH_TERMS_PARTS, pipeline.ABSTRACT_DIGEST_PARTS, pipeline.PREVIEW_PARTS,
        pipeline.SELF_CHECK_PARTS, pipeline.CLAIM_BIAS_PARTS,
        build_service.G0_PARTS, build_service.G1_PARTS, build_service.G2_PARTS,
        build_service.G3_PARTS, build_service.G4_PARTS, build_service.G5_PARTS,
        build_service.G6_PARTS, build_service.G7_PARTS,
    ):
        prompt = assembler.assemble(parts, runtime_ctx={"case_card": {"module": "disclosure"}})
        assert len(prompt) > 200
        assert len(prompt.file_hashes) == len(parts)


def test_search_block_lint() -> None:
    """检索单位校验：块数、单字、泛词、整句长串、空格。"""
    from app.pipelines.disclosure import lint_search_blocks

    assert lint_search_blocks(["批量作业调度", "负载画像"]) == []
    assert any("2–8" in p for p in lint_search_blocks(["只有一个"]))
    assert any("泛词" in p for p in lint_search_blocks(["批量作业调度", "系统"]))
    assert any("过短" in p for p in lint_search_blocks(["批量作业调度", "调"]))
    assert any("过长" in p for p in lint_search_blocks(["批量作业调度", "知识库检索增强大语言模型的调度方法"]))
    assert any("空格" in p for p in lint_search_blocks(["批量作业调度", "负载 画像"]))


def test_step_label_and_module_lint() -> None:
    """3.4 步骤号可见标签 / 3.3 模块集合校验。"""
    from app.services import disclosure_build as build_service

    steps = SKELETON["steps"]
    assert build_service.step_label_lint(CH34, steps) == []

    bad = CH34.replace('S2["S2 计算批量作业调度匹配分"]', "S2[计算批量作业调度匹配分]")
    problems = build_service.step_label_lint(bad, steps)
    assert any("未写出步骤号" in p for p in problems)

    assert build_service.module_lint(CH3_FIXED, MODULES) == []
    assert build_service.module_lint(CH3_FIXED, [*MODULES, "不存在的模块"])


def test_url_and_meta_lint() -> None:
    """URL 白名单、元信息禁词与文末清洁。"""
    from app.services import disclosure_build as build_service

    assert build_service.url_lint(CH1, {URL_1, URL_2}) == []
    assert build_service.url_lint(CH1, {URL_1})          # URL_2 不在白名单 → 报错

    assert build_service.search_note_lint("检索说明：使用 cnipa_epub_search.py 检索。")
    assert build_service.search_note_lint("检索说明：在国家知识产权局专利公布公告系统检索。") == []

    dirty = "## 六、其它\n\n正文结尾。\n\n*本文件为 patent-disclosure-skill 仓库内教学示例，不构成法律承诺。*\n"
    cleaned, removed = build_service.clean_tail(dirty)
    assert removed and "教学示例" not in cleaned
    assert build_service.meta_leak_lint(cleaned) == []


def test_symbol_column_extraction() -> None:
    """3.4.1 与 3.5 两张表的「符号」列都能抽出，且与 formula_plan 同形。"""
    from app.services import disclosure_build as build_service

    symbols = build_service.symbol_column(CH341)
    assert symbols[:3] == ["s_{ij}", "b", "g"]
    assert build_service.symbols_lint(CH341, FORMULA_PLAN_FIXED) == []

    drifted = CH341.replace("| \\(g\\) | 节点内存的可用程度 | [0,1] |", "| \\(h\\) | 节点内存的可用程度 | [0,1] |")
    assert build_service.symbols_lint(drifted, FORMULA_PLAN_FIXED)


def test_section5_and_embodiment_lint() -> None:
    """第五章可实施机制 / 第六章 S 号 ⊆ 骨架步骤。"""
    from app.services import disclosure_build as build_service

    assert build_service.section5_lint(CH45) == []
    weak = CH45.replace(
        "- 以滑动窗口对多维资源采样值做平滑，得到可比较的负载画像，并将其作为派发决策的输入；",
        "- 有三个模块；",
    )
    assert build_service.section5_lint(weak)

    steps = SKELETON["steps"]
    assert build_service.embodiment_lint(CH6, steps) == []
    assert any("不存在的步骤号" in p for p in build_service.embodiment_lint(CH6 + "\n补充 S9 步骤。", steps))


def test_type_suggest_prompt_verbatim() -> None:
    """类型改判反问话术逐字取自 intake.md 的模板。"""
    from app.pipelines.disclosure import type_suggest_prompt

    text = type_suggest_prompt()
    assert "当前按「发明」推进" in text
    assert "是否改为实用新型" in text and "是否改为外观设计" in text
    assert "回复「维持发明」或指定类型即可。" in text
