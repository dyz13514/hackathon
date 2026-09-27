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
# 凭证只在 /etc/planning-agent.env 里，本脚本不打印、不写任何凭证（R23.11）。

set -euo pipefail

APP_ROOT="${APP_ROOT:-/srv/planning-agent}"
BACKEND="$APP_ROOT/backend"
FRONTEND="$APP_ROOT/frontend"
ENV_FILE="${ENV_FILE:-/etc/planning-agent.env}"
SITE_DOMAIN="${SITE_DOMAIN:-}"
SEED_DEMO="${SEED_DEMO:-0}"
SERVICE_NAME="planning-agent"

log() { echo "deploy.sh: $*"; }

# --- 0. 前置检查（缺依赖就明确报错，不静默半装） ---
[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行 make deploy" >&2; exit 1; }
[ "$APP_ROOT" = /srv/planning-agent ] || { echo "APP_ROOT 必须是 /srv/planning-agent（unit 与 Caddyfile 固定使用此路径）" >&2; exit 1; }
[[ "$SITE_DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]] && [ "$SITE_DOMAIN" != example.com ] \
    || { echo "请设置 SITE_DOMAIN 为已指向此实例的真实域名（不要带 https://）" >&2; exit 1; }
[[ "$SEED_DEMO" == 0 || "$SEED_DEMO" == 1 ]] || { echo "SEED_DEMO 只能是 0 或 1" >&2; exit 1; }
for dependency in python3 npm caddy sqlite3 curl runuser; do
    command -v "$dependency" >/dev/null || { echo "缺 $dependency" >&2; exit 1; }
done
id "$SERVICE_NAME" >/dev/null 2>&1 || { echo "缺系统用户 $SERVICE_NAME" >&2; exit 1; }
[ -d "$BACKEND" ] && [ -d "$FRONTEND" ] || { echo "仓库未同步到 $APP_ROOT" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "缺凭证文件 $ENV_FILE（权限应为 600，含必需环境变量）" >&2; exit 1; }
runuser -u "$SERVICE_NAME" -- test -r "$ENV_FILE" \
    || { echo "$SERVICE_NAME 无法读取 $ENV_FILE" >&2; exit 1; }
install -d -m 700 -o "$SERVICE_NAME" -g "$SERVICE_NAME" "$APP_ROOT/var" "$APP_ROOT/var/backups" "$APP_ROOT/var/uploads"

# --- 1. 后端虚拟环境 + 依赖 ---
log "准备后端虚拟环境与依赖"
if [ ! -d "$BACKEND/.venv" ]; then
	runuser -u "$SERVICE_NAME" -- python3 -m venv "$BACKEND/.venv"
fi
runuser -u "$SERVICE_NAME" -- "$BACKEND/.venv/bin/pip" install --upgrade pip >/dev/null
runuser -u "$SERVICE_NAME" -- bash -c 'cd "$1" && ./.venv/bin/pip install -e ".[dev]" >/dev/null' _ "$BACKEND"

# --- 2. 迁移 + seed（凭证经 EnvironmentFile 注入当前 shell 仅本步骤用） ---
log "运行数据库迁移"
runuser -u "$SERVICE_NAME" -- bash -c \
    'set -a; . "$1"; set +a; cd "$2"; exec ./.venv/bin/alembic upgrade head' \
    _ "$ENV_FILE" "$BACKEND"
if [ "$SEED_DEMO" = 1 ]; then
    log "显式加载旧版演示 seed"
    runuser -u "$SERVICE_NAME" -- bash -c \
        'set -a; . "$1"; set +a; cd "$2"; exec ./.venv/bin/python -m app.seed --demo' \
        _ "$ENV_FILE" "$BACKEND"
fi
# 数据目录与库文件权限 600（R23.9：文件权限等价最小权限）。
if [ -f "$APP_ROOT/var/planning.db" ]; then chmod 600 "$APP_ROOT/var/planning.db"; fi

# --- 3. 前端构建（静态产物给 Caddy 伺服） ---
log "构建前端静态产物"
runuser -u "$SERVICE_NAME" -- bash -c 'cd "$1" && npm ci && npm run build' _ "$FRONTEND"

# --- 4. 安装 systemd unit + Caddyfile + 每小时备份 timer ---
log "安装 systemd unit 与 Caddyfile"
TMP_CADDY="$(mktemp)"
trap 'rm -f "$TMP_CADDY"' EXIT
sed "s/__SITE_DOMAIN__/$SITE_DOMAIN/" "$APP_ROOT/deploy/Caddyfile" > "$TMP_CADDY"
caddy validate --config "$TMP_CADDY" --adapter caddyfile
install -m 644 "$APP_ROOT/deploy/planning-agent.service" "/etc/systemd/system/$SERVICE_NAME.service"
install -m 644 "$TMP_CADDY" /etc/caddy/Caddyfile
# 每小时备份：优先 cron；有 systemd timer 也可换成 timer（见 backup.sh 注释）。
CRON_LINE="0 * * * * $APP_ROOT/deploy/backup.sh >> $APP_ROOT/var/backup.log 2>&1"
( crontab -u "$SERVICE_NAME" -l 2>/dev/null | grep -v 'deploy/backup.sh' || true; echo "$CRON_LINE" ) \
	| crontab -u "$SERVICE_NAME" -

# --- 5. 重启服务 ---
log "重载并重启服务"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl reload caddy || systemctl restart caddy

# --- 6. 健康检查（本机探针；不经 TLS） ---
log "健康检查 http://127.0.0.1:8000/health"
sleep 2
if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
	log "部署完成，/health 返回正常"
else
	echo "deploy.sh: /health 探针失败——检查 journalctl -u $SERVICE_NAME" >&2
	exit 1
fi
