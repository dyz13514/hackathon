"""`Orchestrator.run()` 与 ReAct 循环（任务 5.7，design.md Components §1、Architecture §2.2/§2.3、
Error Handling §3；R21.2/4/5/6/7/13、R23.5、R25.3）。

`Orchestrator` 是**确定性路由器**：它自己不含任何 LLM 逻辑，只按 `ROUTING_TABLE` 把请求分派到
两种执行形态之一，并在入口/出口统一开合 `Trace` 与预算作用域（design.md §1 代码草图）。所有编排
入口都经这一个 `run()` 方法，因此记账、审计、预算三件事对流水线与 ReAct 一视同仁——这正是双执行
形态能共享同一套可观测性底座的原因（design.md §2.2 的共享物表）。

## `run()` 的骨架（design.md §1 逐行）

    route  = ROUTING_TABLE[intent]                     # 纯查表，无 LLM
    trace  = tracer.begin(kind=intent, mode=route.mode, agent=route.agent)
    budget = budget_mgr.open_scope(route.budget_scope, trace.trace_id)   # 无条件调用
    try:
        dispatch by route.mode
    finally:
        tracer.end(trace)
        budget_mgr.close_scope(budget)

`open_scope` 对 12 条入口中的 10 条收到 `None`，返回 no-op 句柄（budget.py 的刻意设计），因此
这里**无需**「这条路要不要记账」的分支——`None` 一路安全地流过 `gate` / `record` / `close_scope`。
`finally` 保证无论正常收尾、步数耗尽、预算耗尽还是内部异常，`Trace` 都被 `end`、作用域都被
`close`（R21.6：终止时保留完整 Trace）。

## `_react_loop` 的三个终止条件（design.md §1 末段 / §2.2 时序图）

1. 模型产出 `final` **且**通过输出契约 schema 校验 → 正常成功（`OK`）。
2. `step == MAX_AGENT_STEPS[agent]` → `MAX_STEPS_EXCEEDED`（R21.6）。
3. 每次 LLM 调用前 `budget.gate(scope) != ALLOW`（即 design.md 草图里的 `budget.exhausted()`）
   → `TOKEN_BUDGET_EXCEEDED`，返回**已完成的确定性结果**（R25.3）。

`MAX_AGENT_STEPS = {INGESTION_AGENT: 6, PLANNING_AGENT: 8, RISK_MONITOR_AGENT: 6}`（R21.5）。
这两条护栏是独立的：步数上限在算术上封住调用次数，token 上限直接对应 K-10/K-16。在重排路径上
token 上限通常先咬住，步数上限兜底（design.md 成本章节 §2）。

## 循环内错误不抛到外层（Error Handling §3）

Agent 循环里的错误（非法 JSON、越权工具、输入 schema 不符、契约违反、handler 内部异常）**不抛
到外层**，而是作为「观察结果」回给模型，让它有一次自我修正的机会（R22.2 的原意）。每类错误都
消耗一步；**连续发生 2 次后终止**（Error Handling §3 表最后一列）。终止时按各路径的确定性收尾
返回已完成的成果，并一律保留完整 `Trace`（R21.6）。

「连续 2 次」的计数口径：任意一次成功推进（工具调用成功、或产出可解析且合规的一步）把连续错误
计数清零；连续两步都以错误观察收尾则终止。这既给了模型自我修正的机会，又不让一个反复出错的
模型把步数烧到上限（那也会终止，但更晚、更贵）。

## 为什么用注入的 seam，而不在此 import Bedrock / DB

本任务要能**独立测试**——终止性单元测试（承接原属性 23）用 `adversarial_agent_outputs` 固化后
的用例集喂进来，断言循环在任意模型行为下必然终止。因此：

- **`AgentDriver`**（LLM + 上下文装配的每轮产物）是一个协议：真实实现串起 `Context_Manager` +
  `Bedrock_Adapter`（接线属重排/列映射路径的落地），测试注入一个「按脚本吐字符串」的桩。循环
  逻辑因此不依赖网络。
- **`Tracer`**（`Trace` 的开合与逐步记录）是一个协议：完整的 `Trace_Recorder`（写 `traces` /
  `trace_steps`）归任务 5.12，本任务提供一个内存实现 `InMemoryTracer` 让 `run()` 现在就可跑、
  可测，5.12 落地时替换实例即可，`run()` 一字不改（与 `Bedrock_Adapter` 的 `BudgetRecorder`
  接缝同一思路）。
- **`Tool_Registry`** 已是注入的（任务 5.1），循环通过它调用工具，白名单/schema/记账全在其
  7 步闸门里，编排层不重复。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from pydantic import BaseModel, ValidationError

from app.agents.contracts import AgentContract
from app.llm.budget import BudgetScopeHandle, GateDecision, TokenBudgetManager
from app.orchestrator.context_manager import (
    AgentContext,
    ObservationRecord,
    SessionState,
)
from app.orchestrator.routing import ROUTING_TABLE, Intent, Mode, Route
from app.orchestrator.tracing import InMemoryTracer, TraceHandle, Tracer
from app.services.guardrail import validate_agent_output
from app.tools.registry import CallerId, ToolContext, ToolRegistry, ToolResult

__all__ = [
    "MAX_AGENT_STEPS",
    "AgentDriver",
    "AgentTurn",
    "InMemoryTracer",
    "Orchestrator",
    "OrchestratorResult",
    "RouteNotWiredError",
    "TraceHandle",
    "Tracer",
]


#: 每个 Agent 单次运行的步数上限（R21.5，design.md §1 末段）。它在算术上封住一次 ReAct 运行
#: 的 LLM 调用次数——即使模型永不产出 `final`，循环也必然在此步数处终止。
MAX_AGENT_STEPS: Final[Mapping[str, int]] = {
    "INGESTION_AGENT": 6,
    "PLANNING_AGENT": 8,
    "RISK_MONITOR_AGENT": 6,
}

#: 连续错误观察达此次数即终止（Error Handling §3 表「连续发生 2 次后」列）。
_CONSECUTIVE_ERROR_LIMIT: Final = 2


# --------------------------------------------------------------------------
# 每轮模型产物（AgentDriver 的返回）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTurn:
    """一轮模型输出的**原始**产物：模型自己吐出的字符串（design.md §2.2 / ADR-005）。

    ReAct 协议约定模型每轮只输出一个 JSON 对象，形如
    `{"thought": "...", "action": {"tool": "...", "args": {...}}}` 或
    `{"thought": "...", "final": {...}}`。本类只承载**未解析的原始字符串** `raw`——解析、
    协议校验、契约校验全在 `_react_loop` 里做，因为「模型可能吐出任何东西」正是终止性要防的
    对抗面（非法 JSON、缺 action/final、越权工具、契约违反）。把解析放在循环里，桩驱动只需
    「按脚本返回一个字符串」，与真实驱动（LLM 原样返回文本）完全同构。
    """

    raw: str


class AgentDriver(Protocol):
    """产出「下一轮模型输出」的 seam。

    真实实现：用 `Context_Manager.assemble_messages(ctx, prefix=...)` 装配请求，交
    `Bedrock_Adapter.invoke()`，返回响应文本包成 `AgentTurn`。接线在重排/列映射路径的落地
    任务里；本任务只依赖这个协议，因此循环逻辑与 LLM 出口解耦、可用桩测试。

    `next_turn` 接收累积到当前步的 `AgentContext`（含全部观察历史）——真实驱动据此装配上下文，
    桩驱动可忽略它按脚本产出。返回 `None` 表示驱动主动结束（真实实现一般不会，桩用它模拟
    「模型停止响应」这种边界；循环遇 `None` 视作一次错误观察）。
    """

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None: ...


# --------------------------------------------------------------------------
# Trace seam（`TraceHandle` / `Tracer` / `InMemoryTracer` 抽到 `tracing.py` 以打破
# 一条 import 环，见 `tracing.py` 模块 docstring；此处再导出保持公开面不变）
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 结果值对象
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorResult:
    """一次 `run()` 的结果（design.md §1 `OrchestratorResult`）。

    `outcome` 取 `traces.outcome` 的取值域：`OK` / `MAX_STEPS_EXCEEDED` /
    `TOKEN_BUDGET_EXCEEDED` / `VALIDATION_FAILED` / `ERROR`（design.md Data Models §7 的
    `outcome` 列）。`final` 是成功时经契约校验的输出（`AgentContract` 实例）或 `None`。
    `trace_id` 让调用方能把结果关联回 `Trace`（R24.5）。
    """

    outcome: str
    trace_id: str
    final: AgentContract | None = None
    #: 供 UI / 上层展示的错误码（非 OK 时非空），取自 Error Handling §2 的 `ErrorCode`。
    error_code: str | None = None


class RouteNotWiredError(NotImplementedError):
    """请求的路由在 P0 存在于 `ROUTING_TABLE`，但其执行体尚未接线。

    这不是可回给模型的软错误，而是「这条路 P0 不该被 `Orchestrator` 直接跑」的编排信号：
    - `Mode.PIPELINE` 的流水线入口接线属后续任务（`Route.pipeline` 恒为 None）；
    - `Mode.NONE` 的服务入口（审批/导出）由各自服务直接处理，不经 ReAct；
    - `Mode.REACT` 的 P1 路线（WHATIF_NL / RISK_NARRATION / DISTIL_PREFERENCE）在 P0 不接线。

    以显式异常拒绝，胜过静默走一条空路径——后者会让「P0 跑了一条 P1 路线」这种错误无声通过。
    调用方（API 边界）负责只用已接线的意图，或提供对应的 `agent_driver`。
    """


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


class Orchestrator:
    """确定性路由器 + ReAct 循环宿主（design.md Components §1）。

    构造时注入四个 seam：`ToolRegistry`（工具调用的 7 步闸门）、`TokenBudgetManager`（预算记账
    与闸门）、`Tracer`（Trace 开合）、以及一个「意图/Agent → `AgentDriver`」的映射（哪条 ReAct
    路径由哪个驱动产出模型输出）。`agent_drivers` 缺省为空——只跑 `Mode.PIPELINE` / `Mode.NONE`
    或做纯路由的调用方无需提供；跑某条 ReAct 路径时必须为其 Agent 注入驱动，否则 `run()` 以
    `RouteNotWiredError` 拒绝（而不是假装能跑）。
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        budget: TokenBudgetManager,
        tracer: Tracer,
        agent_drivers: Mapping[str, AgentDriver] | None = None,
        contracts: Mapping[str, type[AgentContract]] | None = None,
    ) -> None:
        self._registry = registry
        self._budget = budget
        self._tracer = tracer
        self._agent_drivers = dict(agent_drivers or {})
        # Agent → 输出契约（final 的 schema 校验用）。缺省空——由跑 ReAct 的调用方注入，与
        # `agent_drivers` 成对提供（每个被驱动的 Agent 都要有它的输出契约）。
        self._contracts = dict(contracts or {})

    def run(
        self, intent: Intent, payload: BaseModel, session_id: str
    ) -> OrchestratorResult:
        """全部编排入口的唯一方法（design.md §1）。

        次序严格照草图：查表 → 开 Trace → 无条件 `open_scope` → 按 `mode` 分派 →
        `finally` 中 `tracer.end` + `budget.close_scope`。`open_scope` 收到 `None`（10 条
        入口）返回 no-op 句柄，因此这里没有「要不要记账」的分支。
        """
        route: Route = ROUTING_TABLE[intent]
        trace = self._tracer.begin(kind=intent.value, mode=route.mode.value, agent=route.agent)
        # route.budget_scope 对 10 条入口是 None；open_scope 接受 None 并返回 no-op 句柄，
        # 因此这里无条件调用、无需分支（design.md §1 注释）。
        budget = self._budget.open_scope(route.budget_scope, trace.trace_id)
        try:
            if route.mode is Mode.PIPELINE:
                # 形态 A 流水线入口在本任务不接线（Route.pipeline 恒为 None）。计划生成走
                # `pipelines/plan_generation.py` 的专用入口，其签名与 (payload, trace, budget)
                # 草图不同，接线属后续任务。
                raise RouteNotWiredError(
                    f"{intent.value} 是 Mode.PIPELINE，其流水线入口未接线到 Orchestrator"
                )
            if route.mode is Mode.REACT:
                return self._react_loop(
                    route.agent, payload, trace, budget, session_id=session_id
                )
            # Mode.NONE：审批 / 导出 / 风险扫描等纯服务入口不经 ReAct（见 RouteNotWiredError）。
            raise RouteNotWiredError(
                f"{intent.value} 是 Mode.NONE，由对应服务直接处理，不经 Orchestrator 执行体"
            )
        finally:
            self._tracer.end(trace)
            self._budget.close_scope(budget)

    # -- ReAct 循环 -------------------------------------------------------

    def _react_loop(
        self,
        agent: str | None,
        payload: BaseModel,
        trace: TraceHandle,
        budget: BudgetScopeHandle | None,
        *,
        session_id: str,
    ) -> OrchestratorResult:
        """Reason/Act/Observe 循环（design.md §2.2 时序图、Error Handling §3）。

        终止条件（三者任一，见模块 docstring）：`final` 通过 schema 校验 / 步数达上限 /
        预算耗尽。循环内错误作为观察结果回给模型，连续 2 次后终止。终止时一律保留完整 Trace。
        """
        assert agent is not None  # Route.__post_init__ 已保证 REACT 有 agent
        max_steps = MAX_AGENT_STEPS[agent]
        driver = self._agent_drivers.get(agent)
        contract = self._contracts.get(agent)
        if driver is None or contract is None:
            # ReAct 路径必须为其 Agent 注入驱动与输出契约（P1 路线在 P0 不接线即走这里）。
            raise RouteNotWiredError(
                f"Agent {agent} 的 ReAct 驱动或输出契约未注入（P0 不接线该路径）"
            )

        caller: CallerId = agent  # type: ignore[assignment]  # 三个 Agent 名同属 CallerId
        state = SessionState(session_id=session_id)
        observations: list[ObservationRecord] = []
        consecutive_errors = 0

        for step in range(max_steps):
            # ---- 终止条件 3：预算耗尽（每次 LLM 调用前问一次 gate，即 budget.exhausted()）----
            if self._budget.gate(budget) is not GateDecision.ALLOW:
                self._tracer.record_step(
                    trace,
                    step_kind="GUARDRAIL",
                    outcome="TOKEN_BUDGET_EXCEEDED",
                    detail="预算耗尽，返回已完成的确定性结果（R25.3）",
                )
                trace.outcome = "TOKEN_BUDGET_EXCEEDED"
                return OrchestratorResult(
                    outcome="TOKEN_BUDGET_EXCEEDED",
                    trace_id=trace.trace_id,
                    error_code="TOKEN_BUDGET_EXCEEDED",
                )

            ctx = AgentContext(
                agent=agent,  # type: ignore[arg-type]
                task_block=_task_block_of(payload),
                running_state=state,
                observations=list(observations),
            )

            # ---- Reason：取下一轮模型输出（seam；真实实现串 Context_Manager + Bedrock）----
            turn = driver.next_turn(ctx)

            # ---- 解析 + 分派，得到本步的观察（成功或错误）----
            step_result = self._process_turn(
                turn, caller=caller, contract=contract, trace=trace, step=step
            )

            if step_result.final is not None:
                # ---- 终止条件 1：final 通过 schema 校验 → 正常成功 ----
                trace.outcome = "OK"
                return OrchestratorResult(
                    outcome="OK", trace_id=trace.trace_id, final=step_result.final
                )

            # 记一条观察回给下一轮模型（成功的工具结果，或错误观察）。
            observations.append(step_result.observation)

            if step_result.is_error:
                consecutive_errors += 1
                if consecutive_errors >= _CONSECUTIVE_ERROR_LIMIT:
                    # ---- 连续 2 次错误 → 终止（Error Handling §3）----
                    trace.outcome = step_result.terminal_outcome or "VALIDATION_FAILED"
                    return OrchestratorResult(
                        outcome=trace.outcome,
                        trace_id=trace.trace_id,
                        error_code=step_result.error_code,
                    )
            else:
                consecutive_errors = 0  # 任意成功推进清零连续错误计数

        # ---- 终止条件 2：步数达上限 → MAX_STEPS_EXCEEDED（R21.6）----
        self._tracer.record_step(
            trace,
            step_kind="GUARDRAIL",
            outcome="MAX_STEPS_EXCEEDED",
            detail=f"step 达上限 {max_steps}（R21.6）",
        )
        trace.outcome = "MAX_STEPS_EXCEEDED"
        return OrchestratorResult(
            outcome="MAX_STEPS_EXCEEDED",
            trace_id=trace.trace_id,
            error_code="MAX_STEPS_EXCEEDED",
        )

    def _process_turn(
        self,
        turn: AgentTurn | None,
        *,
        caller: CallerId,
        contract: type[AgentContract],
        trace: TraceHandle,
        step: int,
    ) -> _StepOutcome:
        """解析一轮模型输出并分派，返回本步结果（成功观察 / 错误观察 / final）。

        遵循 Error Handling §3：每类错误都作为观察结果回给模型（消耗一步），不抛到外层。
        `final` 经输出契约（`extra="forbid"`）校验通过才算成功；校验失败按
        `AGENT_OUTPUT_CONTRACT_VIOLATION` 处理。
        """
        # 驱动主动结束（真实实现罕见；桩用它模拟模型停止响应）视作一次错误观察。
        if turn is None:
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_NOT_JSON",
                observation="上一轮无输出，请只输出一个 JSON 对象",
                terminal_outcome="VALIDATION_FAILED",
            )

        # ① 解析 JSON —— 失败 → AGENT_OUTPUT_NOT_JSON（Error Handling §3）
        try:
            parsed = json.loads(turn.raw)
        except (ValueError, json.JSONDecodeError):
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_NOT_JSON",
                observation="上一轮输出不是合法 JSON，请只输出 JSON 对象",
                terminal_outcome="VALIDATION_FAILED",
            )
        if not isinstance(parsed, dict):
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_NOT_JSON",
                observation="上一轮输出不是 JSON 对象，请只输出一个对象",
                terminal_outcome="VALIDATION_FAILED",
            )

        # ② 协议形状：必须含 `final` 或 `action` 之一（design.md §2.2 / ADR-005 的两种形态）
        if "final" in parsed:
            return self._validate_final(parsed["final"], contract=contract, trace=trace, step=step)
        if "action" not in parsed:
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_CONTRACT_VIOLATION",
                observation='每轮必须输出 {"action": {...}} 或 {"final": {...}} 之一',
                terminal_outcome="VALIDATION_FAILED",
            )

        # ③ 分派工具调用（经 Tool_Registry 的 7 步闸门；白名单/schema/记账全在那里）
        return self._dispatch_action(parsed["action"], caller=caller, trace=trace, step=step)

    def _dispatch_action(
        self, action: Any, *, caller: CallerId, trace: TraceHandle, step: int
    ) -> _StepOutcome:
        """执行一次 `action`（工具调用），把结果转成观察（Error Handling §3）。

        工具的白名单、输入 schema、执行、输出 schema、投影、截断、记账全部由
        `ToolRegistry.invoke` 的 7 步闸门完成——编排层不重复任何一道。`invoke` 从不抛异常
        （成功与失败都是 `ToolResult`），因此这里只看 `result.ok` 决定观察是成功还是错误。
        """
        if not isinstance(action, dict) or "tool" not in action:
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_CONTRACT_VIOLATION",
                observation='action 必须形如 {"tool": "...", "args": {...}}',
                terminal_outcome="VALIDATION_FAILED",
            )
        tool_name = action["tool"]
        raw_args = action.get("args") or {}
        if not isinstance(tool_name, str) or not isinstance(raw_args, dict):
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_CONTRACT_VIOLATION",
                observation="tool 必须是字符串，args 必须是对象",
                terminal_outcome="VALIDATION_FAILED",
            )

        ctx = ToolContext(trace_id=trace.trace_id, step_id=None)
        result: ToolResult = self._registry.invoke(caller, tool_name, raw_args, ctx)

        if result.ok:
            self._tracer.record_step(
                trace, step_kind="TOOL_CALL", outcome="OK", detail=tool_name
            )
            payload_json = json.dumps(result.payload, ensure_ascii=False, sort_keys=True)
            return _StepOutcome(
                observation=ObservationRecord(
                    step_index=step,
                    tool_name=tool_name,
                    outcome="OK",
                    payload_json=payload_json,
                    payload_tokens=result.tokens,
                ),
                is_error=False,
            )

        # 工具失败：作为错误观察回给模型（TOOL_NOT_PERMITTED / TOOL_INPUT_INVALID / ...）。
        # 越权（TOOL_NOT_PERMITTED）的安全审计已由 registry 的白名单闸门写下（R22.10）。
        code = result.error_code or "ERROR"
        terminal = "VALIDATION_FAILED" if code == "TOOL_INPUT_INVALID" else code
        self._tracer.record_step(
            trace, step_kind="TOOL_CALL", outcome=code, detail=tool_name
        )
        return _StepOutcome(
            observation=ObservationRecord(
                step_index=step,
                tool_name=tool_name,
                outcome="ERROR",
                payload_json=_observation_for_tool_error(code, result.error_detail),
                payload_tokens=0,
            ),
            is_error=True,
            error_code=code,
            terminal_outcome=terminal,
        )

    def _validate_final(
        self, final_obj: Any, *, contract: type[AgentContract], trace: TraceHandle, step: int
    ) -> _StepOutcome:
        """剥离保留键后用输出契约校验 `final`（Guardrail_Layer (c)，任务 5.9，design.md §2.7(c)）。

        两步，顺序固定（R13.11 在前、R23.5 在后）：

        1. `validate_agent_output` 递归剥离 `RESERVED_KEYS`（`impact_class` / `autonomy_level`
           / `plan_status` / `approved` / `feasibility` / `start_time` / `end_time`）——模型
           无权在 `final` 里声称这些字段，命中即静默剪掉并写 `AGENT_RESERVED_KEY_DROPPED`
           审计（R13.11，EVAL-209 的第二道防线；第一道是任务 7.3 的结构隔离）。**先剥离再
           校验**：否则一个混入 `impact_class` 的合法输出会被 `extra="forbid"` 以「多字段」
           整体拒掉，而 R13.11 要的是丢掉那个字段、放行其余（见 guardrail.py §(c) docstring）。
        2. 剥离后用契约（`extra="forbid"`）校验；多字段/缺字段/类型不符抛 `ValidationError`，
           按 R21.6 转成 `AGENT_OUTPUT_CONTRACT_VIOLATION` 的错误观察回给模型。

        校验通过才算终止条件 1（正常成功）。审计写入走 `Trace` 的 `trace_id`（R24.5）；`agent`
        取本 Trace 的 `agent`（哪个 Agent 试图声称保留键）。
        """
        try:
            validated = validate_agent_output(
                final_obj,
                contract,
                agent=trace.agent or "UNKNOWN_AGENT",
                trace_id=trace.trace_id,
            )
        except ValidationError as exc:
            return self._error_step(
                trace, step, code="AGENT_OUTPUT_CONTRACT_VIOLATION",
                observation=f"final 不符合输出契约：{exc.error_count()} 处错误",
                terminal_outcome="VALIDATION_FAILED",
            )
        self._tracer.record_step(
            trace, step_kind="GUARDRAIL", outcome="OK", detail="final 通过契约校验"
        )
        return _StepOutcome(observation=_EMPTY_OBS, is_error=False, final=validated)

    def _error_step(
        self,
        trace: TraceHandle,
        step: int,
        *,
        code: str,
        observation: str,
        terminal_outcome: str,
    ) -> _StepOutcome:
        """构造一条错误观察并记一步 Trace（Error Handling §3：错误作为观察，消耗一步）。"""
        self._tracer.record_step(
            trace, step_kind="GUARDRAIL", outcome=code, detail=observation
        )
        return _StepOutcome(
            observation=ObservationRecord(
                step_index=step,
                tool_name="<agent_output>",
                outcome="ERROR",
                payload_json=observation,
                payload_tokens=0,
            ),
            is_error=True,
            error_code=code,
            terminal_outcome=terminal_outcome,
        )


