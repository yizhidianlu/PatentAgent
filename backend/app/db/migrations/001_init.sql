-- 001_init.sql — 引途医疗专利智能体 初始 DDL（backend-architecture.md §2）
PRAGMA journal_mode=WAL;

-- schema_version 由迁移执行器负责创建与写入；此处仅保证存在
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

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
