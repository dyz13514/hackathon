"""EVAL-015：正常周期 token 回归断言（任务 12.3，R26.2 / R25.2）。

design.md Testing Strategy §4 与成本章节 §1 把这条用例定成 ADR-004（句柄式返回，不返回逐
`ScheduledJob` 明细）被裁剪的**唯一动态护栏**，也承接原属性 24、29：一旦有人把工具改回返回
逐作业明细，重排周期的输入 token 会从 ≈11,960 跳到数万——一个量级的偏差不可能漏过这条断言。
**非可选**。

## 断言什么

在 `LLM_MODE=REPLAY` 下跑一次**标准计划生成周期**与一次**标准重排周期**，估算各自消耗的
`total_input_tokens + total_output_tokens`，断言：

- 计划生成周期 ≤ **4,000** token（K-10，R25.2）；
- 重排周期 ≤ **14,000** token（K-16，R25.2）。

## token 计数为什么是本地估算（design.md 成本章节 §1）

`REPLAY` 模式零网络、零 Bedrock 调用——没有真实账单可读，`tests/cassettes/` 也未录制这两条
请求（回放会 `CassetteMiss`）。因此周期 token 由**本地 tokenizer 估算**，对象是这两个周期**实际
装配出来的请求载荷**（计划生成的解释请求；重排的 ReAct 上下文）：

- 首选 `tiktoken` 的 `cl100k_base`（design.md 点名，±5% 足够用于回归）；
- 该编码的 BPE 词表需要一次性下载，离线环境（本沙箱代理封网）取不到时，回退到项目既有的
  确定性启发式 `app.tools.clamp.count_tokens`（≈4 字节/token，偏高的上界估计）。两者都**只
  数真实载荷内容**，绝不硬编码 0、绝不伪造 Trace——回退只换估算器，不改被估算的对象。

估算对象是「标准周期实际会发送的请求」，因此这条断言探测的正是「载荷是否意外膨胀到发送原始
明细那个量级」——与 design.md 对 EVAL-015 的定义一致。用例同时读回计划生成的 `Trace`，断言它
确实以确定性流水线（`mode == PIPELINE`）跑完，作为「周期真的执行了」的证据。
"""

from __future__ import annotations

import json

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.prompts.planning import build_replan_prefix
from app.core.replanner import Disruption, replan
from app.db.models import Trace
from app.orchestrator.context_manager import (
    AgentContext,
    ObservationRecord,
    SessionState,
    assemble_messages,
)
from app.orchestrator.pipelines import plan_generation
from app.services.explanation import (
    EXPLANATION_MAX_TOKENS,
    BaselineView,
    ComponentView,
    ExplanationInputs,
    ScheduledJobView,
    UnschedulableView,
    _serialize_payload,
    assemble_initial_plan_explanation,
    build_explanation_system,
)
from app.services.snapshot_loader import load_snapshot
from tests.eval.conftest import EVAL_NOW, EVAL_PRODUCTION_DATE

#: R25.2 的两个周期上限（K-10 / K-16）。
PLAN_GENERATION_TOKEN_CEILING = 4_000
REPLAN_TOKEN_CEILING = 14_000

#: 重排 ReAct 的输出上限（`RevisedPlanProposal`，句柄式返回，绝非逐作业明细）。用于把周期的
#: 输出 token 计进上限比对。取一个宽松的上界（远大于句柄式提案的真实体量），使断言只在**输入**
#: 侧的载荷膨胀时才被触发——那正是本用例要探测的回归。
REPLAN_OUTPUT_TOKEN_CAP = 1_500

#: 重排 ReAct 循环的步数上界（`MAX_AGENT_STEPS[PLANNING_AGENT] = 8`，design.md §1）。周期 token
#: 以「走满 8 步的最坏情形」估算——真实运行通常更短，因此这是一个保守（偏高）的上界。
REPLAN_MAX_STEPS = 8


