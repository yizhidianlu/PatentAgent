# 引途医疗专利智能体 · 开发指南

面向要改代码、加模块、接新流程的开发者。产品使用说明见 [USER_GUIDE.md](./USER_GUIDE.md)，
原始设计文档在 [`docs/design/`](./design/)（后端架构 / 前端实现 / prompt 移植规格三份，是本项目的规格来源，**本文只描述已落地的实现**）。

---

## 目录

- [一、跑起来](#一跑起来)
- [二、架构总览](#二架构总览)
- [三、流水线引擎](#三流水线引擎)
- [四、SSE 事件表](#四sse-事件表)
- [五、StageCard：人机确认环节](#五stagecard人机确认环节)
- [六、Prompt 装配器与资产目录约定](#六prompt-装配器与资产目录约定)
- [七、如何新增一个模块](#七如何新增一个模块)
- [八、测试策略](#八测试策略)
- [九、已知限制与技术债](#九已知限制与技术债)

---

## 一、跑起来

### 开发模式（前后端分离，前端热更新）

```powershell
# 终端 1：后端
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

# 终端 2：前端
cd frontend
npm run dev        # http://localhost:5173，/api 由 vite proxy 转发到 127.0.0.1:8000
```

### 生产模式（单进程）

```powershell
.\start.ps1        # 构建前端 → uvicorn 静态挂载 frontend/dist + SPA fallback
```

`app/main.py` 在 `frontend/dist` 存在时 `app.mount("/", SpaStaticFiles(...))`，
未命中的**非 `/api` 路径**一律回退 `index.html`（前端路由生效）。

### 验证

```powershell
# 后端：必须全绿
cd backend; .\.venv\Scripts\python.exe -m pytest tests/ -q

# 前端：必须带 -p，裸 tsc --noEmit 会静默通过
cd frontend; npx tsc -p tsconfig.app.json --noEmit; npm run build
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `<repo>/data` | 运行时数据根目录 |
| `PORT` | 8000 | 服务端口 |
| `LOG_LEVEL` | INFO | 日志级别 |
| `PATENT_BROWSER_CHANNEL` | 自动 | 强制 `chrome` / `msedge` / `chromium` |
| `PLAYWRIGHT_HEADED` | 0 | 置 1 让浏览器可见（调查新问题时用） |

可写在 `backend/.env`（`app/config.py` 的 pydantic-settings 会读）。

---

## 二、架构总览

```
┌─ frontend/ (React 19 + Vite + TS + Tailwind v4) ────────────────────────┐
│  pages/          四模块工作台 + 首页 + 设置 + 案例库                    │
│  components/pipeline/   PipelineStream / StageCardShell / StepProgress   │
│  components/stages/     20 张 StageCard（按 interaction kind 分派）      │
│  lib/sse.ts      手写 SSE 解析（fetch + ReadableStream，支持续传/重连）  │
│  stores/         zustand（ui / session / composer）                     │
│  mocks/          VITE_USE_MOCKS=1 时脱离后端回放脚本化时间线            │
└──────────────────────────── /api/v1 ────────────────────────────────────┘
┌─ backend/app/ (FastAPI，单进程 + asyncio 后台任务) ─────────────────────┐
│  api/       13 个 router，49 条路径 / 60 个方法端点，全挂 /api/v1        │
│  pipelines/ engine.py 通用状态机 + 四模块步骤定义 + registry 注册表     │
│  services/  llm / sse / assembler / assets_loader / convert / mermaid    │
│             export_docx / export_pdf / cnipa / drawings / formula        │
│             claims_lint / vector / artifacts / patches / terminology …    │
│  tools/     移植的 CLI 脚本，以**子进程**调用（崩溃隔离 + stdout 协议）  │
│  assets/    prompts/（运行时加载）+ references/（YAML 规则）             │
│             + vendor/（两个上游仓库的原文快照，仅溯源，运行时不加载）    │
│  db/        stdlib sqlite3（WAL）+ 单写连接 + 编号 SQL 迁移             │
│  models/    pydantic v2：API 契约与 LLM 结构化输出契约共用              │
└─────────────────────────────────────────────────────────────────────────┘
```

### 几个关键决策（照抄自设计文档，改动前请先读原文）

1. **不引入 Celery/Redis**。本地单用户，长任务 = `asyncio.Task`，进程内 SSE hub 推送。
2. **Playwright / 格式转换类工具保持为独立 CLI 脚本，用子进程调**。理由：崩溃隔离、避免
   sync-Playwright 与 asyncio 事件循环冲突、沿用上游的 stdout 机读协议
   （`EPUB_HITS_JSON:` / `MERMAID: ok=` / `DOCX: ok=1`）。
3. **不用 ORM**。sqlite-vec 需要原生连接 `enable_load_extension`；迁移用
   `app/db/migrations/NNN_*.sql` + `schema_version` 表。同步 DB 调用经
   `anyio.to_thread` / `db.arun()` 进线程池。
4. **LLM 编排是固定多步流水线**，不是 agent loop。每步一次或数次结构化输出调用，
   可控、可流式、可断点续跑、token 可预算。
5. **大 JSON 不让模型一次吐**。分段结构化生成 + **服务端确定性装配**（见
   `pipelines/paper2patent.py` 的 `rules_check`）。
6. **降级永远优先于失败**：PDF 导出三级链、sqlite-vec 加载失败降级暴力余弦、
   查新失败转人工门控、附图不合格降级为 prompt-only。任何降级都要在 API/UI 上**明示**。

### 数据表

`cases` / `messages` / `files` / `pipeline_runs` / `artifacts` / `search_queries` /
`search_hits` / `oa_library` / `oa_chunks` / `oa_vec`(vec0 虚拟表) / `llm_calls` / `settings`。

两条铁律：

- `cases.state_json` 是流水线**工作内存**，每步 done 时把 output 合并进去。恢复 =
  读 state_json + 在 `pipeline_runs` 里找第一个非 done 的步骤重跑。
- `artifacts` **只增不改**。迭代永远 INSERT 新行 + 新时间戳文件，禁止覆盖。

---

## 三、流水线引擎

`app/pipelines/engine.py`，约 530 行，是整个后端的心脏。

### 步骤定义

```python
@dataclass
class StepDef:
    key: str                 # 稳定标识，落 pipeline_runs.step_key
    name_zh: str             # 中文步骤名（前端步骤条与事件里回显）
    handler: StepHandler     # async (ctx) -> StepResult | dict | None
    gate: str | None = None  # None | 'form' | 'confirm' | 'select' | 'review' | 自定义 kind
    retryable: bool = True
```

`handler` 返回的 dict 会被 `ctx.state.update(output)` 合并进工作内存并持久化。

### Ctx 能做什么

```python
ctx.case_id / ctx.run_group / ctx.step_key / ctx.attempt / ctx.gate
ctx.state                     # 工作内存 dict（步骤成功后引擎负责落库）
ctx.case                      # 案件行快照；await ctx.reload_case() 刷新
ctx.start_payload             # /pipeline/start 的 body

await ctx.emit(event, data, persist=True)      # 发 SSE，默认落 messages（重放源）
await ctx.chat_delta(text) / ctx.chat_done()   # 会话通道流式（不落库）
await ctx.doc_delta(doc_id, text) / ctx.doc_done(doc_id)   # 文档通道流式

answer = await ctx.await_user(InteractionRequest(kind=..., schema=..., prompt=..., default=...))

ctx.llm      # services.llm 直通（chat / chat_stream / structured）
ctx.assets   # services.assembler 直通
ctx.db       # db.database 直通
```

**`await_user` 是这套设计的关键**：门控不是 prepare/consume 两段回调，而是**协程挂起**——
emit `interaction_required` → 状态置 `waiting_user` → `await asyncio.Event()` →
`/pipeline/input` 注入 payload 后唤醒并返回。于是一个门控步骤可以写成一段自然的顺序代码：

```python
async def points_mining(ctx: Ctx) -> dict[str, Any]:
    candidates = await _analyze(ctx)                     # 流式分析
    picked = await ctx.await_user(InteractionRequest(    # 停在这里等用户
        kind="patent_points", schema=..., prompt="请勾选要写入交底书的专利点",
        default={"selected": [c["id"] for c in candidates if c["recommended"]]},
    ))
    return {"points": _merge(candidates, picked)}        # 用户点了确认才会走到这
```

### 生命周期

| 入口 | 行为 |
|---|---|
| `POST /pipeline/start` | `registry.build_steps(case)` → `engine.start()` 起后台任务；已在运行返回 409 |
| `POST /pipeline/input` | `engine.submit_input(case_id, step_key, payload)` 唤醒挂起协程；step_key 不匹配返回 409 |
| `POST /pipeline/resume` | 同 start，但**已 done 的步骤自动跳过**，等于从第一个非 done 步骤续跑 |
| `POST /pipeline/retry` | 同上；失败步骤会以 `attempt+1` 重跑（每次 attempt = 一行 `pipeline_runs`） |
| `POST /pipeline/cancel` | `task.cancel()`，回写 `cancelled`，case 回 `draft` |
| startup | `engine.recover_interrupted()`：遗留的 `running` → `failed('interrupted')`；`waiting_user` 保持原状（resume 重跑该步骤时自然重发卡片） |

**重要**：`_pending`（挂起中的交互）是**内存态**，进程重启即失。这不是 bug——
resume 会重跑该步骤，`await_user` 会重新 emit 一次 `interaction_required`。
所以门控步骤的 handler 必须能安全地重跑（幂等或至少无害）。

### 错误处理层次

1. **LLM 网络错**：`services/llm` 内部自动重试 1 次（退避 2s / 8s），引擎不重复。
2. **结构化解析失败**：`llm.structured()` 带 ValidationError 反馈重试 1 次。
3. **业务规则不过**（lint 失败）：各步骤自己发 REPAIR 调用，通常 ≤2 次。
4. **步骤抛异常**：`_run_step` 捕获 → run 置 failed → emit `step_status(failed)` + `error{retryable}` → 流水线停。
5. **引擎级异常**（handler 返回了不支持的类型、DB/emit 本身炸了）：`run_pipeline` 的兜底
   except 会收尾残留的 running 行并补发 `error` + `pipeline_done`，**绝不让前端永远转圈**。

---

## 四、SSE 事件表

`GET /api/v1/cases/{case_id}/events`，`sse-starlette`。`id:` 字段 = `messages.seq`，
支持 `Last-Event-ID` / `?after=` 重放。前端 canonical 类型定义在
`frontend/src/types/stream.ts` 的 `CaseSseEventMap`，**改后端事件契约必须同步改它**。

| 事件 | 载荷 | 何时发 | 落库 |
|---|---|---|---|
| `step_status` | `{step_key, status, name_zh?, attempt?, error?}` | 每步 running / waiting_user / done / failed | ✅ |
| `llm_delta` | `{step_key, channel:'chat'\|'doc', text, doc_id?}` | GEN/CHAT 流式增量（高频） | ❌ |
| `llm_done` | `{step_key, channel, doc_id?}` | 一段流式结束 | ✅ |
| `doc_version` | `{doc_id?, version_id, version, kind, filename, artifact_id?, downloads[], iteration_type?, summary?, created_at}` | 文档定稿一个新版本 | ✅ |
| `interaction_required` | `{step_key, kind, schema, prompt, default}` | `ctx.await_user()` 挂起时 | ✅ |
| `search_progress` | `{message, phase?, count?}` | 查新进度滚动 | ✅ |
| `artifact_created` | Artifact 记录 | 交付物落盘 | ✅ |
| `case_title` | `{title}` | 案件名被流水线确定/改写 | ✅ |
| `log` | `{message, level?}` | 过程提示 | ✅ |
| `error` | `{msg, message, retryable, step_key?}` | 步骤或引擎失败 | ✅ |
| `pipeline_done` | `{run_group, status:'done'\|'failed'\|'cancelled'}` | 流水线终止 | ✅ |
| `ping` | `{t}` | 保活 | ❌ |

> `error` 载荷同时带 `msg` 与 `message`：`msg` 是契约字段，`message` 是给前端既有
> `ErrorEvent` 类型的兼容字段。加新字段时保持这个习惯——**只增不删**。

高频的 `llm_delta` 刻意不落库（否则 messages 表会爆）。重放时用 `llm_done` 与
`messages` 里的完整文本恢复段落边界。

---

## 五、StageCard：人机确认环节

### 后端 → 前端的映射

后端 `InteractionRequest.kind` → 前端 `stageCardRegistry[kind]` → 具体卡片组件。
未注册的 kind 一律兜底 `UnknownStageCard`（渲染 prompt + 原始 JSON + 确认/跳过），
**所以后端加新 kind 不会让前端白屏**。

当前后端实际下发的 14 种 kind：

| kind | 卡片 | 出现在 |
|---|---|---|
| `intake` | IntakeCard | 交底书 · 边界录入 |
| `type_suggest` | TypeSuggestCard | 交底书 · 类型改判反问（条件触发） |
| `patent_points` | PatentPointsCard | 交底书 · 专利点挖掘 |
| `schema_fill` | SchemaFillCard | 交底书 · 填表与线稿（实用/外观） |
| `prior_art` | PriorArtCard | 交底书 · 联网查新 |
| `preview_confirm` | PreviewConfirmCard | 交底书 · 摘要预览 |
| `claim_bias` | ClaimBiasCard | 交底书 · 交付（含迭代交付） |
| `review` | ReviewCard | 论文转专利 · 内容确认；解读 · 权要树 |
| `claim_tree` | ClaimTreeCard | 解读 · 权要树消歧 |
| `oa_issues` | OaIssuesCard | 答复 · 通知书结构化（强制人审） |
| `oa_retrieve` | OaRetrieveCard | 答复 · 案例检索（强制人审） |
| `oa_strategy` | OaStrategyCard | 答复 · 策略选择（强制人审） |
| `confirm` | ConfirmCard | 答复 · 汇总交付；通用确认 |
| `form` | FormCard | 通用 JSON Schema 表单（如解读取证失败时请求上传） |

注册表里还有 6 张卡（`material_upload` / `mode_select` / `self_check` /
`content_review` / `figures_preview` / `delivery`）——`content_review` 是 `review` 的别名，
其余目前**只在 mock 剧本里出现**，真实流程用 `doc_version` / `artifact_created` 事件
与既有门控替代。要接真实流程，直接在对应步骤里 `await_user(kind='delivery', ...)` 即可，
前端不需要改。

### 卡片的契约

```ts
type Stage = {
  id: string
  type: StageType | (string & {})   // = interaction_required.kind
  status: 'active' | 'completed' | 'skipped'
  payload: unknown                  // interaction_required 原始载荷
  result?: unknown                  // 用户提交内容（乐观 completed 时立即写入）
  stepKey?: string
}
```

提交走 `POST /cases/:id/pipeline/input {step_key, payload}` → 前端**乐观**置 completed →
后端 SSE 续推。同一时刻只有最新的 stage 可 active；侧栏红点 = 有 active stage。

`StageCardShell` 负责壳（active 描边 + 「待确认」徽章 + 底部跳过/确认；completed 折叠成
一行摘要可重展只读；skipped 置灰），卡片组件只管中间的 body 和提交出去的 payload 形状。

---

## 六、Prompt 装配器与资产目录约定

### 目录

```
backend/app/assets/
├── prompts/                运行时实际加载的「Web 适配版」
│   ├── common/             system_base.md / desensitization.md / terminology_rules.md
│   ├── disclosure/         intake.md material_digest.md preview.md self_check_*.md
│   │                       invention/ utility_model/ design/ shared/ prior_art/ iteration/
│   ├── paper2patent/       pro/(9 份逐字拆分) extraction.md claims_spec_rules.md …
│   ├── reader/             claim_tree.md claim_deltas.md report_writer.md report_template.md …
│   ├── oa/                 notice_struct.md strategy.md respond_issue.md guardrails.md …
│   └── <module>/manifest.yaml     ← 资产清单：逐文件记 source_repo/source_path/treatment
├── references/             原样保留、按需注入的数据型资产
│   ├── formulas/paradigms.yaml    schemas/*.schema.yaml
│   ├── patent_type_search.yaml    ipc_application_hints.yaml
│   ├── patent_pdf_sources.yaml    patent_domain_rules.yaml
│   └── p2p/                六份规则文档（verbatim）
└── vendor/                 两个上游仓库的原文快照（**仅溯源，运行时绝不加载**）
    ├── patent-disclosure-skill/   (MIT, handsomestWei)
    └── paper2patent/              (MIT + CLAUSE.md, 7toCR)
```

### 资产加载

`services/assets_loader.py`：

```python
assets_loader.get_text("prompts/common/system_base.md")   # 正文（剥 front-matter）
assets_loader.get_text("common/system_base")              # 等价：缺前缀默认 prompts/，缺后缀补 .md
assets_loader.get_asset(key).meta                         # front-matter 元数据
assets_loader.list_assets("prompts/oa")                   # 列 key
assets_loader.clear_cache()                               # 改了文件后清 lru 缓存
```

带路径穿越防护（必须仍在 `assets/` 内）。

### 每个适配版 prompt 文件必须有 front-matter

```yaml
---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/disclosure/invention/disclosure_builder.md（§7.5）
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted          # verbatim | verbatim_split | adapted | new
ported_version: 2026-08-25.1
notes: 说明这次改编改了什么、为什么
---
```

`treatment` 语义：`verbatim` 逐字整份 / `verbatim_split` 逐字拆分片段 / `adapted` 改编 /
`new` 本项目新写。**改编时写作规则正文必须逐字保留**，只剥离宿主 Agent 指令
（`Read xxx.md`、`运行 xxx.py`、落盘路径、allowed-tools）——这些行为在本项目里都由服务端代码承担。

### 装配器

`services/assembler.py`：

```python
system = assembler.assemble(
    [
        "common/system_base",              # [1] 平台角色头
        "disclosure/invention/builder_core",# [2][3] 模块 role + 类型规则
        "common/desensitization",          # [4] 脱敏（所有 GEN 类必注入）
        "common/terminology_rules",        # [5] 术语/标题贯穿（所有 GEN 类必注入）
        "disclosure/invention/g2",         # [6] 本阶段专属指令
    ],
    runtime_ctx={                          # [7] 定界符包裹的运行时上下文
        "case_card": {...},                # 固定注入顺序：案件卡片 →
        "terminology_sheet": {...},        # 术语表 → 骨架 → extra
        "outline": {...},
        "extra": {...},
    },
)
system.file_hashes    # [{key, sha256}]，写进 llm_calls.meta 保证可复现
```

调用分类参数表（`assembler.CALL_CLASS` / `call_params(cls)`）：

| Class | temperature | 流式 | 用途 |
|---|---|---|---|
| `STRUCT` | 0.1 | 否 | 结构化抽取/规划（JSON） |
| `GEN` | 0.5 | 是 | 章节/文书生成（markdown） |
| `REPAIR` | 0.2 | 否 | 带错误反馈的修复 |
| `AUDIT` | 0.2 | 否 | 自检/审校（补丁清单） |
| `CHAT` | 0.6 | 是 | 暂停点复述/答疑 |

### LLM 客户端

`services/llm.py`（OpenAI 兼容，`AsyncOpenAI` + 可配 base_url）：

- `chat(messages, **kw) -> str`
- `chat_stream(messages, **kw) -> AsyncIterator[str]`
- `structured(messages, model_cls, **kw) -> model_cls`
  —— 优先 `response_format={"type":"json_object"}`（设置页的能力位控制），不支持时退化为
  「只输出一个 ```json 围栏块」+ 服务端 `extract_first_json` + pydantic 校验 + 带
  ValidationError 重试 1 次。
- 每次调用记账到 `llm_calls`（model / tokens / duration / status / prompt 文件 hash）。
- 传 `step_key=` 参数——**测试的 FakeLLM 就是按它分派脚本的**，见第八节。

---

## 七、如何新增一个模块

以加一个假想的「专利无效宣告请求书」模块（`invalidation`）为例。

### 1. 允许新的 module 值（DB 迁移）

`cases.module` 有 CHECK 约束，加新模块要写一条迁移：

```sql
-- app/db/migrations/003_invalidation.sql
-- SQLite 不能直接改 CHECK，需要重建表；参考 001_init.sql 的写法
```

新增迁移文件后 `db.init_db()` 会按编号顺序执行并更新 `schema_version`。

### 2. 定义步骤（`app/pipelines/invalidation.py`）

```python
from typing import Any
from .engine import Ctx, InteractionRequest, StepDef


async def collect(ctx: Ctx) -> dict[str, Any]:
    """S1：取目标专利与证据。"""
    await ctx.chat_delta("正在解析目标专利…")
    await ctx.chat_done()
    return {"target": {...}}


async def pick_grounds(ctx: Ctx) -> dict[str, Any]:
    """S2：无效理由勾选（门控）。"""
    grounds = await _suggest(ctx)
    picked = await ctx.await_user(InteractionRequest(
        kind="invalidation_grounds",          # ← 前端未注册也不会白屏（兜底卡）
        schema={"type": "object", "properties": {
            "selected": {"type": "array", "items": {"type": "string"}},
        }},
        prompt="请勾选主张的无效理由",
        default={"selected": [g["id"] for g in grounds if g["recommended"]]},
    ))
    return {"grounds": picked.get("selected", [])}


async def draft(ctx: Ctx) -> dict[str, Any]:
    system = ctx.assets.assemble(
        ["common/system_base", "invalidation/writer", "common/terminology_rules"],
        runtime_ctx={"case_card": ..., "extra": {"grounds": ctx.state["grounds"]}},
    )
    text = ""
    async for delta in ctx.llm.chat_stream(
        [{"role": "system", "content": str(system)}, {"role": "user", "content": "…"}],
        step_key=f"{ctx.step_key}.draft",     # ← FakeLLM 按 '.' 后缀分派
        **ctx.assets.call_params("GEN"),
    ):
        text += delta
        await ctx.doc_delta("main", delta)
    await ctx.doc_done("main")
    return {"draft_md": text}


def build_steps(case: dict[str, Any]) -> list[StepDef]:
    return [
        StepDef(key="collect", name_zh="取证与解析", handler=collect),
        StepDef(key="grounds", name_zh="理由勾选", handler=pick_grounds, gate="invalidation_grounds"),
        StepDef(key="draft",   name_zh="起草请求书", handler=draft),
    ]
```

### 3. 注册（`app/pipelines/registry.py` 末尾）

```python
from . import invalidation as _invalidation     # 置于 register 定义之后，避免循环导入
register("invalidation", _invalidation.build_steps)
```

注册表用**模块 key → build_steps(case_row) 工厂**的形式，工厂能读到 `patent_type` 等字段，
所以同一模块的不同分支（如交底书的发明 8 步 vs 实用/外观 9 步）在工厂里分派即可。

### 4. 资产

新建 `app/assets/prompts/invalidation/`，放 prompt 文件（每个带 front-matter）
和一份 `manifest.yaml`。若内容移植自第三方，**必须**同时更新 `NOTICE.md`
并在 `assets/vendor/` 放原文快照。

### 5. 前端：卡片 + 页面

```ts
// frontend/src/components/stages/InvalidationGroundsCard.tsx
export function InvalidationGroundsCard({ stage, onSubmit, onSkip }: StageCardProps) { … }

// frontend/src/components/pipeline/stageCardRegistry.ts
invalidation_grounds: InvalidationGroundsCard,
```

页面照抄 `PaperPage.tsx` 的结构：`WorkspaceShell` + `stepPresets` + accent 色 +
空态 mini-hero。`stepPresets` 的 `key` 要对上后端 `StepDef.key`
（可用 `matchKeys` 做多对一映射）。

再把模块加进：`src/i18n/zh.ts`（全部 UI 文案的单一来源）、`routes/router.tsx`、
首页 `ModuleToggle` 的分段、侧栏分组。

### 6. Mock 剧本

`frontend/src/mocks/mockEvents.ts` 里加一条脚本化时间线，`mockServer.ts` 里挂上。
这样 `VITE_USE_MOCKS=1 npm run dev` 就能脱离后端做 UI 开发和像素 QA。剧本要覆盖：
流式 delta、每种门控卡、doc_version、artifact_created、以及至少一个 error 分支。

### 7. 测试

`backend/tests/test_invalidation.py`，照第八节的套路。

---

## 八、测试策略

277 个用例 + 1 skip，约 100 秒跑完。**改动后必须保持全绿。**

```
tests/
├── conftest.py                在 import app 之前把 DATA_DIR 指向临时目录
├── test_engine.py        13   引擎：门控挂起/唤醒、resume 跳过、retry attempt、cancel、恢复钩子
├── test_pipeline_api.py   5   /pipeline/* 的状态机与 409 语义
├── test_disclosure.py    22   交底书发明分支端到端（含 REPAIR 触发断言）
├── test_disclosure_types.py 11 实用新型/外观分支与 schema_fill
├── test_disclosure_iterate.py 26 迭代：意图路由、受影响章节、版本化、修订记录
├── test_p2p.py           15   论文转专利端到端 + 确定性装配
├── test_reader.py        23   解读：权要树 lint、白话增量长度、段落号引用
├── test_oa.py            19   答复：枚举校验、三处人审、跨条一致性
├── test_claims_lint.py   25   权项 lint 全规则（含禁用词白名单误伤用例）
├── test_formula.py       22   公式范式与数值复算
├── test_vector.py        14   sqlite-vec + 降级路径
├── test_cnipa.py         23   查新解析与失败兜底
├── test_convert.py / test_export.py / test_patches.py / test_settings.py / test_smoke.py
└── fixtures/                  上游 examples/ 的教学案件材料 + 样例论文 PDF
```

### FakeLLM：按 step_key 后缀回放

不打真实模型。`tests/test_disclosure.py` 里的 `fake_llm` fixture 把
`llm.chat` / `chat_stream` / `structured` 三个入口整体换掉：

```python
class FakeLLM:
    @staticmethod
    def _tag(kwargs):
        step_key = str(kwargs.get("step_key") or "")
        return step_key.split(".", 1)[1] if "." in step_key else step_key   # 'build.g2' → 'g2'

    async def structured(self, messages, model_cls, **kwargs):
        tag = self._tag(kwargs)
        self.calls.append(f"struct:{tag}")
        data = self.struct_script.get(tag)
        if data is None:
            raise AssertionError(f"FakeLLM 未脚本化的结构化调用：{tag}")
        return model_cls.model_validate(data)
```

三条纪律：

1. **未脚本化的调用直接 AssertionError**，不静默返回默认值。流程一变，测试立刻炸给你看。
2. **脚本里故意放不合格产物**（`CH3_BAD` 的 mermaid 语法错、`FORMULA_PLAN_BAD` 的数值不闭合、
   `CLAIM_BIAS_BAD` 的 basis_quote 不是终稿子串），再断言 `"chat:g2.repair1" in fake_llm.calls`
   —— **这样测的是"门禁真的触发了 REPAIR"，而不只是"happy path 能跑通"**。
3. **每个 LLM 调用点都要传 `step_key=f"{ctx.step_key}.<tag>"`**，否则测试无法定位它。

### 真实产物断言

不止断言"没报错"，还要断言磁盘上的东西是对的：

- `.docx` 用 `python-docx` 打开，检查章节标题、图片数量、OMML 公式节点；
- `.md` 检查章节结构、URL 全部落在命中集合内、mermaid 围栏可解析；
- `artifacts` 表检查版本号递增、文件名带时间戳、旧文件仍在；
- SSE：`TestClient` 拉 `/events`，断言事件序列与顺序。

外部依赖用桩替换（`cnipa.search`、`export_pdf`、浏览器子进程）——见
`_install_search()` 的写法。Word COM 相关的测试在没装 Word 的机器上 skip（就是那 1 个 skip）。

### 前端

```powershell
npx tsc -p tsconfig.app.json --noEmit    # ← 必须带 -p
npm run build
npm run lint                              # oxlint
```

UI 回归目前靠 Playwright 截图人工比对（双主题 × 1440/1024/768/390 四断点），
脚本用 backend 的 venv 跑，`channel="chrome", headless=True`。

---

## 九、已知限制与技术债

### 功能范围（v1 明确裁剪，非 bug）

| # | 项 | 现状 | 扩展点 |
|---|---|---|---|
| 1 | **迭代仅支持发明交底书** | 实用新型/外观走 `PatentTypeNotSupportedError` 并给出中文提示；其余三模块无迭代 | `pipelines/disclosure_iterate.py` 的步骤对 schema 分支是通的，主要缺 figure_plan 同步与第五章书式 lint 的迭代版 |
| 2 | **AI 线稿生成未接入流水线** | `settings/image-gen` 可配置并测试连通，`tools/image_gen.py` 与 `{design,structure}_lineart_gate.py` 已移植，但 `schema_fill` 只下发「线稿绘制说明」请用户上传 | 在 `pipelines/disclosure.py::schema_fill` 里接 gen_prompt → image_gen → gate → 叠标 → 语义自查（≤2 轮）；prompt 模板已 verbatim 就位 |
| 3 | **CAD / STEP 不解析** | 只按扩展名归类提示，且硬规则「CAD 永不入文」 | 上游的 CadQuery 方案需要 3.10–3.12 的隔离 venv，故本期不做 |
| 4 | **模式 C「政策进化」未移植** | 上游 `evolution/*` 整体弃用 | — |
| 5 | **论文转专利的 flash 快速档不进脚本管线** | 单次 GEN 出五部分纯文本直接交付，不产附图/JSON | 有意为之（省 token），需要完整产物请用 direct/hil |
| 6 | **解读与答复不出 PDF** | `artifacts.py` 的 `_DOCX_TO_PDF_KIND` 只映射了 `disclosure_docx` / `patent_docx` | 加两行映射即可，已验证 Word COM 链路通用 |
| 7 | **人工确认模式（hil）在 p2p 只停一处** | 只有 `content_review` 是真门控，B1 输入评估与 B6 附图预览的 `[PAUSE]` 未接 | 步骤已就位，`await_user` 加上即可 |

### 实现层技术债

| # | 项 | 影响 | 建议 |
|---|---|---|---|
| 8 | **前端 6 张 StageCard 只在 mock 里出现** | `material_upload` / `mode_select` / `self_check` / `figures_preview` / `delivery` 后端不下发（`content_review` 是 `review` 的别名）| 要么在对应步骤 `await_user` 用起来，要么标注为 mock-only 避免误导 |
| 9 | **`docx2pdf` 是死依赖** | 声明在 `pyproject.toml` 里但代码从不 import（`services/export_pdf.py` 直接用 `win32com` 以便控制 `DisplayAlerts`），白白拉长首装时间 | 确认无回退需求后删掉这一行 |
| 10 | **`pymupdf<1.28` / `lxml<6` 是本机规避性上限** | 这两个上限是因为本机 Windows + Py3.13 上新版 DLL 加载失败；换机器可能没必要 | 定期复验，能放开就放开，并在 pyproject 注释里记清放开条件 |
| 11 | **挂起中的交互是纯内存态** | 进程重启后 `_pending` 丢失，必须 resume 重跑该步骤才能重新拿到卡片 | 已在设计上接受；如果要免 resume，需要把 `InteractionRequest` 落 `pipeline_runs.input_json` 并在 startup 重建 |
| 12 | **`llm_delta` 不落库** | SSE 重放时只能靠 `messages` 里的完整文本恢复，无法逐 token 重放 | 有意为之（避免 messages 表膨胀） |
| 13 | **单写连接 + 全局锁** | 本地单用户没问题；多人同时用会串行排队 | 本项目定位就是单用户，不打算改 |
| 14 | **Word COM 全局串行** | PDF 导出用 `asyncio.Lock` 串行 + 专用 STA 线程，多个案件同时导出会排队 | COM 限制，无解；失败时降级只交 docx |
| 15 | **禁用词表 14 个 vs 设计文档说的 15 个** | 以上游 paper2patent README 的原表为准（14 个），已在 `claims_lint.py` 文件头注明 | 保持现状，别为了对齐文档数字硬凑 |
| 16 | **`start.ps1` 有两个 PowerShell 5.1 陷阱** | 见下方「改 start.ps1 前必读」 | 改完务必用 `powershell.exe -File start.ps1 -SetupOnly` 实跑一遍，别只看代码 |
| 17 | **UI 回归靠人工看截图** | 没有自动化视觉回归 | 可接 Playwright 截图 diff，基线图已有 130+ 张 |

### 改 start.ps1 前必读（两个 PowerShell 5.1 陷阱，都真实踩过）

用户双击 `start.bat` / 右键「使用 PowerShell 运行」跑的是 **Windows PowerShell 5.1**
（不是 PowerShell 7），它有两个会静默毁掉冷启动体验的行为：

**① 脚本文件必须是 UTF-8 with BOM。**
PS 5.1 读取**没有 BOM** 的 UTF-8 脚本时按系统 ANSI 代码页（简体中文机器上是 GBK）解码，
于是脚本里所有中文提示都会变成 `涓枃娴嬭瘯` 这种乱码——而且**不报任何错**。
改完 `start.ps1` 后检查前三字节必须是 `EF BB BF`：

```powershell
$b = [System.IO.File]::ReadAllBytes('start.ps1')
if ($b[0] -ne 0xEF) { '缺 BOM！' }
# 补 BOM：
$t = [System.IO.File]::ReadAllText('start.ps1', [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText('start.ps1', $t, [System.Text.UTF8Encoding]::new($true))
```

`start.bat` 相反——保持**纯 ASCII**，因为 cmd 按 OEM 代码页读 `.bat`，中文同样会乱码。
所有面向用户的中文都由 `start.ps1` 打印。

**② 向原生程序传参时，内嵌的双引号会被吞掉。**

```powershell
# ✗ 坏：python 收到的是  import sys;print(%d.%d %  sys.version_info[:2])  → SyntaxError
& $Py -c 'import sys; print("%d.%d" % sys.version_info[:2])'

# ✓ 好：Python 代码里不出现双引号（外双内单，或干脆不用引号）
& $Py -c "import importlib.util as u,sys;sys.exit(0 if u.find_spec('fastapi') else 1)"
& $Py -c 'import sys;print(sys.version.split()[0])'
```

另外记住 PS 5.1 **没有** `&&` / `||` / 三元 / `??`，且原生命令失败**不会**因
`$ErrorActionPreference='Stop'` 抛异常——每个 `& $exe ...` 后面都要自己查 `$LASTEXITCODE`。

### 上游同步

两个 vendor 快照（`assets/vendor/`）是 2026-08-25 的版本，`ported_version: 2026-08-25.1`。
上游更新时的流程：拉新快照 → 对着各模块的 `manifest.yaml` 逐条 diff → 更新受影响的
适配版 prompt 与其 front-matter 的 `ported_version` → 跑全量测试 → 更新 `NOTICE.md`。
`treatment: verbatim` 的条目必须逐字同步，`adapted` 的只同步规则正文的变化。
