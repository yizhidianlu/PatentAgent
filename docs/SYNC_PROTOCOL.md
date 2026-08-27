# 双机同步协议 — 维护端 ↔ 部署端

本项目由两台机器分工：一台写代码，一台跑服务。这份文档是两端之间的契约，
**部署端的 Claude 请完整读完再动手**。

| 角色 | 机器 | 职责 |
|---|---|---|
| **维护端** | 开发机 | 改代码、跑测试、推 GitHub、发更新通知 |
| **部署端** | 服务器 | 拉代码、构建、重启、守护服务、回报结果 |

单一事实源是 GitHub：`https://github.com/yizhidianlu/PatentAgent`（分支 `main`）。
代码只从维护端流向部署端，**从不反向**。

---

## 一、给部署端 Claude 的规则

### 1.1 可以做的

- 跑 `deploy\update.ps1` 同步更新
- 改 `backend\.env`（端口、域名、管理员、API Key 等**本机配置**）
- 管理本机的服务进程、看门狗、cloudflared 隧道
- 查日志、备份、排障

### 1.2 不要做的

- **不要改源码**。发现 bug 请回报给维护端，由维护端修完推上来。
  部署端一旦有本地改动，`update.ps1` 会拒绝执行（这是有意的）。
- **不要 push**。部署端对 GitHub 只读。
- **不要碰 `data\`**。里面是这台机器的生产数据：用户账号、案件、上传的材料、
  已配置的 API Key。它不在 git 里，任何 `git clean -x`、`git clean -X`
  都会把它删掉且无法从仓库恢复。
- **不要手动 `git reset --hard` 到未知版本**。要回退请用 `update.ps1` 的
  自动回滚，或明确回到日志里记录过的 commit。

### 1.3 两端的数据是独立的

部署端有自己的数据库、自己的管理员账号、自己的 API Key、自己的域名和隧道。
`git pull` 只动代码，不会覆盖这些。维护端也**看不到**部署端的业务数据。

---

## 二、首次部署

按顺序读并执行：

1. **`docs\DEPLOYMENT.md`** — 环境准备、克隆、安装、生产配置（`.env`）、常驻运行
2. **`docs\DEPLOY_CLOUDFLARE.md`** — 若走 Cloudflare Tunnel 对外
3. 装好后跑一次自检：

   ```powershell
   .\start.ps1 -SetupOnly          # 只装不起，验证环境完整
   .\deploy\update.ps1 -CheckOnly  # 验证能连上 GitHub、能识别版本
   ```

首次部署必须落实的几件事（`DEPLOYMENT.md` 有详细说明，这里只列不能漏的）：

- `.env` 里 `SESSION_COOKIE_SECURE=true`（生产走 HTTPS，Cookie 不能明文传）
- `.env` 里 `ALLOWED_ORIGINS` 收紧到实际域名
- 管理员密码首次启动后立即改掉
- 看门狗常驻：`powershell -File watchdog.ps1 -Port 8000`
- **`.env` 与 `data\` 都不入 git**，备份要单独做（见 `DEPLOYMENT.md` §6）

装完请把这些回报给维护端：部署路径、端口、对外域名、`revision`。

---

## 三、日常更新

### 3.1 收到更新通知后

```powershell
cd <部署目录>
.\deploy\update.ps1 -Port <你的端口>
```

脚本会依次完成：

```
停看门狗 → 备份数据库 → 停应用 → git pull → 按需装依赖
→ 构建前端 → 起应用 → 健康检查 → 起看门狗
```

**任何一步失败都会自动回滚**：代码回退到更新前的 commit、数据库从备份恢复、
重新构建并把服务拉回来，然后以退出码 1 结束。也就是说结果只有两种——
用上了新版本，或者回到了动手之前，不会停在中间。

退出码：`0` 成功或已是最新 · `1` 失败已回滚 · `2` 工作区有本地改动，拒绝执行。

### 3.2 更新脚本自身被更新时，要跑两次

PowerShell 在启动时就把整个脚本解析进内存了。如果这次更新恰好改了
`deploy\update.ps1`，**本次运行用的仍是旧版逻辑**，新逻辑要下次才生效。

所以：更新日志里出现 `deploy/update.ps1` 时，成功后再跑一次
`.\deploy\update.ps1 -CheckOnly` 确认无残留即可（通常会显示"已是最新"）。
若上一次是失败回滚，则**必须**再完整跑一次 —— 因为回滚也把脚本退回了旧版。

### 3.3 更新失败了怎么办

脚本已经自动回滚，服务应当是好的。请把这些回报给维护端，**不要自己改源码**：

- `data\update.log` 里从 `=== 同步更新开始` 到结尾的完整片段
- 失败步骤的错误摘要
- `curl http://127.0.0.1:<端口>/api/v1/system/health` 的输出

