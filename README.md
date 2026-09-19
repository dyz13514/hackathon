# AI Production Planning Agent

面向中小制造车间的排产助手：**确定性内核做决策，LLM 只做解释、映射与叙述，人做审批**。

规格文档在 `.kiro/specs/production-planning-agent/`（`requirements.md` 28 条需求、
`design.md` 架构、`tasks.md` 实施计划）。本 README 是入口，不重复设计细节。

---

## 架构

四层，边界可测（`design.md` Architecture §1）：

```
浏览器 (React + TS)
   │  HTTPS，全部路径前缀 /api
FastAPI 应用层        REST + Pydantic 边界校验 + Session_Auth
   │
编排层（确定性）      Orchestrator / Context_Manager / Tool_Registry
   │                  Guardrail_Layer / Token_Budget_Manager
   ├── Agent 层       Ingestion / Planning / Risk_Monitor（只经 Tool_Registry 触达能力）
   │      └── Bedrock_Adapter（全仓唯一 LLM 出口）
确定性内核（纯 Python，无 I/O）
   │                  Scheduling_Core / Constraint_Validator / Objective_Scorer
   │                  Baseline_Scheduler / Replanner / Autonomy_Policy_Engine ...
持久层                SQLite（WAL）via SQLAlchemy，schema 保持 PostgreSQL 兼容
```

三条分层规则由 `backend/tests/structure/test_layering.py` 静态扫描断言（任务 1.8）：

1. `app/core/**` 不 import `sqlalchemy` / `fastapi` / `httpx` / `boto3`。
2. `app/agents/**` 不 import 内核或 `tools/handlers`。
3. 全仓库仅 `app/llm/adapter.py` 出现 Bedrock 网关 URL。

两种执行形态（ADR-002）：**形态 A** 确定性流水线用于计划生成（零 LLM 编排 + 末端
1 次解释调用）；**形态 B** ReAct 循环只用于路线可变的路径（P0 接线重排与列映射两条）。

目录树见 `design.md`「项目结构」。当前仓库状态：骨架与工具链已就位（任务 1.1），
各包的 `__init__.py` 标注了后续由哪个任务填充。

---

## 启动

前置：Python ≥ 3.11、Node ≥ 18、GNU Make。

```bash
cp .env.example .env      # 填 SESSION_SHARED_PASSWORD 与 SESSION_SECRET_KEY
make dev
```

`make dev` 依次做四件事（`design.md`「运维要点」R27.7）：建虚拟环境并安装依赖 →
`alembic upgrade head` → `python -m app.seed --demo` → 并行启动 uvicorn（`:8000`）
与 Vite（`:5173`）。前端经 Vite 代理把 `/api` 转发到后端，因此浏览器始终同源。

配置只有一个入口：`backend/app/settings.py`。**必需环境变量缺失即拒绝启动**
（R23.11）——`DATABASE_URL`、`SESSION_SHARED_PASSWORD`、`SESSION_SECRET_KEY` 三个，
另外 `LLM_MODE=LIVE` 时追加要求 `BEDROCK_GATEWAY_URL` 与 `BEDROCK_API_KEY`。
`.env` 在 `.gitignore` 中，凭证不入版本控制。

`LLM_MODE` 默认 `REPLAY`：构建期与 CI 回放录制的响应，不消耗 Bedrock 额度。
取值 `LIVE`（真实调用）/ `REPLAY`（回放）/ `STUB`（固定模板）/ `DISABLED`
（`DETERMINISTIC_ONLY` 降级）。

### Windows 上的等价命令

`Makefile` 面向 POSIX shell。Windows 建议在 WSL 中运行；若要在 PowerShell 下直接跑：

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\pip install -e "./backend[dev]"
cd backend
.venv\Scripts\alembic upgrade head
.venv\Scripts\python -m app.seed --demo
.venv\Scripts\uvicorn app.main:create_app --factory --reload --workers 1 --port 8000
# 另开一个终端
cd frontend
npm install
npm run dev
```

从 `backend/` 目录启动时，`app/settings.py` 会依次尝试 `../.env`（仓库根）与
`backend/.env`，因此把 `.env` 放在仓库根即可。

`uvicorn` 固定 `--workers 1`：SQLite 写并发受限，且计划审批的乐观并发控制
（R12.7）在单进程下更容易正确。

### 登录（Session_Auth）

**全部写端点都要认证**（R23.12）：`POST` / `PUT` / `PATCH` / `DELETE` 一律经服务端校验
会话令牌，唯一豁免是登录与登出本身。读端点与 `GET /health` 公开。

```bash
# 共享口令换 HttpOnly Cookie（口令即 .env 里的 SESSION_SHARED_PASSWORD）
curl -c cookies.txt -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' -d '{"password":"change-me-before-demo"}'

