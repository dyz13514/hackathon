"""全部 26 个工具的 Pydantic v2 契约（任务 5.2，R22.3–R22.6 / R22.13–R22.15）。

design.md §2.4 把工具契约的规则钉成三条，本模块逐条落地：

1. **全部模型 `extra="forbid"`**（`_Contract` 基类统一给出）。JSON Schema 由
   `model_json_schema()` 生成后喂给 LLM（R22.1），因此契约就是给模型看的接口——多一个字段
   都会让模型学到一个我们并不支持的参数。
2. **句柄 + 聚合，绝无逐 `ScheduledJob` 明细**（ADR-004）。`PlanHandle` / `ObjectiveSummary`
   / `Feasibility` 是共享形态，`generate_schedule` / `run_scenario` / `compare_plans` /
   `save_proposed_plan` 全部返回句柄。作业级明细只经 `get_job_details`（`job_ids ≤ 10`）取。
3. **每个 array 字段都声明 `max_length`**（Pydantic v2 在 JSON Schema 里渲染成 `maxItems`）。
   契约测试 `test_every_output_model_array_declares_maxitems` 逐字段断言这一点——一个没有
   上限的列表会让响应无上界增长，把注入上下文的 token 撑爆（第 3 道结构性保障 / ADR-004）。

## 为什么 `PlanHandle` 刻意不含任何 array / 逐作业字段

30 条 `ScheduledJob` 明细 ≈1,350 token，作为观察结果会在其后每一轮被重传（ADR-004 的算术）。
句柄化后，14 个作业与 60 个作业的上下文成本相同。`test_handle_output_has_no_detail_rows`
断言 `PlanHandle` 的 JSON Schema 里既没有 `scheduled_jobs` 也没有任何 `array` 属性——这是
ADR-004 的**类型层**防线（另一道是 `EVAL-015` 的周期 token 回归）。因此本模块里凡是返回
计划的工具，输出类型都收敛到 `PlanHandle`（或其 `status` 加字段的子类 `SaveProposedPlanOut`），
而不是任何带明细列表的形状。

## 与 `app/core` 值对象的关系：契约是**投影**，不是复用

内核的 `PlanCandidate` / `ScheduledJob` / `ObjectiveBreakdown` 带完整明细，是计算的中间产物；
本模块的契约是它们**面向 Agent 上下文的紧凑投影**。handler（`tools/handlers/`）负责把内核值
对象压成这里的句柄/聚合形态。刻意不 `from app.core... import ScheduledJob` 复用类型：契约的
稳定性不该被内核的重构牵动，且内核类型带着明细字段，直接复用就等于把明细泄漏进契约。

本模块只依赖标准库与 pydantic，不 import ORM、不 import 内核——它是纯声明，handler 才接线。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------


class _Contract(BaseModel):
    """全部工具输入/输出模型的基类：拒绝未声明字段（R22.1、design.md §2.4）。

    `extra="forbid"`：契约喂给 LLM 后，模型只应看到我们真正支持的字段。放行额外字段会让
    「Agent 编了个参数」这种错误静默通过 schema 校验，直到 handler 里以别的方式炸掉——而
    R22.2 要求输入错误作为**可读的观察结果**当场返回，前提是 schema 把它挡在 handler 之前。
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# 共享类型（design.md §2.4「共享类型」）
# --------------------------------------------------------------------------


class Feasibility(str, Enum):
    """计划可行性三态（R8.1）。与内核 `scheduler.Feasibility` 取值一致，但独立声明——契约
    不依赖内核类型（见模块 docstring）。"""

    FEASIBLE = "FEASIBLE"
    PARTIAL = "PARTIAL"
    NO_FEASIBLE_PLAN = "NO_FEASIBLE_PLAN"


class ObjectiveSummary(_Contract):
    """注入上下文的紧凑目标摘要（design.md §2.4）。

    是 `Objective_Scorer.ObjectiveBreakdown`（7 条 `ComponentScore` + 逐规则归因）的投影——
    只保留 Agent 决策真正需要的聚合值。`churn_ratio` 只在有参照计划（重排）时有值，初始生成
    时为 `None`。
    """

    total_score: float
    late_order_count: int
    total_tardiness_minutes: int
    churn_ratio: float | None = None
    total_changeover_minutes: int
    preference_penalty: float


