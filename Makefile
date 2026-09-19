# AI Production Planning Agent —— 开发与运维入口
#
# 五个目标（design.md「项目结构」）：dev / test / eval / eval-live / deploy。
# 面向 POSIX shell（Lightsail 与本地 macOS/Linux/WSL）。Windows 上请在 WSL 中运行，
# 或按 README「Windows 上的等价命令」逐条执行。

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT      := $(CURDIR)
BACKEND   := $(ROOT)/backend
FRONTEND  := $(ROOT)/frontend
VENV      := $(BACKEND)/.venv
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
PYTEST    := $(VENV)/bin/pytest
ALEMBIC   := $(VENV)/bin/alembic
UVICORN   := $(VENV)/bin/uvicorn
PYTHON    ?= python3

API_PORT ?= 8000
WEB_PORT ?= 5173

.PHONY: help dev test eval eval-report eval-live deploy venv node-modules env-check lint clean

help:
	@echo "make dev        建虚拟环境 → 迁移 → seed 演示数据 → 并行启动 uvicorn 与 Vite"
	@echo "make test       后端 pytest（不含 eval）+ 前端 vitest"
	@echo "make eval        评估套件，LLM_MODE=REPLAY，零 Bedrock 消耗"
	@echo "make eval-report 评估套件（REPLAY）并生成 eval_report.md（逐用例状态 + 断言明细）"
	@echo "make eval-live   评估套件，LLM_MODE=LIVE，消耗真实额度（需二次确认）"
	@echo "make deploy     部署到 Lightsail（deploy/deploy.sh，任务 12.6）"
	@echo "make lint       ruff + mypy + 前端 typecheck"

# --- 环境 ---

$(VENV)/pyvenv.cfg:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	cd $(BACKEND) && $(PIP) install -e ".[dev]"

venv: $(VENV)/pyvenv.cfg

node-modules: $(FRONTEND)/package.json
	cd $(FRONTEND) && npm install

env-check:
	@test -f $(ROOT)/.env || { \
	  echo "缺少 .env。执行 cp .env.example .env 并填值后重试。"; \
	  echo "（凭证只经环境变量注入，.env 不入版本控制 —— R23.11）"; \
	  exit 1; }

# --- 一键启动（R27.7） ---
# 顺序：迁移 → seed → 并行起两个开发服务器。uvicorn 固定单 worker：
# SQLite 写并发受限，且 R12.7 的乐观并发在单进程下更容易正确。

dev: venv node-modules env-check
	set -a && source $(ROOT)/.env && set +a && \
	cd $(BACKEND) && $(ALEMBIC) upgrade head && $(PY) -m app.seed --demo
	@echo "后端 http://127.0.0.1:$(API_PORT)   前端 http://127.0.0.1:$(WEB_PORT)"
	@trap 'kill 0' EXIT INT TERM; \
	( set -a && source $(ROOT)/.env && set +a && cd $(BACKEND) && \
	  $(UVICORN) app.main:create_app --factory --reload --workers 1 --port $(API_PORT) ) & \
	( cd $(FRONTEND) && npm run dev -- --port $(WEB_PORT) ) & \
	wait

# --- 测试 ---
# LLM_MODE=STUB：测试永不消耗 Bedrock 额度。

test: venv node-modules
	cd $(BACKEND) && LLM_MODE=STUB \
	  DATABASE_URL=sqlite:///:memory: \
	  SESSION_SHARED_PASSWORD=test-shared-password \
	  SESSION_SECRET_KEY=test-secret-key-that-is-long-enough-32 \
	  $(PYTEST) tests --ignore=tests/eval
	cd $(FRONTEND) && npm run test

# --- 评估套件（R26） ---

eval: venv
	cd $(BACKEND) && LLM_MODE=REPLAY \
	  DATABASE_URL=sqlite:///:memory: \
	  SESSION_SHARED_PASSWORD=test-shared-password \
	  SESSION_SECRET_KEY=test-secret-key-that-is-long-enough-32 \
	  $(PYTEST) tests/eval

# 生成 eval_report.md（R26.4）：逐用例通过状态 + 失败断言明细。仍是 REPLAY，零成本。
# EVAL_REPORT 指向输出路径，tests/eval/conftest.py 的会话钩子据此汇总落盘；不加 -q
# 以保留常规 pytest 摘要。报告写到仓库根的 eval_report.md。
eval-report: venv
	cd $(BACKEND) && LLM_MODE=REPLAY \
	  DATABASE_URL=sqlite:///:memory: \
	  SESSION_SHARED_PASSWORD=test-shared-password \
	  SESSION_SECRET_KEY=test-secret-key-that-is-long-enough-32 \
	  EVAL_REPORT=$(ROOT)/eval_report.md \
	  $(PYTEST) tests/eval
	@echo "报告已生成：$(ROOT)/eval_report.md"

# 真实调用会计入 PROJECT_REAL_RUN_CAP = 150 的配额并产生美元支出，
# 因此要求显式确认，不做成一条随手可敲的命令。
eval-live: venv env-check
	@read -r -p "将以 LLM_MODE=LIVE 运行评估套件，消耗真实 Bedrock 额度。继续？[y/N] " ans; \
	  [ "$$ans" = "y" ] || { echo "已取消"; exit 1; }
	set -a && source $(ROOT)/.env && set +a && \
	cd $(BACKEND) && LLM_MODE=LIVE $(PYTEST) tests/eval

# --- 质量与部署 ---

lint: venv node-modules
	cd $(BACKEND) && $(VENV)/bin/ruff check app tests && $(VENV)/bin/mypy app
	cd $(FRONTEND) && npm run typecheck

deploy:
	@test -x $(ROOT)/deploy/deploy.sh || { \
	  echo "deploy/deploy.sh 尚未实现（任务 12.6：Lightsail + Caddy + systemd）"; \
	  exit 1; }
	$(ROOT)/deploy/deploy.sh

clean:
	rm -rf $(VENV) $(FRONTEND)/node_modules $(FRONTEND)/dist
	find $(BACKEND) -type d -name __pycache__ -prune -exec rm -rf {} +
