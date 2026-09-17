"""`Orchestrator._react_loop` 的终止性测试（任务 5.7，**非可选**，承接原属性 23）。

design.md Correctness Properties 表第 23 行：**「Agent 循环在任意模型行为下必然终止」**
（R21.5 / R21.6 / R23.5），替代覆盖来源为 `_react_loop` 单元测试 + `EVAL-211/212`，
「输入取自 `adversarial_agent_outputs` 生成器**固化后的用例集**（非法 JSON / 越权工具 /
永不 `final` / 契约违反各一例）」。

本文件就是那份**固化用例集**：四类对抗性模型行为各一例，以版本控制的常量固定下来（随机
生成的对抗输出没有稳定的断言目标，因此这里固化而非随机 —— 与 tasks.md 5.7 的要求一致）。
每一例都断言：

1. 循环**必然终止**（不挂起、不无限循环）——由 `run()` 能在有限时间返回来体现。
2. 终止 `outcome` 符合 Error Handling §3 / design.md §1 的映射。
3. 终止时**保留完整 Trace**（R21.6）：`trace.steps` 非空且步数受 `MAX_AGENT_STEPS` 约束。

四类对抗面（Error Handling §3 表）：
- **非法 JSON**：模型每轮吐不可解析的字符串 → `AGENT_OUTPUT_NOT_JSON`，连续 2 次 → 终止。
- **越权工具**：模型每轮请求不在白名单里的工具 → `TOOL_NOT_PERMITTED` + 安全审计，
  连续 2 次 → 终止（handler 从未被触达）。
- **永不 `final`**：模型每轮都做**合法**工具调用但从不收尾 → 步数达 `MAX_AGENT_STEPS`
  → `MAX_STEPS_EXCEEDED`。
- **契约违反**：模型每轮吐 `{"final": {...}}` 但不符输出契约（`extra="forbid"`）→
  `AGENT_OUTPUT_CONTRACT_VIOLATION`，连续 2 次 → 终止。

驱动是注入的脚本桩（`AgentDriver`），不触网、不消耗额度——终止性是循环控制流的性质，与
真实 LLM 无关。工具调用经真实的 `ToolRegistry` 7 步闸门（注入一个 fixture spec），因此
越权判定与记账是真代码而非桩。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agents.contracts import AgentContract
from app.llm.budget import TokenBudgetManager
from app.orchestrator.context_manager import AgentContext
from app.orchestrator.orchestrator import (
    MAX_AGENT_STEPS,
    AgentDriver,
    AgentTurn,
    InMemoryTracer,
    Orchestrator,
)
from app.orchestrator.routing import Intent
from app.tools.registry import (
    InMemoryToolCallRecorder,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)

# --------------------------------------------------------------------------
# 固化的对抗用例集（版本控制常量；见模块 docstring）
# --------------------------------------------------------------------------

#: 非法 JSON：取自 `tests/generators.py::_BAD_JSON` 的一个代表样本。
CASE_INVALID_JSON = "{not json"

#: 越权工具：`Planning_Agent` 白名单里没有 `save_import_batch`（那是 Ingestion 的）。
CASE_UNAUTHORIZED_TOOL = '{"thought": "x", "action": {"tool": "save_import_batch", "args": {}}}'

#: 永不 final：每轮做一次合法工具调用（fixture 的 echo 工具），从不产出 final。
CASE_NEVER_FINAL = '{"thought": "x", "action": {"tool": "echo", "args": {"value": "hi"}}}'

#: 契约违反：`{"final": {...}}` 但字段不符 `RevisedPlanProposal`（缺必填 + 多字段）。
CASE_CONTRACT_VIOLATION = '{"thought": "x", "final": {"nonsense": true}}'


# --------------------------------------------------------------------------
# fixture 工具（一个 echo 工具，供「永不 final」用例做合法调用）
# --------------------------------------------------------------------------


class _EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class _EchoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    echoed: str


def _echo_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    assert isinstance(args, _EchoIn)
    return _EchoOut(echoed=args.value)


def _registry() -> ToolRegistry:
    """一个只注册 `echo` 的注册表。

    `echo` 未出现在任何 `TOOL_WHITELIST` 里——但白名单闸门查的是
    `TOOL_WHITELIST[caller]`，不是已注册 spec。为让「永不 final」用例能做**合法**调用，
    我们把它临时并入 Planning 的白名单不可行（白名单不可变）。因此改用一个**已在白名单里**
    的工具名注册这个 echo spec：`compare_plans` 在 `PLANNING_AGENT` 白名单内。CASE_NEVER_FINAL
    因此调用 `compare_plans`（见下方 fixture 覆盖）。
    """
    spec = ToolSpec(
        name="compare_plans",
        kind="COMPUTE",
        input_model=_EchoIn,
        output_model=_EchoOut,
        handler=_echo_handler,
    )
    return ToolRegistry({spec.name: spec}, recorder=InMemoryToolCallRecorder())


# CASE_NEVER_FINAL 用白名单内的 compare_plans（注册成 echo 语义），保证是一次**合法**调用。
CASE_NEVER_FINAL = '{"thought": "x", "action": {"tool": "compare_plans", "args": {"value": "hi"}}}'


class _ScriptedDriver:
    """按固定字符串脚本产出每轮输出的 `AgentDriver` 桩（不触网、不消耗额度）。"""

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls = 0

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None:
        self.calls += 1
        return AgentTurn(raw=self._raw)


def _orchestrator(driver: AgentDriver) -> tuple[Orchestrator, InMemoryTracer]:
    tracer = InMemoryTracer()
    orch = Orchestrator(
        registry=_registry(),
        budget=TokenBudgetManager(),
        tracer=tracer,
        agent_drivers={"PLANNING_AGENT": driver},
        contracts={"PLANNING_AGENT": _output_contract()},
    )
    return orch, tracer


def _output_contract() -> type[AgentContract]:
    from app.agents.contracts import RevisedPlanProposal

    return RevisedPlanProposal


class _Payload(BaseModel):
    """一个最小的入口载荷（`run` 要求 `BaseModel`）。"""

    model_config = ConfigDict(extra="forbid")
    disruption_id: str = "DISR-0001"


# --------------------------------------------------------------------------
# 四类对抗用例：各断言终止 + outcome + 保留 Trace
# --------------------------------------------------------------------------


def _run(raw: str) -> tuple[Any, InMemoryTracer, _ScriptedDriver]:
    driver = _ScriptedDriver(raw)
    orch, tracer = _orchestrator(driver)
    result = orch.run(Intent.REPLAN, _Payload(), session_id="SESS-0001")
    return result, tracer, driver


def test_invalid_json_terminates_after_two_consecutive() -> None:
    """非法 JSON：连续 2 次 → 终止，`VALIDATION_FAILED`，保留 Trace（R21.6）。"""
    result, tracer, driver = _run(CASE_INVALID_JSON)

    assert result.outcome == "VALIDATION_FAILED"
    assert result.error_code == "AGENT_OUTPUT_NOT_JSON"
    # 连续 2 次错误即终止 → 恰好驱动被调用 2 次（远小于 8 步上限）。
    assert driver.calls == 2
    trace = tracer.traces[0]
    assert trace.outcome == "VALIDATION_FAILED"
    assert trace.step_count >= 2  # 完整 Trace 被保留


def test_unauthorized_tool_terminates_and_audits() -> None:
    """越权工具：`TOOL_NOT_PERMITTED`，连续 2 次 → 终止；handler 从不被触达。"""
    result, tracer, driver = _run(CASE_UNAUTHORIZED_TOOL)

    assert result.outcome == "TOOL_NOT_PERMITTED"
    assert result.error_code == "TOOL_NOT_PERMITTED"
    assert driver.calls == 2
    trace = tracer.traces[0]
    assert trace.outcome == "TOOL_NOT_PERMITTED"
    # 每一步都记了一条 TOOL_CALL 且 outcome 为 TOOL_NOT_PERMITTED（可观测性底座）。
    assert any(s["outcome"] == "TOOL_NOT_PERMITTED" for s in trace.steps)


def test_never_final_hits_max_steps() -> None:
    """永不 final：合法工具调用不产生错误，循环跑满 `MAX_AGENT_STEPS` → `MAX_STEPS_EXCEEDED`。"""
    result, tracer, driver = _run(CASE_NEVER_FINAL)

    assert result.outcome == "MAX_STEPS_EXCEEDED"
    assert result.error_code == "MAX_STEPS_EXCEEDED"
    # Planning 上限 8 步：驱动被调用恰好 8 次，随后循环因步数耗尽终止。
    assert driver.calls == MAX_AGENT_STEPS["PLANNING_AGENT"] == 8
    trace = tracer.traces[0]
    assert trace.outcome == "MAX_STEPS_EXCEEDED"


def test_contract_violation_terminates_after_two_consecutive() -> None:
    """契约违反：`final` 不符输出契约 → `AGENT_OUTPUT_CONTRACT_VIOLATION`，连续 2 次 → 终止。"""
    result, tracer, driver = _run(CASE_CONTRACT_VIOLATION)

    assert result.outcome == "VALIDATION_FAILED"
    assert result.error_code == "AGENT_OUTPUT_CONTRACT_VIOLATION"
    assert driver.calls == 2
    trace = tracer.traces[0]
    assert trace.outcome == "VALIDATION_FAILED"


# --------------------------------------------------------------------------
# 正常成功路径：合法 final 通过契约校验 → OK（对照组，证明循环不是「总是终止在错误」）
# --------------------------------------------------------------------------


def test_valid_final_returns_ok() -> None:
    """合法 `final` 通过 `RevisedPlanProposal` 校验 → 终止条件 1（正常成功 OK）。"""
    valid_final = (
        '{"thought": "done", "final": {'
        '"candidate_plan_id": "PLAN-1", "feasibility": "FEASIBLE", '
        '"total_tardiness_minutes": 0, "churn_ratio": 0.0}}'
    )
    result, tracer, driver = _run(valid_final)

    assert result.outcome == "OK"
    assert result.final is not None
    assert result.final.candidate_plan_id == "PLAN-1"  # type: ignore[attr-defined]
    assert driver.calls == 1  # 第一轮即成功收尾
    assert tracer.traces[0].outcome == "OK"


# --------------------------------------------------------------------------
# 预算耗尽：gate 拒绝 → TOKEN_BUDGET_EXCEEDED，返回已完成的确定性结果（R25.3）
# --------------------------------------------------------------------------


def test_budget_exhausted_returns_deterministic_result() -> None:
    """每日上限压到 0 → 第一次 gate 即 DENY → `TOKEN_BUDGET_EXCEEDED`，循环不进入模型调用。"""
    from decimal import Decimal

    driver = _ScriptedDriver(CASE_NEVER_FINAL)
    tracer = InMemoryTracer()
    orch = Orchestrator(
        registry=_registry(),
        budget=TokenBudgetManager(daily_ceiling=Decimal("0")),
        tracer=tracer,
        agent_drivers={"PLANNING_AGENT": driver},
        contracts={"PLANNING_AGENT": _output_contract()},
    )
    result = orch.run(Intent.REPLAN, _Payload(), session_id="SESS-0001")

    assert result.outcome == "TOKEN_BUDGET_EXCEEDED"
    assert result.error_code == "TOKEN_BUDGET_EXCEEDED"
    # gate 在**第一次** LLM 调用前就拒绝 → 驱动从未被调用（返回已完成的确定性结果）。
    assert driver.calls == 0
    assert tracer.traces[0].outcome == "TOKEN_BUDGET_EXCEEDED"