class PlanHandle(_Contract):
    """句柄 + 聚合值（R22.13、ADR-004）。**刻意不含任何逐 `ScheduledJob` 字段 / array 字段。**

    JSON 实例约 120 token，与计划里有 14 个还是 60 个作业无关。想看作业级明细只能经
    `get_job_details`。`changed_job_count` 只在重排（有参照）时有值。契约测试
    `test_handle_output_has_no_detail_rows` 断言本模型的 JSON Schema 里没有任何 `array` 属性。
    """

    plan_id: str
    plan_version: int
    feasibility: Feasibility
    objective: ObjectiveSummary
    scheduled_job_count: int
    unschedulable_count: int
    changed_job_count: int | None = None
    trace_id: str


class ObjectiveDelta(_Contract):
    """两份计划目标分量的差值（正=变差，负=变好）。用于 `run_scenario` / `compare_plans`。"""

    total_score: float
    late_order_count: int
    total_tardiness_minutes: int
    total_changeover_minutes: int


class ViolationBrief(_Contract):
    """一处硬约束违反的紧凑形态（校验器 `Violation` 的投影，去掉长描述里的资源明细）。"""

    violation_type: str
    job_ids: list[str] = Field(default_factory=list, max_length=20)
    human_description: str


class RiskFindingBrief(_Contract):
    """一条风险发现的摘要（R14.9）。叙述文本在 P0 恒为模板来源。"""

    finding_id: str
    risk_type: str
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    subject_id: str
    metric_value: float
    threshold: float
    narrative: str


class RiskFindingListOut(_Contract):
    """`get_risk_findings` / `scan_risks` 的输出：按严重度排序的发现摘要。"""

    items: list[RiskFindingBrief] = Field(default_factory=list, max_length=10)
    total: int


# --------------------------------------------------------------------------
# 只读工具（R22.3）——10 个
# --------------------------------------------------------------------------


class OrderBrief(_Contract):
    order_id: str
    product_id: str
    quantity: float
    due_date: date
    promised_date: date | None = None
    priority: Literal["URGENT", "HIGH", "NORMAL", "LOW"]


class GetOrdersIn(_Contract):
    """分页 + 投影。`fields` 是投影请求集合（R22.12），由 registry 第 ⑤ 步执行。"""

    date_from: date | None = None
    date_to: date | None = None
    priorities: list[Literal["URGENT", "HIGH", "NORMAL", "LOW"]] | None = Field(
        default=None, max_length=4
    )
    fields: list[str] | None = Field(default=None, max_length=20)
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class OrderListOut(_Contract):
    items: list[OrderBrief] = Field(default_factory=list, max_length=50)
    total: int
    truncated: bool = False


class ProductBrief(_Contract):
    product_id: str
    name: str
    operation_count: int
    # 路线摘要：每道工序的机型（仅在 include_routing 时非空）。
    machine_types: list[str] = Field(default_factory=list, max_length=3)


class GetProductsIn(_Contract):
    product_ids: list[str] | None = Field(default=None, max_length=50)
    include_routing: bool = True
    fields: list[str] | None = Field(default=None, max_length=20)


class ProductListOut(_Contract):
    items: list[ProductBrief] = Field(default_factory=list, max_length=50)
    total: int


class MaterialBrief(_Contract):
    material_id: str
    name: str
    unit: str
    quantity_available: float
    reserved_quantity: float
    incoming_quantity: float | None = None


class GetInventoryIn(_Contract):
    material_ids: list[str] | None = Field(default=None, max_length=50)
    include_incoming: bool = False
    fields: list[str] | None = Field(default=None, max_length=20)


class InventoryOut(_Contract):
    items: list[MaterialBrief] = Field(default_factory=list, max_length=50)
    total: int


class MachineBrief(_Contract):
    machine_id: str
    machine_type: str
    status: Literal["AVAILABLE", "BUSY", "DOWN", "MAINTENANCE"]
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    rate_multiplier: float


class GetMachinesIn(_Contract):
    machine_types: list[str] | None = Field(default=None, max_length=20)
    statuses: list[Literal["AVAILABLE", "BUSY", "DOWN", "MAINTENANCE"]] | None = Field(
        default=None, max_length=4
    )
    fields: list[str] | None = Field(default=None, max_length=20)


class MachineListOut(_Contract):
    items: list[MachineBrief] = Field(default_factory=list, max_length=50)
    total: int


