/**
 * 脚本化 SSE 事件时间线（frontend-design.md §5 Mock 模式）。
 * 两条剧本：
 * - disclosureScript：一条完整交底书会话（8 步 chat 叙述 + intake / material_upload /
 *   type_suggest / patent_points / prior_art / preview_confirm / self_check / claim_bias /
 *   delivery 全部 kind 的 interaction_required + doc 通道流式输出含 mermaid 框图 / 流程图 /
 *   LaTeX 公式 / 符号表的六章交底书节选 + 两轮迭代产出 V2（合并）/ V3（纠正））；
 * - paperScript：论文转专利剧本节选；
 * - oaScript：审查意见答复全流程（notice_extract 的 oa_issues 门控 / retrieve 的
 *   oa_retrieve 门控（含 search_progress 与无命中空态）/ strategy 的 oa_strategy 门控 /
 *   draft 的 doc 通道意见陈述书 / review_deliver 的 confirm + delivery 与 MD/DOCX 交付）；
 * - readerScript：专利解读全流程（acquire / claim_tree 门控 / claim_deltas /
 *   note 的 doc 通道 11 节报告（含 mermaid、平台装配的权要树表与四种 callout）/
 *   lint_deliver 交付 reader_note_md），report.json 与流式 markdown 同源。
 * 时间线以「距上一事件的毫秒延迟」描述，mockServer 按速度因子回放。
 */

import type { ArtifactKind } from '../types/models'
import type { CaseSseEventMap, CaseSseEventName } from '../types/stream'

/**
 * 门控来源：
 * - 'input'  → POST /cases/{id}/pipeline/input（HITL 卡片确认）放行；
 * - 'iterate' → POST /cases/{id}/disclosure/iterate（chat composer 迭代）放行。
 */
export type MockGateKind = 'input' | 'iterate'

/** 单条时间线事件：delay = 距上一事件的延迟（速度 1x 时，ms）。 */
export interface MockTimelineEvent {
  delay: number
  event: CaseSseEventName
  data: CaseSseEventMap[CaseSseEventName]
  /**
   * 门控：发出该事件后暂停回放，直到对应来源的请求到达（mockServer 拦截）
   * 或超时兜底 —— 让 HITL 卡片与迭代在 mock 下可真正点击走通。
   * `true` 等价于 'input'。
   */
  gate?: boolean | MockGateKind
  /** 该门控的兜底放行时长（ms；缺省用 transport 的 gateTimeoutMs）。 */
  gateTimeoutMs?: number
}

export interface MockScript {
  key: 'disclosure' | 'paper' | 'reader' | 'oa' | 'schema_fill' | 'schema_fill_design'
  caseIdHint: string
  timeline: MockTimelineEvent[]
}

// ---------------------------------------------------------------------------
// 构造工具
// ---------------------------------------------------------------------------

/** 按大致 size 切块（中文流式手感：20–40 字符/块）。 */
export function chunkText(text: string, size = 28): string[] {
  const chunks: string[] = []
  for (let i = 0; i < text.length; i += size) chunks.push(text.slice(i, i + size))
  return chunks
}

function chat(stepKey: string, text: string, perChunkDelay = 45): MockTimelineEvent[] {
  return chunkText(text).map((piece, i) => ({
    delay: i === 0 ? 220 : perChunkDelay,
    event: 'llm_delta' as const,
    data: { step_key: stepKey, channel: 'chat' as const, text: piece },
  }))
}

function chatDone(stepKey: string): MockTimelineEvent {
  return { delay: 120, event: 'llm_done', data: { step_key: stepKey, channel: 'chat' } }
}

function doc(
  stepKey: string,
  docId: string,
  text: string,
  perChunkDelay = 36,
): MockTimelineEvent[] {
  return chunkText(text, 36).map((piece, i) => ({
    delay: i === 0 ? 260 : perChunkDelay,
    event: 'llm_delta' as const,
    data: { step_key: stepKey, channel: 'doc' as const, text: piece, doc_id: docId },
  }))
}


/**
 * 一拍步骤内进度心跳。
 *
 * mock 里也要有它：LiveProgress 的三种「不动」（等用户 / 等外部超时 / 事件流断）
 * 只有在能重现的情况下才改得动样式，否则每次调它都要真跑一次 40 分钟的流水线。
 */
function beat(
  stepKey: string,
  phase: string,
  opts: {
    index?: number
    total?: number
    detail?: string
    elapsedMs?: number
    idleMs?: number
    stalled?: boolean
    suspended?: boolean
    waitingFor?: string
    stallHint?: string
    delay?: number
  } = {},
): MockTimelineEvent {
  return {
    delay: opts.delay ?? 400,
    event: 'step_progress',
    data: {
      step_key: stepKey,
      phase,
      index: opts.index,
      total: opts.total,
      detail: opts.detail,
      elapsed_ms: opts.elapsedMs ?? 12_000,
      idle_ms: opts.idleMs ?? 800,
      stalled: opts.stalled ?? false,
      suspended: opts.suspended,
      waiting_for: opts.waitingFor,
      stall_hint: opts.stallHint,
    },
  }
}

function step(
  stepKey: string,
  status: CaseSseEventMap['step_status']['status'],
  nameZh: string,
  delay = 300,
): MockTimelineEvent {
  return {
    delay,
    event: 'step_status',
    data: { step_key: stepKey, status, name_zh: nameZh },
  }
}

/** 一条 interaction_required（HITL 卡片）。 */
function ask(
  stepKey: string,
  kind: string,
  prompt: string,
  extra: { schema?: unknown; default?: unknown } = {},
  delay = 320,
): MockTimelineEvent {
  return {
    delay,
    event: 'interaction_required',
    data: {
      step_key: stepKey,
      kind,
      prompt,
      schema: extra.schema ?? null,
      default: extra.default,
    },
  }
}

// ---------------------------------------------------------------------------
// 剧本一：交底书全流程（step_key 与后端 §4 模块① 一致）
// intake → material_scan → points_mining → prior_art_search → preview → build →
// self_check → deliver；交付后两轮迭代分别产出 V2（合并）与 V3（纠正）
// ---------------------------------------------------------------------------

const DISCLOSURE_DOC_ID = 'disclosure_md'
const DISCLOSURE_CASE_ID = 'demo-disclosure'

/** 交底书正文（六章节选，含 mermaid 系统框图 / 流程图、LaTeX 公式与符号表）。 */
const disclosureDocV1 = `# 技术交底书

**案件名称**：一种便携式术后康复监测装置及方法

**技术联系人**：
- 姓名：待填写
- 电话：待填写
- 邮箱：待填写

**专利类型**：发明

---

## 一、技术背景与现有技术

### 1.1 现有技术

本次检索在国家知识产权局专利公布公告网站进行，检索日期 2026 年 8 月 25 日，检索词为「术后 康复 监测 多模态」「可穿戴 体征 分级预警」「体征 缺失 补全」，共命中相关公开文献 12 篇，经摘要消化后筛出最接近的 3 篇：

1. **CN118512345A《一种穿戴式术后监护系统》**（申请人：某医疗科技有限公司；公开日 2024-08-16；来源：https://epub.cnipa.gov.cn/patent/CN118512345A ）。该文献公开一种腕带式术后体征采集装置，采集心率与体温并在数值超出预设阈值时本地报警；其未公开体征数据缺失时的补全机制，也未涉及基于趋势的风险分级。
2. **CN117998765A《康复训练依从性监测方法》**（申请人：某大学；公开日 2024-05-10；来源：https://epub.cnipa.gov.cn/patent/CN117998765A ）。该文献以运动传感器统计训练次数与时长评估依从性，与体征风险评估不构成耦合关系。
3. **CN116887654A《基于多参数融合的病房预警方法》**（申请人：某信息技术公司；公开日 2023-10-13；来源：https://epub.cnipa.gov.cn/patent/CN116887654A ）。该文献在病房网关侧融合多参数计算综合评分，依赖床旁供电与固定网关，未覆盖居家康复期。

### 1.2 现有技术存在的缺点

1. **监测覆盖期不足**：设备依赖床旁供电与固定网关，患者出院后的居家康复期出现监测空窗；
2. **单一体征刻画能力有限**：以心率或体温阈值报警为主，难以反映康复进程的整体状态；
3. **缺乏前瞻性**：阈值报警属事后响应，往往在并发症显性化之后才被发现；
4. **数据缺失无补偿**：佩戴松脱或信号丢失时直接丢弃样本，导致评分中断、预警失效。

## 二、本发明所要解决的技术问题

如何在低功耗便携形态下对术后康复期的多模态体征进行连续采集，并在部分体征通道数据缺失的条件下仍能给出稳定的康复风险评分与分级预警，从而把监测覆盖期由住院期延伸至完整康复期。

## 三、技术方案详细阐述

### 3.1 背景

术后康复期的体征变化具有缓慢漂移与突发波动并存的特点，单一通道阈值难以同时兼顾灵敏度与误报率。

本方案在便携式装置内完成多模态体征采集、完整性校验、缺失补全、特征提取与风险评分，仅将评分结果与分级预警上行，从而在有限功耗与带宽下维持连续监测。

装置整机以电池供电，通过低功耗无线链路与患者端应用及医护端工作台同步，形成居家康复期的闭环随访。

### 3.2 系统框图

\`\`\`mermaid
flowchart TB
  subgraph acq[" "]
    direction LR
    A["多模态体征采集单元"]
    B["数据完整性校验单元"]
    C["缺失数据补全单元"]
  end
  subgraph infer[" "]
    direction LR
    D["体征特征提取模块"]
    E["康复风险评估模型"]
  end
  F["分级预警输出单元"]
  G["医患双端同步单元"]
  A --> B
  B --> C
  B --> D
  C --> D
  D --> E
  E --> F
  F --> G
\`\`\`

### 3.3 模块功能说明

- **多模态体征采集单元**：以固定采样周期同步采集心率、血氧、体表温度与体位活动四路体征，向数据完整性校验单元输出带时间戳的原始体征帧。
- **数据完整性校验单元**：逐帧判定各通道的可用性并生成可用性掩码；完整帧直接进入特征提取，含缺失通道的帧转交缺失数据补全单元。
- **缺失数据补全单元**：依据同一通道的历史窗口与相关通道的当前取值补全缺失分量，并将补全结果标记为低置信样本回送特征提取模块。
- **体征特征提取模块**：在滑动窗口内计算各通道的均值、趋势斜率与波动度，归一化后形成体征特征向量。
- **康复风险评估模型**：对体征特征向量按掩码加权求和并归一化，输出康复风险评分。
- **分级预警输出单元**：依据风险评分所处区间输出一级/二级预警或维持观察，并抑制同一区间内的重复推送。
- **医患双端同步单元**：将评分、预警等级与关键片段同步至患者端与医护端，并在本地留档以备回溯。

### 3.4 系统流程说明

#### 流程图

\`\`\`mermaid
flowchart TD
  S1["S1 多模态体征同步采集"] --> S2["S2 逐帧完整性校验与掩码生成"]
  S2 -->|通道完整| S4["S4 康复风险评分计算"]
  S2 -->|存在缺失| S3["S3 缺失体征补全与置信标记"]
  S3 --> S4
  S4 --> S5["S5 风险分级与预警推送"]
  S5 --> S6["S6 医患双端同步与本地留档"]
\`\`\`

#### 流程说明

- **S1**：以 1 Hz 同步采集心率、血氧、体表温度与体位活动四路体征，形成带时间戳的体征帧。
- **S2**：逐帧判定各通道有效性，生成可用性掩码；掩码全为有效时直接转 S4。
- **S3**：对缺失通道以历史窗口均值与相关通道映射值补全，并将该帧标记为低置信样本。
- **S4**：在滑动窗口内提取体征特征并按掩码加权计算康复风险评分。
- **S5**：按评分区间输出预警等级，同区间内重复预警予以抑制。
- **S6**：将评分曲线与预警记录同步至医患双端并本地留档。

### 3.4.1 符号与公式

#### （1）体征侧符号

| 符号 | 含义 | 下标/量纲 |
| --- | --- | --- |
| $i$ | 体征通道索引 | $i=1,\\ldots,n$ |
| $x_i$ | 第 $i$ 路归一化体征特征 | $[0,1]$ |
| $w_i$ | 第 $i$ 路体征的评分权重 | 无量纲，$w_i>0$ |
| $m_i$ | 第 $i$ 路的可用性掩码 | $m_i \\in \\lbrace 0,1 \\rbrace$ |

#### （2）评分侧符号

| 符号 | 含义 | 下标/量纲 |
| --- | --- | --- |
| $b$ | 评分偏置项 | 无量纲 |
| $R$ | 康复风险评分 | $[0,1]$ |
| $\\theta_1$ | 一级预警阈值 | $[0,1]$ |
| $\\theta_2$ | 二级预警阈值 | $[0,1]$，$\\theta_2<\\theta_1$ |

康复风险评分由掩码加权体征特征经 Sigmoid 归一化得到：

$$
R = \\sigma\\left( \\frac{\\sum_{i=1}^{n} m_i w_i x_i}{\\sum_{i=1}^{n} m_i w_i} + b \\right), \\qquad \\sigma(z) = \\frac{1}{1 + e^{-z}}
$$

式中掩码 $m_i$ 使缺失通道不参与加权，分母随之收缩，从而在通道缺失时保持评分量纲一致；当 $R \\ge \\theta_1$ 时输出一级预警，当 $\\theta_2 \\le R < \\theta_1$ 时输出二级预警，否则维持观察。

**数值例**：取 $n=4$、$w=(0.4,0.3,0.2,0.1)$、$b=-0.5$，某帧血氧通道缺失（$m=(1,0,1,1)$）、$x=(0.82,-,0.61,0.45)$，则加权均值为 $(0.4\\times0.82+0.2\\times0.61+0.1\\times0.45)/(0.4+0.2+0.1)=0.706$，$R=\\sigma(0.206)\\approx0.551$，落入二级预警区间。

### 3.5 关键技术参数

| 参数 | 取值范围 | 说明 |
| --- | --- | --- |
| 采样频率 | 0.5–2 Hz | 兼顾功耗与趋势刻画 |
| 滑动窗口长度 | 60–300 s | 窗口越长趋势越稳、响应越慢 |
| 一级预警阈值 $\\theta_1$ | 0.70–0.80 | 默认 0.75 |
| 二级预警阈值 $\\theta_2$ | 0.50–0.60 | 默认 0.55 |
| 单通道最长补全时长 | ≤ 120 s | 超时则该通道置为不可用 |
| 电池续航 | ≥ 72 h | 连续监测条件下 |

## 四、与现有技术相比的优点

1. 监测周期由住院期延伸至完整康复期，数据连续性显著提升；
2. 掩码加权评分使单通道缺失不中断评估，评分可用率由约 78% 提升至约 96%；
3. 以趋势特征替代瞬时阈值，预警提前量平均提升约 40%；
4. 装置整机重量小于 150 g，佩戴依从性好。

## 五、技术关键点和欲保护点

1. 一种便携式术后康复监测装置，包括多模态体征采集单元、数据完整性校验单元、缺失数据补全单元、体征特征提取模块、康复风险评估模型与分级预警输出单元；其特征在于，所述数据完整性校验单元生成通道可用性掩码，所述康复风险评估模型按所述掩码对体征特征加权归一化得到康复风险评分。
2. 缺失数据补全单元以同通道历史窗口与相关通道映射值补全缺失分量，并将补全帧标记为低置信样本参与评分。
3. 分级预警输出单元按评分区间输出分级预警，并在同区间内抑制重复推送。

## 六、其它

### 实施例

**实施例一：居家康复期连续监测**。患者出院后佩戴本装置，装置按 1 Hz 同步采集四路体征（S1），逐帧完成完整性校验（S2）后计算康复风险评分（S4）；连续 5 个窗口评分自 0.42 升至 0.71 时，装置输出二级预警并推送至医护端（S5–S6），随访确认为切口局部感染早期表现。

**实施例二：信号短时丢失下的评分保持**。患者夜间翻身致血氧探头短时脱落，校验单元将该通道掩码置零（S2），补全单元以前 90 s 历史窗口补全并标记低置信（S3）；评分仍按剩余三通道加权归一化输出（S4），评分曲线未出现中断。
`

