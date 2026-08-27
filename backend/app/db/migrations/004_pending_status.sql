-- 004: 用户状态增加 pending（自助注册后等待管理员审核）
--
-- SQLite 改不了 CHECK 约束，只能重建表。步骤按官方推荐的 12 步做法精简：
-- 建新表 → 复制数据 → 删旧表 → 改名 → 重建索引。
--
-- 为什么单独一态而不是复用 disabled：两者的运维含义不同。
-- disabled 是「管理员停用了它」，pending 是「还没人看过它」——后台要能把
-- 待审的挑出来，也不该让停用过的账号混进待审列表。

CREATE TABLE users_new (
  id                   TEXT PRIMARY KEY,                 -- ULID
  username             TEXT NOT NULL,                    -- 登录名（小写存，小写不敏感）
  display_name         TEXT NOT NULL DEFAULT '',
  password_hash        TEXT NOT NULL,                    -- argon2id
  role                 TEXT NOT NULL DEFAULT 'user'   CHECK (role   IN ('admin','user')),
  status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','pending')),
  must_change_password INTEGER NOT NULL DEFAULT 0,       -- 管理员建号/重置密码后置 1
  failed_logins        INTEGER NOT NULL DEFAULT 0,       -- 连续失败次数（成功登录清零）
  locked_until         TEXT,                             -- 锁定制解除的绝对时间
  last_login_at        TEXT,
  quota_json           TEXT NOT NULL DEFAULT '{}',       -- {daily_llm_calls,monthly_tokens,storage_mb,max_cases}
  created_by           TEXT REFERENCES users(id),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

INSERT INTO users_new
SELECT id, username, display_name, password_hash, role, status,
       must_change_password, failed_logins, locked_until, last_login_at,
       quota_json, created_by, created_at, updated_at
FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

CREATE UNIQUE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_role ON users(role, status);