class WorkerBrief(_Contract):
    worker_id: str
    name: str
    skills: list[str] = Field(default_factory=list, max_length=20)
    shift_start: datetime
    shift_end: datetime


class GetWorkersIn(_Contract):
    skills: list[str] | None = Field(default=None, max_length=20)
    fields: list[str] | None = Field(default=None, max_length=20)


class WorkerListOut(_Contract):
    items: list[WorkerBrief] = Field(default_factory=list, max_length=50)
    total: int


class GetCurrentPlanIn(_Contract):
    """`plan_id` 缺省取当前 `ACTIVE` 计划。返回 `PlanHandle`（不含明细，ADR-004）。"""

    plan_id: str | None = None
    fields: list[str] | None = Field(default=None, max_length=20)


class PreferenceRuleBrief(_Contract):
    rule_id: str
    human_text: str
    kind: str
    enabled: bool


class GetPreferenceRulesIn(_Contract):
    enabled_only: bool = True
    fields: list[str] | None = Field(default=None, max_length=20)


class PreferenceRuleListOut(_Contract):
    items: list[PreferenceRuleBrief] = Field(default_factory=list, max_length=20)
    total: int


class GetRiskFindingsIn(_Contract):
    min_severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    limit: int = Field(default=5, ge=1, le=10)
    fields: list[str] | None = Field(default=None, max_length=20)


class ValueMetricsOut(_Contract):
    """价值台账当前值的紧凑摘要（R19.1）。无 array 字段——它是一组标量 KPI。"""

    plan_id: str | None = None
    on_time_rate: float
    baseline_on_time_rate: float
    total_tardiness_minutes: int
    baseline_total_tardiness_minutes: int
    churn_ratio: float | None = None
    auto_handled_count: int = 0
    escalated_count: int = 0


class GetValueMetricsIn(_Contract):
    plan_id: str | None = None
    fields: list[str] | None = Field(default=None, max_length=20)


class JobDetail(_Contract):
    """一条作业级明细。**只经 `get_job_details` 返回**（R22.14–15、ADR-004 的唯一明细出口）。"""

    job_id: str
    order_id: str
    product_id: str
    operation_sequence: int
    predecessor_job_id: str | None = None
    machine_id: str
    worker_id: str
    start_time: datetime
    end_time: datetime
    setup_minutes: int


class GetJobDetailsIn(_Contract):
    """`job_ids` 由 Pydantic 强制 `min_length=1, max_length=10`（R22.14）。

    超限时 registry 第 ② 步直接返回 `TOOL_INPUT_INVALID`，不进入 handler——这是 ADR-004
    「明细只能小批量取」在类型层面的强制点。`fields` 允许对每条明细再投影。
    """

    job_ids: list[str] = Field(min_length=1, max_length=10)
    fields: list[str] | None = Field(default=None, max_length=20)


class JobDetailListOut(_Contract):
    items: list[JobDetail] = Field(default_factory=list, max_length=10)


# --------------------------------------------------------------------------
# 确定性计算工具（R22.4）——9 个
# --------------------------------------------------------------------------

#: 可被 `weight_overrides` 覆盖的软目标分量（design.md §2.4：仅 SOFT_WEIGHT_KEYS）。
SoftWeightKey = Literal[
    "late_order_count",
    "total_tardiness_minutes",
    "urgent_order_lateness",
    "churn_ratio",
    "machine_utilisation",
    "total_changeover_minutes",
]


class GenerateScheduleIn(_Contract):
    """确定性全序排产的入参（design.md §2.4）。返回 `PlanHandle`（句柄，ADR-004）。"""

    production_date: date
    freeze_job_ids: list[str] = Field(default_factory=list, max_length=200)
    exclude_machine_ids: list[str] = Field(default_factory=list, max_length=20)
    weight_overrides: dict[str, float] | None = None
    apply_preference_rules: bool = True
    label: str = Field(default="candidate", max_length=40)


class CheckConstraintsIn(_Contract):
    plan_id: str


class ValidationOut(_Contract):
    plan_id: str
    feasibility: Feasibility
    violation_count: int
    violations: list[ViolationBrief] = Field(default_factory=list, max_length=20)


class EvaluateScheduleIn(_Contract):
    plan_id: str


class ComponentScoreBrief(_Contract):
    name: str
    raw_value: float
    weight: float
    weighted_contribution: float


