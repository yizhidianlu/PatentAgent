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

**备份**（建议每日）：用 `deploy\backup.ps1`，它会自动定位 `DATA_DIR`（与更新脚本同一套判据），
数据库走 SQLite 的 backup API 热备，服务不必停：

```powershell
.\deploy\backup.ps1 -Destination D:\backup\PatentAgent

# 只备份数据库，跳过可能很大的 uploads/ 与 outputs/
.\deploy\backup.ps1 -Destination D:\backup\PatentAgent -SkipMedia
```

挂进计划任务即可每日自动备份。数据库备份默认保留最近 30 份（`-Keep` 可调）。

> 别用 `os.environ['DATA_DIR']` 取数据目录——`DATA_DIR` 是 pydantic-settings 从
> `backend\.env` 读进配置**字段**的，不会回写进程环境变量，那样只会拿到 `KeyError`。

### 6.1 已删除文件的保护窗口

媒体侧（`uploads/`、`outputs/`）用 robocopy `/MIR` 镜像，源里删掉的文件本来会在
下一次备份时被一并删掉——那等于**备份对删除毫无保护**。现在这类文件会先移入
`_deleted\<时间戳>\`，默认保留 90 天（`-KeepDeletedDays` 可调）后才真正丢弃。

所以误删一个案件、或删账号时勾了「同时删除磁盘文件」，在这个窗口内都还能取回：
到备份目录的 `_deleted\` 里按原相对路径找。

> 删账号时的「同时删除磁盘文件」开关**默认是关的**。磁盘上的原始材料与交付物
> 不可再生，而删账号是常见操作；两件事不该绑在一起默认发生。

### 6.2 恢复

用 `deploy\restore.ps1`，**默认是演练模式**：只报告将要做什么，不动任何文件。

```powershell
# 1) 先演练：看清会恢复哪一份快照、多少个文件
.\deploy\restore.ps1 -Source D:\backup\PatentAgent

