# deploy/

Lightsail 部署产物（design.md Architecture §4、运维要点；任务 12.7）。单实例（建议 2 GB）：
Caddy 负责 TLS、静态文件与反向代理，uvicorn 单 worker 跑 FastAPI，SQLite 文件开 WAL。

```
浏览器 →(HTTPS)→ Caddy → uvicorn + FastAPI → SQLite（WAL）
                    │
              frontend/dist（静态）
uvicorn →(HTTPS JSON)→ Bedrock 网关
```

Starter Kit 展示的是 **连接**比赛提供的 Bedrock 网关；这里部署的是本项目自己的 React/FastAPI 应用。两者独立：应用可运行在 Lightsail，后端通过 HTTPS 调用网关；无需在本机安装 OpenClaw 或 Hermes。比赛网关示例的认证头是 `X-API-Key`，本项目用 `LLM_AUTH_STYLE=X_API_KEY` 选择它。

## 文件

| 文件 | 作用 |
|------|------|
| `Caddyfile` | TLS（自动证书）+ 静态文件（`frontend/dist`）+ `/api/*` 反向代理到 `127.0.0.1:8000` |
| `planning-agent.service` | systemd unit：`uvicorn app.main:create_app --factory --workers 1`；凭证经 `EnvironmentFile=/etc/planning-agent.env` 注入；`ProtectSystem=strict` + `ReadWritePaths=/srv/planning-agent/var` 最小权限 |
| `backup.sh` | SQLite 每小时 `VACUUM INTO backups/`，保留最近 24 份（滚动）；权限 600 |
| `deploy.sh` | `make deploy` 的入口：建 venv、装依赖、迁移、构建前端、安装 unit/Caddyfile、装每小时备份 cron、重启、`/health` 探针。旧版 demo seed 只有显式设 `SEED_DEMO=1` 才执行。**幂等**，只部署到已存在的实例，不创建云资源 |

## 一次性准备（实例上，手工或 IaC——不由 `deploy.sh` 做）

1. Lightsail Ubuntu 实例，开放 80/443，绑定静态 IPv4。建议本应用先用 2 GB 规格；Starter Kit 的 4 GB 规格是其 OpenClaw/Hermes 示例，不是本应用的硬性要求。Lightsail 的费用与比赛网关调用费用分别计算。
2. 系统包：`python3`（≥3.11）、`nodejs`（≥18）、`npm`、`caddy`、`sqlite3`、`make`、`git`、`curl`、`cron`、`util-linux`（提供 `runuser`）。这些命令需对系统用户 `planning-agent` 可见，不要只安装在 `ubuntu` 用户的 nvm 目录。
3. 用户与目录：`useradd -r planning-agent`，仓库同步到 `/srv/planning-agent`，`chown -R planning-agent:planning-agent /srv/planning-agent`。确保 `/srv/planning-agent` 和 `frontend` 可供 Caddy 遍历；数据库目录由部署脚本设为 700。
4. 凭证文件 `/etc/planning-agent.env`（权限 **600**，属主 `planning-agent`）：
   ```
   DATABASE_URL=sqlite:////srv/planning-agent/var/planning.db
   SESSION_SHARED_PASSWORD=<强口令>
   SESSION_SECRET_KEY=<≥32 字符随机串>
   APP_ENV=LOCAL
   TZ=Asia/Shanghai
   UPLOAD_DIR=/srv/planning-agent/var/uploads
   LLM_MODE=LIVE
   BEDROCK_GATEWAY_URL=https://<比赛网关主机>/api/chat
   BEDROCK_API_KEY=<比赛提供的密钥>
   BEDROCK_MODEL=<比赛网关支持的模型 ID>
   LLM_API_STYLE=OLLAMA
   LLM_AUTH_STYLE=X_API_KEY
   CORS_ALLOW_ORIGINS=https://<你的域名>
   ```
   凭证只经这个文件注入进程（R23.11），不入版本控制、不进日志。
5. 记下应用域名，部署时作为 `SITE_DOMAIN` 传入。如果没有自有域名，可临时用 `<静态IP>.sslip.io`（例如 `203.0.113.10.sslip.io`），它会解析到该 IP，让 Caddy 为这个主机名申请 HTTPS 证书；这是第三方免费 DNS，正式长期地址建议换自有域名。`BEDROCK_GATEWAY_URL` 必须是**完整 POST 地址** `/api/chat`，而 Starter Kit 的 `LLM_GATEWAY_URL` 是供 ChatOllama 使用的主机基址。若暂时没有网关凭证，用 `LLM_MODE=REPLAY` 启动；页面会标出非 LIVE 结果。

## 部署

在实例上（仓库已同步到 `/srv/planning-agent`，域名 DNS 已生效）：

```bash
cd /srv/planning-agent
sudo env SITE_DOMAIN=app.example.org make deploy
```

脚本要求 root 权限安装 Caddy/systemd 配置，但 Python 依赖安装、迁移和前端构建均以 `planning-agent` 身份运行。首次部署默认保留空业务库，可在网页 Import 上传完整工作簿；只有明确要加载旧固定 seed 时才用 `sudo env SITE_DOMAIN=app.example.org SEED_DEMO=1 make deploy`。部署结束分别检查 `https://app.example.org/` 与 `https://app.example.org/api/health`。公开访问或切换 LIVE 前，确认共享登录口令、TLS 和网关配额设置均为预期值。

## 数据库最小权限（R23.9 的 SQLite 差异）

SQLite 无账户概念，以「应用只持有一个库文件句柄 + 文件权限 600 + 不开放任何 SQL 执行端点」
落实等价约束。迁移到 PostgreSQL 时改为表级 `GRANT`；schema 已保持 PostgreSQL 兼容（无 SQLite
专有类型、JSON 列用 `JSON`、时间列统一 `DateTime`、主键为可读字符串 ID），迁移是配置变更而非
重写。