/** 迭代一（合并新材料）追加的「## 合并摘要（留档）」。 */
const disclosureMergeSummary = `

---

## 合并摘要（留档）

- 本轮合并新增材料《居家随访试验记录_2026Q2.xlsx》，新增有效样本 214 例、随访周期 12 周。
- 受影响章节：**四、与现有技术相比的优点**（评分可用率由约 96% 更新为 95.7%，预警提前量由约 40% 更新为 38.6%，均改为以本次试验记录为依据的实测口径）。
- 受影响章节：**3.5 关键技术参数**（滑动窗口长度推荐值收窄为 120–240 s，依据为新样本下的误报率拐点）。
- 新增材料未引入新的技术手段，专利点集合与保护点表述保持不变，故未触发补充查新。
- 术语表未发生漂移，全文「体征特征」「康复风险评分」表述保持一致。
`

/** 迭代二（纠正）追加的「## 纠正摘要（留档）」。 */
const disclosureCorrectionSummary = `

---

## 纠正摘要（留档）

- 纠正点 1（术语类）：全文「体征特征」统一替换为「生理体征特征」，共替换 14 处，符号表与 3.4.1 叙述同步更新。
- 纠正点 2（事实类）：3.5 中电池续航由「≥ 72 h」修正为「≥ 96 h（样机实测 103 h）」，依据为随附的功耗实测记录。
- 纠正点 3（保护点类）：第五章第 1 项补入「所述掩码随通道可用性逐帧更新」的限定，避免被解读为静态权重方案。
- 公式与符号未变更，未触发公式门禁复跑。
`

/** 候选专利点（invention/points.md 契约：id/title/background/innovation/distinction/feasibility/score）。 */
const disclosureCandidates = [
  {
    id: 'P1',
    title: '掩码加权的多模态体征风险评分方法',
    background:
      '术后居家监测中佩戴松脱、信号丢失频发，现有方案在通道缺失时直接丢弃样本，导致评分中断。',
    innovation:
      '以逐帧通道可用性掩码参与加权求和并同步收缩分母，使缺失通道不参与评分而评分量纲保持一致。',
    distinction:
      '最接近的 CN118512345A 仅对单通道做阈值报警，未公开掩码加权与分母收缩；CN116887654A 的多参数融合依赖全通道完整。',
    feasibility: '在现有 MCU 上即可实现，单帧计算量为 O(n)，实测新增功耗小于 3%。',
    score: 88,
  },
  {
    id: 'P2',
    title: '基于趋势预测的康复风险分级预警策略',
    background: '阈值报警属事后响应，并发症显性化后才被发现，缺乏提前量。',
    innovation:
      '以滑动窗口趋势斜率与波动度构造特征，按评分区间输出分级预警，并在同区间内抑制重复推送。',
    distinction: '现有技术为瞬时阈值报警，未公开分级区间与同区间抑制机制。',
    feasibility: '已在 214 例随访样本上验证，预警提前量平均提升约 40%。',
    score: 82,
  },
  {
    id: 'P3',
    title: '低功耗佩戴式多模态采集硬件拓扑',
    background: '多路体征同步采集通常带来功耗与体积的双重压力。',
    innovation: '四路传感前端共用一路低功耗采集总线，按体位活动状态动态调整采样占空比。',
    distinction: '与现有腕带式装置的差异集中在硬件布局，属结构改进，创造性高度相对有限。',
    feasibility: '样机已完成，整机重量 148 g，续航实测 103 h。',
    score: 64,
  },
]

/** 查新命中（prior_art/abstract_digest.md 字段：pub_number / title / applicant / link / abstract）。 */
const disclosureHits = [
  {
    id: 'H1',
    pub_number: 'CN118512345A',
    title: '一种穿戴式术后监护系统',
    applicant: '某医疗科技有限公司',
    date: '2024-08-16',
    link: 'https://epub.cnipa.gov.cn/patent/CN118512345A',
    abstract:
      '本发明公开一种穿戴式术后监护系统，包括腕带式采集装置与病房网关。采集装置采集心率与体温并周期性上报，网关在数值超出预设阈值时触发本地与远端报警，同时记录报警时刻的体征快照供医护回溯。该系统未涉及体征通道缺失时的补全处理，报警判定以单通道瞬时阈值为准。',
  },
  {
    id: 'H2',
    pub_number: 'CN116887654A',
    title: '基于多参数融合的病房预警方法',
    applicant: '某信息技术股份有限公司',
    date: '2023-10-13',
    link: 'https://epub.cnipa.gov.cn/patent/CN116887654A',
    abstract:
      '该方法在病房网关侧接收床旁监护仪的多路参数，按固定权重加权求和得到综合评分，并依据评分与既定阈值的比较结果推送预警。方法要求各路参数同时可用，未公开参数缺失情形下的处理方式；系统依赖床旁供电与固定网关部署。',
  },
  {
    id: 'H3',
    pub_number: 'CN117998765A',
    title: '康复训练依从性监测方法',
    applicant: '某大学',
    date: '2024-05-10',
    link: 'https://epub.cnipa.gov.cn/patent/CN117998765A',
    abstract:
      '该方法通过运动传感器统计康复训练的次数、时长与动作幅度，形成依从性指标并生成随访报告，用于评估患者居家训练的执行情况。该方法关注训练行为本身，与生理体征的风险评估不构成耦合关系。',
  },
  {
    id: 'H4',
    pub_number: 'CN115774321A',
    title: '一种生理信号缺失值插补装置',
    applicant: '某电子科技有限公司',
    date: '2023-03-10',
    link: 'https://epub.cnipa.gov.cn/patent/CN115774321A',
    abstract:
      '该装置针对生理信号采集中的短时缺失，采用历史均值与线性插值相结合的方式补全缺失点，以维持波形连续性。补全结果直接参与后续波形分析，未区分补全样本与实测样本的置信度差异，也未与风险评分环节耦合。',
  },
]

/** 自检检查项（A7 self_check 补丁契约摘要）。 */
const disclosureChecks = [
  { id: 'C1', label: '文头模板与案件名称行规范', note: '案件名称与标题实词一致' },
  { id: 'C2', label: '六章结构完整、编号连续', note: '一至六章齐备' },
  { id: 'C3', label: '1.1 检索说明含数据库名与检索词、URL 可核验', note: '3 条来源链接均在命中集合内' },
  { id: 'C4', label: '术语一致性（体征特征 / 康复风险评分）', note: '已统一 3 处表述' },
  { id: 'C5', label: 'mermaid 图可渲染、S 号标签连续', note: 'S1–S6 齐全' },
  { id: 'C6', label: '公式符号与符号表逐字同形', note: '已修正 1 处上标维度' },
  { id: 'C7', label: '保护点术语属于术语表核心词', note: '第五章 3 项均命中' },
  { id: 'C8', label: '元信息 / 脚本名 / 文件名泄漏扫描', note: '未检出' },
]

/** 权利要求偏向（invention/claim_bias.md：basis_quote 必须是终稿子串）。 */
const disclosureBiasGroups = [
  {
    axis: '保护主体：装置 or 方法',
    option_a: {
      label: '以装置为主保护点（便携式术后康复监测装置）',
      basis_quote: '一种便携式术后康复监测装置，包括多模态体征采集单元、数据完整性校验单元',
    },
    option_b: {
      label: '以方法为主保护点（康复风险评分方法）',
      basis_quote: '所述康复风险评估模型按所述掩码对体征特征加权归一化得到康复风险评分',
    },
  },
  {
    axis: '技术侧重：缺失补全 or 分级预警',
    option_a: {
      label: '侧重缺失通道补全与低置信标记',
      basis_quote: '缺失数据补全单元以同通道历史窗口与相关通道映射值补全缺失分量',
    },
    option_b: {
      label: '侧重风险分级与重复预警抑制',
      basis_quote: '分级预警输出单元按评分区间输出分级预警，并在同区间内抑制重复推送',
    },
  },
]

interface DisclosureDelivery {
  kind: ArtifactKind
  artifact_id: string
  filename: string
}

/** 某一版本的三件交付物（MD / DOCX / PDF）。 */
function disclosureFiles(version: number, stamp: string): DisclosureDelivery[] {
  const base = `一种便携式术后康复监测装置及方法_${stamp}`
  return [
    { kind: 'disclosure_md', artifact_id: `art-disc-v${version}-md`, filename: `${base}.md` },
    { kind: 'disclosure_docx', artifact_id: `art-disc-v${version}-docx`, filename: `${base}.docx` },
    { kind: 'disclosure_pdf', artifact_id: `art-disc-v${version}-pdf`, filename: `${base}.pdf` },
  ]
}

/** 一个版本的 doc_version + 三条 artifact_created。 */
function disclosureVersionEvents(
  version: number,
  stamp: string,
  createdAt: string,
  iterationType: 'initial' | 'merge' | 'correction',
  summary: string | null,
): MockTimelineEvent[] {
  const files = disclosureFiles(version, stamp)
  const events: MockTimelineEvent[] = [
    {
      delay: 320,
      event: 'doc_version',
      data: {
        doc_id: DISCLOSURE_DOC_ID,
        version_id: files[0].artifact_id,
        version,
        kind: 'disclosure_md',
        filename: files[0].filename,
        artifact_id: files[0].artifact_id,
        downloads: files,
        iteration_type: iterationType,
        summary,
        created_at: createdAt,
      },
    },
  ]
  for (const file of files) {
    events.push({
      delay: 160,
      event: 'artifact_created',
      data: {
        id: file.artifact_id,
        case_id: DISCLOSURE_CASE_ID,
        version,
        kind: file.kind,
        filename: file.filename,
        stored_path: `outputs/${DISCLOSURE_CASE_ID}/${file.filename}`,
        run_group: iterationType,
        iteration_type: iterationType,
        summary,
        source_artifact_id: null,
        created_at: createdAt,
      },
    })
  }
  return events
}

