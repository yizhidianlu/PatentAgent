# 引途医疗专利智能体 — 服务器部署指南

> 目标环境：**Windows Server**，小团队 1–10 人同时使用。
> 提供两套方案：**直装（推荐）** 与 **Docker**。

---

## 0. 先读这一节：一个必须先做的决定

平台的 PDF 导出优先调用本机 **Microsoft Word**（效果最好，中文排版与公式最忠实）。但把 Word 自动化搬到服务器上有个绕不开的限制：

> **微软官方不支持在无人值守的服务/多用户环境中自动化 Office**
> （参见微软知识库对 Office 服务端自动化的一贯声明）。
> 以「Windows 服务」方式运行时，进程处于 **Session 0 隔离**，Word 可能启动失败、
> 弹不出的对话框会让调用永久挂起，且多用户并发时行为不可预期。

因此在 Windows Server 上你必须三选一：

| 方案 | PDF 效果 | 稳定性 | 适用 |
|---|---|---|---|
| **A. 以登录用户身份运行（推荐）** | ★★★ Word 原生 | 良好 | 小团队、服务器可保持一个登录会话 |
| **B. 装 LibreOffice，禁用 Word** | ★★☆ 接近 | 最稳 | 追求无人值守、不想维护登录会话 |
| **C. 只交付 .docx，不出 PDF** | — | 最稳 | 用户自己另存 PDF 即可 |

本文档默认走 **方案 A**，并在 §5 给出切换到 B/C 的方法。1–10 人规模下，Word COM 由全局锁串行执行，实测单次转换 2–5 秒，排队可接受。

---

## 1. 服务器准备

| 项 | 要求 | 备注 |
|---|---|---|
| 系统 | Windows Server 2019 / 2022 | |
| CPU / 内存 | 2 核 4GB 起（建议 4 核 8GB） | argon2 密码哈希每次约 64MB 内存 |
| 磁盘 | 50GB 起 | 上传件与交付物累积增长 |
| Python | 3.11 – 3.13 | 安装时勾选 *Add python.exe to PATH* |
| Node.js | 18+ | 仅构建前端时需要 |
| Microsoft Word | 方案 A 必需 | 需合法授权；首次手动打开一次，把激活/隐私弹窗全部点掉 |
| Google Chrome | 建议安装 | 国知局联网查新与 Mermaid 渲染 |
| 反向代理 | Nginx / Caddy / IIS | 负责 HTTPS 终止 |

---

## 2. 方案 A：直装部署（推荐）

### 2.1 拉取代码与安装

```powershell
cd C:\Apps
git clone <你的仓库地址> PatentAgent
cd PatentAgent

# 一键完成 venv 创建、依赖安装、前端构建（不启动服务）
.\start.ps1 -SetupOnly
```

### 2.2 生产配置

新建 `backend\.env`：

```ini
# --- 数据与端口 ---
DATA_DIR=D:\PatentAgentData
PORT=8000
LOG_LEVEL=INFO

# --- 账号系统（M8）---
# 生产必须为 true：会话 Cookie 仅经 HTTPS 传输
COOKIE_SECURE=true
# 收紧为实际部署域名（含协议，不带结尾斜杠）
ALLOWED_ORIGINS=["https://patent.yourcompany.com"]
# 首次启动创建的管理员；不填 ADMIN_PASSWORD 则随机生成并在日志打印一次
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<设一个足够强的初始密码>
```

> `DATA_DIR` 建议放数据盘而非系统盘。该目录内含 `app.db`（**存有平台的 LLM API Key 与全部案件**），务必纳入备份并限制访问权限。

### 2.3 以登录用户身份常驻运行

因为要用 Word COM（见 §0），**不要**注册成 Windows 服务，改用「登录时自动启动 + 保持会话」：

