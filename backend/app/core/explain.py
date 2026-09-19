"""`Explanation_Builder` 的**确定性内核部分**（任务 5.11，design.md §3.7、R10.2–R10.6）。

## 这一层守的边界

`Explanation_Builder` 在 design.md §3 的属性表里是**混合**组件：结构化证据、反事实数值、
假设清单、置信度**全部由确定性组件给出**；只有**措辞**由 LLM 生成，且措辞里的每个数字都要
经 `Guardrail_Layer` 的闭世界比对（§2.7(d)、R10.7）。本模块承担那句话里「确定性组件给出」
的全部——它是纯函数，产出两样东西：

1. **结构化证据对象**（`Explanation` 及其字段 `DecisionEvidence` / `Counterfactual` /
   `Assumption` / `Confidence`）——这是解释的**事实骨架**，UI 的对比解释面板逐块渲染它
   （design.md §6 `/plans/:a/compare/:b` 行）。这些数字是权威真值，LLM 不产生它们、也不能
   改它们。
2. **紧凑 LLM 载荷**（`build_explanation_payload`）——喂给那**唯一一次** LLM 调用的输入，
   ≈3,000 token，**绝不含原始 Order / Machine / Worker / Material 清单**（R21.12）。
3. **模板渲染器**（`TemplateExplanationRenderer`）——不经 LLM，把 `Explanation` 直接渲成
   中文散文。降级模式（`DETERMINISTIC_ONLY`）与数值比对回退（R10.7）两条路径都用它，因此
   计划生成在无 Bedrock 时仍带完整解释（R25.9）。

## 为什么本模块是纯的（不 import llm / db / services）

`app/core/**` 的分层规则（`tests/structure/test_layering.py` 第 ①②条静态断言）：内核不得
import `sqlalchemy` / `app.db` / `app.llm` / `app.services`。解释的**事实**是排产结果的纯函数，
把它放在内核让「同一份计划必得同一份结构化证据」成为一个可逐字节断言的性质（与 R5.7 同一
条纪律）。真正发起那次 LLM 调用、跑数值比对、按结果发布 LLM 文本或模板文本、落读路径的
**编排**属服务层（`app/services/explanation.py`，任务 5.11 的另一半）——它能 import 本模块、
`Guardrail_Layer`、`Bedrock_Adapter` 与 Agent 契约。纯计算与副作用就此分离。

## `counterfactual` 是**单值**，不是 `Optional`、不是 `list`（R10.3）

R10.3 要求「恰好 1 项（最关键的取舍）」。因此 `Explanation.counterfactual` 的类型是
`Counterfactual`（一个联合别名，取 `Tradeoff | NoTradeoff` 二者之一的**单个**值），既不是
`Counterfactual | None`，也不是 `list[Counterfactual]`——类型层面就把「零项」与「多项」排除。

**P0 的计划生成路径（形态 A）没有 delta**：它是初始计划，不是对某个 `ACTIVE` 计划的重排，
因此没有 `MOVED` / `REASSIGNED` 作业，`decision_evidence` 为空，`counterfactual` 取
`NoTradeoff` 占位。真正的反事实（`pick_pivotal_job` 三级判据 + 沙箱重算 Z 值，design.md §3.7）
由**任务 8.4**在重排路径上填 `Tradeoff`。在那之前，`NoTradeoff(reason=...)` 是一个诚实的占位
——它明说「这条路径没有可比的取舍」，而不是编一个。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from app.core.delta import PlanDelta
    from app.core.scheduler import PlanCandidate

__all__ = [
    "Assumption",
    "BaselineComparisonSummary",
    "ComponentSummary",
    "Confidence",
    "ConfidenceLevel",
    "Counterfactual",
    "DecisionEvidence",
    "Explanation",
    "KeyJobDetail",
    "MachineLoadSummary",
    "NoTradeoff",
    "Tradeoff",
    "TemplateExplanationRenderer",
    "UnschedulableSummary",
    "build_decision_evidence",
    "build_explanation_payload",
    "default_plan_assumptions",
    "derive_confidence",
    "estimate_payload_tokens",
    "initial_plan_counterfactual",
]


# --------------------------------------------------------------------------
# 结构化证据的原子类型（design.md §3.7 的 Explanation 模型）
# --------------------------------------------------------------------------


class DecisionEvidence(BaseModel):
    """一条决策证据：为**每个** `MOVED` / `REASSIGNED` 作业输出一条（R10.2）。

    三项内容逐字对齐 R10.2：`trigger`（触发原因）、`constraint`（被违反或将被违反的约束）、
    `resources`（涉及资源，如机器/工人 ID）。全部由确定性组件填——这是解释的事实，不是
    模型的措辞。

    P0 的计划生成路径没有 `MOVED` / `REASSIGNED`（初始计划无 delta），因此
    `decision_evidence` 为空列表；本类型供任务 8.4 的重排解释路径逐作业填充。
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    trigger: str
    constraint: str
    resources: tuple[str, ...] = ()


