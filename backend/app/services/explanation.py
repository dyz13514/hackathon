"""`Explanation_Builder` 的**编排部分**：单次解释调用 + 数值护栏 + 模板回退（任务 5.11）。

design.md Architecture §2.1 的时序图把这条路径画得很清楚：确定性流水线跑完（`Note over O:
至此 LLM token 消耗 = 0`）之后，`Orchestrator` 用 `build_explanation_payload` 造一个紧凑载荷，
经 `Guardrail_Layer` 发起**恰好 1 次** `Bedrock_Adapter.invoke`，拿回解释文本，跑闭世界数值
一致性比对（R10.7），一致则发布 LLM 文本、不一致则回退模板解释。本模块就是那几步的落地。

## 为什么它在服务层而不在 `core/explain.py`

`core/explain.py` 是纯的（分层规则禁止内核 import `app.llm` / `app.db` / `app.services`）：
它产出结构化证据与紧凑载荷，但**不发起**任何调用。真正发起那次网络调用、跑数值比对、按
结果二选一发布、并处理降级异常的**副作用编排**属这一层——它能 import `core.explain`、
`Guardrail_Layer`、`Bedrock_Adapter` 与 Agent 契约。纯计算与副作用的分离让
`build_explanation_payload` / `TemplateExplanationRenderer` 可被逐字节断言，而本模块专注
「哪次调用、失败怎么退」。

## 恰好 1 次调用，且**不发送任何工具 schema**（K-10 的关键）

解释调用只写解释文本——它不选工具、不做多步编排。因此它的 `LlmRequest.system` 里**只有
提示词 prose 块，没有工具 schema 块**（`build_explanation_system` 不拼 `build_tools_block`）。
省下的 ≈2,200 token（工具 schema 段，见 requirements 第 21 条决策说明的 token 经济学）正是
让「计划生成周期 ≤4,000 token」（K-10）能成立的一环。整条路径上 `invoke` 恰好被调用一次
（`test` 断言调用计数为 1）。

## 三条回退路径都落到 `TemplateExplanationRenderer`

1. **降级模式**（`LlmDisabledError`）：Bedrock 已被禁用（`DETERMINISTIC_ONLY`），计划生成仍
   要带解释（R25.9）——渲染结构化 `Explanation` 的模板文本。
2. **调用失败降级**（`BedrockUnavailableError`）：本次 LIVE 调用连续失败已触发降级，同上。
3. **数值不一致**（R10.7）：LLM 文本里出现载荷外的数字，`guard_explanation_numeric_consistency`
   返回 `TemplateExplanation` 并写 `EXPLANATION_NUMERIC_MISMATCH` 审计——回退模板文本。

三者的 `numeric_check` 分别是 `FALLBACK`（后两者明确回退）与 `FALLBACK`（降级也算未经 LLM
校验的模板）。只有「LLM 文本 + 数值比对通过」才 `PASS`。模板文本本身天然通过比对（它的
数字全来自结构化真值），因此回退不会二次触发不一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import Engine

from app.agents.prompts.shared import (
    AUTHORITY_BLOCK,
    DATA_RULES_BLOCK,
    PROTOCOL_BLOCK,
)
from app.core.explain import (
    BaselineComparisonSummary,
    ComponentSummary,
    Explanation,
    KeyJobDetail,
    MachineLoadSummary,
    TemplateExplanationRenderer,
    UnschedulableSummary,
    build_explanation_payload,
    default_plan_assumptions,
    derive_confidence,
    initial_plan_counterfactual,
)
from app.llm.adapter import (
    BedrockAdapter,
    BedrockUnavailableError,
    LlmDisabledError,
    LlmRequest,
)
from app.services.guardrail import guard_explanation_numeric_consistency

__all__ = [
    "EXPLANATION_AGENT",
    "EXPLANATION_MAX_TOKENS",
    "BaselineView",
    "ComponentView",
    "ExplanationInputs",
    "ExplanationResult",
    "NumericCheck",
    "ScheduledJobView",
    "UnschedulableView",
    "assemble_initial_plan_explanation",
    "build_explanation",
    "build_explanation_system",
]

#: 解释调用归属的 Agent 名。与 `Planning_Agent` 的解释路径同源（design.md §3.3：解释由
#: `Planning_Agent` 生成），因此 `content_hash` 覆盖它，解释请求与重排请求不共享缓存条目。
EXPLANATION_AGENT = "PLANNING_AGENT"

#: 解释文本的输出上限。design.md §2.1 的 token 账把输出估为 ≈500 token；给一点余量到 700，
#: 仍远低于 K-10 的 4,000 周期上限（输入 ≈3,000 + 输出 ≤700 + 无工具 schema）。
EXPLANATION_MAX_TOKENS = 700

#: [1 ROLE]——解释路径专用。不复用 `planning.ROLE_BLOCK`（那句强调重排职责）：这次调用
#: **只写解释文本**，因此角色描述聚焦「把结构化载荷转成可读解释」，并重申不改数。
_EXPLANATION_ROLE_BLOCK = (
    "[ROLE]\n"
    "You are the Planning_Agent's explanation subtask. Your only responsibility is to turn the "
    "structured payload below into an English explanation for a production planner. You do not "
    "schedule, do not select tools, and do not perform multi-step operations — you only write "
    "the explanation text."
)

#: [NUMBERS]——解释路径专用，数值一致性的**提示词侧**约束（guardrail §2.7(d) 措施①）。
#: 明确要求：文中每个数字逐字复制自载荷、不换算不四舍五入、且用阿拉伯数字（不用中文数词）。
#: `NUMBER_RE` 只识别阿拉伯数字，中文数词会导致每次都回退——把这条最常见的误报源在输入侧消除。
_NUMBERS_BLOCK = (
    "[NUMBERS]\n"
    "Every number that appears in the text must be copied verbatim from the payload below; do "
    "not convert, do not round, and do not derive new numbers absent from the payload. Always "
    "use Arabic numerals (for example write 5, not \"five\"). The payload already provides "
    "readable converted forms (such as total_tardiness_human); copy them directly when needed."
)

#: [OUTPUT]——解释路径专用。与 ReAct 路径不同：这里**不要求 JSON、不给工具 schema**，
#: 只要求一段纯解释文本。这正是「不发送任何工具 schema」在输出侧的对应约束。
_EXPLANATION_OUTPUT_BLOCK = (
    "[OUTPUT]\n"
    "Output only the explanation text itself; do not output JSON, do not output a reasoning "
    "chain, and do not restate the payload structure. Organize it into fluent English "
    "paragraphs in the order: Decision highlights / Comparison with baseline / Assumptions and "
    "confidence."
)


def build_explanation_system() -> tuple[str, ...]:
    """装配解释调用的 `system` 数组——**只有提示词 prose 块，没有工具 schema 块**。

    段序：[1 ROLE][2 AUTHORITY][3 PROTOCOL][5 DATA_RULES][NUMBERS][OUTPUT]。复用共享的
    [2][3][5] 常量（`Guardrail_Layer` 与提示词纪律一致），加解释专用的 role / numbers /
    output。**刻意不拼** `build_tools_block`——解释调用不选工具，省下 ≈2,200 token 是 K-10
    成立的关键（模块 docstring）。

    返回一个单元素元组 `(prose,)`：`LlmRequest.system` 因此只有一个块，`assemble_body` 把它
    放进 `system[0]`，没有 `system[1]`（工具块）。给定本函数无入参，返回逐字节确定，因此
    `content_hash` 稳定、缓存可命中。
    """
    prose = "\n\n".join(
        [
            _EXPLANATION_ROLE_BLOCK,  # [1]
            AUTHORITY_BLOCK,  # [2]
            PROTOCOL_BLOCK,  # [3]
            DATA_RULES_BLOCK,  # [5]
            _NUMBERS_BLOCK,  # [NUMBERS]
            _EXPLANATION_OUTPUT_BLOCK,  # [OUTPUT]
        ]
    )
    return (prose,)


@dataclass(frozen=True)
class ScheduledJobView:
    """一条已排产作业的解释所需视图（供 `assemble_initial_plan_explanation`）。

    只带聚合与关键字段——机器聚合与「关键作业明细」都从它派生。`duration_minutes` 是
    `[start_time, end_time)` 的整段占用长度（含换型），调用方（API / 流水线结果）算好传入，
    本模块不碰原始时间戳（那些是 `Guardrail_Layer` 的 `literals`）。
    """

    job_id: str
    order_id: str
    machine_id: str
    duration_minutes: int


@dataclass(frozen=True)
class UnschedulableView:
    """一条不可排产作业的解释所需视图。"""

    job_id: str
    order_id: str
    blocking_reason: str


@dataclass(frozen=True)
class ComponentView:
    """一个目标分量的解释所需视图（`Objective_Scorer` 的 7 分量之一）。"""

    name: str
    raw_value: float
    weight: float
    weighted_contribution: float


@dataclass(frozen=True)
class BaselineView:
    """基线对比的六个同口径数值。"""

    on_time_rate: float
    baseline_on_time_rate: float
    total_tardiness_minutes: int
    baseline_total_tardiness_minutes: int
    late_order_count: int
    baseline_late_order_count: int


@dataclass(frozen=True)
class ExplanationInputs:
    """`assemble_initial_plan_explanation` 的产物：结构化证据 + 紧凑载荷。

    两者同源（同一份确定性数据），交给 `build_explanation`：`explanation` 是发布/回退都要
    带的事实骨架，`payload` 既是 LLM 输入又是数值比对的事实集（闭世界）。
    """

    explanation: Explanation
    payload: dict[str, Any]


def _aggregate_machine_loads(
    scheduled: tuple[ScheduledJobView, ...],
) -> tuple[MachineLoadSummary, ...]:
    """把逐作业排产压成按机器聚合的摘要（tasks.md 5.11：计划摘要按机器聚合）。

    这是「绝不发送原始明细」（R21.12）的落点：载荷里出现的是每台机器排了几个作业、占用
    多少分钟，而不是上百条逐作业行。按 `machine_id` 升序排序，使载荷字节稳定（缓存命中）。
    """
    counts: dict[str, int] = {}
    minutes: dict[str, int] = {}
    for job in scheduled:
        counts[job.machine_id] = counts.get(job.machine_id, 0) + 1
        minutes[job.machine_id] = minutes.get(job.machine_id, 0) + job.duration_minutes
    return tuple(
        MachineLoadSummary(
            machine_id=machine_id,
            job_count=counts[machine_id],
            busy_minutes=minutes[machine_id],
        )
        for machine_id in sorted(counts)
    )


def _pick_key_jobs(
    scheduled: tuple[ScheduledJobView, ...],
) -> tuple[KeyJobDetail, ...]:
    """挑代表性的关键作业明细（占用最长的在前，稳定 tie-break 按 job_id）。

    解释叙述只需要少数几条有代表性的作业（占用最长的往往最能说明排产取舍），不需要全量。
    `build_explanation_payload` 会再截到 `MAX_KEY_JOB_DETAILS`；这里做全序是为了「哪几条被选中」
    可复现（占用时长降序、job_id 升序）。
    """
    ordered = sorted(scheduled, key=lambda j: (-j.duration_minutes, j.job_id))
    return tuple(
        KeyJobDetail(
            job_id=j.job_id,
            order_id=j.order_id,
            machine_id=j.machine_id,
            duration_minutes=j.duration_minutes,
        )
        for j in ordered
    )


def assemble_initial_plan_explanation(
    *,
    plan_id: str,
    feasibility: str,
    scheduled: tuple[ScheduledJobView, ...],
    unschedulable: tuple[UnschedulableView, ...],
    components: tuple[ComponentView, ...],
    baseline: BaselineView,
) -> ExplanationInputs:
    """从一份**初始计划**（形态 A）的确定性数据装配结构化证据 + 紧凑载荷（R10、R21.12）。

    初始计划没有 delta（不是重排），因此 `decision_evidence` 为空、`counterfactual` 取
    `NoTradeoff` 占位（`initial_plan_counterfactual`，任务 8.4 前的诚实占位）。`assumptions`
    是 P0 的三条固定可能过期输入（`default_plan_assumptions`），`confidence` 由它们确定性派生
    （`derive_confidence` → LOW）。

    载荷按机器聚合、只带 7 分量 / 基线 / 不可排产摘要 / 假设 / 关键作业明细——**绝不含原始
    实体清单**（R21.12）。返回 `ExplanationInputs`，交给 `build_explanation` 发起单次调用。
    """
    assumptions = default_plan_assumptions()
    explanation = Explanation(
        plan_id=plan_id,
        decision_evidence=(),  # 初始计划无 MOVED/REASSIGNED（任务 8.4 在重排路径填）
        counterfactual=initial_plan_counterfactual(),
        assumptions=assumptions,
        confidence=derive_confidence(assumptions),
    )

    machine_loads = _aggregate_machine_loads(scheduled)
    key_jobs = _pick_key_jobs(scheduled)
    payload = build_explanation_payload(
        plan_id=plan_id,
        feasibility=feasibility,
        machine_loads=machine_loads,
        components=tuple(
            ComponentSummary(
                name=c.name,
                raw_value=c.raw_value,
                weight=c.weight,
                weighted_contribution=c.weighted_contribution,
            )
            for c in components
        ),
        baseline=BaselineComparisonSummary(
            on_time_rate=baseline.on_time_rate,
            baseline_on_time_rate=baseline.baseline_on_time_rate,
            total_tardiness_minutes=baseline.total_tardiness_minutes,
            baseline_total_tardiness_minutes=baseline.baseline_total_tardiness_minutes,
            late_order_count=baseline.late_order_count,
            baseline_late_order_count=baseline.baseline_late_order_count,
        ),
        unschedulable=tuple(
            UnschedulableSummary(
                job_id=u.job_id,
                order_id=u.order_id,
                blocking_reason=u.blocking_reason,
            )
            for u in unschedulable
        ),
        assumptions=assumptions,
        key_jobs=key_jobs,
        scheduled_job_count=len(scheduled),
        unschedulable_job_count=len(unschedulable),
    )
    return ExplanationInputs(explanation=explanation, payload=payload)


class NumericCheck(str, Enum):
    """解释文本的数值一致性检查状态（design.md §2.1、R10.7）。

    - `PASS`——LLM 文本里每个数字都能匹配回载荷，发布 LLM 文本。
    - `FALLBACK`——数值不一致 / 降级 / 调用失败，发布模板文本（未经或未通过 LLM 校验）。
    """

    PASS = "PASS"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True)
class ExplanationResult:
    """一次解释生成的产物（供 `GET /api/plans/{id}/explanation` 序列化）。

    `explanation` 是结构化证据骨架（确定性真值，恒非空）；`narrative` 是最终发布的解释
    **文本**——要么是通过数值比对的 LLM 文本，要么是模板渲染文本。`numeric_check` 标明
    这段文本的来源与校验状态（UI 的 PASS / FALLBACK 徽章，design.md §6）。`fallback_reason`
    在回退时说明原因（降级 / 数值不一致 / 调用失败），`PASS` 时为 `None`。
    """

    explanation: Explanation
    narrative: str
    numeric_check: NumericCheck
    fallback_reason: str | None = None


_RENDERER = TemplateExplanationRenderer()


def build_explanation(
    explanation: Explanation,
    payload: dict[str, Any],
    adapter: BedrockAdapter,
    *,
    trace_id: str | None = None,
    engine: Engine | None = None,
) -> ExplanationResult:
    """发起**恰好 1 次**解释调用并按数值比对结果发布 LLM 文本或模板文本（design.md §2.1）。

    参数：
    - `explanation`——`core.explain` 产出的结构化证据骨架（确定性真值）。
    - `payload`——`build_explanation_payload` 产出的紧凑载荷；**同一份载荷既是模型输入、又是
      数值比对的事实集**（闭世界，§2.7(d)）。
    - `adapter`——`Bedrock_Adapter`（唯一 LLM 出口，R21.10）。`invoke` 在本函数里被调用
      **至多一次**（成功路径恰好一次；降级模式下 `LlmDisabledError` 在 `invoke` 内即抛，
      仍算一次尝试但无网络）。

    三条回退路径（模块 docstring）都渲染 `TemplateExplanationRenderer`，`numeric_check =
    FALLBACK`。只有 LLM 文本经 `guard_explanation_numeric_consistency` 返回 `None`（一致）时
    才发布 LLM 文本、`numeric_check = PASS`。

    `trace_id` 关联本次运行的 `Trace`（R24.5）；`engine` 透传给护栏的审计写入（测试指向临时库）。
    """
    user = _serialize_payload(payload)
    request = LlmRequest(
        agent=EXPLANATION_AGENT,
        system=build_explanation_system(),
        user=user,
        max_tokens=EXPLANATION_MAX_TOKENS,
    )

    try:
        response = adapter.invoke(request)
    except (LlmDisabledError, BedrockUnavailableError) as exc:
        # 降级：Bedrock 不可用，发布模板解释（R25.9）。
        return _template_result(
            explanation, reason=f"LLM_UNAVAILABLE:{type(exc).__name__}"
        )

    fallback = guard_explanation_numeric_consistency(
        response.content,
        payload,
        plan_id=explanation.plan_id,
        trace_id=trace_id,
        actor=EXPLANATION_AGENT,
        engine=engine,
    )
    if fallback is not None:
        # 数值不一致（R10.7）：护栏已写 EXPLANATION_NUMERIC_MISMATCH 审计并回退。
        return _template_result(explanation, reason=fallback.reason)

    return ExplanationResult(
        explanation=explanation,
        narrative=response.content,
        numeric_check=NumericCheck.PASS,
    )


def _template_result(explanation: Explanation, *, reason: str) -> ExplanationResult:
    """回退：渲染模板文本，`numeric_check = FALLBACK`。"""
    return ExplanationResult(
        explanation=explanation,
        narrative=_RENDERER.render(explanation),
        numeric_check=NumericCheck.FALLBACK,
        fallback_reason=reason,
    )


def _serialize_payload(payload: dict[str, Any]) -> str:
    """把紧凑载荷序列化成 `LlmRequest.user`（稳定 JSON：排序键 + 紧凑分隔符）。

    排序键让同一份载荷在任何进程都得到同一段字节，因此 `content_hash` 稳定、缓存可命中
    （R25.7：同输入第二次零支出）。`ensure_ascii=False` 保留中文可读，不影响哈希稳定性
    （编码是确定性的）。
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
