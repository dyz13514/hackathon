# deploy/

部署产物目录（design.md Architecture §4）。内容在任务 12.6 落地：

- `Caddyfile` —— TLS + 静态文件 + 反向代理到 uvicorn
- `planning-agent.service` —— systemd unit，`uvicorn --workers 1`
- `backup.sh` —— SQLite 每小时 `VACUUM INTO backups/`，保留最近 24 份
- `deploy.sh` —— `make deploy` 调用的入口（需可执行位）

拓扑：浏览器 →(HTTPS)→ Caddy → uvicorn + FastAPI → SQLite（WAL）；
uvicorn →(HTTPS JSON)→ Bedrock 网关。单 Lightsail 实例（1 GB）。