class Tradeoff(BaseModel):
    """反事实的「有取舍」形态（R10.3）：若维持原方案 X，则分量 Y 变为 Z。

    `component` 是 `Objective_Scorer` 的分量名（Y）；`current_value` 是建议方案下该分量的
    值；`counterfactual_value` 是把关键作业冻回原位后由 `Scenario_Sandbox` 实算出的 Z
    （R10.3 要求 Z「由沙箱对原方案实际计算得出」，不能估算）。`pivotal_job_id` 与
    `selection_basis` 记录「为什么这一项最关键」（design.md §3.7 的 `pick_pivotal_job`），
    UI 与回归测试都断言它。

    任务 8.4 在重排路径上构造本类型；P0 的计划生成路径用 `NoTradeoff`。
    """

    model_config = ConfigDict(frozen=True)

    pivotal_job_id: str
    component: str
    current_value: float
    counterfactual_value: float
    selection_basis: str


class NoTradeoff(BaseModel):
    """反事实的「无取舍」占位（design.md §3.7）。

    用于没有 `MOVED` / `REASSIGNED`（也没有可退化到的新增作业集合）的路径——**P0 的初始
    计划生成恒走这里**：初始计划不是对谁的重排，没有「若维持原方案」可谈。`reason` 诚实地
    说明这一点，而不是编一个不存在的取舍。任务 8.4 落地重排反事实后，这条只在真正无 delta
    时出现。
    """

    model_config = ConfigDict(frozen=True)

    reason: str


#: 反事实：**恰好 1 项**（R10.3）。单值联合别名——非 `Optional`、非 `list`（见模块 docstring）。
Counterfactual = Tradeoff | NoTradeoff


class Assumption(BaseModel):
    """一条本次决策依赖的、**可能过期**的输入假设（R10.4）。

    R10.4 点名的例子：到货 ETA、机器修复时间估计。design.md 的物料模型把「物料在首道工序
    一次性预留」作为一条简化假设，也应在此列出。`stale_risk` 用一句话说明它为什么可能过期
    （例如「依赖尚未确认的到货 ETA」），供 UI 展示与 `Confidence` 的判定依据引用。
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    description: str
    stale_risk: str


class ConfidenceLevel(str, Enum):
    """置信等级三态（R10.5）。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Confidence(BaseModel):
    """置信度及其**判定依据**（R10.5）。

    `level` 是三态之一；`basis` 是该等级的可读依据（例如「依赖 1 项未确认的到货 ETA」）。
    依据由确定性规则从 `assumptions` 派生（见 `derive_confidence`），不是模型的判断——解释的
    核心结论不能落在不可审计的一步上。
    """

    model_config = ConfigDict(frozen=True)

    level: ConfidenceLevel
    basis: str


