# 开发阶段部署：本机 + Cloudflare Tunnel

> 适用：拿自己的 Windows 电脑当服务器，通过 Cloudflare Tunnel 让同事从公网访问。
> 无需公网 IP、无需在路由器上开端口、自动 HTTPS。

**这个方案有个额外好处**：本机跑的是交互式登录会话，不受 Windows 服务的 Session 0 限制
（见 [DEPLOYMENT.md §0](DEPLOYMENT.md)），所以 **Word 导出 PDF 可以正常用**，排版效果最好。

---

## 1. 准备一个域名

Cloudflare Tunnel 需要一个托管在 Cloudflare 的域名。已有域名就把 NS 指向 Cloudflare；没有的话
在 Cloudflare 注册一个便宜的（`.xyz` / `.top` 之类年费很低）即可。

假设你的域名是 `example.com`，计划用子域 `patent.example.com`。

---

## 2. 安装 cloudflared

```powershell
winget install --id Cloudflare.cloudflared
# 或下载 https://github.com/cloudflare/cloudflared/releases 的 cloudflared-windows-amd64.exe
cloudflared --version
```

## 3. 创建隧道

```powershell
# 浏览器会打开，登录并授权你的域名
cloudflared tunnel login

# 创建隧道（名字随意）
cloudflared tunnel create patent-agent
# 输出里记下 Tunnel ID，凭据文件在 C:\Users\<你>\.cloudflared\<TunnelID>.json

# 把子域名指向这条隧道（自动建 CNAME 记录）
cloudflared tunnel route dns patent-agent patent.example.com
```

## 4. 隧道配置

新建 `C:\Users\<你>\.cloudflared\config.yml`：

```yaml
tunnel: patent-agent
credentials-file: C:\Users\<你>\.cloudflared\<TunnelID>.json

ingress:
  - hostname: patent.example.com
    service: http://127.0.0.1:8000
    originRequest:
      # ↓ SSE 必需：关闭对源站响应的分块缓冲，否则前端长时间收不到流式内容
      disableChunkedEncoding: false
      # 长流水线单步可能几分钟，给足超时
      connectTimeout: 30s
      tlsTimeout: 30s
      # 不限制响应头等待时间（SSE 首字节可能较慢）
      noHappyEyeballs: false
  - service: http_status:404
```

> `cloudflared` 本身对 SSE 是透明转发、不做缓冲，这点比 Nginx 省心（不需要 `proxy_buffering off`）。
> 我们的 SSE 有 15 秒心跳 ping，能稳定绕开 Cloudflare 边缘的空闲超时。

## 5. 应用侧配置

编辑 `backend\.env`：

```ini
DATA_DIR=D:\PatentAgentData
PORT=8000
LOG_LEVEL=INFO

# Cloudflare 已提供 HTTPS，Cookie 必须只走加密通道
COOKIE_SECURE=true

# 收紧为你的实际域名（前端与后端同源，这里主要防跨站）
ALLOWED_ORIGINS=["https://patent.example.com"]

# 首次启动创建的管理员
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<设一个足够强的初始密码>
```

> 应用通过 `X-Forwarded-Proto` 判断是否 HTTPS，cloudflared 会自动带上这个头，无需额外配置。
> 客户端真实 IP 从 `X-Forwarded-For` 取（审计日志与登录限流用），cloudflared 同样会带。

## 6. 启动

开两个终端（或把 cloudflared 注册成服务，见 §8）：

```powershell
# 终端 1：应用
cd C:\Users\jielu\Desktop\PatentAgent
.\start.ps1 -NoBrowser

# 终端 2：隧道
cloudflared tunnel run patent-agent
```

浏览器打开 `https://patent.example.com` —— 应该看到登录页。
应用日志里有管理员初始密码（若 `.env` 里没指定则随机生成且**只打印一次**）。

---

## 7. 强烈建议：加一层 Cloudflare Access

隧道一开，你的电脑就真的暴露在公网了。平台自带账号密码，但**开发阶段代码尚未经过完整安全审计**，
建议在 Cloudflare 前面再加一道门 —— Cloudflare Access 免费版支持 50 个用户：

1. Cloudflare 控制台 → **Zero Trust** → Access → Applications → **Add an application** → Self-hosted
2. Application domain 填 `patent.example.com`
3. 加一条 Policy：Action = Allow，Include = **Emails** 填你和同事的邮箱（或 Emails ending in `@yourcompany.com`）
4. 保存

之后访问会先要求邮箱验证码（或你配置的 Google/GitHub 登录），通过后才到平台自己的登录页。
**双层认证**，开发阶段这样最稳妥。

