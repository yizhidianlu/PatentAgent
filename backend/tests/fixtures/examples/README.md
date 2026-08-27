# 示例案件目录

本目录提供**可随仓库提交的演练材料**。

| 路径 | 类型 | 说明 |
|------|------|------|
| `example_batch_job_scheduler/` | **发明**交底 | 虚构「批任务调度」；`knowledge/` 供 Step 2 扫描 |
| `example_utility_model_ev_powertrain/` | **实用新型**交底 | 集成式电驱桥壳体教学 brief；展台实拍作参考；**文生图**出结构线稿 |
| `example_design_desk_lamp/` | **外观设计**交底 | 折臂台灯教学 brief；国内媒体实拍 + README 下载链接 |
| `example_patent_reader/` | **解读** | README 内 CDN / `fetch_patent_pdf` 下载链接；PDF 本地自备（gitignore） |
| `example_oa_response/` | **审查答复（模式 D）** | 2 篇历史案例 md + 1 篇待答复通知书；测入库/标签检索/答复草稿 |

冒烟用极简 TXT 见 `tests/fixtures/patent_reader_sample.txt`。

---

## 实用新型交底演练

见 [example_utility_model_ev_powertrain/README.md](example_utility_model_ev_powertrain/README.md)。

要点：intake 指定「实用新型」→ 填 StructureSchema + **`figure_plan.yaml`**（实拍不入文）→ 按 brief **文生图**线稿（`image_gen.md`）→ `prompts/disclosure/utility_model/` 成文（只嵌合格线稿）→ 查新 `--type utility_model`。  
STEP 扫描夹具在 `tests/fixtures/cad/demo_snap_plate.step`，与本示例无关。

## 外观设计交底演练

见 [example_design_desk_lamp/README.md](example_design_desk_lamp/README.md)。

要点：指定「外观设计」→ AppearanceSchema + **`figure_plan.yaml`** → 线稿必做 → 成文时 **实拍与线稿都写入 md 和 Word** → 查新 `--type design`。

## 专利解读（含实用 / 外观 PDF）

见 [example_patent_reader/README.md](example_patent_reader/README.md)。实用新型 / 外观公开号下载后，解读时按 `type_hooks.md` 写 `structure_schema.json` / `appearance_schema.json`，入库自动写入笔记与 Canvas。

## 审查答复（模式 D）

见 [example_oa_response/README.md](example_oa_response/README.md)。

要点：先入库 `cases/*.md` → 用 `pending/oa_notice_pending.md` 做 `search_cases`（可 `--tags-only`）→ 再按 `prompts/oa/respond_office_action.md` 出草稿。

## 发明交底（原有）

### 如何使用 `example_batch_job_scheduler`

全流程产物由技能写入 **`outputs/{案件标识}/`**。命名见 **`prompts/disclosure/invention/disclosure_builder.md` §7.3**。

#### 方式 A：只看原材料

打开 `example_batch_job_scheduler/knowledge/`。

#### 方式 B：Agent 全流程

```text
请按 patent-disclosure-skill 全流程执行：
- 项目扫描目录：examples/example_batch_job_scheduler/knowledge/
- 技术主题：分布式批任务调度、异构集群、资源感知与限频重排队
```

（未指定类型时**默认发明**。）

查新见 `prompts/disclosure/prior_art_search.md`。定稿经 `tools/shared/mermaid_render.py`。

#### 迭代

`prompts/disclosure/iteration_context.md` + `merger.md` / `correction_handler.md`。
