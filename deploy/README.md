# deploy/

Lightsail 部署产物（design.md Architecture §4、运维要点；任务 12.7）。单实例（1 GB）：
Caddy 负责 TLS、静态文件与反向代理，uvicorn 单 worker 跑 FastAPI，SQLite 文件开 WAL。

```
浏览器 →(HTTPS)→ Caddy → uvicorn + FastAPI → SQLite（WAL）
                    │
              frontend/dist（静态）
uvicorn →(HTTPS JSON)→ Bedrock 网关
```

## 文件

| 文件 | 作用 |
|------|------|
| `Caddyfile` | TLS（自动证书）+ 静态文件（`frontend/dist`）+ `/api/*` 反向代理到 `127.0.0.1:8000` |
| `planning-agent.service` | systemd unit：`uvicorn app.main:create_app --factory --workers 1`；凭证经 `EnvironmentFile=/etc/planning-agent.env` 注入；`ProtectSystem=strict` + `ReadWritePaths=/srv/planning-agent/var` 最小权限 |
| `backup.sh` | SQLite 每小时 `VACUUM INTO backups/`，保留最近 24 份（滚动）；权限 600 |
| `deploy.sh` | `make deploy` 的入口：建 venv、装依赖、迁移、seed、构建前端、安装 unit/Caddyfile、装每小时备份 cron、重启、`/health` 探针。**幂等**，只部署到已存在的实例，不创建云资源 |

## 一次性准备（实例上，手工或 IaC——不由 `deploy.sh` 做）

1. Lightsail 实例（1 GB，Ubuntu），开放 80/443；DNS 指向其静态 IP。
2. 系统包：`python3.12`、`nodejs`（≥18）、`caddy`、`sqlite3`、`make`、`git`。
3. 用户与目录：`useradd -r planning-agent`，仓库同步到 `/srv/planning-agent`，`chown -R planning-agent /srv/planning-agent`。
4. 凭证文件 `/etc/planning-agent.env`（权限 **600**，属主 `planning-agent`）：
   ```
   DATABASE_URL=sqlite:////srv/planning-agent/var/planning.db
   SESSION_SHARED_PASSWORD=<强口令>
   SESSION_SECRET_KEY=<≥32 字符随机串>
   LLM_MODE=REPLAY            # 或 LIVE（另加下面两行）
   # BEDROCK_GATEWAY_URL=...
   # BEDROCK_API_KEY=...
   CORS_ALLOW_ORIGINS=https://<你的域名>
   ```
   凭证只经这个文件注入进程（R23.11），不入版本控制、不进日志。
5. 把 `Caddyfile` 里的 `example.com` 换成真实域名。

## 部署

在实例上（仓库已同步到 `/srv/planning-agent`）：

```bash
cd /srv/planning-agent && make deploy      # 调用 deploy/deploy.sh
```

## 数据库最小权限（R23.9 的 SQLite 差异）

SQLite 无账户概念，以「应用只持有一个库文件句柄 + 文件权限 600 + 不开放任何 SQL 执行端点」
落实等价约束。迁移到 PostgreSQL 时改为表级 `GRANT`；schema 已保持 PostgreSQL 兼容（无 SQLite
专有类型、JSON 列用 `JSON`、时间列统一 `DateTime`、主键为可读字符串 ID），迁移是配置变更而非
重写。
