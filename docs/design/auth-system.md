# 引途医疗专利智能体 — 账号系统与服务端部署设计（M8）

> 2026-08-26。本文档定义从「本地单用户无认证」升级为「多用户服务端部署」的架构改动。

## 0. 用户已确认的决策

| 决策点 | 选择 | 架构含义 |
|---|---|---|
| LLM API Key | **管理员统一配置** | Key 属平台级设置，普通用户完全不可见、不可改；成本由平台承担，需按用户配额管控 |
| 账号创建 | **仅管理员创建** | 无公开注册入口、无邮箱验证流程；管理员建号并设初始密码，用户首登强制改密 |
| 数据可见性 | **管理员可查看全部案件** | 管理员拥有跨用户读权限；**必须配套审计日志**，否则无法追责 |
| 存量数据 | **清空重来** | 无需写数据迁移脚本；升级时重建库，首启创建管理员 |

> **实现比该决策更宽容一档**：迁移 003 给 `cases`/`oa_library` 加 `user_id` 时默认空串，
> 首次以新代码启动、创建 bootstrap 管理员之后，会把这些 `user_id=''` 的孤儿行**认领给该管理员**。
> 否则存量案件会变成谁都改不了的幽灵数据（管理员看得见，但 `resolve_case` 判定为"他人案件"而只读）。
> 因此直接在旧库上升级也能用，不必真的清库。

## 1. 威胁模型（部署到公网后新增的风险）

本地单用户时不存在、上服务器后必须处理的：

| 风险 | 处置 |
|---|---|
| 任何人可访问接口 | 全局认证中间件，白名单仅登录/健康检查/静态资源 |
| 用户 A 读取用户 B 的案件/文件/交付物 | **所有数据查询强制按 user_id 过滤**（见 §4 隔离策略） |
| 密码被拖库后明文泄露 | argon2id 哈希（内存硬，抗 GPU 爆破） |
| 会话 Cookie 被 XSS 窃取 | httpOnly + Secure + SameSite=Lax |
| CSRF 伪造请求 | SameSite=Lax 兜底 + 双提交 CSRF token（非 GET 强制校验） |
| 暴力破解密码 | 登录失败计数 + 指数锁定（同账号/同 IP 双维度） |
| 平台 API Key 被普通用户套取 | Key 仅存服务端，任何接口都不回传明文；普通用户的设置页不含 LLM 配置 |
| 单用户跑满 LLM 配额 | 按用户的调用/token 配额与速率限制 |
| 上传文件路径穿越 / 恶意文件 | 已有 sanitize（4 号 tester 已加固），再加每用户存储配额 |
| 管理员滥用跨用户读权限 | 审计日志记录每次跨用户访问（谁、何时、看了谁的什么） |

## 2. 数据模型

### 2.1 新增表

```sql
CREATE TABLE users (
  id            TEXT PRIMARY KEY,              -- ULID
  username      TEXT NOT NULL UNIQUE,          -- 登录名（大小写不敏感，存小写）
  display_name  TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,                 -- argon2id
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  must_change_password INTEGER NOT NULL DEFAULT 0,   -- 管理员建号/重置后置 1
  failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until  TEXT,                          -- 暴力破解锁定到期时刻
  last_login_at TEXT,
  quota_json    TEXT NOT NULL DEFAULT '{}',    -- {daily_llm_calls, monthly_tokens, storage_mb, max_cases}
  created_by    TEXT REFERENCES users(id),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_username ON users(username);

CREATE TABLE sessions (
  id            TEXT PRIMARY KEY,              -- 会话 ID（随机 32 字节 base64url），即 Cookie 值
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token    TEXT NOT NULL,
  ip            TEXT,
  user_agent    TEXT,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  expires_at    TEXT NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions(user_id, expires_at);

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id    TEXT REFERENCES users(id),
  action      TEXT NOT NULL,      -- login / login_failed / logout / user_create / user_disable /
                                  -- password_reset / cross_user_read / settings_update / case_delete
  target_type TEXT,               -- user / case / artifact / settings
  target_id   TEXT,
  target_owner TEXT,              -- 被访问资源的属主（跨用户访问时与 actor_id 不同）
  detail_json TEXT,
  ip          TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_audit_actor ON audit_log(actor_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_log(action, created_at DESC);

CREATE TABLE usage_counters (          -- 配额计数（按天/月滚动）
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  period      TEXT NOT NULL,          -- '2026-08-26' 或 '2026-08'
  llm_calls   INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, period)
);
```