curl -b cookies.txt -X POST localhost:8000/api/plans/generate   # 带 Cookie 才能写
curl -b cookies.txt -X POST localhost:8000/api/auth/logout
```

未认证的写请求返回 `401` + `{"error": {"code": "UNAUTHENTICATED", ...}}`。令牌有效期
12 小时，**改 `SESSION_SHARED_PASSWORD` 或 `SESSION_SECRET_KEY` 会立即让全部既有令牌
失效**（签名密钥绑定口令指纹），这就是「口令泄漏了怎么办」的答案。浏览器里的 Cookie
标 `Secure` 的条件是 `CORS_ALLOW_ORIGINS` 全为 https。

---

## 测试与评估套件

```bash
make test        # 后端 pytest（不含 eval）+ 前端 vitest，LLM_MODE=STUB
make eval        # 评估套件，LLM_MODE=REPLAY，零 Bedrock 消耗
make eval-live   # 评估套件，LLM_MODE=LIVE，消耗真实额度（需二次确认）
make lint        # ruff + mypy + 前端 typecheck
```

`backend/tests/` 的分工：

| 目录 | 内容 |
|------|------|
| `properties/` | 7 条属性测试，一文件一属性（编号 1、2、4、10、15、21、37），全部非可选 |
| `unit/` | 单元测试；`Scheduling_Core` / `Constraint_Validator` / `Objective_Scorer` / `Autonomy_Policy_Engine` 四模块要求 100% 分支覆盖（R27.10） |
| `contracts/` | 工具输入输出契约与白名单矩阵 |
| `structure/` | 分层与「日志中无凭证」的静态断言 |
| `smoke/` | 装配、演示重置、性能与可访问性冒烟 |
| `eval/` | 29 条 EVAL 用例：EVAL-001–015（黄金）与 EVAL-201–214（对抗），全部非可选 |
| `cassettes/` | 录制的 LLM 响应，供 `REPLAY` 模式回放 |

成本纪律：真实端到端运行有硬上限 `PROJECT_REAL_RUN_CAP = 150`（按 `traces` 表中
`mode != REPLAY` 的行数在启动时计数强制），项目累计上限 USD 35。因此**默认一切走
`REPLAY`**，`make eval-live` 需要显式确认。

---

## 部署

单 AWS Lightsail 实例（1 GB）：Caddy 负责 TLS、静态文件与反向代理，uvicorn 单
worker 跑 FastAPI，SQLite 文件开 WAL。仅使用两类 AWS 能力：Lightsail 与 Bedrock
Claude Sonnet 4.5 的 JSON API（R27.2）。

```bash
make deploy      # 在目标实例上调用 deploy/deploy.sh（幂等：建环境→迁移→seed→构建前端→装 unit/Caddy→重启→探活）
```

部署产物在 `deploy/`（见 `deploy/README.md`）：`Caddyfile`（TLS + 静态文件 +
`/api/*` 反向代理）、`planning-agent.service`（systemd，`uvicorn --workers 1`，凭证经
`EnvironmentFile` 注入、`ProtectSystem=strict` 最小权限）、`backup.sh`（SQLite 每小时
`VACUUM INTO backups/`，保留最近 24 份）、`deploy.sh`（`make deploy` 入口）。

一次性准备（实例上手工/IaC，不由 `deploy.sh` 做——它只部署代码、不创建云资源）：开通
Lightsail 实例并放行 80/443、装 `python3.12`/`nodejs`/`caddy`/`sqlite3`、建 `planning-agent`
用户与 `/srv/planning-agent` 目录、写 `/etc/planning-agent.env`（权限 600，含
`DATABASE_URL`/`SESSION_*`/可选 `BEDROCK_*`）、把 `Caddyfile` 的 `example.com` 换成真实
域名。逐项清单见 `deploy/README.md`。仅使用两类 AWS 能力：Lightsail 与 Bedrock（R27.2）。

**数据库最小权限的 SQLite 差异**（R23.9）：SQLite 无账户概念，因此以「应用只持有
一个数据库文件句柄 + 文件权限 600 + 不开放任何 SQL 执行端点」落实等价约束。迁移到
PostgreSQL 时改为表级 `GRANT`；schema 本身已保持 PostgreSQL 兼容（不用 SQLite 专有
类型，JSON 列用 `JSON`，时间列统一 `DateTime`，主键为可读字符串 ID），迁移是配置
变更而非重写。