function buildDisclosureTimeline(): MockTimelineEvent[] {
  const t: MockTimelineEvent[] = []

  t.push({ delay: 350, event: 'case_title', data: { title: '一种便携式术后康复监测装置' } })

  // --- 1. 边界确认（intake） ---------------------------------------------
  t.push(step('intake', 'running', '边界确认', 200))
  t.push(
    ...chat(
      'intake',
      '你好，我是引途医疗专利智能体。为便于挖掘专利点与组织交底书，先确认三项边界：技术主题、专利类型与文头联系人；可跳过，我会按已有材料推断并注明假设。',
    ),
    chatDone('intake'),
  )
  t.push(
    ask('intake', 'intake', '请确认本案边界（技术主题 / 专利类型 / 文头联系人）', {
      schema: {
        type: 'object',
        hint: 'intake',
        properties: {
          topic: { type: 'string', title: '技术主题或产品模块' },
          patent_type: {
            type: 'string',
            title: '专利类型',
            enum: ['invention', 'utility_model', 'design', 'unsure'],
          },
          contact: { type: 'object', title: '文头技术联系人' },
          contact_placeholder: { type: 'boolean', title: '使用占位符' },
        },
      },
      default: {
        topic: '便携式术后康复监测装置',
        patent_type: 'invention',
        contact_placeholder: true,
        contact: { name: '', phone: '', email: '' },
      },
    }),
  )
  t.push({ ...step('intake', 'waiting_user', '边界确认', 120), gate: 'input' })
  t.push(
    ...chat(
      'intake',
      '已确认边界：\n\n- **技术主题**：便携式术后康复监测装置\n- **专利类型**：发明（默认）\n- **文头联系人**：使用占位符「待填写」\n- **默认假设**：先按发明推进；材料扫描后若更偏结构改进，我会再反问一次。',
    ),
    chatDone('intake'),
  )
  t.push(step('intake', 'done', '边界确认', 300))

  // --- 2. 材料扫描（material_scan） --------------------------------------
  t.push(step('material_scan', 'running', '材料扫描', 250))
  t.push(
    ...chat(
      'material_scan',
      '正在逐份消化已上传材料。如还有补充的结构图、实验数据或现有方案说明，可在下方卡片继续上传，也可直接确认继续。',
    ),
    chatDone('material_scan'),
  )
  t.push(
    ask(
      'material_scan',
      'material_upload',
      '可补充上传项目材料（结构图 / 实验数据 / 现有方案说明），或直接确认继续',
      {
        schema: {
          type: 'object',
          properties: { file_ids: { type: 'array', items: { type: 'string' } } },
        },
      },
    ),
  )
  t.push({ ...step('material_scan', 'waiting_user', '材料扫描', 120), gate: 'input' })
  t.push({
    delay: 420,
    event: 'search_progress',
    data: { phase: 'digest', message: '正在消化材料 1/3：技术方案说明.docx…', count: 1 },
  })
  t.push(
    beat('material_scan', '消化材料《技术方案说明.docx》', {
      index: 1, total: 3, elapsedMs: 8_000, waitingFor: '模型',
    }),
    beat('material_scan', '消化材料《实验数据.xlsx》', {
      index: 2, total: 3, elapsedMs: 26_000, waitingFor: '模型', delay: 900,
    }),
  )
  t.push({
    delay: 900,
    event: 'search_progress',
    data: { phase: 'digest', message: '正在消化材料 3/3：样机结构图.pdf…', count: 3 },
  })
  t.push(
    beat('material_scan', '消化材料《样机结构图.pdf》', {
      index: 3, total: 3, elapsedMs: 41_000, detail: '长材料分片处理：第 2/4 片',
      waitingFor: '模型',
    }),
    // 静默一段时间但还没到卡住阈值：如实说在等谁、静了多久
    beat('material_scan', '消化材料《样机结构图.pdf》', {
      index: 3, total: 3, elapsedMs: 74_000, idleMs: 33_000, waitingFor: '模型', delay: 1200,
    }),
  )
  t.push(
    ...chat(
      'material_scan',
      '材料消化完成：共 3 份，抽取技术点 14 条、部件 9 个，检出敏感表述 2 处（已计入案件级敏感词表，成文时自动脱敏）。\n\n注意：材料中出现较多**卡扣、支架、散热布局**等结构改进描述。',
    ),
    chatDone('material_scan'),
  )
  t.push(
    ask(
      'material_scan',
      'type_suggest',
      '当前按「发明」推进。材料更像：\n- 产品形状/构造改进（卡扣、散热布局、支架等）→ 是否改为实用新型？\n- 外观造型/图案/配色 → 是否改为外观设计？\n回复「维持发明」或指定类型即可。',
      {
        default: {
          current_type: 'invention',
          suggested_type: 'utility_model',
          reason:
            '结构改进描述占材料约 40%，但材料同时包含掩码加权评分与分级预警策略，仍具备方法层面的创造性；改判为实用新型将无法保护评分方法。',
        },
      },
    ),
  )
  t.push({ ...step('material_scan', 'waiting_user', '材料扫描', 120), gate: 'input' })
  t.push(step('material_scan', 'done', '材料扫描', 300))

  // --- 3. 专利点挖掘（points_mining） ------------------------------------
  t.push(step('points_mining', 'running', '专利点挖掘', 250))
  t.push(
    ...chat(
      'points_mining',
      '基于材料共挖掘出 **3 个候选专利点**，已按创造性高度与可实施性打分。建议保留 P1、P2 两点并融合为一个技术方案：P1 解决「缺失通道下评分中断」，P2 解决「预警缺乏提前量」，二者在同一评分链路上互为支撑；P3 属结构改进，创造性高度有限，可留作后续实用新型。',
    ),
    chatDone('points_mining'),
  )
  t.push(
    ask('points_mining', 'patent_points', '请勾选写入交底书的专利点（可多选并融合）', {
      schema: {
        type: 'object',
        properties: {
          selected_ids: { type: 'array', items: { type: 'string' } },
          fuse: { type: 'boolean' },
        },
      },
      default: {
        candidates: disclosureCandidates,
        recommended: ['P1', 'P2'],
        fusion_note:
          '以 P1 的掩码加权评分为主线，把 P2 的趋势分级预警作为评分下游环节，形成「采集—校验—补全—评分—分级预警」的单一技术方案。',
      },
    }),
  )
  t.push({ ...step('points_mining', 'waiting_user', '专利点挖掘', 120), gate: 'input' })
  t.push(step('points_mining', 'done', '专利点挖掘', 300))

  // --- 4. 联网查新（prior_art_search） -----------------------------------
  t.push(step('prior_art_search', 'running', '联网查新', 250))
  t.push({
    delay: 400,
    event: 'search_progress',
    data: {
      phase: 'terms',
      message: '已构造 3 组检索式：术后 康复 监测 多模态 / 可穿戴 体征 分级预警 / 体征 缺失 补全',
      count: 3,
    },
  })
  t.push({
    delay: 1100,
    event: 'search_progress',
    data: { phase: 'cnipa', message: '正在检索国家知识产权局专利公布公告网站…', count: 0 },
  })
  t.push({
    delay: 1200,
    event: 'search_progress',
    data: { phase: 'cnipa', message: '3 组检索式合计命中 12 条公开文献', count: 12 },
  })
  t.push({
    delay: 1000,
    event: 'search_progress',
    data: { phase: 'digest', message: '正在消化命中文献摘要，筛选最接近的 4 条…', count: 4 },
  })
  t.push(
    ...chat(
      'prior_art_search',
      '查新完成：最接近的现有技术为 **CN118512345A《一种穿戴式术后监护系统》**，其未公开缺失通道掩码加权与趋势分级预警。请确认要纳入交底书 1.1 的文献；如有已知在先文献，可在卡片内手动补录。',
    ),
    chatDone('prior_art_search'),
  )
  t.push(
    ask('prior_art_search', 'prior_art', '请确认纳入 1.1 的命中文献（可取消勾选无关项或手动补录）', {
      schema: {
        type: 'object',
        properties: { hit_ids: { type: 'array', items: { type: 'string' } } },
      },
      default: { hits: disclosureHits, failed: false },
    }),
  )
  t.push({ ...step('prior_art_search', 'waiting_user', '联网查新', 120), gate: 'input' })
  t.push(step('prior_art_search', 'done', '联网查新', 300))

  // --- 5. 预览确认（preview） --------------------------------------------
  t.push(step('preview', 'running', '预览确认', 250))
  t.push(
    ...chat('preview', '交底书摘要已就绪，请确认方向后我再开始逐章成文。'),
    chatDone('preview'),
  )
  t.push(
    ask('preview', 'preview_confirm', '请确认本案摘要；如需调整方向，可在卡片内写明后由我重做', {
      schema: { type: 'object', properties: { confirmed: { type: 'boolean' } } },
      default: {
        name: '一种便携式术后康复监测装置及方法',
        patent_type: 'invention',
        problem:
          '如何在低功耗便携形态下连续监测术后康复期多模态体征，并在部分通道缺失时仍给出稳定的风险评分与分级预警。',
        modules: [
          '多模态体征采集单元',
          '数据完整性校验单元',
          '缺失数据补全单元',
          '体征特征提取模块',
          '康复风险评估模型',
          '分级预警输出单元',
          '医患双端同步单元',
        ],
        distinction:
          '相对最接近的 CN118512345A：本案以逐帧通道可用性掩码参与加权并同步收缩分母，使缺失通道不中断评分；并以趋势特征替代瞬时阈值输出分级预警且抑制同区间重复推送。',
      },
    }),
  )
  t.push({ ...step('preview', 'waiting_user', '预览确认', 120), gate: 'input' })
  t.push(step('preview', 'done', '预览确认', 300))

  // --- 6. 生成全文（build；doc 通道流式） --------------------------------
  t.push(step('build', 'running', '生成全文', 250))
  t.push(
    ...chat('build', '开始按骨架逐章成文，正文将在右侧文档面板实时呈现 →'),
    chatDone('build'),
  )
  // 成文是全流程最长的一步：八个阶段逐个报，分子是真实的阶段序号
  t.push(
    beat('build', '产出骨架（案件名称 / 模块 / 流程步骤 / 章节大纲）', {
      index: 1, total: 8, elapsedMs: 6_000, waitingFor: '模型',
    }),
    beat('build', '撰写第一、二章（技术领域与背景技术）', {
      index: 2, total: 8, elapsedMs: 52_000, waitingFor: '模型', delay: 800,
    }),
    beat('build', '撰写 3.1–3.3（技术方案总体与模块框图）', {
      index: 3, total: 8, elapsedMs: 121_000, waitingFor: '模型', delay: 800,
    }),
  )
  t.push(...doc('build', DISCLOSURE_DOC_ID, disclosureDocV1))
  t.push({
    delay: 200,
    event: 'llm_done',
    data: { step_key: 'build', channel: 'doc', doc_id: DISCLOSURE_DOC_ID },
  })
  t.push(step('build', 'done', '生成全文', 300))

  // --- 7. 自检（self_check） ---------------------------------------------
  t.push(step('self_check', 'running', '自检', 250))
  t.push(
    ...chat(
      'self_check',
      '全文自检进行中：共核查 8 项，已自动应用 5 处补丁（术语统一 3 处、符号同形 1 处、检索说明措辞 1 处）。',
    ),
    chatDone('self_check'),
  )
  t.push(
    ask('self_check', 'self_check', '自检结果如下，确认后进入交付', {
      schema: { type: 'object', properties: { acknowledged: { type: 'boolean' } } },
      default: {
        items: disclosureChecks,
        patch_count: 5,
        unresolved: ['3.4.1 中权重训练数据来源建议补充脱敏说明（涉及合作方数据，需人工确认口径）'],
      },
    }),
  )
  t.push({ ...step('self_check', 'waiting_user', '自检', 120), gate: 'input' })
  t.push(step('self_check', 'done', '自检', 300))

  // --- 8. 交付（deliver）：claim_bias → delivery -------------------------
  t.push(step('deliver', 'running', '交付', 250))
  t.push(...disclosureVersionEvents(1, '20260825143210', '2026-08-25T14:32:10+08:00', 'initial', null))
  t.push(
    ask('deliver', 'claim_bias', '定稿已生成。以下两组偏向仅作记录留档，不写入正文：', {
      schema: { type: 'object', properties: { choices: { type: 'array' } } },
      default: { groups: disclosureBiasGroups },
    }),
  )
  t.push({ ...step('deliver', 'waiting_user', '交付', 120), gate: 'input' })
  t.push(
    ask('deliver', 'delivery', '交底书 V1 已就绪，可下载 MD / DOCX / PDF，或在下方输入框继续迭代', {
      default: { files: disclosureFiles(1, '20260825143210') },
    }),
  )
  t.push({
    delay: 400,
    event: 'log',
    data: { level: 'info', message: '流水线 initial 轮次全部完成' },
  })
  t.push(step('deliver', 'done', '交付', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'initial', status: 'done' } })

  // --- 迭代一：合并新材料 → V2 ------------------------------------------
  // 等待 chat composer 触发 POST /cases/{id}/disclosure/iterate
  t.push({
    delay: 200,
    event: 'log',
    data: { level: 'info', message: '可在下方输入框补充材料或提出修改意见，进入迭代轮次' },
    gate: 'iterate',
    gateTimeoutMs: 900_000,
  })
  t.push(step('material_scan', 'running', '材料扫描', 250))
  t.push(
    ...chat(
      'material_scan',
      '意图判定：**合并迭代**（新增材料）。正在消化《居家随访试验记录_2026Q2.xlsx》并识别增量。',
    ),
    chatDone('material_scan'),
  )
  t.push({
    delay: 700,
    event: 'search_progress',
    data: { phase: 'digest', message: '新增有效样本 214 例，识别增量数据点 6 处', count: 214 },
  })
  t.push(step('material_scan', 'done', '材料扫描', 300))
  t.push(step('build', 'running', '生成全文', 250))
  t.push(
    ...chat(
      'build',
      '受影响章节为 **3.5 关键技术参数** 与 **四、与现有技术相比的优点**，正在按增量重写这两节；未引入新技术手段，故不触发补充查新。',
    ),
    chatDone('build'),
  )
  t.push(...doc('build', DISCLOSURE_DOC_ID, disclosureMergeSummary))
  t.push({
    delay: 200,
    event: 'llm_done',
    data: { step_key: 'build', channel: 'doc', doc_id: DISCLOSURE_DOC_ID },
  })
  t.push(step('build', 'done', '生成全文', 300))
  t.push(step('deliver', 'running', '交付', 250))
  t.push(
    ...disclosureVersionEvents(
      2,
      '20260825151845',
      '2026-08-25T15:18:45+08:00',
      'merge',
      '合并《居家随访试验记录_2026Q2.xlsx》：更新 3.5 参数区间与第四章实测口径',
    ),
  )
  t.push(
    ask('deliver', 'delivery', '交底书 V2（合并迭代）已就绪，版本历史可逐版下载', {
      default: { files: disclosureFiles(2, '20260825151845') },
    }),
  )
  t.push(step('deliver', 'done', '交付', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'merge-1', status: 'done' } })

  // --- 迭代二：纠正 → V3 --------------------------------------------------
  t.push({
    delay: 200,
    event: 'log',
    data: { level: 'info', message: '如需纠正术语、事实或保护点表述，可继续在下方输入框提出' },
    gate: 'iterate',
    gateTimeoutMs: 900_000,
  })
  t.push(step('build', 'running', '生成全文', 250))
  t.push(
    ...chat(
      'build',
      '意图判定：**纠正迭代**。纠正点分类：术语类 1 项、事实类 1 项、保护点类 1 项；术语类将执行全文族替换，其余按补丁定位修改。',
    ),
    chatDone('build'),
  )
  t.push(...doc('build', DISCLOSURE_DOC_ID, disclosureCorrectionSummary))
  t.push({
    delay: 200,
    event: 'llm_done',
    data: { step_key: 'build', channel: 'doc', doc_id: DISCLOSURE_DOC_ID },
  })
  t.push(step('build', 'done', '生成全文', 300))
  t.push(step('self_check', 'running', '自检', 250))
  t.push(
    ...chat(
      'self_check',
      '纠正后复检通过：术语「生理体征特征」全文同形（14 处），符号表与公式未受影响，保护点表述已补入掩码逐帧更新限定。',
    ),
    chatDone('self_check'),
  )
  t.push(step('self_check', 'done', '自检', 300))
  t.push(step('deliver', 'running', '交付', 250))
  t.push(
    ...disclosureVersionEvents(
      3,
      '20260825160312',
      '2026-08-25T16:03:12+08:00',
      'correction',
      '纠正术语「体征特征」→「生理体征特征」、续航参数与第五章保护点限定',
    ),
  )
  t.push(
    ask('deliver', 'delivery', '交底书 V3（纠正迭代）已就绪，可下载或继续迭代', {
      default: { files: disclosureFiles(3, '20260825160312') },
    }),
  )
  t.push(step('deliver', 'done', '交付', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'correction-1', status: 'done' } })

  return t
}

export const disclosureScript: MockScript = {
  key: 'disclosure',
  caseIdHint: DISCLOSURE_CASE_ID,
  timeline: buildDisclosureTimeline(),
}


// ---------------------------------------------------------------------------
// 剧本二：论文转专利（p2p 全流程；step_key 与后端 §4 模块② 一致）
// input_check → extraction → draft → rules_check → content_review → drawings → build
// ---------------------------------------------------------------------------

const PAPER_DOC_ID = 'patent_content'

/** 附图占位图（内联 SVG data URI，供 FiguresPreviewCard 预览/下载）。 */
function figureDataUri(title: string, subtitle: string): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 400" width="300" height="400">
<rect width="300" height="400" fill="#ffffff"/>
<rect x="16" y="16" width="268" height="368" fill="none" stroke="#9ca3af" stroke-width="1.5"/>
<rect x="48" y="72" width="204" height="64" fill="#eef2ff" stroke="#6366f1" stroke-width="1.5" rx="6"/>
<rect x="48" y="184" width="204" height="64" fill="#eef2ff" stroke="#6366f1" stroke-width="1.5" rx="6"/>
<rect x="48" y="296" width="204" height="64" fill="#eef2ff" stroke="#6366f1" stroke-width="1.5" rx="6"/>
<path d="M150 136 L150 184 M150 248 L150 296" stroke="#6366f1" stroke-width="1.5"/>
<text x="150" y="44" font-size="16" text-anchor="middle" fill="#111827">${title}</text>
<text x="150" y="110" font-size="12" text-anchor="middle" fill="#374151">${subtitle}</text>
<text x="150" y="222" font-size="12" text-anchor="middle" fill="#374151">图卷积特征聚合</text>
<text x="150" y="334" font-size="12" text-anchor="middle" fill="#374151">病灶分割掩膜输出</text>
</svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

/** mock PatentContent（content_review 卡预览 + GET /cases/{id}/p2p/content）。 */
export const paperPatentContent = {
  invention_name: '一种基于图神经网络的医学影像病灶分割方法及装置',
  abstract:
    '本发明公开了一种基于图神经网络的医学影像病灶分割方法及装置。该方法将待分割医学影像超像素化后构建邻接图，经图卷积网络逐层聚合邻域特征得到节点级病灶概率，再依据节点分类结果聚合生成病灶分割掩膜。相较于逐像素卷积分割方案，本发明在保持精度的同时显著降低推理时延。',
  abstract_drawing: {
    figure: '图1',
    reason: '图1 完整体现了「超像素构图—图卷积聚合—掩膜输出」的核心处理链路，适合作为摘要附图。',
  },
  claims: [
    '一种基于图神经网络的医学影像病灶分割方法，其特征在于，包括：获取待分割医学影像并划分为若干超像素节点；基于所述超像素节点的灰度与纹理特征构建邻接图；将所述邻接图输入训练好的图卷积网络，逐层聚合邻域特征得到节点级病灶概率；根据所述节点级病灶概率聚合生成病灶分割掩膜。',
    '根据权利要求 1 所述的方法，其特征在于，所述划分为若干超像素节点采用 SLIC 算法，超像素数量取值范围为 500–2000。',
    '根据权利要求 1 所述的方法，其特征在于，所述邻接图的边权由相邻超像素的灰度直方图距离与空间距离加权确定。',
    '根据权利要求 1 所述的方法，其特征在于，所述图卷积网络的层数为 3–5 层，且各层之间设有残差连接。',
    '一种基于图神经网络的医学影像病灶分割装置，其特征在于，包括超像素划分单元、邻接图构建单元、图卷积推理单元与掩膜聚合单元，用于执行权利要求 1–4 任一项所述的方法。',
  ],
  description: {
    technical_field: '本发明涉及医学影像处理技术领域，具体涉及一种基于图神经网络的医学影像病灶分割方法及装置。',
    background:
      '现有病灶分割方案多采用逐像素卷积网络，存在计算量随分辨率平方增长、边界处易过分割、对小病灶召回不足等问题。',
    invention_content:
      '本发明的目的在于提供一种兼顾精度与推理效率的病灶分割方案：以超像素为图节点大幅压缩计算规模，以图卷积聚合邻域上下文提升边界一致性，并通过残差连接缓解深层图卷积的过平滑。',
    brief_description_of_drawings:
      '图1 为本发明方法的总体流程示意图；图2 为图卷积网络结构示意图；图3 为本发明与基线方法的对比实验结果示意图。',
    detailed_description:
      '下面结合附图对本发明的具体实施方式进行说明。实施例一：对输入的 512×512 CT 影像执行 SLIC 超像素划分，得到约 1200 个超像素节点……',
  },
  drawings: [
    { number: 1, caption: '总体流程示意图' },
    { number: 2, caption: '图卷积网络结构示意图' },
    { number: 3, caption: '对比实验结果示意图' },
  ],
  gaps: [],
}