class ObjectiveBreakdownOut(_Contract):
    """`evaluate_schedule` 的输出：7 条分量摘要 + 总分（R7.1–R7.2）。恒 7 条，故上限设 7。"""

    plan_id: str
    total_score: float
    components: list[ComponentScoreBrief] = Field(default_factory=list, max_length=7)


class ComparePlansIn(_Contract):
    plan_id_a: str
    plan_id_b: str


class ComparePlansOut(_Contract):
    """句柄形态：只给聚合，不给逐行 diff。想看明细 → `get_job_details`（ADR-004）。"""

    added_count: int
    removed_count: int
    moved_count: int
    reassigned_count: int
    unchanged_count: int
    churn_ratio: float
    objective_delta: ObjectiveDelta
    top_changed_job_ids: list[str] = Field(default_factory=list, max_length=10)


class GetAffectedJobsIn(_Contract):
    disruption_id: str


class AffectedJobsOut(_Contract):
    """受扰动影响的作业/订单聚合（`get_affected_jobs`）。`affected_job_ids` 上限 60。"""

    disruption_id: str
    affected_job_ids: list[str] = Field(default_factory=list, max_length=60)
    affected_order_ids: list[str] = Field(default_factory=list, max_length=60)
    affected_job_count: int
    affected_order_count: int


class ClassifyImpactIn(_Contract):
    candidate_plan_id: str
    baseline_plan_id: str | None = None  # 缺省取当前 ACTIVE


class ImpactOut(_Contract):
    impact_class: Literal["IMPACT_MINOR", "IMPACT_MODERATE", "IMPACT_MAJOR"]
    autonomy_level: Literal["L1", "L2", "L3", "L4", "L5"]
    decisive_predicates: list[str] = Field(default_factory=list, max_length=8)
    churn_ratio: float
    tardiness_delta_minutes: int
    changed_job_count: int
    promised_date_changed: bool
    touches_high_priority: bool
    new_unschedulable_count: int


# --- ScenarioMutation：R16.2 的 5 类结构化场景变更（判别联合） ---


class AddOrChangeOrder(_Contract):
    """新增订单或改变订单交期（R16.2 第 1 类）。"""

    kind: Literal["ADD_OR_CHANGE_ORDER"] = "ADD_OR_CHANGE_ORDER"
    order_id: str | None = None  # None = 新增
    product_id: str | None = None
    quantity: float | None = None
    due_date: date | None = None
    priority: Literal["URGENT", "HIGH", "NORMAL", "LOW"] | None = None


class SetMachineUnavailable(_Contract):
    """把某机器设为不可用，含时间区间（R16.2 第 2 类）。"""

    kind: Literal["SET_MACHINE_UNAVAILABLE"] = "SET_MACHINE_UNAVAILABLE"
    machine_id: str
    start_time: datetime
    end_time: datetime


class ChangeMaterialAvailability(_Contract):
    """改变某物料可用量（R16.2 第 3 类）。"""

    kind: Literal["CHANGE_MATERIAL_AVAILABILITY"] = "CHANGE_MATERIAL_AVAILABILITY"
    material_id: str
    quantity_available: float


class SetWorkerUnavailable(_Contract):
    """把某工人设为不可用（R16.2 第 4 类）。"""

    kind: Literal["SET_WORKER_UNAVAILABLE"] = "SET_WORKER_UNAVAILABLE"
    worker_id: str
    start_time: datetime
    end_time: datetime


class ChangeOrderPriority(_Contract):
    """改变某订单优先级（R16.2 第 5 类）。"""

    kind: Literal["CHANGE_ORDER_PRIORITY"] = "CHANGE_ORDER_PRIORITY"
    order_id: str
    priority: Literal["URGENT", "HIGH", "NORMAL", "LOW"]


ScenarioMutation = Annotated[
    AddOrChangeOrder
    | SetMachineUnavailable
    | ChangeMaterialAvailability
    | SetWorkerUnavailable
    | ChangeOrderPriority,
    Field(discriminator="kind"),
]


class RunScenarioIn(_Contract):
    mutations: list[ScenarioMutation] = Field(min_length=1, max_length=5)
    compare_with_plan_id: str | None = None


class ScenarioOut(_Contract):
    """句柄形态（R22.13）：沙箱推演结果的聚合摘要，无逐作业明细。"""

    scenario_id: str
    feasibility: Feasibility
    objective: ObjectiveSummary
    delta_vs_active: ObjectiveDelta
    new_unschedulable_count: int
    delayed_order_ids: list[str] = Field(default_factory=list, max_length=10)


