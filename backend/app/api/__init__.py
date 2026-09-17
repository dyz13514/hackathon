"""REST 路由层。全部端点前缀 `/api`（design.md Components §5）。

`api_router` 是唯一被 `app.main.create_app()` 装配的路由聚合点。各分组模块按
design.md §5 的表落地，落地任务如下（本任务只建骨架，不含任何端点）：

- `deps.py`（Session_Auth 中间件与写端点依赖）── 任务 1.5 ✔
- `errors.py`（统一错误响应包）── 任务 1.5 ✔
- `auth.py`（`/auth/login`、`/auth/logout`、`/auth/session`）── 任务 1.5 ✔
- `admin.py`（`/health`、`/demo/reset`、`/settings/*`）── 任务 1.6、1.7
- `state.py`（`/state/dashboard`）── 任务 3.7
- `plans.py`（`/plans/*`）── 任务 2.12、3.6、5.11
- `approvals.py`（`/plans/{id}/approve|reject|modify`）── 任务 3.1、3.2
- `traces.py`（`/traces`、`/audit-log`）── 任务 5.12
- `disruptions.py` ── 任务 7.x；`risks.py` ── 任务 8.x；`scenarios.py` ── 任务 9.x
- `imports.py` ── 任务 10.x；`preferences.py` ── 任务 11.x
- `value_ledger.py` ── 任务 11.x；`autonomy.py` ── 任务 13.x（P1）

写端点一律经 `Session_Auth` 校验（R23.12）。校验的主体是 `api/deps.py` 的
`SessionAuthMiddleware`：它按**方法**拦截（`POST/PUT/PATCH/DELETE`），因此新增写路由
默认就是受保护的，不存在「忘了挂依赖」这种失效方式。新增写路由时仍应显式标注
`session: PlannerSession`，让保护关系出现在签名与 OpenAPI 里（第二道，同样在服务端）。
"""

from fastapi import APIRouter

from app.api import admin, approvals, auth, plans, state, traces

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(plans.router)
api_router.include_router(approvals.router)
api_router.include_router(state.router)
api_router.include_router(traces.router)

#: `/health` 同时挂在根路径上（`app.main` 装配，`include_in_schema=False`）：systemd 与
#: Lightsail 的探针打的是本机 uvicorn，不经反向代理，因此不该被 `/api` 前缀绑住。
#: design.md「运维要点」写的正是 `GET /health`。
ops_router = admin.router

__all__ = ["api_router", "ops_router"]
