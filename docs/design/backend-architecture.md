# 引途医疗专利智能体 — 后端实施计划 (FastAPI + SQLite)

> 由规划 Agent 产出，2026-08-25。产品名：引途医疗专利智能体。

## 0. 环境事实与总体决策

已验证的本机事实（影响技术选型）：
- Python 3.13.13（唯一解释器；所有依赖需兼容 3.13）
- MS Word 已安装（`C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE`）→ **PDF 导出主路径 = Word COM (docx2pdf)**
- LibreOffice 未安装 → 仅作为可探测的次级路径
- Chrome (`C:\Program Files\Google\Chrome\Application\chrome.exe`) 与 Edge 均在 → Playwright 用 `channel="chrome"`（CNIPA 爬虫过 WAF 需复用本机浏览器，与原 skill 的 browser.py 策略一致）

核心架构决策：
1. **单进程 FastAPI + asyncio 后台任务**，不引入 Celery/Redis（本地单用户）。长任务 = `asyncio.Task`，进程内 SSE hub 推送。
2. **Playwright/转换类工具保留为独立 CLI 脚本、以子进程调用**，沿用原 skill 的机读 stdout 前缀协议（`EPUB_HITS_JSON:` / `MERMAID: ok=` / `DOCX: ok=1`）。理由：崩溃隔离、避免 sync-Playwright 与 asyncio 事件循环冲突、移植成本最低、原有 pytest 思路可复用。
3. **Mermaid 渲染 = 服务端 Playwright**（移植 `mermaid_render.py` + vendored `mermaid.min.js`）。理由：docx 嵌图必须要服务端 PNG；Playwright 反正是 CNIPA 的必装依赖；离线可用；预览与导出渲染结果一致。前端可以另用 mermaid.js 做实时预览（纯装饰），但导出用图一律以服务端 PNG 为准。
4. **PDF 链**：docx2pdf(Word COM，STA 线程 + 全局锁串行) → 探测到 soffice 则 LibreOffice → paper2patent 的 Pillow 图片版兜底（仅论文转专利模块有 JSON 可渲）→ 全部失败则只交付 docx 并提示。
5. **LLM 编排 = 固定多步流水线**（每步一次或数次结构化输出调用），不复用 skill 的"渐进式 Read 加载 agent loop"。理由：可控、可流式、可断点续跑、token 可预算；原 prompt 文件的**写作规则正文逐字保留**，仅剥离"Read/Write/运行脚本"等宿主 Agent 指令（脚本调用改为服务端代码）。
6. DB 访问：stdlib `sqlite3`（WAL 模式）+ 单写连接 + `threading.Lock`，经 `anyio.to_thread` 进入异步上下文；不用 ORM。理由：sqlite-vec 需要原生连接 `enable_load_extension`；本地单用户无并发压力；迁移用自带的编号 SQL 文件 + `schema_version` 表。

---

## 1. 项目布局与依赖

### 1.1 Monorepo 结构

