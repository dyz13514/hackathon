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
        "尽快联系供应商确认到货，或调整受影响订单的排产顺序以延后该物料的消耗。"
    ),
    RiskType.ZERO_SLACK_ORDER: "复核该订单的交期与优先级，考虑提前排产或与客户协商交期。",
    RiskType.BOTTLENECK_RESOURCE: (
        "评估把部分作业改派到同类替代机器，或错峰安排以降低该机器的负载。"
    ),
    RiskType.OVERCOMMITTED_SHIFT: "为该工人减载或增派同技能工人，避免班次内工时超配导致延误。",
    RiskType.SINGLE_POINT_OF_FAILURE_MACHINE: "识别或准备同能力替代机器，降低对该单点机器的依赖。",
}

#: 每类风险「风险来源」段的度量含义描述，使模板对不同风险读起来自然。
_METRIC_LABEL: dict[RiskType, str] = {
    RiskType.MATERIAL_RUNOUT_FORECAST: "预计在 {metric} 小时内降至 0",
    RiskType.ZERO_SLACK_ORDER: "剩余 slack 仅 {metric} 分钟",
    RiskType.BOTTLENECK_RESOURCE: "利用率达 {metric}",
    RiskType.OVERCOMMITTED_SHIFT: "所需工时 {metric} 分钟超过可用工时 {threshold} 分钟",
    RiskType.SINGLE_POINT_OF_FAILURE_MACHINE: "承担了 {metric} 的已排产作业且无替代机器",
}

_ENTITY_LABEL: dict[str, str] = {
    "MATERIAL": "物料",
    "ORDER": "订单",
    "MACHINE": "机器",
    "WORKER": "工人",
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
    """把一条 `RiskFinding` 渲成三段式中文叙述（R14.5）。纯函数、无 LLM，`source=TEMPLATE`。

    只依赖 `finding` 自身的字段（度量/阈值/实体/受影响订单），因此同一发现必得同一叙述
    （与 R5.7 同一纪律，也便于回归断言）。受影响订单为空时省略第 2 段。
    """
    entity_label = _ENTITY_LABEL.get(finding.entity_type, finding.entity_type)
    source_part = (
        f"{entity_label} {finding.entity_id} {_format_metric(finding)}"
        f"（{finding.severity}）。"
    )

    if finding.affected_order_ids:
        affected_part = f" 受影响订单：{'、'.join(finding.affected_order_ids)}。"
    else:
        affected_part = ""

    action_part = f" 建议：{NEXT_ACTION[finding.risk_type]}"

    return Narrative(text=source_part + affected_part + action_part, source="TEMPLATE")