# --------------------------------------------------------------------------
# 内部：一步的结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _StepOutcome:
    """`_process_turn` 一步的结果：成功观察 / 错误观察 / final（三者互斥地表达）。"""

    observation: ObservationRecord
    is_error: bool
    final: AgentContract | None = None
    error_code: str | None = None
    terminal_outcome: str | None = None


#: `final` 成功时不需要观察（循环立即返回，从不把它 append 进历史），用一条占位观察保持
#: `_StepOutcome` 非可选。`step_index=0` 只为满足 `ObservationRecord` 的 `ge=0` 约束——它
#: 永不进入任何上下文，取值无语义。
_EMPTY_OBS: Final = ObservationRecord(
    step_index=0, tool_name="<final>", outcome="OK", payload_json="{}"
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _task_block_of(payload: BaseModel) -> str:
    """把入口载荷渲成本次运行的目标陈述（`AgentContext.task_block`）。

    用契约声明字段的规范 JSON——载荷是**已校验的 Pydantic 实例**（`run` 的签名要求
    `BaseModel`），因此这里只序列化声明字段，模型多吐的东西不会进 task。真实驱动会在装配
    上下文时把它放进 `<task>` 块。
    """
    return json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _observation_for_tool_error(code: str, detail: Any) -> str:
    """把工具错误转成回给模型的观察文本（Error Handling §3 表「回给模型的观察内容」列）。

    每类错误给模型一句可据以自我修正的话。`TOOL_NOT_PERMITTED` 刻意**不**回列出可用工具的
    完整清单以外的信息（那已在 [TOOLS] 段里），只提示越权；`TOOL_INPUT_INVALID` 回字段级
    校验错误让模型能改参数。
    """
    if code == "TOOL_NOT_PERMITTED":
        return "该工具不在你的权限范围内，可用工具见 TOOLS 段"
    if code == "TOOL_INPUT_INVALID":
        return json.dumps(
            {"outcome": "ERROR", "code": code, "errors": _safe_detail(detail)},
            ensure_ascii=False,
            sort_keys=True,
        )
    if code == "TOOL_TIMEOUT":
        return json.dumps(
            {"outcome": "ERROR", "code": code, "message": "工具执行超时"},
            ensure_ascii=False,
        )
    # handler 内部异常等：脱敏摘要（不泄露栈信息，Error Handling §1 FATAL 处置）。
    return json.dumps(
        {"outcome": "ERROR", "code": code, "message": "工具执行失败"},
        ensure_ascii=False,
    )


def _safe_detail(detail: Any) -> Any:
    """把错误明细收敛成可 JSON 序列化的形式（pydantic 的 `errors()` 是 list[dict]，可直接用）。"""
    try:
        json.dumps(detail, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(detail)
    return detail
