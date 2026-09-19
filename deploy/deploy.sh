#!/usr/bin/env bash
# 部署入口（任务 12.7）—— `make deploy` 调用它。design.md Architecture §4：单 Lightsail 实例，
# Caddy（TLS + 静态 + 反代）+ uvicorn 单 worker + SQLite（WAL）。
#
# 这个脚本在**目标实例上**运行（先把仓库同步/拉到实例，再在实例上 `make deploy`）。它是幂等的：
# 建/更新虚拟环境、装依赖、跑迁移、构建前端、安装 systemd unit 与 Caddyfile、重启服务。
# **它不购买、不创建任何云资源**——实例、DNS、防火墙的初次开通是一次性的手工/IaC 步骤（见
# README §部署），本脚本只负责把代码部署到一台**已存在**的实例上。
#
# 前置（实例上一次性准备，README 有清单）：
#   - 系统包：python3.12、nodejs≥18、caddy、sqlite3、make、git
#   - 用户 planning-agent 与目录 /srv/planning-agent（本仓库同步到此）
#   - /etc/planning-agent.env（权限 600，含 DATABASE_URL / SESSION_* / 可选 BEDROCK_*）
#
# 凭证只在 /etc/planning-agent.env 里，本脚本不读、不打印、不写任何凭证（R23.11）。

set -euo pipefail

APP_ROOT="${APP_ROOT:-/srv/planning-agent}"
BACKEND="$APP_ROOT/backend"
FRONTEND="$APP_ROOT/frontend"
ENV_FILE="${ENV_FILE:-/etc/planning-agent.env}"
SERVICE_NAME="planning-agent"

log() { echo "deploy.sh: $*"; }

# --- 0. 前置检查（缺依赖就明确报错，不静默半装） ---
command -v python3 >/dev/null || { echo "缺 python3" >&2; exit 1; }
command -v caddy >/dev/null || log "警告：未找到 caddy，可先装 Caddyfile 稍后再装 caddy"
[ -f "$ENV_FILE" ] || { echo "缺凭证文件 $ENV_FILE（权限应为 600，含必需环境变量）" >&2; exit 1; }

# --- 1. 后端虚拟环境 + 依赖 ---
log "准备后端虚拟环境与依赖"
if [ ! -d "$BACKEND/.venv" ]; then
	python3 -m venv "$BACKEND/.venv"
fi
"$BACKEND/.venv/bin/pip" install --upgrade pip >/dev/null
(cd "$BACKEND" && ./.venv/bin/pip install -e ".[dev]" >/dev/null)

# --- 2. 迁移 + seed（凭证经 EnvironmentFile 注入当前 shell 仅本步骤用） ---
log "运行数据库迁移与演示数据 seed"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
(cd "$BACKEND" && ./.venv/bin/alembic upgrade head)
(cd "$BACKEND" && ./.venv/bin/python -m app.seed --demo)
# 数据目录与库文件权限 600（R23.9：文件权限等价最小权限）。
mkdir -p "$APP_ROOT/var/backups"
chmod 700 "$APP_ROOT/var" "$APP_ROOT/var/backups"

# --- 3. 前端构建（静态产物给 Caddy 伺服） ---
log "构建前端静态产物"
(cd "$FRONTEND" && npm ci && npm run build)

# --- 4. 安装 systemd unit + Caddyfile + 每小时备份 timer ---
log "安装 systemd unit 与 Caddyfile"
sudo cp "$APP_ROOT/deploy/planning-agent.service" "/etc/systemd/system/$SERVICE_NAME.service"
sudo cp "$APP_ROOT/deploy/Caddyfile" /etc/caddy/Caddyfile
# 每小时备份：优先 cron；有 systemd timer 也可换成 timer（见 backup.sh 注释）。
CRON_LINE="0 * * * * $APP_ROOT/deploy/backup.sh >> $APP_ROOT/var/backup.log 2>&1"
( sudo crontab -u "$SERVICE_NAME" -l 2>/dev/null | grep -v 'deploy/backup.sh' || true; echo "$CRON_LINE" ) \
	| sudo crontab -u "$SERVICE_NAME" -

# --- 5. 重启服务 ---
log "重载并重启服务"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
if command -v caddy >/dev/null; then
	sudo systemctl reload caddy || sudo systemctl restart caddy
fi

# --- 6. 健康检查（本机探针；不经 TLS） ---
log "健康检查 http://127.0.0.1:8000/health"
sleep 2
if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
	log "部署完成，/health 返回正常"
else
	echo "deploy.sh: /health 探针失败——检查 journalctl -u $SERVICE_NAME" >&2
	exit 1
fi