```
PatentAgent/
├── README.md                       # 中文总说明 + 启动方式
├── NOTICE.md                       # 许可与来源声明（两仓库 MIT；7toCR/paper2patent CLAUSE.md 要求注明来源）
├── start.ps1 / start.bat           # 一键启动：检查 venv → 起 uvicorn → 打开浏览器
├── .gitignore                      # data/, node_modules/, .venv/, __pycache__/
├── data/                           # 运行时数据（gitignore；路径可在设置页改）
│   ├── app.db                      # SQLite 主库（含 sqlite-vec 虚拟表）
│   ├── uploads/{case_id}/          # 原始上传件 + 转换后 md
│   ├── outputs/{case_id}/          # 版本化交付物（时间戳命名，永不覆盖）
│   └── tmp/
├── frontend/                       # React SPA（构建产物 frontend/dist）
└── backend/
    ├── pyproject.toml              # 见 1.3
    ├── app/
    │   ├── main.py                 # FastAPI 实例、CORS、静态挂载 frontend/dist + SPA fallback、startup 迁移
    │   ├── config.py               # pydantic-settings：DATA_DIR、端口、日志级别
    │   ├── db/
    │   │   ├── database.py         # 连接工厂(WAL/foreign_keys/load sqlite-vec)、写锁、run_migrations()
    │   │   └── migrations/001_init.sql, 002_oa_vec.sql ...
    │   ├── models/                 # pydantic v2 模型（API 契约 + LLM 结构化输出契约）
    │   │   ├── common.py  settings.py  case.py  file.py  artifact.py  search.py
    │   │   ├── disclosure.py       # IntakeAnswer/PatentPoint/StructureSchema/AppearanceSchema/FigurePlan/FormulaPlan/PreviewSummary（由 references/schemas/*.schema.yaml 转写为 pydantic）
    │   │   ├── paper2patent.py     # PatentContent（document-generation.md 的 JSON 契约逐字段转写：invention_name/abstract/abstract_drawing/claims[]/description{...}/drawings[]/source_figures[]/drawing_assets[]/image_model_prompts[]/drawing_validation[]/gaps[]）
    │   │   ├── reader.py           # ClaimTree/ClaimDelta/NotePlan
    │   │   └── oa.py               # NoticeStruct/OaCaseNote frontmatter
    │   ├── api/                    # 路由（见 §3），每文件一个 APIRouter
    │   │   ├── settings.py  cases.py  files.py  pipeline.py  artifacts.py
    │   │   ├── search.py  p2p.py  oa_library.py  render.py  system.py
    │   ├── services/
    │   │   ├── llm.py              # OpenAI 兼容客户端封装：从 settings 读配置；chat()（非流）与 chat_stream()（SSE 转发）；structured()（json_object/提示词双保险 + pydantic 校验 + 1次自动重试）；llm_calls 记账
    │   │   ├── sse.py              # 进程内 hub：case_id → set[asyncio.Queue]；emit(case_id, event, data)；Last-Event-ID 重放（读 messages/pipeline_runs）
    │   │   ├── assets.py           # prompt/reference 加载器（带缓存；按 key 取文本，如 assets.get("disclosure/invention/builder")）
    │   │   ├── convert.py          # 上传转换调度：docx→md / pptx→md（子进程调 tools/）、pdf→md（pymupdf 直接 import，含图片抽取到 uploads/{case}/figures/）、图片直存
    │   │   ├── mermaid.py          # 子进程调 tools/mermaid_render.py，解析 "MERMAID: ok=" 前缀
    │   │   ├── export_docx.py      # 子进程调 tools/md_to_docx.py（OMML 公式；失败降级 matplotlib PNG 可选）
    │   │   ├── export_pdf.py       # docx2pdf(Word COM, STA线程+asyncio锁) → soffice 探测 → Pillow 兜底 → 报错降级
    │   │   ├── cnipa.py            # 子进程调 tools/cnipa_epub_search.py --type，解析 EPUB_HITS_JSON，超时/失败→标记需人工兜底
    │   │   ├── drawings.py         # 子进程调 tools/generate_patent_drawings.py（--update-json 回写）
    │   │   ├── formula.py          # 移植 check_formula_plan.py 为可 import 的校验函数（含 --eval 数值复算）+ paradigms.yaml 装载
    │   │   ├── claims_lint.py      # 确定性代码校验：15禁用词正则、每权项仅结尾一句号、从权引用基础、多引不引多引、名称一致（paper2patent Critical Rules 的机器可判部分）
    │   │   ├── vector.py           # sqlite-vec 封装：embed()（OpenAI兼容embedding）、upsert/search/rebuild；load 失败时降级为纯 Python 余弦暴搜（本地小语料可接受）
    │   │   └── artifacts.py        # 版本化落盘：规范化案件名（§7.3：取正文"**案件名称**："行，去占位/非法字符，≤80字符）+ _{YYYYMMDDHHmmss} 命名；插 artifacts 行；追加"交底书修订对话记录.md"
    │   ├── pipelines/
    │   │   ├── engine.py           # 通用状态机
    │   │   ├── disclosure.py       # 模块① 8步定义 + 迭代 run
    │   │   ├── paper2patent.py     # 模块② 定义
    │   │   ├── reader.py           # 模块③
    │   │   └── oa.py               # 模块④
    │   ├── tools/                  # 移植的 CLI 脚本（保持可独立运行 + stdout 协议）
    │   │   ├── md_to_docx.py  mermaid_render.py  docx_to_md.py  pptx_to_md.py
    │   │   ├── browser.py  patent_type.py                     # 来自 repo1 tools/shared
    │   │   ├── cnipa_epub_search.py  cnipa_epub_crawler.py  cnipa_epub_parse.py   # repo1 tools/crawl
    │   │   ├── generate_patent_drawings.py  generate_patent_docx.py  export_patent_pdf.py  # repo2 scripts（零依赖，原样）
    │   │   └── vendor/mermaid.min.js
    │   └── assets/
    │       ├── vendor/             # 两仓库原文快照（逐字，仅溯源用，运行时不加载）
    │       ├── prompts/            # 运行时实际加载的"Web 适配版"
    │       │   ├── disclosure/{intake.md, project_scan.md, prior_art_search.md, preview.md, self_check.md, iteration_context.md, merger.md, correction_handler.md}
    │       │   ├── disclosure/invention/{points_analyzer.md, builder.md, template_reference.md}
    │       │   ├── disclosure/utility_model/{points.md, builder.md, template.md}
    │       │   ├── disclosure/design/{points.md, builder.md, template.md}
    │       │   ├── disclosure/shared/{fill_structure_schema.md, fill_appearance_schema.md}
    │       │   ├── p2p/{orchestrator.md, extraction.md, draft_abstract_claims.md, draft_description.md, draft_embodiments.md, draft_drawings.md, review.md}
    │       │   ├── reader/{plain_reader.md, note_template.md(11节), type_hooks.md}
    │       │   └── oa/{notice_struct.md, respond.md, case_note_template.md, ingest.md}
    │       └── references/         # 原样保留、运行时按需注入的 YAML/规则
    │           ├── formulas/paradigms.yaml
    │           ├── schemas/*.schema.yaml   # figure_plan/structure/appearance/formula_plan/oa_case
    │           ├── patent_type_search.yaml  ipc_application_hints.yaml
    │           └── p2p/{input-requirements.md, text-conversion-workflow.md, claims-and-specification-rules.md, drawing-generation.md, document-generation.md, quality-checklist.md}
    └── tests/
        ├── test_convert.py  test_export.py  test_engine.py  test_claims_lint.py
        └── fixtures/（repo1 examples/ 的教学案件材料 + 一篇样例论文 PDF）
```

