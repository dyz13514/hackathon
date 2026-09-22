"""What-if 场景推演与采纳端点（design.md Components §5「场景」、§6 `/whatif`，任务 8.3，R16）。

两个端点：

- `POST /api/scenarios/run`（**P0 唯一的 What-if 入口，无 LLM**，R16.7）—— 接收 1–5 类结构化
  `ScenarioMutation`，在沙箱里确定性推演，30 秒内返回与当前 `ACTIVE` 计划的对比
  （`feasibility`、迟交订单数变化、总拖期分钟变化、新增 `unschedulable` 清单）。写端点
  （受 `Session_Auth` 保护——沙箱虽不写生产数据，但触发一次推演是有意的操作，且与采纳同一
  信任边界）。无 `ACTIVE` 计划 → `NO_ACTIVE_PLAN`。
- `POST /api/scenarios/{scenario_id}/adopt`（R16.9）—— 以该场景生成正式 `PENDING_APPROVAL`
  提案，**仍走审批流程**。场景过期/不存在 → `SCENARIO_NOT_FOUND`。

## 数值全部确定性、无 LLM

推演与采纳都走确定性内核（`apply_mutations` → `generate_schedule` → `validate` → `score`）。
本端点不调用任何 LLM。自然语言输入框是 P1（任务 13.1）——P0 前端只有结构化场景表单。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.core.sandbox import ScenarioMutationError
from app.seed.dataset import DEMO_ANCHOR
from app.services.replanning import NoActivePlanError
from app.services.sandbox import (
    ScenarioNotFoundError,
    ScenarioStore,
    adopt_scenario,
    run_sandbox,
)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


# --------------------------------------------------------------------------
# 请求契约（5 类结构化 ScenarioMutation，判别联合）
# --------------------------------------------------------------------------


class _Mut(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddOrChangeOrderMut(_Mut):
    kind: Literal["ADD_OR_CHANGE_ORDER"] = "ADD_OR_CHANGE_ORDER"
    order_id: str | None = None
    product_id: str | None = None
    quantity: float | None = Field(default=None, gt=0)
    due_date: date | None = None
    priority: Literal["URGENT", "HIGH", "NORMAL", "LOW"] | None = None


class SetMachineUnavailableMut(_Mut):
    kind: Literal["SET_MACHINE_UNAVAILABLE"] = "SET_MACHINE_UNAVAILABLE"
    machine_id: str
    start_time: datetime
    end_time: datetime


class ChangeMaterialAvailabilityMut(_Mut):
    kind: Literal["CHANGE_MATERIAL_AVAILABILITY"] = "CHANGE_MATERIAL_AVAILABILITY"
    material_id: str
    quantity_available: float = Field(ge=0)


class SetWorkerUnavailableMut(_Mut):
    kind: Literal["SET_WORKER_UNAVAILABLE"] = "SET_WORKER_UNAVAILABLE"
    worker_id: str
    start_time: datetime
    end_time: datetime


class ChangeOrderPriorityMut(_Mut):
    kind: Literal["CHANGE_ORDER_PRIORITY"] = "CHANGE_ORDER_PRIORITY"
    order_id: str
    priority: Literal["URGENT", "HIGH", "NORMAL", "LOW"]


ScenarioMutationBody = Annotated[
    AddOrChangeOrderMut
    | SetMachineUnavailableMut
    | ChangeMaterialAvailabilityMut
    | SetWorkerUnavailableMut
    | ChangeOrderPriorityMut,
    Field(discriminator="kind"),
]


class RunScenarioRequest(BaseModel):
    """`POST /scenarios/run` 的请求体。1–5 类结构化变更（R16.2）。"""

    model_config = ConfigDict(extra="forbid")

    mutations: list[ScenarioMutationBody] = Field(min_length=1, max_length=5)
    now: datetime | None = None


class ScenarioResultOut(BaseModel):
    """一次推演结果 + 与 ACTIVE 的对比（R16.8）。全部确定性、无逐作业明细外的重字段。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    feasibility: str
    late_order_count: int
    active_late_order_count: int
    late_order_count_delta: int
    total_tardiness_minutes: int
    active_total_tardiness_minutes: int
    total_tardiness_delta_minutes: int
    total_score: float
    active_total_score: float
    new_unschedulable_jobs: list[str]
    delayed_order_ids: list[str]


class AdoptScenarioResponse(BaseModel):
    """`POST /scenarios/{id}/adopt` 的响应：新提案句柄（仍待审批，R16.9）。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    plan_id: str
    status: str


class TranslateScenarioRequest(BaseModel):
    """`POST /scenarios/translate` 请求体：一句自然语言 What-if 提问（任务 13.1，R16.1）。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)


class TranslateScenarioResponse(BaseModel):
    """翻译结果（**不执行**，供规划员确认后再交 `/scenarios/run`，R16.1）。

    `mutations` 是任务 8.3 的表单载荷（`ScenarioMutationBody` 序列化）——确认后原样作为
    `RunScenarioRequest.mutations` 提交。`injection_suspected` 供 UI 提示（不阻断，R16.10）。
    """

    model_config = ConfigDict(extra="forbid")

    mutations: list[dict[str, Any]]
    supported_kinds: list[str]
    injection_suspected: bool
    source_query_echo: str


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


def _store(request: Request) -> ScenarioStore:
    """取（或惰性建）挂在 app.state 上的进程内场景暂存。"""
    store = getattr(request.app.state, "scenario_store", None)
    if store is None:
        store = ScenarioStore()
        request.app.state.scenario_store = store
    return store


