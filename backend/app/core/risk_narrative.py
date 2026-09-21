"""风险发现的**确定性模板叙述**（任务 8.6，R14.5、design.md §3.8）。

## P0 用模板，LLM 归因是 P1

R14 第 5 条把 LLM 归因叙述降为 P1。P0 的每一条风险发现仍带完整叙述，只是由确定性模板渲染
——纯函数、无 LLM。三段式逐条对齐 R14.5 要求的三项内容：

1. **风险来源**：度量值 + 阈值 + 实体（「什么、多严重」）；
2. **受影响订单**：`affected_order_ids` 展开（「牵连了谁」）；
3. **建议的下一步动作**：`NEXT_ACTION[risk_type]`（「该做什么」）。

产出的 `Narrative.source` 恒为 `TEMPLATE`——UI 以徽章显示，使模板文本与（P1 的）LLM 文本
可区分（R14.11）。模板渲染无成本，因此 P0 对**全部**发现都渲染叙述，不设 R14.10 的「单次扫描
最多 5 项」上限（那个上限本身是为 LLM 叙述设的成本闸门）。

本模块是纯内核：不 import `sqlalchemy` / `app.db` / `app.services`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.risk import RiskFinding, RiskType

__all__ = ["NEXT_ACTION", "Narrative", "render_template_narrative"]

#: 每类风险的「建议下一步动作」（R14.5 第 3 项）。确定性、面向车间可执行。
NEXT_ACTION: dict[RiskType, str] = {
    RiskType.MATERIAL_RUNOUT_FORECAST: (
        "Contact the supplier to confirm delivery as soon as possible, or reorder the "
        "affected orders to defer consumption of this material."
    ),
    RiskType.ZERO_SLACK_ORDER: (
        "Review the order's due date and priority; consider scheduling it earlier or "
        "renegotiating the due date with the customer."
    ),
    RiskType.BOTTLENECK_RESOURCE: (
        "Consider reassigning some jobs to an equivalent alternate machine, or "
        "stagger the schedule to reduce the load on this machine."
    ),
    RiskType.OVERCOMMITTED_SHIFT: (
        "Reduce this worker's load or add a worker with the same skill to avoid "
        "delays from overcommitting the shift."
    ),
    RiskType.SINGLE_POINT_OF_FAILURE_MACHINE: (
        "Identify or prepare an alternate machine with the same capability to reduce "
        "reliance on this single point of failure."
    ),
}

#: 每类风险「风险来源」段的度量含义描述，使模板对不同风险读起来自然。
_METRIC_LABEL: dict[RiskType, str] = {
    RiskType.MATERIAL_RUNOUT_FORECAST: "is projected to reach 0 within {metric} hours",
    RiskType.ZERO_SLACK_ORDER: "has only {metric} minutes of slack remaining",
    RiskType.BOTTLENECK_RESOURCE: "utilization has reached {metric}",
    RiskType.OVERCOMMITTED_SHIFT: (
        "requires {metric} minutes of work, exceeding the {threshold} minutes available"
    ),
    RiskType.SINGLE_POINT_OF_FAILURE_MACHINE: (
        "carries {metric} of the scheduled jobs with no alternate machine"
    ),
}

_ENTITY_LABEL: dict[str, str] = {
    "MATERIAL": "Material",
    "ORDER": "Order",
    "MACHINE": "Machine",
    "WORKER": "Worker",
}


@dataclass(frozen=True)
class Narrative:
    """一段渲染好的叙述 + 来源标注。`source` 在 P0 恒为 `TEMPLATE`（R14.11）。"""

    text: str
    source: Literal["TEMPLATE", "LLM"]


def _format_metric(finding: RiskFinding) -> str:
    """按风险类型把 metric/threshold 填进「风险来源」段。百分比类做 ×100 展示。"""
    template = _METRIC_LABEL[finding.risk_type]
    metric = finding.metric_value
    threshold = finding.threshold_value
    if finding.risk_type in (
        RiskType.BOTTLENECK_RESOURCE,
        RiskType.SINGLE_POINT_OF_FAILURE_MACHINE,
    ):
        return template.format(metric=f"{float(metric) * 100:.1f}%")
    return template.format(metric=metric, threshold=threshold)


def render_template_narrative(finding: RiskFinding) -> Narrative:
    """把一条 `RiskFinding` 渲成三段式英文叙述（R14.5）。纯函数、无 LLM，`source=TEMPLATE`。

    只依赖 `finding` 自身的字段（度量/阈值/实体/受影响订单），因此同一发现必得同一叙述
    （与 R5.7 同一纪律，也便于回归断言）。受影响订单为空时省略第 2 段。
    """
    entity_label = _ENTITY_LABEL.get(finding.entity_type, finding.entity_type)
    source_part = (
        f"{entity_label} {finding.entity_id} {_format_metric(finding)} "
        f"({finding.severity})."
    )

    if finding.affected_order_ids:
        affected_part = f" Affected orders: {', '.join(finding.affected_order_ids)}."
    else:
        affected_part = ""

    action_part = f" Recommendation: {NEXT_ACTION[finding.risk_type]}"

    return Narrative(text=source_part + affected_part + action_part, source="TEMPLATE")