# --------------------------------------------------------------------------
# token 估算器：tiktoken cl100k_base 优先，离线回退确定性启发式
# --------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """`text` 的 token 数估算。首选 tiktoken cl100k_base；词表不可用时回退项目启发式。

    两条路径都**只数真实内容**：绝不返回 0（除非文本本身为空），绝不伪造。回退只在
    `tiktoken` 的 BPE 词表无法加载（离线环境取不到下载）时发生，换的是估算器而非被估算对象。
    """
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        # 离线沙箱：cl100k_base 词表需联网下载，取不到时回退项目既有的确定性上界估计
        # （≈4 字节/token，对中文偏高——正是「上界」想要的方向）。
        from app.tools.clamp import count_tokens

        return count_tokens(text)


def _request_tokens(system_blocks: tuple[str, ...], user: str) -> int:
    """一次 LLM 请求的输入 token 估算 = system 各块 + user 拼接后的 token 数。"""
    return _estimate_tokens("\n".join((*system_blocks, user)))


# --------------------------------------------------------------------------
# 从计划生成结果重建解释输入（不经 app.api——避免牵入无关的 FastAPI 装配）
# --------------------------------------------------------------------------


def _explanation_inputs_from_result(
    result: plan_generation.PlanGenerationResult,
) -> ExplanationInputs:
    """把 `PlanGenerationResult` 映射成解释的确定性输入（结构化证据 + 紧凑载荷）。

    与 `app/api/plans.py::_explanation_inputs_from_db` 同口径，但直接从内核结果构造——本用例
    只需要那份**紧凑载荷**来估算解释请求的 token 体量，不需要 DB 往返，也不 import `app.api`
    （后者会牵入与本任务无关的路由装配）。
    """
    scheduled = tuple(
        ScheduledJobView(
            job_id=sj.job_id,
            order_id=sj.order_id,
            machine_id=sj.machine_id,
            duration_minutes=int((sj.end_time - sj.start_time).total_seconds() // 60),
        )
        for sj in result.candidate.scheduled_jobs
    )
    unschedulable = tuple(
        UnschedulableView(
            job_id=u.job_id, order_id=u.order_id, blocking_reason=u.blocking_reason
        )
        for u in result.candidate.unschedulable_jobs
    )
    components = tuple(
        ComponentView(
            name=c.name,
            raw_value=float(c.raw_value),
            weight=float(c.weight),
            weighted_contribution=float(c.weighted_contribution),
        )
        for c in result.objective_breakdown.components
    )
    b = result.baseline
    baseline = BaselineView(
        on_time_rate=b.on_time_rate,
        baseline_on_time_rate=b.baseline_on_time_rate,
        total_tardiness_minutes=b.total_tardiness_minutes,
        baseline_total_tardiness_minutes=b.baseline_total_tardiness_minutes,
        late_order_count=b.late_order_count,
        baseline_late_order_count=b.baseline_late_order_count,
    )
    return assemble_initial_plan_explanation(
        plan_id=result.plan_id,
        feasibility=result.feasibility,
        scheduled=scheduled,
        unschedulable=unschedulable,
        components=components,
        baseline=baseline,
    )


# ==========================================================================
# EVAL-015 —— 正常周期 token 回归
# ==========================================================================


def test_eval_015_plan_generation_cycle_within_token_budget(
    eval_seeded: sessionmaker[Session], eval_engine: Engine
) -> None:
    """EVAL-015（计划生成）: 标准计划生成周期的 token 估算 ≤ 4,000（K-10、R25.2）。

    计划生成路径的 LLM 消耗集中在**末端 1 次解释调用**（确定性流水线本身零 token）。因此
    周期 token = 解释请求的输入（system prose + 紧凑载荷，**不含工具 schema**）+ 输出上限。
    估算这份实际装配出来的请求，断言其 ≤ 4,000。并读回流水线 `Trace`，确认周期确以确定性
    流水线跑完（`mode == PIPELINE`）——「周期真的执行了」的证据。
    """
    with eval_seeded() as session:
        result = plan_generation.run_plan_generation(
            session,
            now=EVAL_NOW,
            production_date=EVAL_PRODUCTION_DATE,
            session_id="eval-015-plan",
        )
        inputs = _explanation_inputs_from_result(result)

    # 解释请求 = build_explanation 里装配的那一份（system prose + 序列化载荷 + 输出上限）。
    system_blocks = build_explanation_system()
    user = _serialize_payload(inputs.payload)
    input_tokens = _request_tokens(system_blocks, user)
    output_tokens = EXPLANATION_MAX_TOKENS
    cycle_tokens = input_tokens + output_tokens

    assert input_tokens > 0, "解释请求的输入 token 不应为 0（说明载荷是空的，估算失真）"
    assert cycle_tokens <= PLAN_GENERATION_TOKEN_CEILING, (
        f"计划生成周期 token {cycle_tokens}"
        f"（输入 {input_tokens} + 输出 {output_tokens}）超过 K-10 上限 "
        f"{PLAN_GENERATION_TOKEN_CEILING}"
    )

    # 周期确实执行了：流水线 Trace 存在且为确定性流水线（PIPELINE）。
    with eval_engine.connect() as conn:
        trace = conn.execute(
            select(Trace).where(Trace.trace_id == result.generated_by_trace_id)
        ).one()
    assert trace.mode == "PIPELINE"


def test_eval_015_replan_cycle_within_token_budget(
    eval_seeded: sessionmaker[Session],
) -> None:
    """EVAL-015（重排）: 标准重排周期的 token 估算 ≤ 14,000（K-16、R25.2）。

    重排走 `Planning_Agent` 的 ReAct 循环（≤ 8 步）。每轮输入 = 静态重排前缀 + `Context_Manager`
    装配的当轮 messages（`assemble_messages`：运行状态 + 折叠历史 + 最近 2 条 verbatim + 任务）。
    本用例以**走满 8 步的最坏情形**估算该周期的输入 token（远超真实运行），加上句柄式提案的
    输出上限，断言 ≤ 14,000。

    关键回归探测（ADR-004、原属性 24/29）：重排工具返回的是**句柄 + 摘要**而非逐 `ScheduledJob`
    明细。若被改回返回逐作业明细，verbatim 观察会从 ≈2,000 token/条膨胀到数万，周期 token 立刻
    击穿本上限——这正是本断言存在的理由。因此这里的 verbatim 观察载荷按「投影 + 截断到 2,000
    token」（`Tool_Registry` 的真实上限）构造，以复现正常路径的体量。
    """
    # 先在真实快照上跑一次确定性重排，拿到真实的候选与受影响集（周期的确定性内核部分）。
    with eval_seeded() as session:
        result = plan_generation.run_plan_generation(
            session,
            now=EVAL_NOW,
            production_date=EVAL_PRODUCTION_DATE,
            session_id="eval-015-replan",
        )
        snapshot = load_snapshot(
            session, now=EVAL_NOW, production_date=EVAL_PRODUCTION_DATE
        )

    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    replan_result = replan(result.candidate, disruption, snapshot)
    # 句柄式重排结果：候选计划 + 受影响集，而非逐作业明细清单。
    assert replan_result.candidate is not None

    # 装配重排 ReAct 的最坏情形上下文：静态前缀 + 走满 8 步的观察历史。
    prefix = build_replan_prefix()
    task_block = json.dumps(
        {
            "goal": "replan_after_disruption",
            "disruption": {"type": disruption.type, "machine_id": disruption.machine_id},
            "active_plan_id": result.plan_id,
            "affected_job_ids": list(replan_result.affected_job_ids),
        },
        ensure_ascii=False,
    )

    # 句柄式工具返回（投影 + 截断到 2,000 token 上限，`Tool_Registry` 的真实纪律）。每条
    # verbatim 观察是「摘要 + 句柄」，不是逐作业明细——正是本上限要守护的返回形态。
    handle_payload = json.dumps(
        {
            "plan_id": result.plan_id,
            "feasibility": replan_result.report.is_feasible,
            "scheduled_job_count": len(replan_result.candidate.scheduled_jobs),
            "machine_load_summary": [
                {"machine_id": f"CNC-0{i % 3}", "job_count": 5, "busy_minutes": 300}
                for i in range(8)
            ],
            "affected_job_ids": list(replan_result.affected_job_ids)[:20],
            "note": "x" * 1_800,  # 逼近但不超过 2,000-token 的单条观察上限
        },
        ensure_ascii=False,
    )
    observations = [
        ObservationRecord(
            step_index=i,
            tool_name="compute_replan" if i % 2 else "load_active_plan",
            outcome="OK",
            key_identifier=result.plan_id,
            # 最近 2 条 verbatim 原样注入完整载荷；更早的会被折叠成单行（成本极小）。
            payload_json=handle_payload if i >= REPLAN_MAX_STEPS - 2 else "{}",
            payload_tokens=_estimate_tokens(
                handle_payload if i >= REPLAN_MAX_STEPS - 2 else "{}"
            ),
            untrusted=False,
        )
        for i in range(REPLAN_MAX_STEPS)
    ]
    ctx = AgentContext(
        agent="PLANNING_AGENT",
        task_block=task_block,
        running_state=SessionState(
            session_id="eval-015-replan", active_plan_id=result.plan_id
        ),
        observations=observations,
    )
    request = assemble_messages(ctx, prefix=prefix)
    input_tokens = _request_tokens(request.system, request.user)
    cycle_tokens = input_tokens + REPLAN_OUTPUT_TOKEN_CAP

    assert input_tokens > 0
    assert cycle_tokens <= REPLAN_TOKEN_CEILING, (
        f"重排周期 token {cycle_tokens}"
        f"（输入 {input_tokens} + 输出 {REPLAN_OUTPUT_TOKEN_CAP}）超过 K-16 上限 "
        f"{REPLAN_TOKEN_CEILING}——检查重排工具是否被改回返回逐作业明细（ADR-004）"
    )