### 1.2 Prompt 移植策略：verbatim vs 适配

- **verbatim 保留**（`assets/vendor/` + `assets/references/`）：paradigms.yaml、9 个 schema.yaml、p2p 六个 references、模板正文（六章模板、正反例、脱敏表、禁用词表）。
- **适配改写**（`assets/prompts/`），统一改写规则：
  1. 删除宿主 Agent 指令：`Read xxx.md`、`运行 xxx.py`、文件落盘路径、allowed-tools、stdout 协议说明 —— 这些行为全部变为服务端代码。
  2. "向用户提问/等确认"改写为：**输出指定 JSON**，由服务端转成 `interaction_required` SSE 事件与前端表单；人机交互门控在引擎层实现，不靠 prompt。
  3. 写作规则正文（§7.1-7.9、8.1-8.5 自检项、Pro Prompt 的 Critical Rules 等）**逐字保留**。
  4. 每个适配文件头部加注释行 `<!-- adapted from handsomestWei/patent-disclosure-skill prompts/... (MIT) -->`。
- p2p 注意事项：`reference_skills/meterial.md` 在原仓库不存在，规范内容以 README 的 Pro Prompt 为准；取 `skills/paper2patent/` 主版本（`.claude/` 目录是镜像）。
- v1 范围裁剪：不移植 模式C政策进化、CAD/STEP 解析（CadQuery 需 3.10-3.12 隔离 venv）、AI 线稿生成（依赖图像生成模型）。实用新型/外观仍走 schema 填表 + figure_plan，但附图仅用**用户上传的图片**（按 figure_plan 打分选用），线稿生成留扩展点。

