-- 003_auth.sql —— 账号系统（M8）
--
-- 从「本地单用户无认证」升级为「多用户服务端部署」：
--   * users / sessions：账号与会话；
--   * audit_log：审计（管理员可跨用户读，必须可追责）；
--   * usage_counters：按用户的 LLM 用量配额计数；
--   * cases 加属主 user_id —— 其余业务表（files/artifacts/messages/pipeline_runs/
--     search_*）一律通过 case_id 关联到 cases 做隔离，不重复存 user_id，
--     少一处遗漏就少一个越权面。
--
-- 注：设计上「存量数据清空重来」，故 cases.user_id 直接建为 NOT NULL；
--     升级请重建数据库（首次启动会自动创建管理员）。

-- ---------------------------------------------------------------------------
-- 用户
-- ---------------------------------------------------------------------------
CREATE TABLE users (
  id                   TEXT PRIMARY KEY,                 -- ULID
  username             TEXT NOT NULL,                    -- 登录名（存小写，大小写不敏感）
  display_name         TEXT NOT NULL DEFAULT '',
  password_hash        TEXT NOT NULL,                    -- argon2id
  role                 TEXT NOT NULL DEFAULT 'user'   CHECK (role   IN ('admin','user')),
  status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  must_change_password INTEGER NOT NULL DEFAULT 0,       -- 管理员建号/重置密码后置 1
  failed_logins        INTEGER NOT NULL DEFAULT 0,       -- 连续失败次数（成功登录清零）
  locked_until         TEXT,                             -- 暴力破解锁定到期时刻
  last_login_at        TEXT,
  quota_json           TEXT NOT NULL DEFAULT '{}',       -- {daily_llm_calls,monthly_tokens,storage_mb,max_cases}
  created_by           TEXT REFERENCES users(id),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_role ON users(role, status);

-- ---------------------------------------------------------------------------
-- 会话（服务端 session，便于管理员禁用账号后立即失效）
-- ---------------------------------------------------------------------------
CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,                          -- 随机 32 字节 base64url，即 Cookie 值
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token   TEXT NOT NULL,
  ip           TEXT,
  user_agent   TEXT,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions(user_id, expires_at);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- ---------------------------------------------------------------------------
-- 审计日志
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id     TEXT REFERENCES users(id) ON DELETE SET NULL,
  actor_name   TEXT,                    -- 冗余存名字，删号后日志仍可读
  action       TEXT NOT NULL,           -- login/login_failed/logout/user_create/user_update/
                                        -- user_delete/password_reset/password_change/
                                        -- cross_user_read/settings_update/case_delete
  target_type  TEXT,                    -- user/case/artifact/settings
  target_id    TEXT,
  target_owner TEXT,                    -- 被访问资源的属主；与 actor_id 不同即跨用户访问
  detail_json  TEXT,
  ip           TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_audit_actor  ON audit_log(actor_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_log(action, created_at DESC);
CREATE INDEX idx_audit_target ON audit_log(target_type, target_id);

-- ---------------------------------------------------------------------------
-- 用量计数（period 为 'YYYY-MM-DD' 或 'YYYY-MM'）
-- ---------------------------------------------------------------------------
CREATE TABLE usage_counters (
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  period       TEXT NOT NULL,
  llm_calls    INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (user_id, period)
);

-- ---------------------------------------------------------------------------
-- 既有表加属主
-- ---------------------------------------------------------------------------
ALTER TABLE cases ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_cases_user ON cases(user_id, module, updated_at DESC);

-- 案例库：加属主与可见性（管理员可把优质案例标为 shared 供全员检索）
ALTER TABLE oa_library ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE oa_library ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';
CREATE INDEX idx_oa_library_user ON oa_library(user_id, status);
