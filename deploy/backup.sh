#!/usr/bin/env bash
# SQLite 每小时热备（任务 12.7，design.md 运维要点）：`VACUUM INTO` 出一份一致快照，保留最近 24 份。
#
# 为什么用 `VACUUM INTO` 而不是 `cp`：WAL 模式下直接拷贝 .db 文件可能漏掉 -wal 里未 checkpoint
# 的事务，得到一个不一致的备份。`VACUUM INTO` 在一个只读事务里把整库写成一个**独立、自洽、已
# 压实**的新文件，不阻塞在线写（读事务），因此适合边跑边备。
#
# 安装为每小时定时任务（二选一）：
#   - cron:  0 * * * * /srv/planning-agent/deploy/backup.sh >> /srv/planning-agent/var/backup.log 2>&1
#   - 或 systemd timer（planning-agent-backup.timer，OnCalendar=hourly）
#
# 环境变量（可选，均有默认）：
#   DB_PATH      SQLite 库文件路径（默认 /srv/planning-agent/var/planning.db）
#   BACKUP_DIR   备份目录（默认 /srv/planning-agent/var/backups）
#   KEEP         保留份数（默认 24 —— 24 小时滚动窗口）

set -euo pipefail

DB_PATH="${DB_PATH:-/srv/planning-agent/var/planning.db}"
BACKUP_DIR="${BACKUP_DIR:-/srv/planning-agent/var/backups}"
KEEP="${KEEP:-24}"

if [ ! -f "$DB_PATH" ]; then
	echo "backup.sh: 数据库文件不存在：$DB_PATH" >&2
	exit 1
fi

mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/planning-$timestamp.db"

# VACUUM INTO：一致快照，不阻塞在线写。sqlite3 CLI 必须可用（Lightsail 上 apt install sqlite3）。
sqlite3 "$DB_PATH" "VACUUM INTO '$target';"
# 备份文件权限收到 600（与库文件一致，R23.9：不开放任何多余读取面）。
chmod 600 "$target"
echo "backup.sh: 已写 $target"

# 滚动保留最近 KEEP 份：按文件名（时间戳）逆序，删掉第 KEEP+1 份起的旧备份。
mapfile -t backups < <(ls -1 "$BACKUP_DIR"/planning-*.db 2>/dev/null | sort -r)
if [ "${#backups[@]}" -gt "$KEEP" ]; then
	for old in "${backups[@]:$KEEP}"; do
		rm -f "$old"
		echo "backup.sh: 已清理旧备份 $old"
	done
fi