/** mock 附图清单（drawings 步骤门控载荷 + FiguresPreviewCard 预览）。 */
export const paperFigures = [
  {
    id: 'fig-1',
    number: 1,
    caption: '总体流程示意图',
    filename: '图1_总体流程示意图.svg',
    url: figureDataUri('图 1', '超像素划分与构图'),
  },
  {
    id: 'fig-2',
    number: 2,
    caption: '图卷积网络结构示意图',
    filename: '图2_网络结构示意图.svg',
    url: figureDataUri('图 2', '输入影像预处理'),
  },
  {
    id: 'fig-3',
    number: 3,
    caption: '对比实验结果示意图',
    filename: '图3_对比实验结果.svg',
    url: figureDataUri('图 3', '数据集与评价指标'),
  },
]

/** mock 交付文件（build 步骤门控载荷 + doc_version.downloads）。 */
export const paperDeliveryFiles = [
  {
    kind: 'patent_content_json',
    artifact_id: 'art-p2p-json-v1',
    filename: '图神经网络病灶分割_20260825151002.json',
  },
  {
    kind: 'patent_docx',
    artifact_id: 'art-p2p-docx-v1',
    filename: '图神经网络病灶分割_20260825151002.docx',
  },
  {
    kind: 'patent_pdf',
    artifact_id: 'art-p2p-pdf-v1',
    filename: '图神经网络病灶分割_20260825151002.pdf',
  },
] as const

const paperDocExcerpt = `# 权利要求书（节选）

**1.** 一种基于图神经网络的医学影像病灶分割方法，其特征在于，包括：

获取待分割医学影像，并将其划分为若干超像素节点；

基于所述超像素节点的灰度与纹理特征构建邻接图 $G=(V,E)$；

将所述邻接图输入训练好的图卷积网络，逐层聚合邻域特征：

$$
h_v^{(l+1)} = \\mathrm{ReLU}\\Big( W^{(l)} \\sum_{u \\in \\mathcal{N}(v)} \\frac{h_u^{(l)}}{\\sqrt{d_u d_v}} + b^{(l)} \\Big)
$$

根据节点分类结果聚合生成病灶分割掩膜。

**2.** 根据权利要求 1 所述的方法，其特征在于，所述超像素划分采用 SLIC 算法，超像素数量 $K$ 取值范围为 500–2000。

# 说明书摘要

本发明公开了一种基于图神经网络的医学影像病灶分割方法与装置。该方法将影像超像素化后构建邻接图，经图卷积网络逐层聚合得到节点级病灶概率，最终生成分割掩膜。

| 指标 | U-Net 基线 | 本发明 |
| --- | --- | --- |
| Dice 系数 | 0.861 | **0.907** |
| 推理时延 | 148 ms | 96 ms |

# 说明书（节选）

## 技术领域

本发明涉及医学影像处理技术领域，具体涉及一种基于图神经网络的医学影像病灶分割方法及装置。

## 具体实施方式

\`\`\`mermaid
flowchart TD
  A[输入医学影像] --> B[SLIC 超像素划分]
  B --> C[邻接图构建]
  C --> D[图卷积逐层聚合]
  D --> E[节点级病灶概率]
  E --> F[掩膜聚合输出]
\`\`\`
`

function buildPaperTimeline(): MockTimelineEvent[] {
  const t: MockTimelineEvent[] = []

  t.push({ delay: 350, event: 'case_title', data: { title: '图神经网络病灶分割 · 论文转专利' } })

  // 1. input_check：解析论文 → 补齐关键信息（form 门控）→ 选择生成模式（mode_select 门控）
  t.push(step('input_check', 'running', '输入校验', 200))
  t.push(
    ...chat(
      'input_check',
      '论文《Graph-based Lesion Segmentation》解析完成（14 页，检出 6 幅图、23 条公式）。请先补齐立案所需的关键信息。',
    ),
    chatDone('input_check'),
  )
  t.push({
    delay: 300,
    event: 'interaction_required',
    data: {
      step_key: 'input_check',
      kind: 'form',
      prompt: '请确认发明名称与申请人信息（留空则由系统按论文题目自动生成）',
      schema: {
        type: 'object',
        properties: {
          invention_name: { type: 'string', title: '发明名称', maxLength: 200 },
          applicant: { type: 'string', title: '申请人' },
          keep_original_figures: { type: 'boolean', title: '优先复用论文原图' },
        },
        required: ['invention_name'],
      },
      default: {
        invention_name: '一种基于图神经网络的医学影像病灶分割方法及装置',
        applicant: '引途医疗科技有限公司',
        keep_original_figures: true,
      },
    },
  })
  t.push({ ...step('input_check', 'waiting_user', '输入校验', 120), gate: true })
  t.push(
    ...chat('input_check', '信息已确认。请选择本次转换的执行模式。', 40),
    chatDone('input_check'),
  )
  t.push({
    delay: 260,
    event: 'interaction_required',
    data: {
      step_key: 'input_check',
      kind: 'mode_select',
      prompt: '请选择生成模式：直接生成一次成稿，或在关键节点逐步人工确认',
      schema: {
        type: 'object',
        properties: { mode: { type: 'string', enum: ['direct', 'human_in_loop'] } },
      },
      default: { mode: 'direct' },
    },
  })
  t.push({ ...step('input_check', 'waiting_user', '输入校验', 120), gate: true })
  t.push(step('input_check', 'done', '输入校验', 300))

  // 2. extraction：抽取技术要素与来源映射
  t.push(step('extraction', 'running', '要素抽取', 250))
  t.push({
    delay: 400,
    event: 'search_progress',
    data: { phase: 'extraction', message: '正在抽取技术问题 / 技术手段 / 技术效果，并建立来源映射…', count: 0 },
  })
  t.push({
    delay: 900,
    event: 'search_progress',
    data: { phase: 'extraction', message: '已抽取 18 条技术要素，全部命中论文原文出处', count: 18 },
  })
  t.push(
    ...chat('extraction', '要素抽取完成：核心创新点为「超像素构图 + 图卷积聚合」，附图来源已锁定论文图 1/图 3/图 5。'),
    chatDone('extraction'),
  )
  t.push(step('extraction', 'done', '要素抽取', 400))

  // 3. draft：四次调用生成五大部分（doc 通道流式）
  t.push(step('draft', 'running', '撰写五大部分', 250))
  t.push(
    ...chat('draft', '开始撰写摘要、摘要附图、权利要求书、说明书与附图说明，正文流入右侧文档面板 →'),
    chatDone('draft'),
  )
  t.push(...doc('draft', PAPER_DOC_ID, paperDocExcerpt))
  t.push({
    delay: 200,
    event: 'llm_done',
    data: { step_key: 'draft', channel: 'doc', doc_id: PAPER_DOC_ID },
  })
  t.push(step('draft', 'done', '撰写五大部分', 300))

  // 4. rules_check：claims_lint + 忠实性审计
  t.push(step('rules_check', 'running', '规则校验', 250))
  t.push(
    ...chat(
      'rules_check',
      '规则校验完成：\n\n- **权项引用**：5 项权利要求引用关系合法 ✅\n- **禁用词**：未检出 15 类禁用表述 ✅\n- **忠实性审计**：全部技术特征均可回溯至论文原文 ✅\n- **提示**：权利要求 4 的层数区间建议在说明书中补充实验依据 ⚠️',
    ),
    chatDone('rules_check'),
  )
  t.push(step('rules_check', 'done', '规则校验', 400))

  // 5. content_review：五大部分人工审阅（review 门控）
  t.push(step('content_review', 'running', '内容审阅', 250))
  t.push({
    delay: 320,
    event: 'interaction_required',
    data: {
      step_key: 'content_review',
      kind: 'review',
      prompt: '请审阅五大部分内容；如需修改可在「编辑 JSON」中直接调整后保存',
      schema: { type: 'object', properties: { confirmed: { type: 'boolean' } } },
      default: paperPatentContent,
    },
  })
  t.push({ ...step('content_review', 'waiting_user', '内容审阅', 120), gate: true })
  t.push(step('content_review', 'done', '内容审阅', 300))

  // 6. drawings：附图生成与预览（figures_preview 门控）
  t.push(step('drawings', 'running', '附图生成', 250))
  t.push({
    delay: 500,
    event: 'search_progress',
    data: { phase: 'drawings', message: '正在生成专利附图（SVG / PNG 三版式）…', count: 3 },
  })
  t.push({
    delay: 320,
    event: 'interaction_required',
    data: {
      step_key: 'drawings',
      kind: 'figures_preview',
      prompt: '已生成 3 幅专利附图，请确认选用（点击可查看大图并下载）',
      schema: { type: 'object', properties: { figure_ids: { type: 'array', items: { type: 'string' } } } },
      default: { figures: paperFigures },
    },
  })
  t.push({ ...step('drawings', 'waiting_user', '附图生成', 120), gate: true })
  t.push(step('drawings', 'done', '附图生成', 300))

  // 7. build：JSON → docx → pdf 交付（delivery 门控）
  t.push(step('build', 'running', '构建交付', 250))
  t.push({
    delay: 400,
    event: 'doc_version',
    data: {
      doc_id: PAPER_DOC_ID,
      version_id: 'art-p2p-json-v1',
      version: 1,
      kind: 'patent_content_json',
      filename: '图神经网络病灶分割_20260825151002.json',
      artifact_id: 'art-p2p-json-v1',
      downloads: [...paperDeliveryFiles],
      iteration_type: 'initial',
      summary: null,
      created_at: '2026-08-25T15:10:02+08:00',
    },
  })
  for (const file of paperDeliveryFiles) {
    t.push({
      delay: 180,
      event: 'artifact_created',
      data: {
        id: file.artifact_id,
        case_id: 'demo-paper',
        version: 1,
        kind: file.kind,
        filename: file.filename,
        stored_path: `outputs/demo-paper/${file.filename}`,
        run_group: 'initial',
        iteration_type: 'initial',
        summary: null,
        source_artifact_id: null,
        created_at: '2026-08-25T15:10:02+08:00',
      },
    })
  }
  t.push({
    delay: 300,
    event: 'interaction_required',
    data: {
      step_key: 'build',
      kind: 'delivery',
      prompt: '专利申请文件 V1 已就绪，可下载 JSON / DOCX / PDF，或在下方输入框继续迭代修改',
      schema: null,
      default: { files: [...paperDeliveryFiles] },
    },
  })
  t.push({ ...step('build', 'waiting_user', '构建交付', 120), gate: true })
  t.push({ delay: 300, event: 'log', data: { level: 'info', message: '流水线 initial 轮次全部完成' } })
  t.push(step('build', 'done', '构建交付', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'initial', status: 'done' } })

  return t
}

export const paperScript: MockScript = {
  key: 'paper',
  caseIdHint: 'demo-paper',
  timeline: buildPaperTimeline(),
}

// ---------------------------------------------------------------------------
// 剧本三：专利解读（reader 全流程；step_key 与后端 §4 模块③ 一致）
// acquire → claim_tree（claim_tree 门控）→ claim_deltas → note（doc 通道 11 节报告）
// → lint_deliver（reader_note_md 交付）
// ---------------------------------------------------------------------------

const READER_DOC_ID = 'reader_note'
const READER_CASE_ID = 'demo-reader'
const FENCE = '```'

/** 报告 meta（字段名同后端 models/reader.py ReaderMeta）。 */
export const readerMeta = {
  pub_no: 'CN117994321A',
  type: 'invention',
  type_label: '发明',
  ipc: ['G06T 7/00', 'A61B 6/03'],
  title: '一种基于多模态影像融合的冠脉狭窄自动评估方法及装置',
  applicants: ['引途医疗科技（杭州）有限公司'],
  domain: '医学影像 / 计算机辅助诊断',
  pub_date: '2024-05-24',
  read_date: '2026-08-25',
  perspective: '研发',
  evidence_scope: 'full_text',
  confidence_speculative: false,
}

/** C2 权要树：10 项，2 项独权（方法 / 装置），权 6 为「权 4 或 5」多引。 */
export const readerClaims = [
  {
    no: 1,
    type: 'independent',
    refs: [] as number[],
    ref_mode: 'none',
    preamble: '一种基于多模态影像融合的冠脉狭窄自动评估方法，其特征在于，包括：',
    features: [
      { fid: '1-F1', text: '获取同一受检者的冠脉 CTA 序列与心肌灌注 MR 序列，并按解剖标志点完成配准；' },
      { fid: '1-F2', text: '对配准后的 CTA 序列提取血管中心线，沿中心线按预设步长重建管腔横截面；' },
      { fid: '1-F3', text: '将所述横截面的管腔面积序列与对应位置的灌注特征拼接为通道张量，输入双流卷积网络；' },
      { fid: '1-F4', text: '由所述双流卷积网络输出逐点狭窄率，并在狭窄率超过阈值的连续区段上生成病变标注。' },
    ],
  },
  {
    no: 2,
    type: 'dependent',
    refs: [1],
    ref_mode: 'single',
    preamble: '根据权利要求 1 所述的方法，其特征在于，',
    features: [
      { fid: '2-F1', text: '所述配准为两阶段配准：第一阶段以冠脉开口与心尖为标志点做刚性配准，第二阶段做弹性配准。' },
    ],
  },
  {
    no: 3,
    type: 'dependent',
    refs: [2],
    ref_mode: 'single',
    preamble: '根据权利要求 2 所述的方法，其特征在于，',
    features: [{ fid: '3-F1', text: '所述弹性配准的形变场经心动周期门控加权平均后使用。' }],
  },
  {
    no: 4,
    type: 'dependent',
    refs: [1],
    ref_mode: 'single',
    preamble: '根据权利要求 1 所述的方法，其特征在于，',
    features: [
      { fid: '4-F1', text: '所述双流卷积网络的灌注分支输入为心肌血流量 MBF 与灌注达峰时间 TTP 构成的双通道图。' },
    ],
  },
  {
    no: 5,
    type: 'dependent',
    refs: [1],
    ref_mode: 'single',
    preamble: '根据权利要求 1 所述的方法，其特征在于，',
    features: [
      { fid: '5-F1', text: '所述阈值按血管段自适应设定，近段取 50%，远段取 70%。' },
    ],
  },
  {
    no: 6,
    type: 'dependent',
    refs: [4, 5],
    ref_mode: 'alternative',
    preamble: '根据权利要求 4 或 5 所述的方法，其特征在于，',
    features: [
      { fid: '6-F1', text: '在生成病变标注之后，进一步输出该区段的血流储备分数 FFR 估计值。' },
    ],
  },
  {
    no: 7,
    type: 'dependent',
    refs: [3],
    ref_mode: 'single',
    preamble: '根据权利要求 3 所述的方法，其特征在于，',
    features: [{ fid: '7-F1', text: '所述门控加权的权重由 R 波间期归一化后的相位距离确定。' }],
  },
  {
    no: 8,
    type: 'independent',
    refs: [] as number[],
    ref_mode: 'none',
    preamble: '一种基于多模态影像融合的冠脉狭窄自动评估装置，其特征在于，包括：',
    features: [
      { fid: '8-F1', text: '配准模块，用于对冠脉 CTA 序列与心肌灌注 MR 序列做解剖标志点配准；' },
      { fid: '8-F2', text: '重建模块，用于沿血管中心线按预设步长重建管腔横截面；' },
      { fid: '8-F3', text: '融合评估模块，用于拼接管腔面积序列与灌注特征并经双流卷积网络输出逐点狭窄率；' },
      { fid: '8-F4', text: '标注模块，用于在狭窄率超过阈值的连续区段上生成病变标注。' },
    ],
  },
  {
    no: 9,
    type: 'dependent',
    refs: [8],
    ref_mode: 'single',
    preamble: '根据权利要求 8 所述的装置，其特征在于，',
    features: [{ fid: '9-F1', text: '所述配准模块包括刚性配准单元与弹性配准单元。' }],
  },
  {
    no: 10,
    type: 'dependent',
    refs: [8],
    ref_mode: 'single',
    preamble: '根据权利要求 8 所述的装置，其特征在于，',
    features: [{ fid: '10-F1', text: '还包括报告模块，用于将病变标注导出为 DICOM 结构化报告。' }],
  },
]