### 2.2 既有表加属主

`cases` 加 `user_id TEXT NOT NULL REFERENCES users(id)`，并建 `idx_cases_user ON cases(user_id, module, updated_at DESC)`。

`files` / `artifacts` / `messages` / `pipeline_runs` / `search_*` 全部通过 `case_id` 关联到 `cases`，**不再重复存 user_id**，隔离统一在 case 层做（少一处遗漏风险）。

`oa_library`（审查答复案例库）加 `user_id` + `visibility TEXT DEFAULT 'private' CHECK(visibility IN ('private','shared'))`——案例库有跨用户复用价值，允许管理员把优质案例标为 shared 供全员检索。

`settings` 表语义变更：`llm` / `embedding` / `image_gen` / `general` 四个 key 成为**平台级**配置，仅管理员可读写。

## 3. 认证方案

**选型：服务端 Session + httpOnly Cookie**（不用 JWT）。理由：管理员禁用账号需**立即**生效，JWT 无法即时撤销；SPA 无需在 JS 里保管 token，从根上避免 XSS 窃取；本平台是有状态的长流程应用，本就依赖服务端状态。

- **密码哈希**：argon2id（`argon2-cffi`），参数 time_cost=3 / memory_cost=64MB / parallelism=4。
- **Cookie**：名 `pa_session`，httpOnly、SameSite=Lax、`Secure`（由 `COOKIE_SECURE` 配置，生产必开）、有效期 7 天滑动续期（每次请求刷新 `last_seen_at`，超过 30 天绝对过期）。
- **CSRF**：登录时下发 `csrf_token`（同时写入非 httpOnly 的 `pa_csrf` Cookie），前端对所有非 GET 请求带 `X-CSRF-Token` 头，服务端比对 session 内的值。
- **登录限流**：同用户名连续失败 5 次锁 5 分钟、10 次锁 1 小时；同 IP 每分钟最多 10 次登录尝试。失败响应统一为「用户名或密码错误」，不泄露账号是否存在。

### 首次启动引导

数据库无任何用户时，启动流程：
1. 从环境变量 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 读取；
2. 未提供则生成随机强密码，**在控制台醒目打印一次**（仅此一次），用户名默认 `admin`；
3. 该管理员 `must_change_password=1`，首次登录强制改密后才能使用其它功能。

## 4. 授权与数据隔离

### 4.1 三层防线

1. **中间件层**：全局要求已认证（白名单：`/api/v1/auth/login`、`/api/v1/system/health`、静态资源与 SPA fallback）。
2. **依赖注入层**：`current_user()` / `require_admin()` 两个 FastAPI 依赖；管理员专属路由挂 `require_admin`。
3. **数据访问层（最关键）**：所有按 case 取数的入口统一走 `resolve_case(case_id, user)` —— 普通用户命中他人案件一律返回 **404**（不是 403，避免探测他人 case_id 是否存在）；管理员放行但**写审计日志**。

> 实施纪律：禁止在业务代码里直接 `SELECT * FROM cases WHERE id=?`。统一经 `resolve_case()`，并在 CI 加一条 grep 检查防止回退。

### 4.2 管理员跨用户读

管理员访问他人案件时：正常返回数据 + `audit_log` 记一条 `cross_user_read`；前端在案件页顶部显示醒目提示条「你正在以管理员身份查看 {用户} 的案件，此次访问已记录」。管理员**不可**修改他人案件（只读），删除需二次确认并记审计。

### 4.3 平台设置

`GET/PUT /settings/{llm,embedding,image_gen}` 挂 `require_admin`。普通用户的设置页只保留「外观」与「修改密码」，前端按 `role` 渲染，后端同时兜底鉴权（不能只靠前端隐藏）。

## 5. API 变更