# 2) 确认无误后真正执行（会覆盖目标数据目录）
.\deploy\stop-serving.ps1                     # 必须先停应用与看门狗
.\deploy\restore.ps1 -Source D:\backup\PatentAgent -Apply
.\start.ps1                                   # 起回来
```

脚本会做三件人容易漏掉的事：

1. **拒绝在应用还跑着的时候恢复**——那样等于把库从它脚下抽走，恢复出来的东西
   既不是旧的也不是新的；
2. **先验快照再动目标**（`integrity_check` + 核心表存在性）。恢复一份坏库比不恢复更糟：
   应用能起来、页面能开，坏在哪要等用到才知道；
3. **清掉 `-wal` / `-shm` 边车**。留着旧 WAL 会把刚恢复的主库又盖回去，而且是静默的。

**恢复到与原来不同的目录是受支持的**（换机、换盘、并行验证都算）：库里存的是相对
`DATA_DIR` 的路径，应用首次启动还会把遗留的绝对路径一并归一（幂等，日志里有条数）。

### 6.3 恢复后必须逐项验证的四件事

路径类故障**恢复后不会报错，只会静默地打不开**——数据库好好的、案件列表好好的、
正文好好的，只有点下去才发现不对。所以这四项一个都不能省：

| 验什么 | 怎么验 |
|---|---|
| 下载 | 打开任一案件，下载一份交付物（docx/pdf） |
| 文本预览 | 右侧文档面板能显示正文（不是空白） |
| 正文插图 | 交底书正文里的插图能显示（不是裂图） |
| 说明书附图 | 论文转专利案件的「说明书附图」一节有图 |

### 6.4 定期做一次恢复演练

**恢复是整条链上唯一只在出事时才执行的环节，也就是唯一平时不会被验证的环节。**
建议每季度演练一次，且**不必停生产**：

```powershell
# 恢复到一个全新目录，用另一个端口起一个临时实例
.\deploy\restore.ps1 -Source D:\backup\PatentAgent -DataDir D:\PatentAgentRestoreTest -Apply
# 用一份指向该目录的 .env 起临时实例（不要给它起看门狗、不要开隧道），验完直接关掉
```

演练时**务必恢复到与生产不同的目录**。只换机器不换路径会让路径类问题完全隐身：
旧的绝对路径在那种情况下照样有效，于是什么都测不出来。

#### 光看「四项都返回 200」不算数

在**同一台机器**上演练时，源目录还在，库里那条旧的绝对路径依然指向一个真实存在
的文件。也就是说：**即使恢复完全失败，那几项也可能照样绿**——因为它们读到的是
源目录里的旧文件。

平台内部按「当前数据目录优先」解析，不会去读源目录（有回归测试盯着）。但演练的
意义就在于不轻信这句话，所以要有一个能证伪的判据。**用内容标记，不要用改名**：

```powershell
# 在恢复副本里给某个文件做个标记（只动副本，绝不碰生产）
Add-Content -Path D:\PatentAgentRestoreTest\outputs\<case>\<正文>.md -Value "`n<!-- DRILL-MARK -->"
# 再通过临时实例的接口取这份正文：返回里必须出现 DRILL-MARK
```

图片同理：给副本里的 PNG 追加几百字节填充，让两份**字节数不同**，然后用一条
**指向源目录的绝对路径**去请求 `GET /cases/<id>/media`——返回的字节数必须等于
**副本**那份。这一条直接打在「绝对路径会不会穿回源目录」上，是整个演练里判别力
最强的一步。

> 为什么不用「把源目录改名再验一遍」：那在生产机上做不了——源目录就是线上的
> `data\`，SQLite 句柄开着，改名要么失败要么弄坏线上。
> **改名靠的是「让旧路径读不到」，标记靠的是「让两份内容可区分」**；
> 后者不动源目录，所以是唯一能在真机上执行的做法。

#### 演练后必须清理

恢复出来的目录里有一份**含明文 API Key 的库副本**，验完立刻删干净，不要留过夜。

---

## 7. 运维要点

- **日志**：uvicorn 输出到控制台，任务计划程序可重定向到文件；建议按日轮转。
- **升级**：`git pull` → `.\start.ps1 -SetupOnly -Rebuild` → 重启任务。**升级前务必备份 `DATA_DIR`**。
  部署端用 `deploy\update.ps1` 时会自动备份，命名为 `pre-<更新前的 commit>-<时间戳>.db`——
  名字里带 commit 是为了真要回滚时不必靠日志去对时间。
- **回滚**：**会写数据库的更新，回滚必须连数据库一起回**，只回代码会让新库配旧代码。
  含数据库迁移的更新，`update.ps1` 会另外固定一份 `keep-*.db`，永不自动清理。
  `pre-*.db` 按天保留（默认 30 天，且无论多旧至少留最近 10 份）——按份数保留在
  一天推数次的节奏下会把上周那个唯一正确的回退点挤掉，而那种更新恰恰最需要回退点。
- **忘记管理员密码**：无找回入口（设计如此）。可用 `backend/.venv` 的 Python 直连 `app.db`，调用 `app.services.auth.set_password(user_id, 新密码)` 重置。
- **并发**：1–10 人规模下 SQLite（WAL）与单进程 asyncio 完全够用。Word COM 由全局锁串行，多人同时导出 PDF 会排队但不会出错。若将来超过这个规模，再考虑 PostgreSQL + 多实例。
- **磁盘**：交付物只增不改（版本永不覆盖），会持续增长。定期检查 `DATA_DIR` 占用，可按案件归档旧数据。

---

## 8. 安全提醒

- `DATA_DIR/app.db` 存有**平台 LLM API Key 与全部案件内容**，属最高敏感级别，严格限制文件系统权限与备份介质的访问。
- 管理员可查看全部用户案件（产品决策），每次访问都写审计日志。请让团队知晓这一点，并定期检查「管理后台 → 审计日志」。
- 用户密码用 argon2id 哈希存储，即使拖库也无法直接还原；但**API Key 是明文**，这是为了能直接调用第三方服务，无法避免。
- 平台不向除你所配置的 LLM 服务、国知局网站、专利公开数据源之外的任何地方发送数据。