### 1.3 依赖清单（backend/pyproject.toml）

```toml
[project]
name = "patent-agent-backend"
requires-python = ">=3.11,<3.14"
dependencies = [
  "fastapi>=0.115",  "uvicorn[standard]>=0.30",
  "pydantic>=2.8",   "pydantic-settings>=2.4",
  "sse-starlette>=2.1", "python-multipart>=0.0.9",
  "openai>=1.60",           # OpenAI 兼容 chat + embedding（base_url 可配）
  "httpx>=0.27",            # 连接测试 / 通用抓取
  "playwright>=1.47",       # CNIPA 爬虫 + mermaid 渲染（channel=chrome）
  "python-docx>=1.1", "latex2mathml>=3.77",   # md_to_docx OMML
  "pymupdf>=1.24",          # PDF→md（论文/OA通知书/专利PDF）
  "pillow>=10",             # 附图 PNG / PDF 兜底
  "pyyaml>=6",
  "sqlite-vec>=0.1.6",      # OA 案例向量库
  "docx2pdf>=0.1.8", "pywin32>=306; sys_platform == 'win32'",  # Word COM PDF
  "python-ulid>=2",         # 主键 ULID（时间有序）
]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.6"]
formula-png = ["matplotlib>=3.9"]   # OMML 失败时公式转 PNG 的可选降级
```
安装后置步骤（写进 start.ps1 与 README）：`playwright install chromium`（兜底）；实际优先 `channel="chrome"` 用本机 Chrome。

---