### 5.1 新增

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/auth/login` | 公开 | `{username,password}` → 下发 session cookie + csrf；被锁定/禁用返回明确原因 |
| POST | `/auth/logout` | 登录 | 销毁当前会话 |
| GET | `/auth/me` | 登录 | 当前用户信息（role / must_change_password / quota / usage） |
| POST | `/auth/change-password` | 登录 | `{old,new}`；改密后**吊销该用户其它所有会话** |
| GET | `/admin/users` | 管理员 | 列表（过滤 role/status/关键词，含用量摘要） |
| POST | `/admin/users` | 管理员 | 建号 `{username,display_name,role,password?,quota?}`；未给密码则生成随机密码并**仅此一次**返回 |
| PATCH | `/admin/users/{id}` | 管理员 | 改 display_name/role/status/quota |
| POST | `/admin/users/{id}/reset-password` | 管理员 | 重置并返回新密码（一次性），置 must_change_password |
| DELETE | `/admin/users/{id}` | 管理员 | 删除用户及其全部数据（二次确认；禁止删最后一个管理员、禁止自删） |
| GET | `/admin/users/{id}/cases` | 管理员 | 某用户的案件列表 |
| GET | `/admin/audit` | 管理员 | 审计日志（按 actor/action/时间过滤） |
| GET | `/admin/stats` | 管理员 | 平台总览：用户数、案件数、LLM 调用与 token 用量、存储占用 |

### 5.2 既有端点

全部要求登录；`/cases` 系列自动按属主过滤；`/settings/{llm,embedding,image_gen}` 收紧为管理员专属。

## 6. 前端变更

- **登录页** `/login`：品牌视觉延续现有设计（渐变 logo + 玻璃卡片），用户名/密码/错误提示/锁定倒计时。
- **强制改密页** `/change-password`：`must_change_password=1` 时任何路由都重定向到此。
- **路由守卫**：`useAuth()` 拉 `/auth/me`；未登录跳登录页；`/admin/*` 需 role=admin。
- **管理后台** `/admin`：用户列表（建号/改角色/启禁用/重置密码/删号/看用量）、审计日志、平台统计。建号与重置密码后弹出**一次性密码展示 Modal**（含复制按钮与"仅显示一次"警告）。
- **头部**：右侧加用户菜单（显示名 / 角色徽章 / 修改密码 / 退出登录）；管理员多一个"管理后台"入口。
- **设置页**：按 role 分岔——普通用户只见「外观」「修改密码」；管理员多见「模型服务」「向量与检索」「图像生成」并标注"平台级设置，对全部用户生效"。
- **跨用户查看提示条**：管理员打开他人案件时置顶 amber 提示。
- **API 层**：所有非 GET 请求自动带 `X-CSRF-Token`；401 统一跳登录页；403 提示权限不足。

## 7. 对现有 521 项测试的影响与对策

加认证后所有 API 测试都会 401。对策：
1. `conftest.py` 提供 `client`（已登录普通用户）与 `admin_client`（已登录管理员）两个 fixture，内部真实走登录流程拿 cookie —— **不引入任何"测试环境跳过认证"的后门**（那种后门一旦误配到生产就是灾难）。
2. 现有测试把 `client` 换成对应 fixture 即可，业务断言不变。
3. 新增 `tests/test_auth.py` 与 `tests/test_admin.py`：登录/登出/改密/锁定/CSRF/会话过期/权限矩阵/**跨用户隔离穿透测试**（用户 A 尝试用 B 的 case_id、file_id、artifact_id 访问全部 60 个端点，断言一律 404）。

## 8. 部署加固

- **配置**：`.env` 或环境变量提供 `SECRET_KEY`、`COOKIE_SECURE=true`、`ADMIN_USERNAME/PASSWORD`、`DATA_DIR`、`ALLOWED_ORIGINS`。
- **HTTPS**：生产必须置于反向代理（Nginx/Caddy）之后；应用信任 `X-Forwarded-Proto` 以正确判定 Secure。
- **CORS**：生产收紧为部署域名白名单（当前硬编码 localhost:5173 仅限开发）。
- **安全响应头**：CSP、X-Content-Type-Options、X-Frame-Options、Referrer-Policy。
- **限流**：登录接口专项限流；LLM 调用按用户配额计数拦截。
- **备份**：`data/` 目录整体备份说明（含 SQLite WAL 一致性备份方法）。
- **Docker**（可选）：Dockerfile + docker-compose（含 Nginx + 自动 HTTPS），但 Word COM 转 PDF 在 Linux 容器内不可用，需降级到 LibreOffice —— 部署文档必须写明这一点。

## 9. 实施顺序

1. **数据与认证内核**：迁移 003_auth.sql、users/sessions 模型、argon2 哈希、session 服务、登录限流、首启建管理员。
2. **鉴权接入**：中间件 + `current_user`/`require_admin` 依赖 + `resolve_case()` 隔离层；改造既有路由；conftest 双 fixture；修既有测试。
3. **管理后台 API**：用户 CRUD、审计、统计、配额计数与拦截。
4. **前端**：登录页、改密页、路由守卫、用户菜单、管理后台、设置页分岔、跨用户提示条。
5. **穿透测试与加固**：权限矩阵测试、隔离穿透测试、CSRF/限流测试、安全头、部署文档。