class ScanRisksIn(_Contract):
    horizon_days: int = Field(default=3, ge=1, le=30)


class ComputeBaselineIn(_Contract):
    plan_id: str


class BaselineComparisonOut(_Contract):
    """`compute_baseline` 的同口径 KPI 对比聚合（R19.2）。无 array 字段。"""

    plan_id: str
    baseline_plan_id: str
    snapshot_version: int
    on_time_rate: float
    baseline_on_time_rate: float
    total_tardiness_minutes: int
    baseline_total_tardiness_minutes: int
    late_order_count: int
    baseline_late_order_count: int


# --------------------------------------------------------------------------
# 写入工具（R22.5）——4 个
# --------------------------------------------------------------------------


class SaveProposedPlanIn(_Contract):
    """**没有 `status` 参数**——实现内部硬编码 `PENDING_APPROVAL`（R22.9、R23.4、ADR-004）。"""

    candidate_plan_id: str
    production_date: date
    origin: Literal[
        "PLAN_GENERATION", "REPLANNING", "RISK_MITIGATION", "SCENARIO_ADOPTION"
    ]
    supersedes_pending: bool = False


class SaveProposedPlanOut(PlanHandle):
    """句柄 + 写死的 `status` 字面量（design.md §2.4）。没有任何输入能改这个状态。"""

    status: Literal["PENDING_APPROVAL"] = "PENDING_APPROVAL"


# --- DisruptionPayload：5 类扰动的判别联合（R9.1） ---


class UrgentOrderPayload(_Contract):
    kind: Literal["URGENT_ORDER"] = "URGENT_ORDER"
    product_id: str
    quantity: float
    due_date: date


class MachineBreakdownPayload(_Contract):
    kind: Literal["MACHINE_BREAKDOWN"] = "MACHINE_BREAKDOWN"
    machine_id: str
    start_time: datetime
    end_time: datetime


class MaterialShortagePayload(_Contract):
    kind: Literal["MATERIAL_SHORTAGE"] = "MATERIAL_SHORTAGE"
    material_id: str
    available_quantity: float


class WorkerUnavailablePayload(_Contract):
    kind: Literal["WORKER_UNAVAILABLE"] = "WORKER_UNAVAILABLE"
    worker_id: str
    start_time: datetime
    end_time: datetime


class MaterialDelayPayload(_Contract):
    kind: Literal["MATERIAL_DELAY"] = "MATERIAL_DELAY"
    material_id: str
    delivery_id: str
    new_eta: datetime


DisruptionPayload = Annotated[
    UrgentOrderPayload
    | MachineBreakdownPayload
    | MaterialShortagePayload
    | WorkerUnavailablePayload
    | MaterialDelayPayload,
    Field(discriminator="kind"),
]


class RegisterDisruptionIn(_Contract):
    type: Literal[
        "URGENT_ORDER",
        "MACHINE_BREAKDOWN",
        "MATERIAL_SHORTAGE",
        "WORKER_UNAVAILABLE",
        "MATERIAL_DELAY",
    ]
    payload: DisruptionPayload
    reported_at: datetime


class RegisterDisruptionOut(_Contract):
    disruption_id: str
    type: str
    registered_at: datetime


# --- PreferenceForm：4 类偏好判别联合（design.md §4.3） ---


class AvoidMachineForOrder(_Contract):
    kind: Literal["AVOID_MACHINE_FOR_ORDER"] = "AVOID_MACHINE_FOR_ORDER"
    order_id: str
    machine_id: str
    weight_delta: float = Field(default=1.0, gt=0, le=10)


class AvoidMachineForProduct(_Contract):
    kind: Literal["AVOID_MACHINE_FOR_PRODUCT"] = "AVOID_MACHINE_FOR_PRODUCT"
    product_id: str
    machine_id: str
    weight_delta: float = Field(default=1.0, gt=0, le=10)


class PreferWorkerForSkill(_Contract):
    kind: Literal["PREFER_WORKER_FOR_SKILL"] = "PREFER_WORKER_FOR_SKILL"
    skill: str
    worker_id: str
    weight_delta: float = Field(default=1.0, gt=0, le=10)