/** C3 每权白话增量（12–40 字）。 */
export const readerDeltas = [
  { claim: 1, delta: '把管腔面积序列与心肌灌注特征拼成双流输入判狭窄' },
  { claim: 2, delta: '配准分两阶段，第二阶段改用弹性配准' },
  { claim: 3, delta: '弹性形变场按心动周期门控做加权平均' },
  { claim: 4, delta: '灌注分支明确取 MBF 与 TTP 双通道' },
  { claim: 5, delta: '狭窄阈值按近段 50%、远段 70% 自适应' },
  { claim: 6, delta: '在病变标注之外再给出 FFR 估计值' },
  { claim: 7, delta: '门控权重由 R 波相位距离确定' },
  { claim: 8, delta: '同一方法的装置化，四个模块一一对应' },
  { claim: 9, delta: '配准模块细分为刚性与弹性两个单元' },
  { claim: 10, delta: '增加 DICOM 结构化报告导出' },
]

/** C2 登记的疑难项（claim_tree 门控载荷）。 */
export const readerAmbiguities = [
  {
    no: 6,
    kind: 'multi_ref',
    issue: '权 6 写作「根据权利要求 4 或 5 所述的方法」，为择一引用，需确认按「或」并列展开还是收敛为单一引用',
    candidates: [4, 5],
    chosen: null,
    reason: '原文使用「或」，按《审查指南》通常按择一引用处理；如申请人另有主张可指定单一引用',
  },
  {
    no: 9,
    kind: 'citation_basis',
    issue: '权 9 的「弹性配准单元」在权 8 中无对应上位表述，引用基础存疑',
    candidates: [8],
    chosen: 8,
    reason: '权 8 的配准模块可视为其上位概念，暂按引用权 8 处理',
  },
]

// ---- 报告块（后端 ReportBlock 形态：{type, content}）------------------------

interface ReaderTableContent {
  headers: string[]
  rows: string[][]
  markdown: string
}

interface ReaderTreeRow {
  structure: string
  no: number
  type: string
  refs: number[]
  ref_mode: string
  delta: string
}

type ReaderMockBlock =
  | { type: 'markdown'; content: string }
  | { type: 'mermaid'; content: string }
  | { type: 'table'; content: ReaderTableContent }
  | { type: 'claim_tree'; content: { summary: string; rows: ReaderTreeRow[]; markdown: string } }
  | {
      type: 'callout-meta' | 'callout-claim' | 'callout-grounding' | 'callout-warning'
      content: { title: string; markdown: string }
    }

interface ReaderMockSection {
  id: string
  title: string
  blocks: ReaderMockBlock[]
}

function mdTable(headers: string[], rows: string[][]): string {
  const line = (cells: string[]): string => `| ${cells.join(' | ')} |`
  return [line(headers), line(headers.map(() => '---')), ...rows.map(line)].join('\n')
}

function tableBlock(headers: string[], rows: string[][]): ReaderMockBlock {
  return { type: 'table', content: { headers, rows, markdown: mdTable(headers, rows) } }
}

function callout(
  kind: 'meta' | 'claim' | 'grounding' | 'warning',
  title: string,
  markdown: string,
): ReaderMockBlock {
  return { type: `callout-${kind}`, content: { title, markdown } }
}

/** 站内 block 类型 → 模板 callout 记号（report_template.md 映射表的左列）。 */
const CALLOUT_MARKS: Record<string, string> = {
  'callout-meta': 'patent-meta',
  'callout-claim': 'patent-claim',
  'callout-grounding': 'grounding',
  'callout-warning': 'warning',
}

/** block → markdown（doc 通道流式文本按此还原，与 report.json 同源）。 */
function blockToMarkdown(block: ReaderMockBlock): string {
  switch (block.type) {
    case 'markdown':
      return block.content
    case 'mermaid':
      return `${FENCE}mermaid\n${block.content}\n${FENCE}`
    case 'table':
    case 'claim_tree':
      return block.content.markdown
    default: {
      const mark = CALLOUT_MARKS[block.type] ?? 'warning'
      const body = block.content.markdown
        .split('\n')
        .map((line) => (line ? `> ${line}` : '>'))
        .join('\n')
      return `> [!${mark}] ${block.content.title}\n${body}`
    }
  }
}

/** 平台装配的权要树块（结构 / 权 / 本项新增三列，与后端 build_claim_tree_block 同形）。 */
function readerTreeBlock(): ReaderMockBlock {
  const glyphs: Record<number, string> = {
    1: '◆',
    2: '├─',
    3: '│  └─',
    4: '├─',
    5: '└─',
    6: '│  └─',
    7: '│  │  └─',
    8: '◆',
    9: '├─',
    10: '└─',
  }
  const deltaBy = new Map(readerDeltas.map((d) => [d.claim, d.delta]))
  const rows: ReaderTreeRow[] = readerClaims.map((claim) => ({
    structure: glyphs[claim.no] ?? '◆',
    no: claim.no,
    type: claim.type,
    refs: claim.refs,
    ref_mode: claim.ref_mode,
    delta: deltaBy.get(claim.no) ?? '',
  }))
  return {
    type: 'claim_tree',
    content: {
      summary: '共 10 项 · 独立 2 / 从属 8',
      rows,
      markdown: mdTable(
        ['结构', '权', '本项新增'],
        rows.map((row) => [`\`${row.structure}\``, String(row.no), row.delta]),
      ),
    },
  }
}

/** 11 节报告（report_template.md 逐字保留的节标题）。 */
const readerSections: ReaderMockSection[] = [
  {
    id: 's1',
    title: '一、一句话',
    blocks: [
      callout(
        'meta',
        '著录项',
        [
          '- **公开号**：CN117994321A',
          '- **名称**：一种基于多模态影像融合的冠脉狭窄自动评估方法及装置',
          '- **类型**：发明',
          '- **IPC**：G06T 7/00；A61B 6/03',
          '- **申请人**：引途医疗科技（杭州）有限公司',
          '- **公开日**：2024-05-24',
          '- **证据范围**：全文',
        ].join('\n'),
      ),
      {
        type: 'markdown',
        content:
          '这件专利把**冠脉 CTA 的管腔几何**与**心肌灌注 MR 的功能信息**配准后拼成一路双流输入，让网络在判断狭窄率时同时看见「管子有多窄」和「血到底够不够」，从而把形态学狭窄与功能性缺血一次判完。',
      },
    ],
  },
  {
    id: 's2',
    title: '二、连贯叙事',
    blocks: [
      {
        type: 'markdown',
        content:
          '**要解决的问题**：单纯依据 CTA 的管腔狭窄率判读，常与患者实际缺血情况不符——解剖上「看着窄」未必真缺血，临床需要再做一次有创 FFR 检查。\n\n**怎么做的**：先把 CTA 与灌注 MR 按冠脉开口、心尖等解剖标志点配准（说明书 [0032]–[0035]），再沿血管中心线按固定步长重建管腔横截面，把每个截面的面积序列与该位置的灌注特征拼接成通道张量，交由双流卷积网络逐点回归狭窄率，最后在越过自适应阈值的连续区段上生成病变标注（[0041]–[0048]）。',
      },
      {
        type: 'mermaid',
        content: [
          // TD（纵向）：报告面板栏宽有限，纵向流程图缩略后仍可读
          'flowchart TD',
          '  A[冠脉 CTA 序列] --> C[解剖标志点配准]',
          '  B[心肌灌注 MR 序列] --> C',
          '  C --> D[血管中心线提取]',
          '  D --> E[管腔横截面重建]',
          '  E --> F[面积序列 + 灌注特征<br/>通道张量拼接]',
          '  F --> G[双流卷积网络]',
          '  G --> H[逐点狭窄率]',
          '  H --> I{超过自适应阈值?}',
          '  I -- 是 --> J[生成病变标注]',
          '  I -- 否 --> K[标记为非显著病变]',
        ].join('\n'),
      },
      {
        type: 'markdown',
        content:
          '**达到的效果**：说明书 [0056] 给出的对照实验中，与单模态 CTA 基线相比，功能性缺血判读的准确率由 0.78 提升至 0.91，且无需追加有创检查。',
      },
    ],
  },
  {
    id: 's3',
    title: '三、权利要求树',
    blocks: [
      {
        type: 'markdown',
        content:
          '下表由平台按权利要求书装配：`◆` 为独立权利要求，缩进表示引用关系，「本项新增」只写该项相对其引用项多出来的限定。',
      },
      readerTreeBlock(),
      {
        type: 'markdown',
        content:
          '权 6 写作「根据权利要求 4 或 5 所述的方法」，为择一引用，已按「或」并列处理（在权 4 与权 5 下各出现一次）。',
      },
    ],
  },
  {
    id: 's4',
    title: '四、独立权利要求精读',
    blocks: [
      callout(
        'claim',
        '权利要求 1（方法）',
        [
          '**前序**：一种基于多模态影像融合的冠脉狭窄自动评估方法。',
          '',
          '- **F1 配准**：CTA 与灌注 MR 按解剖标志点配准 —— 这是把两种模态放到同一坐标系的前提。',
          '- **F2 重建**：沿血管中心线按预设步长重建管腔横截面 —— 决定了狭窄率是「逐点」而非「逐段」给出。',
          '- **F3 融合**：面积序列与灌注特征拼接为通道张量后输入双流卷积网络 —— **本案的保护重心**。',
          '- **F4 判定**：逐点狭窄率越阈的连续区段生成病变标注 —— 把逐点输出收敛为可读的病变段。',
        ].join('\n'),
      ),
      {
        type: 'markdown',
        content:
          '权 8 是权 1 的装置化表达，四个模块与 F1–F4 一一对应，规避手段与方法权项一致；实施时若只落地其中三个模块，一般不落入权 8 的保护范围。',
      },
    ],
  },
  {
    id: 's5',
    title: '五、专利内术语表',
    blocks: [
      tableBlock(
        ['专利内用语', '本专利中的含义', '首次出处'],
        [
          ['通道张量', '把管腔面积序列与灌注特征在通道维拼接后得到的输入张量', '[0041]'],
          ['双流卷积网络', '几何分支与灌注分支并行、末端融合的卷积网络', '[0043]'],
          ['自适应阈值', '按血管近/远段分别取 50%/70% 的狭窄率判定阈值', '[0047]'],
          ['门控加权平均', '按心动周期相位对弹性形变场加权平均', '[0038]'],
        ],
      ),
    ],
  },
  {
    id: 's6',
    title: '六、特征—说明书—附图对照',
    blocks: [
      tableBlock(
        ['权项特征', '说明书段落', '附图'],
        [
          ['F1 解剖标志点配准', '[0032]–[0035]', '图 1'],
          ['F2 中心线与横截面重建', '[0036]–[0040]', '图 2'],
          ['F3 通道张量与双流网络', '[0041]–[0046]', '图 3'],
          ['F4 越阈区段标注', '[0047]–[0048]', '图 4'],
        ],
      ),
    ],
  },
  {
    id: 's7',
    title: '七、和现有技术的差别',
    blocks: [
      {
        type: 'markdown',
        content:
          '说明书背景技术部分（[0003]–[0006]）承认：逐点重建管腔截面、以卷积网络回归狭窄率均非首创。**本案与现有技术的差别落在 F3**——把功能性灌注特征与几何特征在同一网络内融合，而非先各自判读再做后处理投票。',
      },
      callout(
        'grounding',
        '判断依据',
        '以上差别取自说明书自认（[0003]–[0006]）与权利要求书的特征分布，未做检索比对；是否具备创造性需另行检索评估。',
      ),
    ],
  },
  {
    id: 's8',
    title: '八、阅读建议',
    blocks: [
      {
        type: 'markdown',
        content:
          '1. 先读权 1 的 F3 与说明书 [0041]–[0046]，这是全案的技术重心；\n2. 再读 [0047] 的自适应阈值，它决定了权 5、权 6 的从属层次；\n3. 若关心规避设计，重点看 F3 的「拼接为通道张量」——改为双网络后融合是否仍落入等同，需个案判断。',
      },
    ],
  },
  {
    id: 's9',
    title: '九、技术应用场景',
    blocks: [
      callout(
        'grounding',
        '应用场景（推断，非说明书自认）',
        '- 冠脉 CTA 影像后处理工作站中的自动狭窄标注；\n- 胸痛中心的分诊：先做无创评估，筛出需进一步做 FFR 的患者；\n- 影像科科研随访队列的批量再处理。',
      ),
    ],
  },
  {
    id: 's10',
    title: '十、附录：行业坐标与公开线索',
    blocks: [
      {
        type: 'markdown',
        content:
          '- IPC：G06T 7/00（图像分析）、A61B 6/03（断层摄影）；\n- 同族与法律状态：本报告未做检索，需在官方数据库另行核实；\n- 说明书共标注段落 68 段、附图 4 幅，附图说明见 [0021]–[0024]。',
      },
    ],
  },
  {
    id: 's11',
    title: '十一、免责声明',
    blocks: [
      callout(
        'warning',
        '免责声明',
        '本报告由「引途医疗专利智能体」基于该专利公开文本自动生成，仅用于技术理解，**不构成侵权判定、专利性评价或任何法律意见**；权利要求的保护范围以国家知识产权局公告文本为准。',
      ),
    ],
  },
]

/** report.json（`GET /cases/{id}/reader/report` 的 report 字段）。 */
export const readerReport = { meta: readerMeta, sections: readerSections }

/** 终稿 markdown：与 report.json 同源，doc 通道逐块流式推送。 */
export const readerReportMarkdown = [
  `# 专利解读 · ${readerMeta.pub_no} · ${readerMeta.title}`,
  ...readerSections.flatMap((section) => [
    `## ${section.title}`,
    ...section.blocks.map(blockToMarkdown),
  ]),
].join('\n\n')

/** C7 自检未决事项（不入正文，报告面板末尾单列）。 */
export const readerUnresolved = [
  '说明书未给出「预设步长」的具体数值范围，权 1 的 F2 无法在正文中量化，已按原文表述保留。',
]

export const readerDeliveryFile = {
  kind: 'reader_note_md' as ArtifactKind,
  artifact_id: 'art-reader-md-v1',
  filename: '冠脉狭窄自动评估_解读报告_20260825161200.md',
}