1. 为平台建一个专用本地账号（如 `patentsvc`），授予该账号对 `C:\Apps\PatentAgent` 与 `DATA_DIR` 的完全控制权限。
2. 用该账号登录服务器一次，**手动打开一次 Word**，把激活、隐私声明、"文档恢复"等所有弹窗点掉再关闭。这一步不做，第一次 PDF 导出会挂起。
3. 用「任务计划程序」创建任务：
   - 常规：使用 `patentsvc` 账号运行；勾选 **"只在用户登录时运行"**（这点关键，不能选"不管用户是否登录"，否则又回到 Session 0）
   - 触发器：**登录时**
   - 操作：程序 `powershell.exe`，参数
     `-NoProfile -ExecutionPolicy Bypass -File C:\Apps\PatentAgent\start.ps1 -NoBrowser`
   - 设置：勾选"如果任务失败，则重新启动"，间隔 1 分钟、最多 3 次
4. 配置服务器自动登录该账号（`netplwiz` 或注册表 `AutoAdminLogon`），并**用组策略锁定屏幕但不注销**，保证会话常驻。

> 若你的安全策略不允许自动登录，请直接改用 §5 的方案 B（LibreOffice）。

### 2.4 HTTPS 反向代理

以 Nginx 为例（Caddy 更省事，能自动签证书）：

```nginx
server {
    listen 443 ssl http2;
    server_name patent.yourcompany.com;

    ssl_certificate     C:/certs/fullchain.pem;
    ssl_certificate_key C:/certs/privkey.pem;

    # 上传件可能较大（论文 PDF、项目材料）
    client_max_body_size 100m;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        # 应用据此判定 Secure，务必传
        proxy_set_header   X-Forwarded-Proto $scheme;

        # SSE 长连接：关缓冲、给足超时（长流水线单步可能几分钟）
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}

server {
    listen 80;
    server_name patent.yourcompany.com;
    return 301 https://$host$request_uri;
}
```

> **`proxy_buffering off` 不能省**。平台靠 SSE 推送生成过程，开着缓冲会让前端长时间收不到任何内容，看起来就像卡死。

### 2.5 防火墙

只对外开放 443（与 80 跳转）。**8000 端口不要暴露到公网**——应用本身监听 127.0.0.1，仅反向代理可达。

### 2.6 首次启动

```powershell
.\start.ps1 -NoBrowser
```

日志里会打印管理员账号信息（若未在 `.env` 指定密码，初始密码**仅打印这一次**）。用它登录 → 系统强制改密 → 进「设置 → 模型服务」配置平台级 LLM API Key → 「管理后台 → 用户管理」为同事建号。

---

## 3. 方案 B：Docker 部署

Windows 容器内无法可靠运行 Office，因此 **Docker 方案一律走 Linux 容器 + LibreOffice** 出 PDF。若你已选定 Windows Server + Word，请用方案 A；Docker 更适合"不要求 Word 级排版"或后续迁到 Linux 的场景。

`Dockerfile`：

```dockerfile
FROM python:3.12-slim AS frontend
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/*
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci
COPY frontend ./frontend
RUN cd frontend && npm run build

FROM python:3.12-slim
WORKDIR /app
# LibreOffice 出 PDF；chromium 供国知局查新与 mermaid 渲染；中文字体不能少
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
COPY backend ./backend
RUN pip install --no-cache-dir -e ./backend
COPY --from=frontend /app/frontend/dist ./frontend/dist
ENV DATA_DIR=/data PORT=8000 COOKIE_SECURE=true
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
```

`docker-compose.yml`（用 Caddy 自动签发 HTTPS）：

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    environment:
      DATA_DIR: /data
      COOKIE_SECURE: "true"
      ALLOWED_ORIGINS: '["https://patent.yourcompany.com"]'
      ADMIN_USERNAME: admin
      ADMIN_PASSWORD: ${ADMIN_PASSWORD:?请在 .env 中设置}
    volumes:
      - patent-data:/data
    expose: ["8000"]

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    depends_on: [app]

volumes:
  patent-data:
  caddy-data:
```

`Caddyfile`：

```
patent.yourcompany.com {
    reverse_proxy app:8000 {
        flush_interval -1   # SSE 必需：禁用缓冲
    }
    request_body {
        max_size 100MB
    }
}
```

启动：`docker compose up -d`，然后 `docker compose logs app` 查看管理员初始密码。

> 容器内 PDF 走 LibreOffice，中文字体已装 `fonts-noto-cjk`。若排版与 Word 有出入属预期。

---

## 4. 上线检查清单

部署完成后逐条确认：

- [ ] 浏览器访问 `https://域名` 正常，证书有效
- [ ] `http://域名` 自动跳转到 https
- [ ] 8000 端口在公网**不可**直接访问
- [ ] 用管理员登录 → 被强制改密 → 改密成功
- [ ] 「设置 → 模型服务」配好 API Key 并**测试连接通过**
- [ ] 新建一个普通用户，用它登录 → 看不到「模型服务」等平台设置
- [ ] 普通用户 A 建一个案件；用普通用户 B 登录后**看不到**该案件
- [ ] 管理员能看到 A 的案件，且「管理后台 → 审计日志」里有 `cross_user_read` 记录
- [ ] 上传一份 PDF → 跑通一条流水线 → 能下载 docx
- [ ] PDF 导出可用（方案 A 需确认 Word 无弹窗）
- [ ] 生成过程中前端能持续看到流式输出（验证 SSE 未被缓冲）
- [ ] `DATA_DIR` 已配置备份

---

## 5. 切换 PDF 引擎

「设置 → 常规」中的 `pdf_engine`：

- `auto`（默认）：Word → LibreOffice → 报错，逐级降级
- `word`：仅用 Word
- `soffice`：仅用 LibreOffice —— **方案 B（无人值守）请选它**，并在服务器装好 LibreOffice
- 都不可用时平台只交付 `.docx` 并明确提示，`.md` 原稿始终先落盘，不会因导出失败丢内容

---

## 6. 备份与恢复

**备份**（建议每日）：停服务后整体复制 `DATA_DIR`；若要热备，先执行 SQLite 的一致性备份再拷贝其余目录：

```powershell
# 热备 app.db（WAL 模式下不能直接拷文件）
.\backend\.venv\Scripts\python.exe -c "import sqlite3,os; s=sqlite3.connect(os.environ['DATA_DIR']+r'\app.db'); d=sqlite3.connect(r'D:\backup\app.db'); s.backup(d); d.close(); s.close()"
# 再拷贝 uploads/ 与 outputs/
robocopy $env:DATA_DIR\uploads D:\backup\uploads /MIR
robocopy $env:DATA_DIR\outputs D:\backup\outputs /MIR
```

**恢复**：停服务 → 用备份覆盖 `DATA_DIR` → 启动。

---

## 7. 运维要点

- **日志**：uvicorn 输出到控制台，任务计划程序可重定向到文件；建议按日轮转。
- **升级**：`git pull` → `.\start.ps1 -SetupOnly -Rebuild` → 重启任务。**升级前务必备份 `DATA_DIR`**。
- **忘记管理员密码**：无找回入口（设计如此）。可用 `backend/.venv` 的 Python 直连 `app.db`，调用 `app.services.auth.set_password(user_id, 新密码)` 重置。
- **并发**：1–10 人规模下 SQLite（WAL）与单进程 asyncio 完全够用。Word COM 由全局锁串行，多人同时导出 PDF 会排队但不会出错。若将来超过这个规模，再考虑 PostgreSQL + 多实例。
- **磁盘**：交付物只增不改（版本永不覆盖），会持续增长。定期检查 `DATA_DIR` 占用，可按案件归档旧数据。

---

## 8. 安全提醒

- `DATA_DIR/app.db` 存有**平台 LLM API Key 与全部案件内容**，属最高敏感级别，严格限制文件系统权限与备份介质的访问。
- 管理员可查看全部用户案件（产品决策），每次访问都写审计日志。请让团队知晓这一点，并定期检查「管理后台 → 审计日志」。
- 用户密码用 argon2id 哈希存储，即使拖库也无法直接还原；但**API Key 是明文**，这是为了能直接调用第三方服务，无法避免。
- 平台不向除你所配置的 LLM 服务、国知局网站、专利公开数据源之外的任何地方发送数据。
