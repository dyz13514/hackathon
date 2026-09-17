"""确定性计算工具的 handler（任务 5.2，R22.4、ADR-004）。

9 个计算工具。它们**委派给确定性内核**（`app/core/*`）与服务层，绝不在这里重新实现排产/
校验/评分逻辑——内核的价值正在于「排产器与校验器独立实现」，handler 再抄一遍会毁掉那道
交叉验证。返回一律为句柄/聚合形态（`PlanHandle` / `ObjectiveBreakdownOut` / …），作业级
明细只经 `get_job_details`（ADR-004）。

## 已接线 vs 占位

`generate_schedule` 在此完整接线：加载快照 → 调 `scheduler.generate_schedule` → 评分 →
投影成 `PlanHandle`。这条路径依赖的内核（任务 2.4 / 2.10）已全部落地。

其余若干工具依赖尚未落地的内核/服务件——从**已持久化的 `plan_id`** 重建 `PlanCandidate`
（需要 §7 的重排流水线里那套「读计划态回内核值对象」的机具）、沙箱推演（§8）、影响分级
（§7.3 的 `Autonomy_Policy_Engine`）、风险扫描内核（§8 的 `Risk_Radar`）。这些 handler
现在定义完整的输入/输出契约（使 26 工具注册表完整、白名单矩阵契约测试可覆盖每一格），执行
体则委派「若内核函数已存在」否则抛 `NotImplementedError` 并指明落地任务。这样契约层此刻
即完整，接线是后续任务把 `raise` 换成 `return delegate(...)` 的一行改动。
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy.orm import Session

from app.core.scheduler import PlanCandidate, generate_schedule as kernel_generate
from app.core.scoring import ObjectiveBreakdown, ObjectiveWeights, score
from app.services.snapshot_loader import load_snapshot
from app.tools import models as m
from app.tools.registry import ToolContext


def _session(ctx: ToolContext) -> Session:
    if ctx.session is None:
        raise RuntimeError("计算 handler 需要 ctx.session；registry 装配时必须注入会话")
    # `ToolContext.session` 刻意是 `Any`（见 registry）；在此显式收窄为 `Session`。
    return cast(Session, ctx.session)


def _now(ctx: ToolContext) -> datetime:
    return datetime.now()  # noqa: DTZ005


def _objective_summary(breakdown: ObjectiveBreakdown) -> m.ObjectiveSummary:
    """把内核 `ObjectiveBreakdown` 投影成契约的紧凑 `ObjectiveSummary`。"""
    by_name = {c.name: c.raw_value for c in breakdown.components}
    return m.ObjectiveSummary(
        total_score=float(breakdown.total_score),
        late_order_count=int(by_name.get("late_order_count", 0.0)),
        total_tardiness_minutes=int(by_name.get("total_tardiness_minutes", 0.0)),
        churn_ratio=by_name.get("churn_ratio"),
        total_changeover_minutes=int(by_name.get("total_changeover_minutes", 0.0)),
        preference_penalty=float(by_name.get("preference_penalty", 0.0)),
    )


def _plan_handle_from_candidate(
    candidate: PlanCandidate, breakdown: ObjectiveBreakdown, *, plan_id: str, trace_id: str
) -> m.PlanHandle:
    return m.PlanHandle(
        plan_id=plan_id,
        plan_version=1,
        feasibility=m.Feasibility(candidate.feasibility),
        objective=_objective_summary(breakdown),
        scheduled_job_count=len(candidate.scheduled_jobs),
        unschedulable_count=len(candidate.unschedulable_jobs),
        trace_id=trace_id,
    )


# --------------------------------------------------------------------------
# generate_schedule —— 完整接线（内核已落地）
# --------------------------------------------------------------------------


def generate_schedule(args: m.GenerateScheduleIn, ctx: ToolContext) -> m.PlanHandle:
    """确定性全序排产，返回句柄（R22.13、ADR-004）。委派给 `scheduler.generate_schedule`。

    在当前快照上跑排产 + 评分，产出 `PlanHandle`——**不含任何逐作业字段**。冻结集/排除机器
    由入参透传给内核；`weight_overrides` 目前不改内核权重（任务 11.x 的 `ADJUST_OBJECTIVE_WEIGHT`
    落地后接入），此处用默认权重评分。这条 handler 不落库——落库是 `save_proposed_plan` 的活。
    """
    snapshot = load_snapshot(
        _session(ctx), now=_now(ctx), production_date=args.production_date
    )
    candidate = kernel_generate(
        snapshot,
        exclude_machine_ids=frozenset(args.exclude_machine_ids),
    )
    breakdown = score(candidate, snapshot, ObjectiveWeights())
    return _plan_handle_from_candidate(
        candidate, breakdown, plan_id=f"CAND-{args.label}", trace_id=ctx.trace_id
    )


# --------------------------------------------------------------------------
# 依赖「从持久化 plan_id 重建 PlanCandidate」的计算工具（§7 落地）
# --------------------------------------------------------------------------


def check_constraints(args: m.CheckConstraintsIn, ctx: ToolContext) -> m.ValidationOut:
    """对已持久化计划跑全部 9 类硬约束（R6.1–R6.6）。委派给 `validation.validate`。

    需要先把 `plan_id` 的 `scheduled_jobs` / `unschedulable_jobs` 重建成内核
    `PlanCandidate`——那套「读计划态回内核值对象」的机具随 §7 重排流水线落地。届时本 handler
    即为：重建 candidate → `validate(candidate, snapshot)` → 投影成 `ValidationOut`。
    """
    raise NotImplementedError(
        "check_constraints 需要从持久化 plan_id 重建 PlanCandidate（随 §7 重排流水线落地）；"
        "契约已定义，接线后委派 app.core.validation.validate"
    )


def evaluate_schedule(
    args: m.EvaluateScheduleIn, ctx: ToolContext
) -> m.ObjectiveBreakdownOut:
    """对已持久化计划评分（R7.1–R7.2）。委派给 `scoring.score`。

    同 `check_constraints`：待「读计划态回内核值对象」的机具落地后，重建 candidate →
    `score(...)` → 投影成 `ObjectiveBreakdownOut`（7 条分量摘要 + 总分）。
    """
    raise NotImplementedError(
        "evaluate_schedule 需要从持久化 plan_id 重建 PlanCandidate（随 §7 落地）；"
        "契约已定义，接线后委派 app.core.scoring.score"
    )


def compute_baseline(
    args: m.ComputeBaselineIn, ctx: ToolContext
) -> m.BaselineComparisonOut:
    """同口径 FCFS 基线对比（R19.2）。委派给 `baseline.fcfs` + `assert_same_version_as`。

    P0 的初始生成流水线（`plan_generation`）已在内部算出 `BaselineComparison` 并落库；作为
    工具的 `compute_baseline` 读回该计划的 `baseline_comparisons` 行即可。此接线随 §7 的
    「计划态读回」机具一并落地。
    """
    raise NotImplementedError(
        "compute_baseline 读回 baseline_comparisons 行（随 §7 落地）；"
        "契约已定义，基线纯计算委派 app.core.baseline.fcfs"
    )


# --------------------------------------------------------------------------
# 依赖 §7 重排 / §8 沙箱 / §7.3 自治引擎的计算工具
# --------------------------------------------------------------------------


def compare_plans(args: m.ComparePlansIn, ctx: ToolContext) -> m.ComparePlansOut:
    """两份计划的聚合 diff（句柄形态，ADR-004）。委派给 §7 的 `compute_plan_delta`。

    只给聚合计数与 churn，不给逐行 diff——想看明细走 `get_job_details`。`compute_plan_delta`
    随 §7 落地（design.md §3.5 的 churn 公式）。
    """
    raise NotImplementedError(
        "compare_plans 委派 §7 的 compute_plan_delta（design.md §3.5）；契约已定义"
    )


def get_affected_jobs(args: m.GetAffectedJobsIn, ctx: ToolContext) -> m.AffectedJobsOut:
    """扰动波及的作业/订单聚合（R9）。委派给 §7 重排流水线的受影响集合计算。"""
    raise NotImplementedError(
        "get_affected_jobs 委派 §7 重排流水线的受影响集合计算；契约已定义"
    )


def classify_impact(args: m.ClassifyImpactIn, ctx: ToolContext) -> m.ImpactOut:
    """影响分级 + 自主等级裁决（R13）。委派给 §7.3 的 `Autonomy_Policy_Engine`。

    分级只吃 `ImpactInput` 的 7 个数值字段（无字符串入口，因此不可被 LLM 影响，
    `test_layering.py` 断言它与 LLM 无 import 边）。本 handler 只负责组装那 7 个数值并调用
    分级函数——`core/autonomy.py` 随 §7.3 落地。
    """
    raise NotImplementedError(
        "classify_impact 委派 §7.3 的 core/autonomy 分级函数；契约已定义"
    )


def run_scenario(args: m.RunScenarioIn, ctx: ToolContext) -> m.ScenarioOut:
    """沙箱推演（R16）。委派给 §8 的 `Scenario_Sandbox.run_sandbox`（句柄形态，ADR-004）。

    5 类结构化 `ScenarioMutation` 已在契约里定义（R16.2）。沙箱在生产数据的**内存副本**上跑
    内核（design.md §3.7 的两层隔离），返回聚合摘要 + 相对 ACTIVE 的 delta——无逐作业明细。
    沙箱随 §8 落地。
    """
    raise NotImplementedError(
        "run_scenario 委派 §8 的 Scenario_Sandbox.run_sandbox；5 类 mutation 契约已定义"
    )


def scan_risks(args: m.ScanRisksIn, ctx: ToolContext) -> m.RiskFindingListOut:
    """滚动时域风险扫描（R14）。委派给 §8 的 `Risk_Radar`（确定性度量 + 模板叙述）。

    5 类风险的度量与 `severity` 阈值由确定性代码计算（R14.4），叙述在 P0 恒为模板。扫描内核
    随 §8 落地；本 handler 届时即为 `Risk_Radar.scan(snapshot, horizon_days)` 的投影。
    """
    raise NotImplementedError(
        "scan_risks 委派 §8 的 Risk_Radar.scan；契约已定义"
    )