@router.post(
    "/run",
    response_model=ScenarioResultOut,
    summary="结构化 What-if 推演（R16.7，无 LLM）",
)
def run_scenario_endpoint(
    request: Request, body: RunScenarioRequest, session: PlannerSession
) -> ScenarioResultOut | JSONResponse:
    """在沙箱里确定性推演一个场景并与当前 `ACTIVE` 计划对比。写端点。"""
    factory = request.app.state.session_factory
    now = body.now or DEMO_ANCHOR
    mutations = list(body.mutations)
    store = _store(request)
    with factory() as db:
        try:
            result = run_sandbox(db, mutations=mutations, now=now, store=store)
        except NoActivePlanError:
            return error_response(
                status_code=409,
                code=ErrorCode.NO_ACTIVE_PLAN,
                message="There is no ACTIVE plan, so a what-if simulation cannot be run. Please generate and approve a plan first.",
                next_actions=[NextAction(action="generate_plan", href="/plans/generate")],
            )
        except ScenarioMutationError as error:
            return error_response(
                status_code=422,
                code=ErrorCode.SCENARIO_INVALID_MUTATION,
                message=str(error),
                next_actions=[NextAction(action="fix_scenario", href="/whatif")],
            )
    return ScenarioResultOut(
        scenario_id=result.scenario_id,
        feasibility=result.feasibility,
        late_order_count=result.late_order_count,
        active_late_order_count=result.active_late_order_count,
        late_order_count_delta=result.late_order_count_delta,
        total_tardiness_minutes=result.total_tardiness_minutes,
        active_total_tardiness_minutes=result.active_total_tardiness_minutes,
        total_tardiness_delta_minutes=result.total_tardiness_delta_minutes,
        total_score=result.total_score,
        active_total_score=result.active_total_score,
        new_unschedulable_jobs=list(result.new_unschedulable_jobs),
        delayed_order_ids=list(result.delayed_order_ids),
    )


@router.post(
    "/{scenario_id}/adopt",
    response_model=AdoptScenarioResponse,
    summary="以该场景生成正式提案，仍走审批（R16.9）",
)
def adopt_scenario_endpoint(
    request: Request, scenario_id: str, session: PlannerSession
) -> AdoptScenarioResponse | JSONResponse:
    """采纳一个场景 → 生成 `PENDING_APPROVAL` 提案（仍须审批）。写端点。"""
    factory = request.app.state.session_factory
    store = _store(request)
    with factory() as db:
        try:
            plan_id = adopt_scenario(db, scenario_id=scenario_id, store=store, now=DEMO_ANCHOR)
        except ScenarioNotFoundError:
            return error_response(
                status_code=404,
                code=ErrorCode.SCENARIO_NOT_FOUND,
                message=f"Scenario {scenario_id} does not exist or has expired; please re-run the what-if simulation.",
                next_actions=[NextAction(action="run_scenario", href="/whatif")],
                details={"scenario_id": scenario_id},
            )
    return AdoptScenarioResponse(
        scenario_id=scenario_id, plan_id=plan_id, status="PENDING_APPROVAL"
    )


@router.post(
    "/translate",
    response_model=TranslateScenarioResponse,
    summary="自然语言 What-if 翻译（Planning_Agent ReAct ≤3 步，不执行，R16.1，任务 13.1）",
)
def translate_scenario_endpoint(
    request: Request, body: TranslateScenarioRequest, session: PlannerSession
) -> TranslateScenarioResponse | JSONResponse:
    """把一句自然语言 What-if 提问翻译成结构化场景变更，**不执行**——供规划员确认后再交
    `POST /scenarios/run`（复用任务 8.3 的表单载荷，R16.1）。写端点（触发一次有界 LLM 翻译
    是有意的操作，与运行/采纳同一信任边界）。

    - 无法映射 → `UNSUPPORTED_SCENARIO`（422），`details.supported_kinds` 列出受支持的场景类型
      （R16.3）。
    - 降级模式（`LLM_MODE=DISABLED`）→ `LLM_UNAVAILABLE_USE_STRUCTURED_FORM`（503），前端据此
      隐藏自然语言入口、退回结构化表单（R16 范围说明）。
    - 查询文本按不受信任输入处理（`wrap_untrusted("whatif.query")` + `scan_injection`，R16.10），
      在翻译服务里完成。
    """
    from app.services.whatif_translate import (
        TranslationOutcome,
        translate_whatif_query,
    )

    adapter = request.app.state.llm_adapter
    # `now=DEMO_ANCHOR`：与下方 `run_scenario(now=body.now or DEMO_ANCHOR)` 同一口径——翻译阶段
    # 解析「today / tomorrow」用的参考时间，必须与执行该场景时的「现在」是同一个值。
    result = translate_whatif_query(
        adapter, body.query, now=DEMO_ANCHOR, actor="PLANNER"
    )

    if result.outcome is TranslationOutcome.LLM_UNAVAILABLE:
        return error_response(
            status_code=503,
            code=ErrorCode.LLM_UNAVAILABLE_USE_STRUCTURED_FORM,
            message="The LLM is in degraded mode, so natural-language translation is unavailable. Please use the structured scenario form instead.",
            next_actions=[NextAction(action="use_structured_form", href="/whatif")],
        )
    if result.outcome is TranslationOutcome.UNSUPPORTED_SCENARIO:
        return error_response(
            status_code=422,
            code=ErrorCode.UNSUPPORTED_SCENARIO,
            message=result.reason
            or "Could not map this question to any supported scenario type.",
            next_actions=[NextAction(action="use_structured_form", href="/whatif")],
            details={
                "supported_kinds": list(result.supported_kinds),
                "injection_suspected": result.injection_suspected,
            },
        )
    return TranslateScenarioResponse(
        mutations=result.mutations,
        supported_kinds=list(result.supported_kinds),
        injection_suspected=result.injection_suspected,
        source_query_echo=result.source_query_echo,
    )