## 2. 数据模型（SQLite DDL，migrations/001_init.sql）

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE settings (            -- 单行 key-value，value 为 JSON
  key TEXT PRIMARY KEY,            -- 'llm' | 'embedding' | 'general'
  value_json TEXT NOT NULL,        -- llm: {base_url, api_key, model, temperature, max_output_tokens, context_window}
                                   -- embedding: {base_url, api_key, model, dim}
                                   -- general: {output_dir, browser_channel:'chrome'|'msedge', pdf_engine:'auto'|'word'|'soffice'|'pillow', language:'zh'}
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE cases (
  id TEXT PRIMARY KEY,             -- ULID
  module TEXT NOT NULL CHECK (module IN ('disclosure','paper2patent','reader','oa')),
  title TEXT NOT NULL DEFAULT '未命名案件',
  patent_type TEXT CHECK (patent_type IN ('invention','utility_model','design')),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','running','waiting_user','completed','failed','archived')),
  current_step TEXT,
  state_json TEXT NOT NULL DEFAULT '{}',  -- 流水线工作内存
  contact_json TEXT,               -- {name,phone,email} 文头联系人（可占位）
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_cases_module ON cases(module, status, updated_at DESC);

CREATE TABLE messages (            -- 会话流（也是 SSE 重放源）
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,            -- 案件内单调递增，作 SSE Last-Event-ID
  role TEXT NOT NULL CHECK (role IN ('user','assistant','system','event')),
  step_key TEXT,
  content TEXT NOT NULL,
  meta_json TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_messages_seq ON messages(case_id, seq);

CREATE TABLE files (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('upload','converted_md','extracted_figure','manual')),
  orig_name TEXT NOT NULL, mime TEXT, size INTEGER,
  stored_path TEXT NOT NULL,
  md_path TEXT,
  meta_json TEXT,                  -- {digest, pages, figure_captions[], convert_error}
  created_at TEXT NOT NULL
);

CREATE TABLE pipeline_runs (       -- 每步每次执行一行（重试/迭代产生新行）→ 可恢复性
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  run_group TEXT NOT NULL,         -- 'initial' | 'iteration-<n>' | 'retry'
  step_key TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('pending','running','waiting_user','done','failed','skipped','cancelled')),
  input_json TEXT, output_json TEXT,
  user_input_json TEXT,
  error TEXT,
  started_at TEXT, finished_at TEXT
);
CREATE INDEX idx_runs_case ON pipeline_runs(case_id, run_group, step_key);

CREATE TABLE artifacts (           -- 版本化交付物：只增不改，禁止覆盖
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN (
    'disclosure_md','disclosure_docx','disclosure_pdf',
    'patent_content_json','patent_docx','patent_pdf',
    'figure_svg','figure_png','mermaid_png',
    'reader_note_md','reader_note_docx',
    'oa_response_md','oa_response_docx',
    'revision_log_md','search_report_json')),
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  run_group TEXT,
  iteration_type TEXT CHECK (iteration_type IN ('initial','merge','correction','rebuild','export')),
  summary TEXT,                    -- "## 合并摘要（留档）" / "## 纠正摘要（留档）" 正文
  source_artifact_id TEXT REFERENCES artifacts(id),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_artifacts_ver ON artifacts(case_id, kind, version);

CREATE TABLE search_queries (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('cnipa','manual','fallback_web')),
  patent_type TEXT, terms_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','done','failed','manual_pending')),
  raw_json TEXT, error TEXT, created_at TEXT NOT NULL
);
CREATE TABLE search_hits (
  id TEXT PRIMARY KEY,
  query_id TEXT REFERENCES search_queries(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  pub_no TEXT, title TEXT, abstract TEXT, applicant TEXT, pub_date TEXT,
  url TEXT NOT NULL,               -- 国知局条目照抄 link 字段（硬规则）
  selected INTEGER NOT NULL DEFAULT 1,
  manual_entry INTEGER NOT NULL DEFAULT 0,
  digest TEXT,                     -- LLM 消化改写后的摘要
  created_at TEXT NOT NULL
);

CREATE TABLE oa_library (          -- OA 历史案例库（RAG 语料）
  id TEXT PRIMARY KEY,
  case_note_md TEXT NOT NULL,
  frontmatter_json TEXT NOT NULL,  -- {case_id,status,patent_type,statutes[],defect_types[],domain,notice_kind,outcome,strategy,compare_refs[],redacted,tags[]}
  status TEXT NOT NULL CHECK (status IN ('draft','confirmed')),  -- 人审闸门：confirmed 才向量化
  embedded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE oa_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id TEXT NOT NULL REFERENCES oa_library(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL, text TEXT NOT NULL
);
-- 002_oa_vec.sql（embedding 配置确定 dim 后动态建；换模型时 DROP 重建 + rebuild）
-- CREATE VIRTUAL TABLE oa_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{dim}]);

CREATE TABLE llm_calls (
  id TEXT PRIMARY KEY, case_id TEXT, step_key TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, duration_ms INTEGER,
  status TEXT, error TEXT, created_at TEXT NOT NULL
);
```

要点：
- `cases.state_json` 是流水线"工作内存"，每步 done 时把 `output_json` 合并进去；恢复 = 读 state_json + pipeline_runs 找第一个非 done 步骤重跑。
- 版本化铁律落在 `artifacts`：迭代永远 INSERT 新行新时间戳文件。
- API key 明文存 settings（本地单用户；README 声明；GET 返回掩码 `sk-***` 尾4位，前端提交空值表示不修改）。

---

## 3. API Surface（全部挂 `/api/v1`）

### 3.1 设置
| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/settings/llm` | base_url/api_key/model/temperature/max_output_tokens/context_window |
| POST | `/settings/llm/test` | 用当前(或请求体临时)配置发一次 1-token chat，返回 {ok, model, latency_ms, error} |
| GET/PUT | `/settings/embedding` | base_url/api_key/model/dim；PUT 且 dim 变化时提示需 rebuild |
| POST | `/settings/embedding/test` | embed("测试") 返回 {ok, dim} |
| GET/PUT | `/settings/general` | output_dir/browser_channel/pdf_engine/language |