function buildReaderTimeline(): MockTimelineEvent[] {
  const t: MockTimelineEvent[] = []

  // 首事件留出较长延迟：空会话「输入来源」卡可被看见（也便于像素 QA 截图）
  t.push({
    delay: 1400,
    event: 'case_title',
    data: { title: 'CN117994321A · 冠脉狭窄自动评估 · 专利解读' },
  })

  // 1. acquire：公开号 → 取证源优先级获取全文 → 文本化与段落索引
  t.push(step('acquire', 'running', '获取与文本化', 200))
  t.push({
    delay: 320,
    event: 'log',
    data: { level: 'info', message: '正在按取证源优先级获取 CN117994321A 的全文 PDF…' },
  })
  t.push({
    delay: 700,
    event: 'log',
    data: { level: 'info', message: 'PDF 已获取（38 页），正在做版面还原与段落编号索引…' },
  })
  t.push(
    ...chat(
      'acquire',
      '专利全文已就绪：\n\n- **公开号**：CN117994321A（发明）\n- **名称**：一种基于多模态影像融合的冠脉狭窄自动评估方法及装置\n- **申请人**：引途医疗科技（杭州）有限公司\n- 说明书 68 段、附图 4 幅、权利要求 10 项，段落编号已建立索引，后续正文引用均可回溯。',
    ),
    chatDone('acquire'),
  )
  t.push(step('acquire', 'done', '获取与文本化', 300))

  // 2. claim_tree：解析权要树 → 树形合法性校验 → 疑难项交人工校对（门控）
  t.push(step('claim_tree', 'running', '权利要求树', 250))
  t.push(
    ...chat(
      'claim_tree',
      '权利要求树解析完成：共 10 项，独立 2 项（方法 / 装置）、从属 8 项；树形合法性校验通过，但有 2 处引用关系需要你确认。',
    ),
    chatDone('claim_tree'),
  )
  t.push(
    ask(
      'claim_tree',
      'claim_tree',
      '权利要求树中有以下待确认之处，请核对后提交（可直接跳过，平台将把仍存疑的多引用按「或」的择一引用处理）：\n- 权6：择一引用「权利要求 4 或 5」\n- 权9：「弹性配准单元」缺少引用基础',
      {
        schema: {
          type: 'object',
          properties: {
            resolutions: { type: 'array', items: { type: 'object' } },
          },
        },
        default: {
          claims: readerClaims,
          ambiguities: readerAmbiguities,
          lint: [],
          skip: false,
        },
      },
    ),
  )
  t.push({ ...step('claim_tree', 'waiting_user', '权利要求树', 120), gate: true })
  t.push({
    delay: 260,
    event: 'log',
    data: { level: 'info', message: '权要树已确认：共 10 项 · 独立 2 / 从属 8' },
  })
  t.push(step('claim_tree', 'done', '权利要求树', 300))

  // 3. claim_deltas：逐权 12–40 字白话增量
  t.push(step('claim_deltas', 'running', '白话增量', 250))
  t.push(
    ...chat(
      'claim_deltas',
      '已为 10 项权利要求逐条写出「本项新增」白话增量（每条 12–40 字），长度校验全部通过；权 6 按择一引用分别相对权 4、权 5 描述增量。',
    ),
    chatDone('claim_deltas'),
  )
  t.push(step('claim_deltas', 'done', '白话增量', 350))

  // 4. note：三次撰写调用产出 11 节报告（doc 通道流式）
  t.push(step('note', 'running', '报告撰写', 250))
  t.push(
    ...chat(
      'note',
      '开始撰写解读报告（结构节 → 叙事节 → 附录节，共三次调用，11 节），正文流入右侧报告面板 →',
    ),
    chatDone('note'),
  )
  t.push(...doc('note', READER_DOC_ID, readerReportMarkdown, 22))
  t.push({
    delay: 200,
    event: 'llm_done',
    data: { step_key: 'note', channel: 'doc', doc_id: READER_DOC_ID },
  })
  t.push(step('note', 'done', '报告撰写', 300))

  // 5. lint_deliver：引用/附图/mermaid lint → 自检补丁 → report.json + reader_note_md
  t.push(step('lint_deliver', 'running', '校对与交付', 250))
  t.push({
    delay: 420,
    event: 'log',
    data: { level: 'info', message: '段落引用 lint：正文 18 处 [00XX] 引用全部命中说明书段落索引' },
  })
  t.push({
    delay: 380,
    event: 'log',
    data: { level: 'info', message: 'mermaid 渲染校验通过；附图引用（图 1–图 4）全部存在' },
  })
  t.push({
    delay: 420,
    event: 'artifact_created',
    data: {
      id: readerDeliveryFile.artifact_id,
      case_id: READER_CASE_ID,
      version: 1,
      kind: readerDeliveryFile.kind,
      filename: readerDeliveryFile.filename,
      stored_path: `outputs/${READER_CASE_ID}/${readerDeliveryFile.filename}`,
      run_group: 'initial',
      iteration_type: 'initial',
      summary: '专利解读报告（CN117994321A）',
      source_artifact_id: null,
      created_at: '2026-08-25T16:12:00+08:00',
    },
  })
  t.push({
    delay: 200,
    event: 'doc_version',
    data: {
      doc_id: READER_DOC_ID,
      version_id: readerDeliveryFile.artifact_id,
      version: 1,
      kind: readerDeliveryFile.kind,
      filename: readerDeliveryFile.filename,
      artifact_id: readerDeliveryFile.artifact_id,
      downloads: [readerDeliveryFile],
      iteration_type: 'initial',
      summary: '专利解读报告（CN117994321A）',
      created_at: '2026-08-25T16:12:00+08:00',
    },
  })
  t.push(
    ...chat(
      'lint_deliver',
      `解读报告已生成：\n- 报告文件：${readerDeliveryFile.filename}（11 节，可导出 Word）\n- 权利要求：共 10 项 · 独立 2 / 从属 8\n- 说明书段落索引：68 段；附图：4 幅\n- 自检未决事项 1 条（不入正文，见报告末尾）`,
    ),
    chatDone('lint_deliver'),
  )
  t.push(step('lint_deliver', 'done', '校对与交付', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'initial', status: 'done' } })

  return t
}

export const readerScript: MockScript = {
  key: 'reader',
  caseIdHint: READER_CASE_ID,
  timeline: buildReaderTimeline(),
}

// ---------------------------------------------------------------------------
// 剧本四：审查意见答复（step_key 与后端 §4 模块④ 一致）
// notice_extract(oa_issues) → retrieve(oa_retrieve) → strategy(oa_strategy) →
// draft（doc 通道流式答复正文）→ review_deliver(confirm) → 交付 MD/DOCX
// ---------------------------------------------------------------------------

const OA_DOC_ID = 'oa_response_md'
const OA_CASE_ID = 'demo-oa'

/** D1 通知书结构化结果（NoticeStruct.model_dump()）。 */
export const oaNotice = {
  notice_kind: 'office_action',
  notice_no: 1,
  notice_kind_note: '',
  application_no: 'CN202410123456.7',
  patent_type: 'invention',
  deadline: '2026-10-20',
  issues: [
    {
      id: 'I1',
      statute: '专利法第22条第3款',
      defect_type: 'inventiveness',
      inferred: false,
      claims_involved: [1, 2, 5],
      examiner_argument_digest:
        'D1 公开了基于 CT 影像的冠脉狭窄识别方法，与权 1 的区别仅在于引入了 OCT 影像的配准融合；'
        + 'D2 给出了多模态医学影像配准的技术启示，本领域技术人员有动机将 D2 的配准手段用于 D1，'
        + '故权 1 相对 D1 结合 D2 不具备突出的实质性特点和显著的进步；权 2、权 5 的附加技术特征属于本领域常规选择。',
      cited_documents: [
        {
          code: 'D1',
          ref_label: '对比文件1',
          identifier: 'CN110123456A',
          used_against: [1, 2, 5],
        },
        { code: 'D2', ref_label: '对比文件2', identifier: 'US2019/0223812A1', used_against: [1] },
      ],
      note: '',
    },
    {
      id: 'I2',
      statute: '专利法第26条第4款',
      defect_type: 'clarity',
      inferred: false,
      claims_involved: [3, 4],
      examiner_argument_digest:
        '权 3 中「弹性配准单元」的含义不清楚：说明书未定义其结构或实现方式，导致保护范围不清楚；'
        + '权 4 引用权 3，存在同样缺陷。',
      cited_documents: [],
      note: '',
    },
    {
      id: 'I3',
      statute: '专利法第26条第4款',
      defect_type: 'support',
      inferred: true,
      claims_involved: [7],
      examiner_argument_digest:
        '权 7 概括的「任意深度学习分割模型」得不到说明书支持：说明书仅记载了 U-Net 一种实现，'
        + '未给出可推广到其他模型的技术依据。',
      cited_documents: [],
      note: '',
    },
  ],
}

/** 本申请权利要求书原文（P1 卡片下方文本框初值）。 */
export const oaClaimsText = [
  '1. 一种基于多模态影像融合的冠脉狭窄自动评估方法，其特征在于，包括：获取同一患者的 CT 与 OCT 影像；'
  + '对两者进行弹性配准得到融合影像；基于融合影像分割血管腔并计算狭窄率。',
  '2. 根据权利要求 1 所述的方法，其特征在于，所述弹性配准包括以预设步长迭代优化形变场。',
  '3. 根据权利要求 1 所述的方法，其特征在于，所述配准由弹性配准单元执行。',
  '4. 根据权利要求 3 所述的方法，其特征在于，所述弹性配准单元包括形变场估计模块。',
  '5. 根据权利要求 1 所述的方法，其特征在于，所述狭窄率按最小管腔面积与参考面积之比计算。',
  '6. 根据权利要求 1 所述的方法，其特征在于，还包括输出狭窄段的三维定位坐标。',
  '7. 根据权利要求 1 所述的方法，其特征在于，所述分割采用深度学习分割模型。',
].join('\n')

/** D2 逐 issue 案例检索结果（hits 结构同 OaSearchHit）。 */
export const oaRetrieveIssues = [
  {
    issue_id: 'I1',
    query: '多模态影像配准 创造性 技术启示 冠脉狭窄',
    retrieval_mode: 'vector',
    relaxed: [] as string[],
    total_candidates: 18,
    message: '',
    error: '',
    hits: [
      {
        library_id: 'lib-001',
        case_id: 'OA-2024-0137',
        title: '超声与 CT 融合导航装置 · 一通创造性答复（授权）',
        chunk_id: 3,
        chunk_index: 3,
        chunk_text:
          '答复思路：以「配准精度指标 + 术中实时性约束」构成区别技术特征的整体技术效果，'
          + '论证 D2 的通用配准手段无法在实时性约束下直接移植，技术启示不成立；未修改权利要求即获授权。',
        score: 0.87,
        frontmatter: {
          title: '超声与 CT 融合导航装置 · 一通创造性答复',
          outcome: 'granted',
          statutes: ['专利法第22条第3款'],
          defect_types: ['inventiveness'],
          strategy: ['argue_only'],
          tags: ['医学影像', '多模态配准', '技术启示'],
          patent_type: 'invention',
        },
      },
      {
        library_id: 'lib-002',
        case_id: 'OA-2023-0912',
        title: '血管内 OCT 图像分割方法 · 二通（修改后授权）',
        chunk_id: 5,
        chunk_index: 5,
        chunk_text:
          '将从权 3 的「以血管中心线为约束的形变场正则项」并入独权，形成新的区别技术特征，'
          + '并以说明书实施例 2 的对比数据论证显著进步；审查员认可修改未超范围。',
        score: 0.81,
        frontmatter: {
          title: '血管内 OCT 图像分割方法 · 二通',
          outcome: 'amended_then_granted',
          statutes: ['专利法第22条第3款'],
          defect_types: ['inventiveness'],
          strategy: ['amend_claims'],
          tags: ['OCT', '并入从权', '专利法第33条'],
          patent_type: 'invention',
        },
      },
      {
        library_id: 'lib-003',
        case_id: 'OA-2022-0455',
        title: '心电信号分类模型 · 创造性答复（驳回）',
        chunk_id: 2,
        chunk_index: 2,
        chunk_text:
          '仅以「参数不同」争辩，未指出区别技术特征带来的预料不到的技术效果，被认定属于常规选择，最终驳回。',
        score: 0.64,
        frontmatter: {
          title: '心电信号分类模型 · 创造性答复',
          outcome: 'rejected',
          statutes: ['专利法第22条第3款'],
          defect_types: ['inventiveness'],
          strategy: ['argue_only'],
          tags: ['反面案例', '常规选择'],
          patent_type: 'invention',
        },
      },
    ],
    selected: ['lib-001', 'lib-002'],
  },
  {
    issue_id: 'I2',
    query: '权利要求不清楚 功能性限定 单元 说明书定义',
    retrieval_mode: 'vector',
    relaxed: ['statutes'],
    total_candidates: 9,
    message: '本条已放宽法条过滤后才有命中。',
    error: '',
    hits: [
      {
        library_id: 'lib-004',
        case_id: 'OA-2023-0288',
        title: '功能性限定「处理单元」不清楚 · 补入说明书定义后克服',
        chunk_id: 1,
        chunk_index: 1,
        chunk_text:
          '答复中将「处理单元」修改为说明书已记载的具体结构表述，并指明说明书对应段落，'
          + '审查员认可保护范围已清楚。',
        score: 0.78,
        frontmatter: {
          title: '功能性限定不清楚答复',
          outcome: 'granted',
          statutes: ['专利法第26条第4款'],
          defect_types: ['clarity'],
          strategy: ['amend_claims'],
          tags: ['功能性限定', '清楚性'],
          patent_type: 'invention',
        },
      },
    ],
    selected: ['lib-004'],
  },
  {
    issue_id: 'I3',
    query: '概括过宽 得不到说明书支持 深度学习模型',
    retrieval_mode: 'vector',
    relaxed: [] as string[],
    total_candidates: 0,
    message: '案例库中暂无与本条高度相关的案例，草稿将只依据本案文本与通用审查规则说理。',
    error: '',
    hits: [] as unknown[],
    selected: [] as string[],
  },
]

/** D3 逐 issue 策略选项（IssueStrategy.model_dump()）。 */
export const oaStrategies = [
  {
    issue_id: 'I1',
    recommended: 'amend_claims',
    options: [
      {
        strategy: 'argue_only',
        rationale:
          'D2 的配准手段用于离线影像，本案在术中实时性约束下完成弹性配准，'
          + 'D1+D2 不存在结合的技术启示，可尝试纯陈述。',
        risk: '',
        amendment_sketch: '',
      },
      {
        strategy: 'amend_claims',
        rationale:
          '将从权 2 的「以预设步长迭代优化形变场」并入独权 1，形成 D1、D2 均未公开的区别技术特征，'
          + '并以说明书实施例 2 的狭窄率误差对比数据论证显著进步。',
        risk:
          '并入内容源自原始权 2 与说明书第 [0043] 段，未引入新内容，超范围（专利法第33条）风险低；'
          + '但保护范围将缩小到含迭代步长的实现。',
        amendment_sketch: '权 1 末尾并入「其中所述弹性配准以预设步长迭代优化形变场」，同时删除原权 2。',
      },
      {
        strategy: 'amend_description',
        rationale: '在说明书中补充配准实时性的定量说明，为创造性论证提供依据。',
        risk: '说明书补入内容须严格来自原始申请文件，否则构成超范围。',
        amendment_sketch: '',
      },
    ],
  },
  {
    issue_id: 'I2',
    recommended: 'amend_claims',
    options: [
      {
        strategy: 'amend_claims',
        rationale:
          '将权 3 的「弹性配准单元」替换为说明书第 [0051] 段已记载的具体结构表述，使保护范围清楚。',
        risk: '替换用语须与说明书原文一致，避免引入新的上位概括。',
        amendment_sketch: '「弹性配准单元」→「包括形变场估计模块与插值模块的配准处理器」。',
      },
      {
        strategy: 'argue_only',
        rationale: '主张本领域技术人员依据说明书能够明确该单元的含义。',
        risk: '',
        amendment_sketch: '',
      },
    ],
  },
  {
    issue_id: 'I3',
    recommended: 'amend_claims',
    options: [
      {
        strategy: 'amend_claims',
        rationale: '将权 7 的「深度学习分割模型」限定为说明书记载的 U-Net 及其变体，回到支持范围内。',
        risk: '限定后保护范围缩小，但与说明书记载一致，超范围风险低。',
        amendment_sketch: '权 7 → 「所述分割采用 U-Net 结构的深度学习分割模型」。',
      },
      {
        strategy: 'argue_only',
        rationale: '主张本领域技术人员可将 U-Net 的实现推广到同类分割网络。',
        risk: '',
        amendment_sketch: '',
      },
    ],
  },
]