class Explanation(BaseModel):
    """一份计划的**结构化解释**（design.md §3.7）。

    四个字段是解释的事实骨架，全部由确定性组件填：`decision_evidence`（每个 MOVED/REASSIGNED
    一条，R10.2）、`counterfactual`（**恰好 1 项**，R10.3）、`assumptions`（R10.4）、
    `confidence`（R10.5）。**不含模型的原始推理链**（R10.6）——本模型里没有任何「思考过程」
    字段，只有结构化结论。LLM 生成的**措辞**是这份骨架的一个渲染，存在别处（服务层的
    `ExplanationResult.narrative`），经数值比对后才发布。
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str
    decision_evidence: tuple[DecisionEvidence, ...]
    counterfactual: Counterfactual
    assumptions: tuple[Assumption, ...]
    confidence: Confidence


# --------------------------------------------------------------------------
# 置信度派生（确定性规则，R10.5）
# --------------------------------------------------------------------------


def derive_confidence(assumptions: tuple[Assumption, ...]) -> Confidence:
    """从假设清单确定性地派生置信度（R10.5）。

    规则简单且可复算：假设越多、越可能过期，置信越低。0 条可能过期的假设 → `HIGH`；
    1 条 → `MEDIUM`；≥2 条 → `LOW`。`basis` 逐字引用假设的 `stale_risk`，使「为什么是这个
    等级」在文本上就能对上假设清单——不是模型的判断，是一条查表规则。
    """
    count = len(assumptions)
    if count == 0:
        return Confidence(
            level=ConfidenceLevel.HIGH,
            basis="不依赖任何可能过期的输入。",
        )
    risks = "；".join(a.stale_risk for a in assumptions)
    if count == 1:
        return Confidence(
            level=ConfidenceLevel.MEDIUM,
            basis=f"依赖 1 项可能过期的输入：{risks}。",
        )
    return Confidence(
        level=ConfidenceLevel.LOW,
        basis=f"依赖 {count} 项可能过期的输入：{risks}。",
    )


# --------------------------------------------------------------------------
# 标准假设与初始计划反事实（R10.4、R10.3）
# --------------------------------------------------------------------------


def default_plan_assumptions() -> tuple[Assumption, ...]:
    """P0 计划生成路径依赖的三条**可能过期的输入假设**（R10.4，tasks.md 5.11）。

    tasks.md 5.11 逐条点名：
    - **未确认的到货 ETA**：物料到货时间来自登记值，可能与实际不符。
    - **机器修复时间估计**：故障机器的修复时刻是估计，不是确认时刻。
    - **物料在首道工序一次性预留这一简化假设**：排产把整单物料在第一道工序一次性扣减，
      不建模逐工序的领用（design.md 物料模型的简化，ADR 相关）。

    三条都是**确定性的固定清单**——它们是本系统排产模型内在的假设，不随具体计划变化，因此
    在这里写死。`derive_confidence` 据此把置信度定为 `LOW`（≥2 条可能过期的输入）。
    """
    return (
        Assumption(
            kind="DELIVERY_ETA",
            description="物料到货时间取自登记的预计到货 ETA。",
            stale_risk="依赖尚未确认的到货 ETA",
        ),
        Assumption(
            kind="MACHINE_REPAIR_ETA",
            description="故障机器的可用时刻取自修复时间估计。",
            stale_risk="依赖机器修复时间的估计值",
        ),
        Assumption(
            kind="MATERIAL_RESERVATION",
            description="整单物料在首道工序一次性预留，不逐工序建模领用。",
            stale_risk="首道工序一次性预留物料的简化假设",
        ),
    )


def initial_plan_counterfactual() -> NoTradeoff:
    """初始计划生成路径的反事实占位（R10.3、design.md §3.7）。

    形态 A 是初始计划、不是重排，没有 `MOVED` / `REASSIGNED` 作业（也没有可退化到的既有
    方案），因此没有「若维持原方案」可谈。返回 `NoTradeoff` 而不是编一个取舍——任务 8.4 在
    重排路径上填真正的 `Tradeoff`（`pick_pivotal_job` + 沙箱重算 Z）。
    """
    return NoTradeoff(
        reason="本计划为初始生成，非对既有方案的重排，无可比较的关键取舍。"
    )


# --------------------------------------------------------------------------
# 决策证据（R10.2）：为每个 MOVED / REASSIGNED 作业输出一条
# --------------------------------------------------------------------------


def build_decision_evidence(
    delta: PlanDelta,
    active: PlanCandidate,
    candidate: PlanCandidate,
) -> tuple[DecisionEvidence, ...]:
    """为每个 `MOVED` / `REASSIGNED` 作业输出一条 `DecisionEvidence`（R10.2）。

    三项内容逐字对齐 R10.2，全部由确定性组件从两份计划的可观测差异推导——不编造、不调用 LLM：

    - `trigger`（触发原因）：`MOVED` → 「开始时间调整」；`REASSIGNED` → 「资源改派」。这是对
      「这个作业相对原方案发生了什么」的确定性归类（`compute_plan_delta` 已判好类，这里只
      转成人类可读的原因）。
    - `constraint`（被违反或将被违反的约束）：描述**若维持原位置会撞上的约束面**——`MOVED`
      对应时间线/前后序约束（`OPERATION_PRECEDENCE` / `MACHINE_DOUBLE_BOOKING` 语义域），
      `REASSIGNED` 对应资源可用/能力约束（`MACHINE_UNAVAILABLE` / `WORKER_UNAVAILABLE` /
      能力匹配语义域）。这是「为什么必须动」的约束归属，不是一次实际的校验违反（那由
      `Constraint_Validator` 在别处报告）。
    - `resources`（涉及资源）：`MOVED` → 该作业所在的机器与工人（未变，但它们是这次时间调整
      的占用主体）；`REASSIGNED` → 变化的资源，形如 `machine:CNC-01→CNC-02` / `worker:W1→W2`。

    `MOVED` 与 `REASSIGNED` 都只涉及**两计划共有**的作业（`compute_plan_delta` 保证），因此
    两份计划里都能查到该作业的 `ScheduledJob`。`ADDED` / `REMOVED` / `UNCHANGED` 不产出证据
    （R10.2 只要求 MOVED/REASSIGNED）。返回按 `job_id` 升序，供确定性断言。

    纯函数：只依赖 `app.core.delta.PlanDelta` 与 `app.core.scheduler.PlanCandidate`（同属内核，
    不违反分层）。同一对 `(delta, active, candidate)` 两次调用产出逐字段相同的结果。
    """
    active_by_id = {sj.job_id: sj for sj in active.scheduled_jobs}
    cand_by_id = {sj.job_id: sj for sj in candidate.scheduled_jobs}

    evidence: list[DecisionEvidence] = []

    for job_id in delta.reassigned:
        before = active_by_id.get(job_id)
        after = cand_by_id.get(job_id)
        if before is None or after is None:
            continue  # reassigned 必为共有作业；防御性跳过异常数据
        resources: list[str] = []
        if before.machine_id != after.machine_id:
            resources.append(f"machine:{before.machine_id}→{after.machine_id}")
        if before.worker_id != after.worker_id:
            resources.append(f"worker:{before.worker_id}→{after.worker_id}")
        evidence.append(
            DecisionEvidence(
                job_id=job_id,
                trigger="资源改派",
                constraint="原资源不可用或能力不匹配（MACHINE_UNAVAILABLE / "
                "WORKER_UNAVAILABLE / 能力匹配）",
                resources=tuple(resources),
            )
        )

    for job_id in delta.moved:
        before = active_by_id.get(job_id)
        after = cand_by_id.get(job_id)
        if before is None or after is None:
            continue
        evidence.append(
            DecisionEvidence(
                job_id=job_id,
                trigger="开始时间调整",
                constraint="维持原开始时间将违反时间线/前后序约束（"
                "OPERATION_PRECEDENCE / MACHINE_DOUBLE_BOOKING）",
                resources=(f"machine:{after.machine_id}", f"worker:{after.worker_id}"),
            )
        )

    return tuple(sorted(evidence, key=lambda e: e.job_id))


# --------------------------------------------------------------------------
# 载荷构建（喂给那唯一一次 LLM 调用，R21.12）
# --------------------------------------------------------------------------

#: 载荷里 `unschedulable_jobs` 摘要的条数上限（tasks.md 5.11）。多于此数只报总数与前若干条，
#: 避免不可排产清单把载荷撑大——完整清单在 `GET /api/plans/{id}` 的读路径里，解释不重复它。
MAX_UNSCHEDULABLE_SUMMARY = 5

#: 载荷里「关键作业明细」的条数上限（tasks.md 5.11）。解释只需要少数几条代表性作业来支撑
#: 叙述，不需要全量 `scheduled_jobs`——那正是 R21.12 禁止发送的原始明细。
MAX_KEY_JOB_DETAILS = 6


class BaselineComparisonSummary(BaseModel):
    """载荷里的基线对比摘要（六个同口径数值 + 派生的可读差值）。

    这是 `build_explanation_payload` 的一个入参形状：调用方（服务层）从
    `BaselineComparison` 行或 `PlanGenerationResult.baseline` 抽出这六个数传进来，本模块
    不 import 那些外层类型（分层规则）。
    """

    model_config = ConfigDict(frozen=True)

    on_time_rate: float
    baseline_on_time_rate: float
    total_tardiness_minutes: int
    baseline_total_tardiness_minutes: int
    late_order_count: int
    baseline_late_order_count: int


class ComponentSummary(BaseModel):
    """载荷里的一个目标分量摘要（`Objective_Scorer` 的 7 分量之一，R7.2）。"""

    model_config = ConfigDict(frozen=True)

    name: str
    raw_value: float
    weight: float
    weighted_contribution: float


class MachineLoadSummary(BaseModel):
    """载荷里按机器聚合的排产摘要（一台机器一条）。

    **按机器聚合**是把「原始 `scheduled_jobs` 明细」压成「每台机器排了几个作业、占用多少
    分钟」的关键一步（tasks.md 5.11：计划摘要按机器聚合）。这样载荷里出现的是聚合量，而不是
    上百条逐作业行——后者既撑大 token，又正是 R21.12 禁止发送的原始明细。
    """

    model_config = ConfigDict(frozen=True)

    machine_id: str
    job_count: int
    busy_minutes: int


class UnschedulableSummary(BaseModel):
    """载荷里的一条不可排产摘要（≤`MAX_UNSCHEDULABLE_SUMMARY` 条）。"""

    model_config = ConfigDict(frozen=True)

    job_id: str
    order_id: str
    blocking_reason: str


class KeyJobDetail(BaseModel):
    """载荷里的一条关键作业明细（≤`MAX_KEY_JOB_DETAILS` 条）。

    只带支撑叙述所需的少量字段。**不含** `start_time` / `end_time` 的原始时间戳串——那些是
    `Guardrail_Layer` 的 `literals`（比对它们只会误报），且解释叙述引用「哪台机器、多长」比
    引用一个精确到秒的时间戳更有意义。
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    order_id: str
    machine_id: str
    duration_minutes: int