### 3.2 案件与消息
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/cases?module=&status=&q=&limit=&offset=` | 侧栏列表 |
| POST | `/cases` | `{module, title?}` → 建案（draft） |
| GET | `/cases/{id}` | 案件 + state + steps 状态 + artifacts 最新版 |
| PATCH | `/cases/{id}` | 改名/归档/联系人 |
| DELETE | `/cases/{id}` | 级联删（`?purge_files=true` 可选） |
| GET | `/cases/{id}/messages?after_seq=` | 会话历史（SSE 重放兜底） |

### 3.3 文件
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases/{id}/files` | multipart 多文件。服务端同步转换 + 返回 md 预览前 2KB 与 convert_error |
| GET | `/files/{id}/download` / `/files/{id}/content` | 原件下载 / 转换 md 全文 |
| DELETE | `/files/{id}` | |

### 3.4 流水线（四模块共用引擎）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases/{id}/pipeline/start` | body 为模块初始载荷。启动后台 asyncio 任务；409 if running |
| GET | `/cases/{id}/pipeline/state` | `{run_group, steps:[{key,name_zh,status,attempt,error}], pending_interaction | null}` |
| POST | `/cases/{id}/pipeline/input` | `{step_key, payload}` — 回答门控 |
| POST | `/cases/{id}/pipeline/resume` | 服务重启后续跑 |
| POST | `/cases/{id}/pipeline/retry` | `{step_key?}` 失败步重试 |
| POST | `/cases/{id}/pipeline/cancel` | 取消当前任务 |
| GET | `/cases/{id}/events` | **SSE**。事件：step_status / llm_delta{step_key,channel:'chat'|'doc',text} / llm_done / doc_version / interaction_required{step_key,kind,schema,prompt} / search_progress / artifact_created / case_title / log / error{retryable} / pipeline_done / ping。`id:` = messages.seq，Last-Event-ID 重放 |

### 3.5 交底书专属
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases/{id}/disclosure/iterate` | `{mode:'merge'|'correct'|'auto', instruction, file_ids[]}` |
| POST | `/cases/{id}/search/cnipa` | `{terms[], patent_type}` 触发爬虫任务 |
| GET | `/cases/{id}/search/hits` | 命中列表 |
| POST | `/cases/{id}/search/hits` | 人工兜底录入 |
| PATCH | `/search/hits/{id}` | `{selected}` |
| POST | `/cases/{id}/search/skip` | 明确跳过查新 |

### 3.6 论文转专利专属
| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/cases/{id}/p2p/content` | 读/改 patent_content JSON（PUT 触发校验+lint，产新版本） |
| POST | `/cases/{id}/p2p/drawings` | 重跑附图脚本 |
| POST | `/cases/{id}/p2p/build` | JSON→docx→pdf |
| GET | `/cases/{id}/p2p/image-prompts` | 每图 Image2 精修 Prompt |

### 3.7 OA 案例库
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/oa/library?tag=&defect_type=&q=` | 列表 |
| POST | `/oa/library/ingest` | 上传案例 PDF/md → 草稿 |
| GET/PUT | `/oa/library/{id}` | 人审；PUT `{status:'confirmed'}` → 分块+嵌入 |
| DELETE | `/oa/library/{id}` | 连带删 chunks + vec |
| POST | `/oa/library/rebuild` | 全量重建（后台任务） |
| GET | `/oa/library/search?q=&k=5` | 向量检索（返回 retrieval_mode） |

### 3.8 渲染 / 工件 / 系统
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/render/mermaid` | `{code, theme?}` → PNG |
| GET | `/cases/{id}/artifacts?kind=` | 全部版本 |
| GET | `/artifacts/{id}/download` / `/artifacts/{id}/content` | 下载 / 文本预览 |
| POST | `/artifacts/{id}/export` | `{format:'docx'|'pdf'}` |
| GET | `/system/health` / `/system/env` | 探测报告 |