/** D4 答复正文（doc 通道流式）。 */
export const oaResponseMarkdown = `# 意见陈述书

**申请号**：CN202410123456.7　**通知书**：第 1 次审查意见通知书　**答复期限**：2026-10-20

## 一、修改说明

依据专利法第33条与专利法实施细则第五十一条第三款，本次答复对权利要求书作如下修改：

1. 将原从属权利要求 2 的附加技术特征「以预设步长迭代优化形变场」并入独立权利要求 1，并删除原权利要求 2，后续权项编号顺延；
2. 将原权利要求 3 中的「弹性配准单元」修改为「包括形变场估计模块与插值模块的配准处理器」（见说明书第 [0051] 段）；
3. 将原权利要求 7 中的「深度学习分割模型」限定为「U-Net 结构的深度学习分割模型」（见说明书第 [0058] 段）。

以上修改内容均记载于原始申请文件，未超出原说明书和权利要求书记载的范围。

## 二、关于创造性（专利法第22条第3款，对应审查意见第 1 点）

修改后的权利要求 1 与 D1（CN110123456A）的区别技术特征在于：

- 区别特征 A：获取同一患者的 **CT 与 OCT 双模态影像**并进行弹性配准；
- 区别特征 B：所述弹性配准 **以预设步长迭代优化形变场**。

D1 仅公开单模态 CT 影像下的狭窄识别，未涉及跨模态配准；D2（US2019/0223812A1）虽给出通用医学影像配准手段，但其面向离线诊断场景，未考虑术中实时性约束，本领域技术人员没有动机将其直接用于 D1 的术中评估流程。

结合说明书实施例 2 的对比数据（表 1）：本申请在保持配准耗时 ≤ 120 ms 的前提下，将狭窄率评估的平均绝对误差由 8.7% 降至 3.1%，取得了预料不到的技术效果。

因此，修改后的权利要求 1 相对 D1 结合 D2 具备突出的实质性特点和显著的进步，符合专利法第22条第3款的规定；其从属权利要求同样具备创造性。

## 三、关于权利要求清楚（专利法第26条第4款，对应审查意见第 2 点）

修改后的权利要求 3 已将功能性表述替换为说明书明确记载的结构表述，保护范围清楚；引用权利要求 3 的权利要求 4 相应克服该缺陷。

## 四、关于权利要求得到说明书支持（专利法第26条第4款，对应审查意见第 3 点）

修改后的权利要求 7 已限定为说明书实施例记载的 U-Net 结构分割模型，与说明书记载的技术方案一一对应，能够得到说明书的支持。

## 五、结论

综上，修改后的权利要求 1-6 具备新颖性与创造性，且清楚、简要、得到说明书支持，恳请审查员在此基础上继续审查并授予专利权。若尚有疑问，敬请与代理人电话沟通。
`

export const oaDeliveryFiles = [
  {
    kind: 'oa_response_md' as ArtifactKind,
    artifact_id: 'art-oa-md-v1',
    filename: '意见陈述书_CN202410123456.7_20260825.md',
  },
  {
    kind: 'oa_response_docx' as ArtifactKind,
    artifact_id: 'art-oa-docx-v1',
    filename: '意见陈述书_CN202410123456.7_20260825.docx',
  },
]

/** OACasesPage 的案例库 mock（含一条待人审草稿）。 */
export const oaLibraryItems = [
  {
    id: 'lib-001',
    case_id: 'OA-2024-0137',
    title: '超声与 CT 融合导航装置 · 一通创造性答复',
    status: 'confirmed',
    embedded: true,
    chunk_count: 6,
    patent_type: 'invention',
    statutes: ['专利法第22条第3款'],
    defect_types: ['inventiveness'],
    domain: '医学影像',
    outcome: 'granted',
    tags: ['医学影像', '多模态配准', '技术启示'],
    frontmatter: {
      case_id: 'OA-2024-0137',
      title: '超声与 CT 融合导航装置 · 一通创造性答复',
      status: 'confirmed',
      patent_type: 'invention',
      statutes: ['专利法第22条第3款'],
      defect_types: ['inventiveness'],
      domain: '医学影像',
      notice_kind: 'office_action',
      outcome: 'granted',
      strategy: ['argue_only'],
      compare_refs: ['CN109876543A'],
      redacted: true,
      tags: ['医学影像', '多模态配准', '技术启示'],
      created_at: '2026-06-11T10:20:00+08:00',
      updated_at: '2026-06-11T10:20:00+08:00',
    },
    created_at: '2026-06-11T10:20:00+08:00',
    updated_at: '2026-06-11T10:20:00+08:00',
  },
  {
    id: 'lib-002',
    case_id: 'OA-2023-0912',
    title: '血管内 OCT 图像分割方法 · 二通答复',
    status: 'confirmed',
    embedded: true,
    chunk_count: 8,
    patent_type: 'invention',
    statutes: ['专利法第22条第3款', '专利法第33条'],
    defect_types: ['inventiveness'],
    domain: '医学影像',
    outcome: 'amended_then_granted',
    tags: ['OCT', '并入从权', '专利法第33条'],
    frontmatter: {
      case_id: 'OA-2023-0912',
      title: '血管内 OCT 图像分割方法 · 二通答复',
      status: 'confirmed',
      patent_type: 'invention',
      statutes: ['专利法第22条第3款', '专利法第33条'],
      defect_types: ['inventiveness'],
      domain: '医学影像',
      notice_kind: 'office_action',
      outcome: 'amended_then_granted',
      strategy: ['amend_claims'],
      redacted: true,
      tags: ['OCT', '并入从权', '专利法第33条'],
      created_at: '2026-03-02T09:05:00+08:00',
      updated_at: '2026-07-18T15:41:00+08:00',
    },
    created_at: '2026-03-02T09:05:00+08:00',
    updated_at: '2026-07-18T15:41:00+08:00',
  },
  {
    id: 'lib-003',
    case_id: 'OA-2022-0455',
    title: '心电信号分类模型 · 创造性答复（反面案例）',
    status: 'confirmed',
    embedded: true,
    chunk_count: 4,
    patent_type: 'invention',
    statutes: ['专利法第22条第3款'],
    defect_types: ['inventiveness'],
    domain: '生理信号处理',
    outcome: 'rejected',
    tags: ['反面案例', '常规选择'],
    frontmatter: {
      case_id: 'OA-2022-0455',
      title: '心电信号分类模型 · 创造性答复',
      status: 'confirmed',
      patent_type: 'invention',
      statutes: ['专利法第22条第3款'],
      defect_types: ['inventiveness'],
      domain: '生理信号处理',
      notice_kind: 'rejection',
      outcome: 'rejected',
      strategy: ['argue_only'],
      redacted: true,
      tags: ['反面案例', '常规选择'],
      created_at: '2025-11-20T11:12:00+08:00',
      updated_at: '2025-11-20T11:12:00+08:00',
    },
    created_at: '2025-11-20T11:12:00+08:00',
    updated_at: '2025-11-20T11:12:00+08:00',
  },
  {
    id: 'lib-004',
    case_id: 'OA-2023-0288',
    title: '功能性限定「处理单元」不清楚 · 补入说明书定义后克服',
    status: 'confirmed',
    embedded: true,
    chunk_count: 5,
    patent_type: 'invention',
    statutes: ['专利法第26条第4款'],
    defect_types: ['clarity'],
    domain: '医疗器械控制',
    outcome: 'granted',
    tags: ['功能性限定', '清楚性'],
    frontmatter: {
      case_id: 'OA-2023-0288',
      title: '功能性限定「处理单元」不清楚',
      status: 'confirmed',
      patent_type: 'invention',
      statutes: ['专利法第26条第4款'],
      defect_types: ['clarity'],
      domain: '医疗器械控制',
      notice_kind: 'office_action',
      outcome: 'granted',
      strategy: ['amend_claims'],
      redacted: true,
      tags: ['功能性限定', '清楚性'],
      created_at: '2026-01-09T14:30:00+08:00',
      updated_at: '2026-01-09T14:30:00+08:00',
    },
    created_at: '2026-01-09T14:30:00+08:00',
    updated_at: '2026-01-09T14:30:00+08:00',
  },
  {
    id: 'lib-005',
    case_id: 'OA-2026-0031',
    title: '内窥镜图像增强装置 · 补正通知书答复（待人审）',
    status: 'draft',
    embedded: false,
    chunk_count: 0,
    patent_type: 'utility_model',
    statutes: ['专利法实施细则第五十一条'],
    defect_types: ['formality'],
    domain: '内窥镜',
    outcome: 'pending',
    tags: ['补正', '形式缺陷'],
    frontmatter: {
      case_id: 'OA-2026-0031',
      title: '内窥镜图像增强装置 · 补正通知书答复',
      status: 'draft',
      patent_type: 'utility_model',
      statutes: ['专利法实施细则第五十一条'],
      defect_types: ['formality'],
      domain: '内窥镜',
      notice_kind: 'correction',
      outcome: 'pending',
      strategy: ['correction'],
      redacted: true,
      tags: ['补正', '形式缺陷'],
      created_at: '2026-08-20T16:02:00+08:00',
      updated_at: '2026-08-20T16:02:00+08:00',
    },
    created_at: '2026-08-20T16:02:00+08:00',
    updated_at: '2026-08-20T16:02:00+08:00',
  },
]

/** 案例笔记正文（Drawer 详情 markdown）。 */
export const oaLibraryNotes: Record<string, string> = {
  'lib-001': `## 案情摘要

某超声与 CT 融合导航装置的发明申请，一通指出权 1 相对 D1 结合 D2 不具备创造性。

## 审查员观点

D1 公开融合导航主体流程，D2 给出通用配准手段，认为结合显而易见。

## 答复思路

1. 明确区别技术特征：术中实时性约束下的配准精度指标；
2. 论证 D2 面向离线场景，缺乏结合动机；
3. 以实施例对比数据支撑显著进步。

## 结果

未修改权利要求，一通答复后直接授权。

## 可复用要点

- 「结合动机」应从**应用场景约束**切入，而非仅比对特征字面差异；
- 对比数据需在原说明书中已记载，答复中只做援引与解释。`,
  'lib-005': `## 案情摘要（草稿 · 待人审）

内窥镜图像增强装置实用新型，收到补正通知书，指出说明书附图标记与文字不一致、摘要超字数。

## 待补内容

- 结案结果尚未确认（当前 pending）；
- 脱敏检查：申请人名称已替换为「某医疗器械公司」，请人工复核是否仍有可识别信息。`,
}

function buildOaTimeline(): MockTimelineEvent[] {
  const t: MockTimelineEvent[] = []

  // 首事件留出较长延迟：空会话上传卡可被看见（便于像素 QA 截图）
  t.push({
    delay: 1400,
    event: 'case_title',
    data: { title: '第1次审查意见通知书 答复（CN202410123456.7）' },
  })

  // 1. notice_extract：结构化 + 枚举校验 → P1 人审（oa_issues 门控）
  t.push(step('notice_extract', 'running', '通知书结构化', 200))
  t.push({
    delay: 320,
    event: 'log',
    data: { level: 'info', message: '通知书 PDF 已转出文本（6 页），正在按输出契约结构化…' },
  })
  t.push(
    ...chat(
      'notice_extract',
      '通知书结构化完成：\n\n- **种类**：第 1 次审查意见通知书\n- **申请号**：CN202410123456.7（发明）\n- **答复期限**：2026-10-20\n- 共拆出 **3 条缺陷**：创造性（权1、2、5）、权利要求不清楚（权3、4）、得不到说明书支持（权7）\n\n法条与缺陷类型已过服务端枚举校验，请逐条核对后再进入检索与策略阶段。',
    ),
    chatDone('notice_extract'),
  )
  t.push(
    ask(
      'notice_extract',
      'oa_issues',
      '以下为通知书结构化结果，请逐条核对后再进入检索与策略阶段（这是防条款幻觉的强制人审闸门：法条须与通知书原文逐字一致）。',
      {
        schema: {
          type: 'object',
          properties: {
            issues: { type: 'array', title: '逐条缺陷（可增删改）', items: { type: 'object' } },
            claims_text: { type: 'string', title: '本申请权利要求书原文' },
          },
        },
        default: {
          ...oaNotice,
          claims_text: oaClaimsText,
          defect_type_options: [
            { value: 'novelty', label: '新颖性' },
            { value: 'inventiveness', label: '创造性' },
            { value: 'clarity', label: '权利要求不清楚' },
            { value: 'support', label: '得不到说明书支持' },
            { value: 'disclosure', label: '说明书公开不充分' },
            { value: 'formality', label: '形式缺陷/补正事项' },
            { value: 'added_matter', label: '修改超范围' },
            { value: 'essential_features', label: '缺少必要技术特征' },
            { value: 'unity', label: '单一性' },
            { value: 'subject_matter', label: '不属于专利保护客体' },
            { value: 'other', label: '其他' },
          ],
          lint_problems: ['I3：statute 未在通知书中明示，缺陷类型由上下文推断，请人工确认。'],
          repairs: 1,
        },
      },
    ),
  )
  t.push({ ...step('notice_extract', 'waiting_user', '通知书结构化', 120), gate: true })
  t.push({
    delay: 240,
    event: 'log',
    data: { level: 'info', message: '问题清单已确认：共 3 条缺陷；本申请权项 7 项。' },
  })
  t.push(step('notice_extract', 'done', '通知书结构化', 300))

  // 2. retrieve：逐 issue 检索（明示 retrieval_mode）→ P2 人审（oa_retrieve 门控）
  t.push(step('retrieve', 'running', '案例检索', 250))
  t.push({
    delay: 300,
    event: 'search_progress',
    data: { message: 'I1 · 向量检索：命中 3 条案例节选', phase: 'retrieve', count: 3 },
  })
  t.push({
    delay: 280,
    event: 'search_progress',
    data: { message: 'I2 · 向量检索（已放宽法条过滤）：命中 1 条', phase: 'retrieve', count: 1 },
  })
  t.push({
    delay: 280,
    event: 'search_progress',
    data: { message: 'I3 · 向量检索：无命中，将只依据本案文本说理', phase: 'retrieve', count: 0 },
  })
  t.push(
    ask(
      'retrieve',
      'oa_retrieve',
      '案例检索完成（检索方式：向量检索），共命中 4 条案例节选。请逐条勾选要作为参考的历史案例；未勾选或无命中时，草稿将只依据本案文本与通用审查规则说理，不会假装引用历史案例。',
      {
        schema: {
          type: 'object',
          properties: {
            selections: { type: 'array', title: '逐条 issue 的参考案例勾选', items: { type: 'object' } },
          },
        },
        default: {
          retrieval_mode: 'vector',
          issues: oaRetrieveIssues,
          selections: oaRetrieveIssues.map((x) => ({
            issue_id: x.issue_id,
            library_ids: x.selected,
          })),
        },
      },
    ),
  )
  t.push({ ...step('retrieve', 'waiting_user', '案例检索', 120), gate: true })
  t.push({
    delay: 240,
    event: 'log',
    data: { level: 'info', message: '参考案例已确认：命中 4 条，选用 3 条。' },
  })
  t.push(step('retrieve', 'done', '案例检索', 300))

  // 3. strategy：逐 issue 策略选项 → P3 人审（oa_strategy 门控）
  t.push(step('strategy', 'running', '策略规划', 250))
  t.push(
    ...chat(
      'strategy',
      '已为 3 条缺陷分别生成可选答复策略（修改类选项均附超范围风险评估）：\n\n- **I1 创造性**：推荐「修改权利要求」——并入原权 2 的迭代步长特征；\n- **I2 不清楚**：推荐「修改权利要求」——替换为说明书已记载的结构表述；\n- **I3 支持**：推荐「修改权利要求」——限定为 U-Net 结构。\n\n请逐条勾选，可改选非推荐项并填写备注。',
    ),
    chatDone('strategy'),
  )
  t.push(
    ask(
      'strategy',
      'oa_strategy',
      '以下为逐条答复策略选项（修改类选项已给出超范围风险评估）。请逐条勾选要采用的策略，可改选非推荐项并填写备注。',
      {
        schema: {
          type: 'object',
          properties: {
            choices: { type: 'array', title: '逐条策略勾选', items: { type: 'object' } },
          },
        },
        default: {
          choices: oaStrategies.map((s) => ({
            issue_id: s.issue_id,
            strategy: s.recommended,
            note: '',
          })),
          strategies: oaStrategies,
        },
      },
    ),
  )
  t.push({ ...step('strategy', 'waiting_user', '策略规划', 120), gate: true })
  t.push(step('strategy', 'done', '策略规划', 300))

  // 4. draft：逐条起草（doc 通道流式答复正文）
  t.push(step('draft', 'running', '逐条起草', 250))
  t.push(
    ...chat(
      'draft',
      '开始逐条起草答复（修改说明 → 创造性 → 清楚 → 支持 → 结论），正文流入右侧文档面板 →',
    ),
    chatDone('draft'),
  )
  t.push(...doc('draft', OA_DOC_ID, oaResponseMarkdown, 20))
  t.push({
    delay: 200,
    event: 'llm_done',
    data: { step_key: 'draft', channel: 'doc', doc_id: OA_DOC_ID },
  })
  t.push({
    delay: 300,
    event: 'log',
    data: {
      level: 'info',
      message: '修改后权项已过 claims_lint：编号连续、引用基础齐备，无 error 级违规。',
    },
  })
  t.push(step('draft', 'done', '逐条起草', 300))

  // 5. review_deliver：跨条一致性 AUDIT → 终稿确认（confirm 门控）→ 交付
  t.push(step('review_deliver', 'running', '汇总与交付', 250))
  t.push({
    delay: 380,
    event: 'log',
    data: { level: 'info', message: '跨条一致性复核：权 1 的修改与 I2/I3 的修改无冲突（verdict=pass）。' },
  })
  t.push(
    ask(
      'review_deliver',
      'confirm',
      '答复终稿已生成，请确认后交付：\n- 修改后权利要求 1-6（原权 2 已并入并删除）\n- 逐条答复：创造性 / 清楚 / 支持\n- 待人工确认事项 1 条：说明书第 [0043] 段的步长数值范围建议由代理人复核。',
      {
        schema: {
          type: 'object',
          properties: {
            confirmed: { type: 'boolean', title: '确认交付' },
            note: { type: 'string', title: '交付备注' },
          },
        },
        default: { confirmed: true, note: '' },
      },
    ),
  )
  t.push({ ...step('review_deliver', 'waiting_user', '汇总与交付', 120), gate: true })
  t.push({
    delay: 380,
    event: 'artifact_created',
    data: {
      id: oaDeliveryFiles[0].artifact_id,
      case_id: OA_CASE_ID,
      version: 1,
      kind: oaDeliveryFiles[0].kind,
      filename: oaDeliveryFiles[0].filename,
      stored_path: `outputs/${OA_CASE_ID}/${oaDeliveryFiles[0].filename}`,
      run_group: 'initial',
      iteration_type: 'initial',
      summary: '意见陈述书（CN202410123456.7）',
      source_artifact_id: null,
      created_at: '2026-08-25T17:40:00+08:00',
    },
  })
  t.push({
    delay: 200,
    event: 'doc_version',
    data: {
      doc_id: OA_DOC_ID,
      version_id: oaDeliveryFiles[0].artifact_id,
      version: 1,
      kind: oaDeliveryFiles[0].kind,
      filename: oaDeliveryFiles[0].filename,
      artifact_id: oaDeliveryFiles[0].artifact_id,
      downloads: oaDeliveryFiles,
      iteration_type: 'initial',
      summary: '意见陈述书（CN202410123456.7）',
      created_at: '2026-08-25T17:40:00+08:00',
    },
  })
  t.push(
    ask(
      'review_deliver',
      'delivery',
      '答复文件已生成，可下载 Markdown 或 Word；如需存入案例库，请在案例库页导入并人工确认。',
      {
        default: {
          files: oaDeliveryFiles,
          version: 1,
          summary: '意见陈述书（CN202410123456.7）· 修改后权利要求 1-6',
        },
      },
    ),
  )
  t.push(
    ...chat(
      'review_deliver',
      `答复已交付：\n- ${oaDeliveryFiles[0].filename}\n- ${oaDeliveryFiles[1].filename}\n- 待人工确认事项 1 条（见终稿末尾）`,
    ),
    chatDone('review_deliver'),
  )
  t.push(step('review_deliver', 'done', '汇总与交付', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'initial', status: 'done' } })

  return t
}

