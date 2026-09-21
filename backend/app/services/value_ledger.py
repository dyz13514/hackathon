"""价值台账的确定性聚合（任务 7.6 的 K-14 部分，R13.13、R19）。

本模块只承担 Task 7.6 需要的那一片：**自主处理 vs 上报人工的计数与逐条判据**（K-14，
R13.13）。它从 `impact_assessments` 表按 `execution_path` 聚合——那张表由重排流水线写入，
每一行是一次确定性的影响分级裁决（`impact_class` + `autonomy_level` + `execution_path` +
`decisive_predicates` + `impact_input`）。

## 计数口径（R13.13、design.md §3.6）

- `auto_handled_count` = `execution_path ∈ {PROPOSED, AUTO_APPLIED}` 的行数。P0 只有
  `PROPOSED`（L3 自主生成 PENDING_APPROVAL 提案）；`AUTO_APPLIED`（L4）属 P1，P0 运行期
  恒不出现，但计数口径此刻就把它算进「自主处理」，这样 P1 打开 L4 后无需改这里。
- `escalated_count` = `execution_path == ESCALATED` 的行数（L5 强制人工审批）。

「自主处理」指系统未经人工介入就推进到某个可执行状态（P0 是生成提案；P1 是自动应用）；
「上报」指系统主动停下要求人工决定。两者之和即被分级的变更总数，比例即 K-14 要展示的
「自主 vs 上报」。

## 为什么是纯查询、不落新表

K-14 是**可从既有事实重算**的派生量——`impact_assessments` 已是权威记录。再落一张计数表
会引入「计数与明细不一致」的失步风险；每次读时聚合，计数永远等于明细。演示规模下这点查询
成本可忽略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import models as orm

__all__ = [
    "AUTO_HANDLED_PATHS",
    "MANUAL_STEP_RUBRIC",
    "PROJECT_REAL_RUN_CAP",
    "AutonomyDecisionRow",
    "AutonomySummary",
    "KpiRow",
    "ManualStepsBreakdown",
    "ValueMetrics",
    "autonomy_decisions",
    "autonomy_summary",
    "build_value_metrics",
    "kpi_rows",
    "manual_steps_breakdown",
]

Label = Literal["MEASURED", "ESTIMATED", "PROJECTED"]

#: 计入「自主处理」的执行路径。`AUTO_APPLIED` 属 P1（P0 恒不出现），但口径此刻就纳入，
#: 使 P1 打开 L4 后计数自动正确，无需改动本模块。
AUTO_HANDLED_PATHS = ("PROPOSED", "AUTO_APPLIED")


@dataclass(frozen=True)
class AutonomySummary:
    """自主 vs 上报的聚合计数（K-14，R13.13）。

    `auto_handled_count + escalated_count == total`（P0 下 `execution_path` 只有这两类落点，
    因此二者之和即被分级的变更总数）。`auto_handled_ratio` 是 UI 顶栏那条比例；分母为 0 时
    取 0.0（还没有任何分级裁决，「自主占比」无定义，展示 0 比展示 NaN 诚实）。
    """

    auto_handled_count: int
    escalated_count: int

    @property
    def total(self) -> int:
        return self.auto_handled_count + self.escalated_count

    @property
    def auto_handled_ratio(self) -> float:
        return self.auto_handled_count / self.total if self.total > 0 else 0.0


@dataclass(frozen=True)
class AutonomyDecisionRow:
    """一次影响分级裁决的可展示摘要（R13.12：judgement + 决定性判据 + 执行路径）。

    逐字段来自 `impact_assessments`。UI 的价值台账逐行渲染它，让「每次判定的决定性判据」
    可见（情节 8 / K-14）。
    """

    assessment_id: str
    candidate_plan_id: str
    impact_class: str
    autonomy_level: str
    execution_path: str
    decisive_predicates: tuple[str, ...]


def autonomy_summary(session: Session) -> AutonomySummary:
    """按 `execution_path` 聚合 `impact_assessments`，返回自主 vs 上报计数（K-14）。

    一次 `GROUP BY execution_path` 的计数查询，确定性且轻量。无任何 assessment 时两个计数
    都是 0。
    """
    rows = session.execute(
        select(
            orm.ImpactAssessment.execution_path,
            func.count().label("n"),
        ).group_by(orm.ImpactAssessment.execution_path)
    ).all()
    counts = {str(path): int(n) for path, n in rows}
    auto_handled = sum(counts.get(path, 0) for path in AUTO_HANDLED_PATHS)
    escalated = counts.get("ESCALATED", 0)
    return AutonomySummary(auto_handled_count=auto_handled, escalated_count=escalated)


def autonomy_decisions(session: Session, *, limit: int = 50) -> list[AutonomyDecisionRow]:
    """最近若干次影响分级裁决，供 UI 逐行展示决定性判据（R13.12）。

    按 `created_at` 降序（最新的在前），最多 `limit` 行。`decisive_predicates` 是 JSON 列，
    读回时归一化为字符串元组。
    """
    stmt = (
        select(orm.ImpactAssessment)
        .order_by(orm.ImpactAssessment.created_at.desc(), orm.ImpactAssessment.assessment_id)
        .limit(limit)
    )
    result: list[AutonomyDecisionRow] = []
    for row in session.execute(stmt).scalars():
        preds = row.decisive_predicates
        predicates = tuple(str(p) for p in preds) if isinstance(preds, list | tuple) else ()
        result.append(
            AutonomyDecisionRow(
                assessment_id=row.assessment_id,
                candidate_plan_id=row.candidate_plan_id,
                impact_class=row.impact_class,
                autonomy_level=row.autonomy_level,
                execution_path=row.execution_path,
                decisive_predicates=predicates,
            )
        )
    return result



# ==========================================================================
# 完整价值台账（任务 11.4，R19.1、R19.4–R19.8、R25.12/13、design.md §4.4）
# ==========================================================================
#
# 本段承接 Task 7.6 的 K-14 计数，补齐 design.md §4.4 的 `ValueMetrics` 全字段、
# `manual_steps_eliminated` 的口径表，以及带 MEASURED / ESTIMATED / PROJECTED 标签的
# KPI 行（供 `GET /value-ledger` 与 CSV 导出用）。**全部确定性计算，不依赖 LLM**（R19.7）。

#: 项目级真实运行次数硬上限（design.md 成本章节 ②）。与 `api.admin.PROJECT_REAL_RUN_CAP`
#: 同值；此处再定义一份常量避免服务层 import API 层（分层规则），值必须一致。
PROJECT_REAL_RUN_CAP = 150

#: 人工基线时间（分钟），标注为 `ESTIMATED`（来源：访谈估计，R19.6、K-01/K-02）。
#: 取各自区间的下界作为保守估计，避免夸大系统收益。
_BASELINE_PLAN_GENERATION_SECONDS = 45 * 60  # K-01：45–90 min，取 45 min
_BASELINE_DISRUPTION_RESPONSE_SECONDS = 30 * 60  # K-02：30–60 min，取 30 min

#: K-17 / K-18 的**预测值**（PROJECTED，非实测，R25.13）。常量取自 requirements 第 4 节。
_PROJECTED_HERO_DEMO_USD = Decimal("0.14")  # K-17
_PROJECTED_BUILD_TOTAL_USD = Decimal("30")  # K-18（≈USD21 LLM + USD5–10 Lightsail）

#: `manual_steps_eliminated` 的口径表（design.md §4.4，R19.5）。UI 原样展示这张表。
#: 每一项是 (动作标识, 中文说明, 计 1 步的条件)；计数在 `manual_steps_breakdown` 里逐项算。
MANUAL_STEP_RUBRIC: tuple[tuple[str, str, str], ...] = (
    ("spreadsheet_import", "Spreadsheet import", "Counts as 1 step per import batch (whether 20 rows or 2,000)"),
    ("mapping_normalisation", "Mapping normalization", "Date/unit normalization for each batch counts as 1 step in total"),
    ("plan_generation", "Plan generation", "Counts as 1 step per successful generation (replaces manual scheduling)"),
    ("constraint_validation", "Hard-constraint validation", "Counts as 1 step per validation (replaces manual item-by-item review)"),
    ("replan", "Replan", "Counts as 1 step per disruption"),
    ("risk_finding", "Risk finding", "Counts as 1 step per new WARNING+ risk (replaces manual inspection)"),
    ("plan_compare", "Plan comparison", "Counts as 1 step per comparison"),
    ("plan_export", "Plan export", "Counts as 1 step per export (replaces hand-copying onto the wall)"),
)


@dataclass(frozen=True)
class ManualStepsBreakdown:
    """`manual_steps_eliminated` 的逐项计数（R19.5）。`total` = 各项之和，UI 展示口径表。"""

    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class ValueMetrics:
    """design.md §4.4 的 `ValueMetrics` 全字段，全部确定性计算（R19.1、R19.7）。

    `labels` 与各字段平行：每个指标带 `MEASURED` / `ESTIMATED` / `PROJECTED` 标签
    （R19.4、R19.6、R25.13）。人工基线时间标 `ESTIMATED`；系统实测指标标 `MEASURED`；
    K-17 / K-18 标 `PROJECTED`。
    """

    plan_id: str | None
    measured_at: datetime
    # 系统实测（MEASURED）
    plan_generation_seconds: float | None
    disruption_response_seconds: float | None
    on_time_rate: float | None
    total_tardiness_minutes: int | None
    churn_ratio: float | None
    manual_steps_eliminated: int
    auto_handled_count: int
    escalated_count: int
    llm_tokens_used: int
    estimated_usd_cost: Decimal
    real_run_count: int
    # 基线（ESTIMATED 人工时间 / FCFS 同口径值）
    baseline_plan_generation_seconds: float
    baseline_disruption_response_seconds: float
    baseline_on_time_rate: float | None
    baseline_total_tardiness_minutes: int | None
    # 预测（PROJECTED，R25.13）
    projected_hero_demo_usd: Decimal
    projected_build_total_usd: Decimal
    labels: dict[str, Label] = field(default_factory=dict)


def manual_steps_breakdown(session: Session) -> ManualStepsBreakdown:
    """按 design.md §4.4 口径表逐项计数 `manual_steps_eliminated`（R19.5，确定性）。

    每一项从既有权威表计数，不落派生表（口径同 K-14：读时聚合，永不失步）：
    - 导入 / 映射归一：`import_batches` 行数（每批各计 1 步导入 + 1 步归一）。
    - 计划生成：`origin == 'PLAN_GENERATION'` 的计划数。
    - 硬约束校验：`plan_approvals` 行数（每次审批都跑一次重校验，R11.3）。
    - 重排：`disruptions` 行数（每个扰动触发一次重排）。
    - 风险发现：`risk_findings` 中 severity != INFO 的行数。
    - 方案对比 / 导出：P0 无持久化事件流，计 0（口径表仍原样展示，值为 0 不夸大）。
    """
    batches = int(
        session.execute(select(func.count()).select_from(orm.ImportBatch)).scalar_one()
    )
    plan_generations = int(
        session.execute(
            select(func.count())
            .select_from(orm.ProductionPlan)
            .where(orm.ProductionPlan.origin == "PLAN_GENERATION")
        ).scalar_one()
    )
    validations = int(
        session.execute(select(func.count()).select_from(orm.PlanApproval)).scalar_one()
    )
    replans = int(
        session.execute(select(func.count()).select_from(orm.Disruption)).scalar_one()
    )
    risk_findings = int(
        session.execute(
            select(func.count())
            .select_from(orm.RiskFinding)
            .where(orm.RiskFinding.severity != "INFO")
        ).scalar_one()
    )
    counts = {
        "spreadsheet_import": batches,
        "mapping_normalisation": batches,
        "plan_generation": plan_generations,
        "constraint_validation": validations,
        "replan": replans,
        "risk_finding": risk_findings,
        "plan_compare": 0,
        "plan_export": 0,
    }
    return ManualStepsBreakdown(counts=counts)


def _token_and_cost_totals(session: Session) -> tuple[int, Decimal, int]:
    """`(llm_tokens_used, estimated_usd_cost, real_run_count)`，从 `traces` 聚合（确定性）。

    与 `GET /health` 同口径：token = Σ(total_input_tokens + total_output_tokens)；
    usd = Σ estimated_usd；real_run_count = COUNT(mode != 'REPLAY')。REPLAY / STUB 下
    这些恒为 0（未消耗真实额度），是正确的真值。
    """
    tokens = session.execute(
        select(
            func.coalesce(func.sum(orm.Trace.total_input_tokens), 0)
            + func.coalesce(func.sum(orm.Trace.total_output_tokens), 0)
        )
    ).scalar_one()
    usd = session.execute(
        select(func.coalesce(func.sum(orm.Trace.estimated_usd), 0))
    ).scalar_one()
    real_runs = session.execute(
        select(func.count()).select_from(orm.Trace).where(orm.Trace.mode != "REPLAY")
    ).scalar_one()
    return int(tokens), Decimal(str(usd)), int(real_runs)


def _active_baseline(session: Session) -> orm.BaselineComparison | None:
    """当前 `ACTIVE` 计划的基线对比行（无 ACTIVE 计划或无对比时 None）。"""
    active_id = session.execute(
        select(orm.ProductionPlan.plan_id).where(orm.ProductionPlan.status == "ACTIVE")
    ).scalars().first()
    if active_id is None:
        return None
    return session.get(orm.BaselineComparison, active_id)


def build_value_metrics(session: Session, *, now: datetime | None = None) -> ValueMetrics:
    """聚合完整 `ValueMetrics`（R19.1，确定性，无 LLM）。

    on_time_rate / total_tardiness / churn 取当前 ACTIVE 计划的 `baseline_comparisons` 行
    （R19.2 同口径对比）；无 ACTIVE 计划时为 None（台账早于任何计划的诚实空态）。
    plan_generation_seconds / disruption_response_seconds 的**人工基线**标 ESTIMATED；系统实测
    时延在 P0 无确定性持久化来源，故实测值留 None（只如实展示可得的量，不发明数字）。
    """
    moment = now if now is not None else datetime.now()  # noqa: DTZ005
    summary = autonomy_summary(session)
    steps = manual_steps_breakdown(session)
    tokens, usd, real_runs = _token_and_cost_totals(session)
    bc = _active_baseline(session)

    on_time = float(bc.on_time_rate) if bc is not None else None
    baseline_on_time = float(bc.baseline_on_time_rate) if bc is not None else None
    tardiness = bc.total_tardiness_minutes if bc is not None else None
    baseline_tardiness = bc.baseline_total_tardiness_minutes if bc is not None else None

    labels: dict[str, Label] = {
        "plan_generation_seconds": "MEASURED",
        "disruption_response_seconds": "MEASURED",
        "on_time_rate": "MEASURED",
        "total_tardiness_minutes": "MEASURED",
        "churn_ratio": "MEASURED",
        "manual_steps_eliminated": "MEASURED",
        "auto_handled_count": "MEASURED",
        "escalated_count": "MEASURED",
        "llm_tokens_used": "MEASURED",
        "estimated_usd_cost": "MEASURED",
        "real_run_count": "MEASURED",
        "baseline_plan_generation_seconds": "ESTIMATED",
        "baseline_disruption_response_seconds": "ESTIMATED",
        "baseline_on_time_rate": "MEASURED",  # FCFS 同口径计算值（R19.3）
        "baseline_total_tardiness_minutes": "MEASURED",
        "projected_hero_demo_usd": "PROJECTED",
        "projected_build_total_usd": "PROJECTED",
    }

    return ValueMetrics(
        plan_id=bc.plan_id if bc is not None else None,
        measured_at=moment,
        plan_generation_seconds=None,
        disruption_response_seconds=None,
        on_time_rate=on_time,
        total_tardiness_minutes=tardiness,
        churn_ratio=None,
        manual_steps_eliminated=steps.total,
        auto_handled_count=summary.auto_handled_count,
        escalated_count=summary.escalated_count,
        llm_tokens_used=tokens,
        estimated_usd_cost=usd,
        real_run_count=real_runs,
        baseline_plan_generation_seconds=float(_BASELINE_PLAN_GENERATION_SECONDS),
        baseline_disruption_response_seconds=float(_BASELINE_DISRUPTION_RESPONSE_SECONDS),
        baseline_on_time_rate=baseline_on_time,
        baseline_total_tardiness_minutes=baseline_tardiness,
        projected_hero_demo_usd=_PROJECTED_HERO_DEMO_USD,
        projected_build_total_usd=_PROJECTED_BUILD_TOTAL_USD,
        labels=labels,
    )


@dataclass(frozen=True)
class KpiRow:
    """一行 KPI（CSV 导出与 UI 表格共用形状，R19.4、R19.8）。

    列与 `GET /value-ledger/export.csv` 逐字对应：`kpi_id`、`metric_name`、`current_value`、
    `baseline_value`、`delta`、`target_value`、`label`、`measured_at`。`current_value` 等为
    字符串（KPI 量纲各异：秒 / 百分比 / 分钟 / 计数 / 美元），字符串是它们唯一统一的表示。
    """

    kpi_id: str
    metric_name: str
    current_value: str
    baseline_value: str
    delta: str
    target_value: str
    label: Label
    measured_at: str


def _fmt(value: float | int | Decimal | None) -> str:
    """把标量渲染成 CSV/表格单元格；None → 空串（诚实空态）。"""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    return str(value)


def kpi_rows(metrics: ValueMetrics) -> list[KpiRow]:
    """把 `ValueMetrics` 展开成引用 K-01…K-18 的 KPI 行（R19.4）。

    只输出有确定性来源的 KPI 行：K-01/K-02（时间，基线 ESTIMATED）、K-03（按期率）、
    K-04（拖期）、K-13（消除人工步数）、K-11（累计美元）、K-17/K-18（PROJECTED）。
    `measured_at` 全行共用 `metrics.measured_at`（一次聚合的时刻）。
    """
    ts = metrics.measured_at.isoformat()
    rows: list[KpiRow] = [
        KpiRow(
            kpi_id="K-01",
            metric_name="Plan generation time (s)",
            current_value=_fmt(metrics.plan_generation_seconds),
            baseline_value=_fmt(metrics.baseline_plan_generation_seconds),
            delta="",
            target_value="≤ 60",
            label="ESTIMATED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-02",
            metric_name="Disruption response latency (s)",
            current_value=_fmt(metrics.disruption_response_seconds),
            baseline_value=_fmt(metrics.baseline_disruption_response_seconds),
            delta="",
            target_value="≤ 90",
            label="ESTIMATED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-03",
            metric_name="On-time delivery rate",
            current_value=_fmt(metrics.on_time_rate),
            baseline_value=_fmt(metrics.baseline_on_time_rate),
            delta=_delta(metrics.on_time_rate, metrics.baseline_on_time_rate),
            target_value="≥ FCFS + 0.20",
            label="MEASURED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-04",
            metric_name="Total tardiness (minutes)",
            current_value=_fmt(metrics.total_tardiness_minutes),
            baseline_value=_fmt(metrics.baseline_total_tardiness_minutes),
            delta=_delta(metrics.total_tardiness_minutes, metrics.baseline_total_tardiness_minutes),
            target_value="≤ 60% of FCFS",
            label="MEASURED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-13",
            metric_name="Manual steps eliminated",
            current_value=_fmt(metrics.manual_steps_eliminated),
            baseline_value="0",
            delta=_fmt(metrics.manual_steps_eliminated),
            target_value="≥ 12/day",
            label="MEASURED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-11",
            metric_name="Cumulative estimated cost (USD)",
            current_value=_fmt(metrics.estimated_usd_cost),
            baseline_value="",
            delta="",
            target_value="≤ 40",
            label="MEASURED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-17",
            metric_name="LLM cost for one full demo (USD, projected)",
            current_value=_fmt(metrics.projected_hero_demo_usd),
            baseline_value="",
            delta="",
            target_value="≈ 0.14",
            label="PROJECTED",
            measured_at=ts,
        ),
        KpiRow(
            kpi_id="K-18",
            metric_name="Total build + rehearsal spend (USD, projected)",
            current_value=_fmt(metrics.projected_build_total_usd),
            baseline_value="",
            delta="",
            target_value="≈ 30 (≤ 100)",
            label="PROJECTED",
            measured_at=ts,
        ),
    ]
    return rows


def _delta(current: float | int | None, baseline: float | int | None) -> str:
    """`current - baseline` 的可读差值；任一为 None → 空串。"""
    if current is None or baseline is None:
        return ""
    diff = current - baseline
    return _fmt(diff)
