"""`Risk_Monitor_Agent` 的静态提示词与前缀（任务 5.6，design.md §3.3、ADR-001）。

`Risk_Monitor_Agent` **只读**（白名单 = 只读工具 + `scan_risks`，无任何写入工具，R22.8），因此
可以由数据变更事件自动触发而无需人工监督。它的输出契约是 `RiskNarrative`（**P1**；P0 的风险
叙述由 `Risk_Scanner` + 模板渲染覆盖，不经 Agent）。P0 不接线本 Agent，但其静态提示词与前缀
一并定型——模型定义与常量字符串零成本，提前定型让 P1 落地只需接线循环。
"""

from __future__ import annotations

from app.agents.contracts import RiskNarrative
from app.agents.prompts.shared import static_prefix
from app.orchestrator.context_manager import StaticPrefix

__all__ = ["ROLE_BLOCK", "build_static_prefix"]

#: [1 ROLE]——每 Agent 不同。强调「只读、无写权限」。
ROLE_BLOCK = (
    "[ROLE]\n"
    "你是 Risk_Monitor_Agent，负责对确定性风险扫描的结果做归因叙述。"
    "你不是排产器，且没有任何写权限：你只能读取数据与调用风险扫描工具，"
    "不能修改任何生产数据，也不能生成或落地任何计划。你的产物是一段供展示的风险叙述。"
)


def build_static_prefix() -> StaticPrefix:
    """装配 `Risk_Monitor_Agent` 的静态前缀（输出契约 `RiskNarrative`）。"""
    return static_prefix(
        "RISK_MONITOR_AGENT",
        role_block=ROLE_BLOCK,
        output_contract=RiskNarrative,
    )