> 注意：开了 Access 之后，用 `curl` 之类的工具直接调 API 会被拦。如需程序化访问，
> 在 Access 里为该客户端配一个 Service Token。

---

## 8. 让它常驻运行

### cloudflared 注册为服务（隧道本身无 Session 0 问题）

> **先确认这台机器上没有别的 cloudflared 服务。**
> Windows 上 cloudflared 服务是**全机唯一**的，`service install` 会覆盖已存在的那一个。
> 若这台机器还跑着别的项目的隧道（尤其是 token 托管模式——服务命令行形如
> `tunnel run --token ...`、本地没有 `~\.cloudflared` 目录），执行下面这条会
> **直接把那条隧道顶掉**，而且不会有任何提示。
>
> ```powershell
> # 装之前先看一眼
> Get-Service cloudflared -ErrorAction SilentlyContinue
> Get-CimInstance Win32_Service -Filter "Name='cloudflared'" | Select-Object PathName
> ```
>
> 已经有了就**不要** `service install`，改用独立进程 + 独立配置文件：
>
> ```powershell
> # 各自一份 config，metrics 端口也要错开，否则两条隧道抢同一个端口
> cloudflared tunnel --config C:\Users\<你>\.cloudflared\yintu-patent.yml `
>                    --metrics 127.0.0.1:20242 run
> ```
>
> 这种形态下让看门狗认得自己那条隧道：
> `watchdog.ps1 -TunnelName yintu-patent -TunnelMetricsPort 20242`。
> 若隧道压根不归本项目管，用 `watchdog.ps1 -NoTunnel` 只守护应用。

机器上只有本项目一条隧道时，才用服务形态：

```powershell
cloudflared service install
Start-Service cloudflared
```

### 应用保持运行

因为要用 Word 导出 PDF，**应用不要注册成 Windows 服务**（会落入 Session 0，Word 起不来）。
开发阶段最简单的做法：

- 直接开着终端跑 `.\start.ps1 -NoBrowser`；或
- 用「任务计划程序」创建**登录时触发**的任务（勾选"只在用户登录时运行"），
  程序 `powershell.exe`，参数
  `-NoProfile -ExecutionPolicy Bypass -File C:\Users\jielu\Desktop\PatentAgent\start.ps1 -NoBrowser`

电脑休眠会中断服务。在「电源选项」里把睡眠设为"从不"（或至少插电时从不）。

---

## 9. Cloudflare 免费版的几个限制

| 限制 | 影响 | 应对 |
|---|---|---|
| **上传单文件 100MB** | 平台上传上限恰好也是 100MB，正常论文/材料远小于此 | 超大 PDF 先压缩或拆分 |
| 边缘空闲超时约 100 秒 | SSE 有 15s 心跳，不受影响 | 无需处理 |
| 不缓存动态内容 | 无影响 | 无需处理 |
| 隧道带宽无硬性限制但有滥用条款 | 小团队日常使用无虞 | 别拿它跑大流量分发 |

---

## 10. 开发阶段检查清单

- [ ] `https://patent.example.com` 能打开登录页，证书有效（Cloudflare 自动签发）
- [ ] 管理员登录 → 强制改密成功
- [ ] 「设置 → 模型服务」配好 API Key 并测试连接通过
- [ ] 新建普通用户，用它登录后**看不到**「模型服务」等平台设置
- [ ] 用户 A 建的案件，用户 B 登录后**看不到**
- [ ] 管理员能看到 A 的案件，且审计日志里有 `cross_user_read` 记录
- [ ] 上传 PDF → 跑通一条流水线 → **生成过程中能持续看到流式输出**（验证 SSE 通过隧道正常）
- [ ] 下载 docx 正常；PDF 导出正常（本机 Word 可用）
- [ ] 已开启 Cloudflare Access（推荐）
- [ ] 电源设置为不休眠
- [ ] `DATA_DIR` 已配置备份

---

## 11. 从开发转正式部署时

把域名指向真正的服务器，按 [DEPLOYMENT.md](DEPLOYMENT.md) 走。数据迁移只需搬 `DATA_DIR`
整个目录（含 `app.db`、`uploads/`、`outputs/`），账号、案件、交付物会原样带过去。

---

## 12. 安全提醒

- 隧道一开，**你的电脑就是一台公网服务器**。请确保：管理员密码足够强、
  及时为每个同事建独立账号（不要共用）、开启 Cloudflare Access。
- `DATA_DIR/app.db` 内含**平台 LLM API Key 与全部案件内容**，是最高敏感级别的文件。
- 不用时随手 `Stop-Service cloudflared` 关掉隧道，最省心。