def _tardiness_human(total_minutes: int) -> str:
    """把总拖期分钟数预置成可读的「H 小时 M 分钟」形式（数值一致性措施②，见 guardrail §(d)）。

    模型逐字抄这个字符串即可，无需自己把 315 换算成「5 小时 15 分钟」；`collect_numeric_facts`
    会从这个字符串里把 5 与 15 收进 hours/minutes 桶，因此抄写的换算形式也能匹配。
    """
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def build_explanation_payload(
    *,
    plan_id: str,
    feasibility: str,
    machine_loads: tuple[MachineLoadSummary, ...],
    components: tuple[ComponentSummary, ...],
    baseline: BaselineComparisonSummary,
    unschedulable: tuple[UnschedulableSummary, ...],
    assumptions: tuple[Assumption, ...],
    key_jobs: tuple[KeyJobDetail, ...],
    scheduled_job_count: int,
    unschedulable_job_count: int,
) -> dict[str, Any]:
    """构造喂给那**唯一一次** LLM 调用的紧凑载荷（≈3,000 token，R21.12）。

    载荷里**只有聚合量与少量摘要**：计划摘要按机器聚合（`machine_loads`）、7 个目标分量、
    `baseline_comparison`、`unschedulable_jobs` 摘要（≤5 条 + 总数）、`assumptions`、关键作业
    明细（≤6 条）。**绝不含原始 Order / Machine / Worker / Material 清单**（R21.12）——那些
    是排产的输入，解释不需要它们，发送它们会让这次调用膨胀到 K-10 之外。

    数字全部是确定性组件算出的权威真值；载荷里预置了 `total_tardiness_human` 这类换算形式
    （措施②），并要求模型逐字复制（提示词侧，服务层的解释提示词第 [NUMBERS] 段）。因此
    `Guardrail_Layer` 的闭世界比对能把「文本里的数字不在载荷中」干净地判为编造（§2.7(d)）。

    返回一个可 JSON 化的 dict：服务层把它序列化进 `LlmRequest.user`，同时把它原样交给
    `guard_explanation_numeric_consistency` 作为 `collect_numeric_facts` 的输入——**同一份
    载荷既是模型的输入、又是数值比对的事实集**，闭世界因此成立。

    上限在此**截断而非报错**：`unschedulable` / `key_jobs` 超限时只取前 N 条并在 `_summary`
    里报真实总数，因为解释只需要代表性样本；报错会把一个可解释的计划变成不可解释。
    """
    tardiness_human = _tardiness_human(baseline.total_tardiness_minutes)
    baseline_tardiness_human = _tardiness_human(baseline.baseline_total_tardiness_minutes)

    return {
        "plan_id": plan_id,
        "feasibility": feasibility,
        "summary": {
            "scheduled_job_count": scheduled_job_count,
            "unschedulable_job_count": unschedulable_job_count,
            "machine_count": len(machine_loads),
        },
        # 计划摘要按机器聚合——不是逐作业明细（R21.12）。
        "machine_loads": [ml.model_dump(mode="json") for ml in machine_loads],
        # 7 个目标分量（R7.2）。
        "objective_breakdown": [c.model_dump(mode="json") for c in components],
        # 同口径基线对比 + 预置换算形式（措施②）。
        "baseline_comparison": {
            **baseline.model_dump(mode="json"),
            "total_tardiness_human": tardiness_human,
            "baseline_total_tardiness_human": baseline_tardiness_human,
        },
        # 不可排产摘要 ≤5 条 + 真实总数。
        "unschedulable_summary": {
            "total": unschedulable_job_count,
            "items": [
                u.model_dump(mode="json")
                for u in unschedulable[:MAX_UNSCHEDULABLE_SUMMARY]
            ],
        },
        # 可能过期的假设（R10.4）。
        "assumptions": [a.model_dump(mode="json") for a in assumptions],
        # 关键作业明细 ≤6 条（不含原始时间戳）。
        "key_jobs": [kj.model_dump(mode="json") for kj in key_jobs[:MAX_KEY_JOB_DETAILS]],
    }