export const oaScript: MockScript = {
  key: 'oa',
  caseIdHint: OA_CASE_ID,
  timeline: buildOaTimeline(),
}

// ---------------------------------------------------------------------------
// 剧本五 / 六：交底书实用新型 · 外观设计分支（A3b schema_fill「填表与线稿」）
// 用于像素 QA 与卡片联调：/disclosure/{id} 的 id 含 utility / design / schema 时命中
// ---------------------------------------------------------------------------

/** 线稿 / 实拍占位图（内联 SVG data URI）。 */
function sketchDataUri(caption: string, filled: boolean): string {
  const body = filled
    ? `<rect x="60" y="70" width="180" height="110" fill="#e5e7eb" stroke="#6b7280" stroke-width="2" rx="10"/>
<circle cx="150" cy="125" r="28" fill="#9ca3af"/>`
    : `<rect x="60" y="70" width="180" height="110" fill="none" stroke="#111827" stroke-width="2" rx="10"/>
<circle cx="150" cy="125" r="28" fill="none" stroke="#111827" stroke-width="2"/>
<path d="M60 150 L240 150" stroke="#111827" stroke-width="1"/>`
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 220" width="300" height="220">
<rect width="300" height="220" fill="#ffffff"/>
${body}
<text x="150" y="205" font-size="13" text-anchor="middle" fill="#374151">${caption}</text>
</svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

const utilityStructure = {
  version: 1,
  mode: 'disclosure',
  parts: [
    { id: 'P1', name: '外壳', shape: '长方体，四角倒圆，顶面开散热槽', material_hint: 'ABS 工程塑料' },
    { id: 'P2', name: '卡扣', shape: '弹性悬臂梁，末端带倒钩', material_hint: 'POM' },
    { id: 'P3', name: '散热鳍片', shape: '平行片状阵列，片距 2mm', material_hint: '6063 铝合金' },
    { id: 'P4', name: '密封圈', shape: 'O 型环，线径 2mm', material_hint: '硅胶' },
  ],
  relations: [
    { from: '卡扣', to: '外壳', type: '卡接', where: '外壳侧壁上缘的卡槽' },
    { from: '散热鳍片', to: '外壳', type: '一体成型', where: '外壳顶面散热槽内' },
    { from: '密封圈', to: '外壳', type: '过盈配合', where: '上下壳体的合模面' },
  ],
  spatial: ['散热鳍片位于外壳顶面，鳍片走向与卡扣悬臂方向垂直。'],
  uncertain: ['卡扣数量在材料中出现 2 处不一致（4 个 / 6 个），已按 4 个记录，请核对。'],
}

const designAppearance = {
  version: 1,
  mode: 'disclosure',
  product_name: '便携式雾化给药装置',
  overall_shape: '扁平椭圆柱体，握持段内收成腰形',
  product_form: 'solid',
  claimed_faces: ['主视图', '后视图', '左视图', '右视图', '俯视图', '立体图'],
  omitted_views: [
    { name: '仰视图', reason: '底面为平面且使用时不可见，不主张保护' },
  ],
  design_points: [
    '机身腰形内收曲面与顶部椭圆出雾口的过渡关系',
    '正面自上而下的三段式弧形分色带',
    '底部环形防滑纹的等距排布',
  ],
  uncertain: ['配色仅有一张实拍，色彩是否主张保护待确认。'],
}

const utilityFigurePlan = {
  version: 1,
  patent_type: 'utility_model',
  theme_summary: '壳体—卡扣—散热鳍片的结构改进',
  figures: [
    {
      fig: 1,
      role: 'assembly',
      kind: 'lineart',
      path: 'figures/um-assembly.png',
      url: sketchDataUri('图1 总装线稿', false),
      score: 86,
      use_in_disclosure: true,
      reason: '线稿清晰、覆盖全部部件，作为总装图入文。',
    },
    {
      fig: 2,
      role: 'detail',
      kind: 'lineart',
      path: 'figures/um-clip.png',
      url: sketchDataUri('图2 卡扣局部', false),
      score: 78,
      use_in_disclosure: true,
      reason: '卡扣倒钩细节，补充说明连接关系。',
    },
    {
      fig: null,
      role: 'reference',
      kind: 'cad',
      path: 'figures/um-cad-screenshot.png',
      url: sketchDataUri('CAD 截图（不入文）', true),
      score: 64,
      use_in_disclosure: false,
      reason: 'CAD 截图含软件界面与尺寸标注，按规则一律不入文，仅供理解。',
    },
    {
      fig: null,
      role: 'rejected',
      kind: 'photo_scene',
      path: 'figures/um-desk.jpg',
      url: sketchDataUri('场景实拍（已排除）', true),
      score: 31,
      use_in_disclosure: false,
      reason: '桌面场景照背景杂乱，未达合格线 70。',
    },
  ],
}

const designFigurePlan = {
  version: 1,
  patent_type: 'design',
  theme_summary: '便携式雾化给药装置的外观',
  figures: [
    {
      fig: 1,
      role: 'perspective',
      kind: 'photo_clean',
      path: 'figures/design-perspective.jpg',
      url: sketchDataUri('图1 立体实拍', true),
      score: 82,
      use_in_disclosure: true,
      reason: '白底干净实拍，可作为立体图使用。',
    },
    {
      fig: 2,
      role: 'ortho',
      kind: 'photo_clean',
      path: 'figures/design-front.jpg',
      url: sketchDataUri('图2 主视实拍', true),
      score: 74,
      use_in_disclosure: true,
      reason: '正面视角基本正投影，可暂代主视图。',
    },
    {
      fig: null,
      role: 'reference',
      kind: 'cad',
      path: 'figures/design-cad.png',
      url: sketchDataUri('CAD 渲染（不入文）', true),
      score: 58,
      use_in_disclosure: false,
      reason: 'CAD 渲染带材质光影，不符合正式视图要求。',
    },
  ],
}

const utilityBrief = {
  needed: true,
  patent_type: 'utility_model',
  product_name: '散热型传感器外壳',
  views: [
    {
      view_name: '爆炸图',
      lineart_goal: '表达外壳、卡扣、散热鳍片、密封圈的装配顺序与相对位置',
      gen_prompt:
        '黑白线稿，白底黑线，等轴测爆炸视图，仅结构轮廓与装配引线，标注部件序号 1-4；'
        + '禁止：阴影、材质、渐变、透视失真、任何文字说明。',
      output_hint: '绘制/生成后上传为 PNG（白底黑线），平台会重跑附图打分并入文',
    },
    {
      view_name: '剖视图',
      lineart_goal: '表达密封圈在合模面的过盈配合关系',
      gen_prompt: '黑白线稿，白底黑线，A—A 剖视，剖面线 45° 等距；禁止：阴影、材质、渐变。',
      output_hint: '绘制/生成后上传为 PNG（白底黑线），平台会重跑附图打分并入文',
    },
  ],
  forbid: ['阴影与材质渲染', '照片贴图', '尺寸公差标注', 'CAD 软件界面'],
  risk_note:
    '本案当前没有合格线稿：实用新型入文只收线稿（CAD/实拍不入文），第三章将以文字与表格描述结构，附图待补。',
  note: '本期不做线稿 AI 生成：请按上述说明线下绘制或用图像工具生成后上传，再重跑本步骤。',
}

const designBrief = {
  needed: true,
  patent_type: 'design',
  product_name: '便携式雾化给药装置',
  views: [
    {
      view_name: '六面正投影',
      lineart_goal: '补齐主、后、左、右、俯视五面正投影（仰视已声明省略）',
      gen_prompt:
        '黑白线稿，白底黑线，严格正投影，各视图比例一致并对齐；'
        + '禁止：透视、阴影、材质、背景元素、文字标注。',
      output_hint: '绘制/生成后上传为 PNG（白底黑线），平台会重跑附图打分并入文',
    },
  ],
  forbid: ['透视变形', '阴影与高光', '背景道具', '文字与尺寸标注'],
  risk_note:
    '本案当前只有干净实拍、没有合格线稿：外观交底可先按实拍推进，但代理人通常仍需线稿或正式视图，**存在补图风险**；可继续成文，也可上传线稿后重跑本步。',
  note: '本期不做线稿 AI 生成：请按上述说明线下绘制或用图像工具生成后上传，再重跑本步骤。',
}

function buildSchemaFillTimeline(kind: 'utility_model' | 'design'): MockTimelineEvent[] {
  const design = kind === 'design'
  const t: MockTimelineEvent[] = []
  t.push({
    delay: 900,
    event: 'case_title',
    data: { title: design ? '便携式雾化给药装置 · 外观设计交底书' : '散热型传感器外壳 · 实用新型交底书' },
  })
  t.push(step('intake', 'done', '边界确认', 200))
  t.push(step('material_scan', 'done', '材料扫描', 200))
  t.push(step('points_mining', 'done', '专利点挖掘', 200))

  t.push(step('schema_fill', 'running', '填表与线稿', 250))
  t.push(
    ...chat(
      'schema_fill',
      design
        ? '外观事实合同已抽取：产品形态为立体产品，主张 6 个视图面，仰视图按「故意省略」记录；'
          + '附图打分完成，2 幅干净实拍达合格线，CAD 渲染按规则不入文。请核对下表与附图选用。'
        : '结构事实合同已抽取：部件 4 个、连接关系 3 条；附图打分完成，2 幅线稿达合格线，'
          + 'CAD 截图与场景实拍不入文。请核对部件表、连接关系表与附图选用。',
    ),
    chatDone('schema_fill'),
  )
  t.push(
    ask(
      'schema_fill',
      'schema_fill',
      design
        ? '以下为外观设计事实合同与附图选用方案，请核对后确认（未确认的字段不会写入正文；不得臆造未在材料中出现的形状或配色）。'
        : '以下为实用新型事实合同与附图选用方案，请核对后确认（未确认的字段不会写入正文；不得臆造材料中未出现的部件或连接方式）。',
      {
        schema: {
          type: 'object',
          properties: {
            schema: { type: 'object', title: '事实合同' },
            figure_plan: { type: 'object', title: '附图选用' },
          },
        },
        default: {
          patent_type: kind,
          schema: design ? designAppearance : utilityStructure,
          figure_plan: design ? designFigurePlan : utilityFigurePlan,
          lineart_brief: design ? designBrief : utilityBrief,
        },
      },
    ),
  )
  t.push({ ...step('schema_fill', 'waiting_user', '填表与线稿', 120), gate: true })
  t.push({
    delay: 260,
    event: 'log',
    data: {
      level: 'info',
      message: design
        ? '外观事实合同已确认：主张 6 面视图，省略仰视图 1 项，入文附图 2 幅。'
        : '结构事实合同已确认：部件 4 个、连接关系 3 条，入文附图 2 幅。',
    },
  })
  t.push(step('schema_fill', 'done', '填表与线稿', 300))
  t.push({ delay: 300, event: 'pipeline_done', data: { run_group: 'initial', status: 'done' } })
  return t
}

/** 实用新型分支（部件表 / 连接关系表 + 线稿说明）。 */
export const utilitySchemaScript: MockScript = {
  key: 'schema_fill',
  caseIdHint: 'demo-utility',
  timeline: buildSchemaFillTimeline('utility_model'),
}

/** 外观设计分支（产品形态 / 视图面 / 省略视图 / 设计要点）。 */
export const designSchemaScript: MockScript = {
  key: 'schema_fill_design',
  caseIdHint: 'demo-design',
  timeline: buildSchemaFillTimeline('design'),
}

export const mockScripts: readonly MockScript[] = [
  disclosureScript,
  paperScript,
  readerScript,
  oaScript,
  utilitySchemaScript,
  designSchemaScript,
]