若日志最后一行是「回滚后服务仍不健康，需要人工介入」，说明自动回滚也没救回来。
这时按顺序处理：

```powershell
# 1. 确认代码在日志记录的旧 commit 上
git -C <部署目录> log --oneline -1

# 2. 重装依赖并重建（回滚时的重建可能也失败了）
.\start.ps1 -SetupOnly

# 3. 数据库若已损坏，从 data\backups\ 取最近一份（脚本保留最近 10 份）
#    恢复前先把当前的 app.db 改名留档，不要直接覆盖

# 4. 起服务
.\start.ps1 -NoBrowser
```

---

## 四、怎么确认更新真的生效了

```powershell
curl http://127.0.0.1:<端口>/api/v1/system/health
```

```json
{"ok": true, "name": "引途医疗专利智能体", "version": "0.1.0",
 "revision": "a8bae00", "time": "..."}
```

`revision` 是当前代码的短 commit sha。**它必须与通知里的目标 commit 一致**。
`version` 只在正式发版时才变，不能用来判断某次同步是否落地。

维护端也会直接请求部署端的公网地址核对这个字段，所以更新完请确保外网可达。

---

## 五、通知机制

### 5.1 主通道：Remote Control

维护端通过 Remote Control 直接给部署端的 Claude 会话发消息。要用这条通道，
部署端需要满足：

- 那台机器上开着 Claude Code 会话
- 该会话启用了 Remote Control 且处于在线（维护端 `ListAgents` 能看到它）
- 两端登录的是同一个账号

部署端首次接入后，请把**会话名**告诉维护端，维护端才知道该发给谁。

### 5.2 通知长这样

```
[PatentAgent 更新] a8bae00 → e04cac1
变更：修复公式渲染 / 设置页自动填模型上限
影响：需重新构建前端；无数据库迁移；依赖未变
动作：cd <部署目录>; .\deploy\update.ps1 -Port 8000
完成后请回报 health 的 revision 字段。
```

「影响」一栏会明确写清三件事，部署端据此判断耗时和风险：

- 是否需要重建前端（改了 `frontend/` 就需要，`dist/` 不入库）
- 是否有数据库迁移（`backend/app/db/migrations/` 有新文件）
- 依赖是否变化（`pyproject.toml` / `package-lock.json` 有改动 → 更新会慢几分钟）

### 5.3 兜底：自己定时查

Remote Control 会话不在线时通知发不到。部署端可以定时自查：

```powershell
.\deploy\update.ps1 -CheckOnly    # 只报告有无新提交，不做任何改动
```

挂进计划任务每天跑一次即可；发现有更新再执行正式更新。

### 5.4 回报格式

更新完请回报维护端：

```
[PatentAgent 部署] 成功 / 失败已回滚
revision: e04cac1（health 实测）
耗时: 约 2 分钟
异常: 无 / <错误摘要>
```

---

## 六、维护端的责任

写在这里是为了让部署端知道可以期待什么，出问题时该找谁：

- 推送前在维护端跑通测试（`pytest` + `tsc` + `npm run build`）
- 通知里如实写清「影响」三项
- 收到部署端的失败回报后，由维护端定位并修复，不要求部署端改源码
- 涉及数据库迁移或配置变更的更新，在通知里单独标注并给出操作步骤