def estimate_payload_tokens(payload: dict[str, Any]) -> int:
    """对载荷的 token 数做一个保守估算（≈4 字符/token 的粗口径）。

    用于测试断言载荷落在 ≈3,000 token 量级（K-10 的 4,000 上限留出输出与前缀余量），也供
    服务层在需要时做一次预算感知的日志。不追求精确——真实 token 数以网关账单为准（adapter
    的 `LlmUsage`）；这里只要能把「载荷意外膨胀到发送原始清单那个量级」这类回归抓出来。
    """
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (len(serialized) + 3) // 4


# --------------------------------------------------------------------------
# 模板渲染器（不经 LLM，R25.9 / R10.7 回退）
# --------------------------------------------------------------------------


class TemplateExplanationRenderer:
    """把结构化 `Explanation` 渲成中文散文——**不经 LLM**（design.md §3.7、R25.9）。

    两条路径用它：
    - **降级模式**（`DETERMINISTIC_ONLY`）：Bedrock 不可用时计划生成仍带完整解释（R25.9）。
    - **数值比对回退**（R10.7）：LLM 文本里出现载荷外的数字时，`Guardrail_Layer` 阻止发布
      并回退到本渲染器。

    渲染是**纯函数**：同一份 `Explanation` 必得同一段文本。所有数字都来自 `Explanation` 的
    结构化字段（那些是确定性真值），因此模板文本天然通过闭世界比对——它不会引入任何载荷外的
    数字。措辞刻意平实：模板是回退路径，读得懂比读得美更重要。
    """

    def render(self, explanation: Explanation) -> str:
        """把 `explanation` 渲成一段中文解释文本。"""
        parts: list[str] = []

        parts.append(self._render_decision_evidence(explanation.decision_evidence))
        parts.append(self._render_counterfactual(explanation.counterfactual))
        parts.append(self._render_assumptions(explanation.assumptions))
        parts.append(self._render_confidence(explanation.confidence))

        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _render_decision_evidence(
        evidence: tuple[DecisionEvidence, ...],
    ) -> str:
        if not evidence:
            return "本计划为初始生成，无相对既有方案的作业调整，因此没有逐作业的决策证据。"
        lines = ["决策证据："]
        for ev in evidence:
            resources = "、".join(ev.resources) if ev.resources else "无"
            lines.append(
                f"- 作业 {ev.job_id}：因 {ev.trigger} 调整，"
                f"涉及约束 {ev.constraint}，相关资源 {resources}。"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_counterfactual(counterfactual: Counterfactual) -> str:
        if isinstance(counterfactual, NoTradeoff):
            return f"反事实：{counterfactual.reason}"
        return (
            f"反事实：若维持原方案，作业 {counterfactual.pivotal_job_id} 的目标分量 "
            f"{counterfactual.component} 将从 {counterfactual.current_value} "
            f"变为 {counterfactual.counterfactual_value}"
            f"（选取依据：{counterfactual.selection_basis}）。"
        )

    @staticmethod
    def _render_assumptions(assumptions: tuple[Assumption, ...]) -> str:
        if not assumptions:
            return ""
        lines = ["假设（可能过期的输入）："]
        for a in assumptions:
            lines.append(f"- {a.description}（{a.stale_risk}）")
        return "\n".join(lines)

    @staticmethod
    def _render_confidence(confidence: Confidence) -> str:
        return f"置信度：{confidence.level.value}。判定依据：{confidence.basis}"