class AdjustObjectiveWeight(_Contract):
    kind: Literal["ADJUST_OBJECTIVE_WEIGHT"] = "ADJUST_OBJECTIVE_WEIGHT"
    component: SoftWeightKey
    multiplier: float = Field(ge=0.5, le=2.0)


PreferenceForm = Annotated[
    AvoidMachineForOrder
    | AvoidMachineForProduct
    | PreferWorkerForSkill
    | AdjustObjectiveWeight,
    Field(discriminator="kind"),
]


class ProposePreferenceRuleIn(_Contract):
    """**没有 `enabled` 参数**：候选规则一律 `enabled=False`（R18.4）。P0 不接线，仅 P1 使用。"""

    human_text: str = Field(max_length=200)
    structured_form: PreferenceForm
    source_decision_ids: list[str] = Field(default_factory=list, max_length=10)


class ProposePreferenceRuleOut(_Contract):
    rule_id: str
    enabled: Literal[False] = False


class SaveImportBatchIn(_Contract):
    """把一批已确认映射的行落成正式记录（R3.2）。摄取 Agent 与系统流水线都用。"""

    upload_id: str
    entity_type: Literal["ORDER", "PRODUCT", "MATERIAL", "MACHINE", "WORKER"]
    accepted_row_count: int = Field(ge=0)


class SaveImportBatchOut(_Contract):
    batch_id: str
    entity_type: str
    imported_row_count: int


# --------------------------------------------------------------------------
# 摄取工具（R22.6）——3 个
# --------------------------------------------------------------------------


class ReadPreviewIn(_Contract):
    upload_id: str
    max_sample_rows: int = Field(default=5, le=8, ge=1)


class ColumnPreview(_Contract):
    index: int
    raw_header: str = Field(max_length=60)
    inferred_kind: Literal["TEXT", "INT", "FLOAT", "DATE_LIKE", "BOOL", "MIXED", "EMPTY"]
    null_ratio: float
    sample_values: list[str] = Field(default_factory=list, max_length=3)  # untrusted


class FilePreviewOut(_Contract):
    upload_id: str
    detected_header_row: int
    total_rows: int
    columns: list[ColumnPreview] = Field(default_factory=list, max_length=40)
    formula_columns: list[str] = Field(default_factory=list, max_length=40)
    preview_tokens: int


class ProposeColumnMappingIn(_Contract):
    upload_id: str
    entity_type_hint: (
        Literal["ORDER", "PRODUCT", "MATERIAL", "MACHINE", "WORKER"] | None
    ) = None


class FieldMapping(_Contract):
    target_field: str
    source_column: str | None
    confidence: float = Field(ge=0, le=1)
    sample_values: list[str] = Field(default_factory=list, max_length=3)  # R2 第 2 条
    status: Literal["AUTO_ACCEPTED", "NEEDS_CONFIRMATION", "NOT_IMPORTED"]


class MissingField(_Contract):
    target_field: str
    reason: str


class NormalisationProposal(_Contract):
    source_column: str
    kind: Literal["DATE_FORMAT", "UNIT_CONVERSION"]
    detected_pattern: str
    conversion_factor: float | None = None  # 单位换算显式给出（R2 第 5 条）
    sample_before: list[str] = Field(default_factory=list, max_length=3)
    sample_after: list[str] = Field(default_factory=list, max_length=3)


class ColumnMappingProposal(_Contract):
    entity_type: Literal["ORDER", "PRODUCT", "MATERIAL", "MACHINE", "WORKER"]
    entity_type_confidence: float = Field(ge=0, le=1)
    field_mappings: list[FieldMapping] = Field(default_factory=list, max_length=30)
    missing_required_fields: list[MissingField] = Field(default_factory=list, max_length=10)
    normalisations: list[NormalisationProposal] = Field(default_factory=list, max_length=20)


class ValidateMappingIn(_Contract):
    """`validate_mapping` 是**确定性**工具：拿映射在整份文件上试跑一遍解析（R2.8）。"""

    upload_id: str
    mapping: ColumnMappingProposal


class UnparsedCell(_Contract):
    row_number: int
    column_name: str
    raw_value: str = Field(max_length=60)
    reason: str


class ValidateMappingOut(_Contract):
    upload_id: str
    parsed_row_count: int
    unparsed_cells: list[UnparsedCell] = Field(default_factory=list, max_length=50)
    type_error_count: int
    normalisation_failure_count: int