静态：`app.mount("/", StaticFiles(directory="frontend/dist", html=True))` + SPA fallback middleware（`/api` 前缀除外）。

---

## 4. 流水线引擎与各模块步骤

（详见 docs/design/prompt-porting-spec.md 与批准计划第四节——两文档合并为准。引擎：StepDef{key,name_zh,handler,gate,retryable}；门控 prepare()/consume(payload) 两阶段；LLM 网络错重试 1 次退避 2s/8s；结构化解析失败带 ValidationError 重试 1 次；startup 把 running→failed(interrupted)；resume 从第一个非 done 步骤重建。）

### 模块① 交底书 8 步：intake(form) → material_scan(条件confirm) → points_mining(select) → [schema_fill(review) 仅实用/外观] → prior_art_search(select/失败form) → preview(confirm) → build(发明分4次调用/实用外观各2次; case_brief 紧凑上下文) → self_check(静默补丁) → deliver(confirm 可跳过; claim_bias)
### 迭代：route → plan → rewrite(仅受影响章节) → sync → deliver(新时间戳+修订记录+摘要留档)
### 模块② p2p：input_check → extraction(source_map) → draft(4次调用) → rules_check(claims_lint+忠实性审计+quality AUDIT→确定性装配JSON) → content_review(review) → drawings → build
### 模块③ reader：acquire → claim_tree(review) → claim_deltas → note(3次调用11节) → lint_deliver
### 模块④ oa：notice_extract → retrieve(select, 明示retrieval_mode) → strategy(form) → draft(逐defect流式) → review_deliver(confirm)

---

## 5. 文档生成管线（汇总）

```
交底书:  分章md草稿 → [服务端 mermaid_render.py(Playwright+vendored mermaid.min.js)→PNG]
        → md_to_docx.py(LaTeX 经 latex2mathml 转可编辑 OMML)
        → export_pdf: docx2pdf(Word COM; pythoncom.CoInitialize STA线程+asyncio.Lock串行)
                      ↘ soffice(若探测到) ↘ 仅docx交付+警告
论文转专利: PatentContent JSON → generate_patent_drawings.py(SVG/PNG三版式)
        → generate_patent_docx.py(A4, --require-drawings)
        → export_pdf 同上; 最后兜底 = export_patent_pdf.py 的 Pillow 图片版PDF(--content-json)
解读/OA: md → md_to_docx.py → export_pdf 同上
```
- 文件命名统一走 `services/artifacts.py`：`{规范化案件名}_{YYYYMMDDHHmmss}.{ext}`（≤80字符），落 `data/outputs/{case_id}/`。

---

## 6. 风险与缓解（执行须知）

1. **CNIPA WAF**：必须 `channel="chrome"` 非 headless 复用本机 Chrome；超时 180s；任何异常都走人工兜底门控，绝不阻塞流水线。
2. **Word COM**：docx2pdf 必须在专用线程 `pythoncom.CoInitialize()`，全局 asyncio.Lock 串行；`DisplayAlerts=False`；失败不致命（降级只交 docx）。
3. **结构化输出**：不是所有 OpenAI 兼容端点支持 json_mode → "提示词强约束 + 提取首个 JSON 块 + pydantic 校验 + 带 ValidationError 重试1次"通用策略。
4. **sqlite-vec on Windows/Py3.13**：加载失败自动降级暴力余弦并在 API 返回 `retrieval_mode` 明示。
5. **许可**：NOTICE.md 注明两仓库来源；适配 prompt 文件头保留出处注释。
