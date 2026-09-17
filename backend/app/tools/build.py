"""装配真实的 `dict[str, ToolSpec]`（任务 5.2）：把 `models.py` 的契约与 `handlers/` 的实现
绑成注册表可消费的 26 个 `ToolSpec`。

## 为什么装配住在这里，而不在 `registry.py` 或 `handlers/__init__.py`

- 不在 `registry.py`：那是 5.1 的**机具**（`invoke()` 的 7 步、白名单、投影、截断、记账），
  它按传入的 `specs` 工作、不 import 任何具体 handler——契约测试正是靠这个解耦喂进 fixture
  spec。让 registry import handlers 会把机具与实现重新耦上，且 handlers import 内核/服务/ORM，
  会让「registry 可在无库环境里测」这一性质丢失。
- 不在 `handlers/__init__.py`：`build_specs()` 要 import `models` 与 `registry`，而
  `handlers/` 被分层扫描盯着「不被 `agents/` import」；把装配单独成模，handler 包保持纯实现。

## `build_registry()` 是应用装配点

生产装配（API 启动 / `Orchestrator` 构造）调 `build_registry(session, ...)`，得到一个真
`ToolRegistry`：spec 用 `build_specs()`、recorder 用写 `tool_calls` 的 `DbToolCallRecorder`、
`audit_write` 用 `app.db.audit.append` 的适配器。`app/agents/**` 不 import 本模块（它只经
`ToolRegistry.invoke()` 触达能力）——分层扫描对 agents 的 import 图断言这一点。

## `kind` 与 `supports_projection` 的取值

`kind` 按工具所属集合（`READ_ONLY_TOOLS` / `COMPUTE_TOOLS` / `WRITE_TOOLS` / `INGEST_TOOLS`）
判定。`supports_projection` 只对只读工具置真（R22.12）——计算与写入返回句柄/聚合，本就紧凑，
投影无意义。`get_preference_rules` 的 `max_response_tokens` 收到 1,200、`get_value_metrics`
收到 800（design.md §2.4 的逐工具上限），其余取默认 2,000。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.tools import models as m
from app.tools.handlers import compute, ingest, read, write
from app.tools.registry import (
    COMPUTE_TOOLS,
    INGEST_TOOLS,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    ToolCallRecorder,
    ToolRegistry,
    ToolSpec,
)

__all__ = ["build_specs", "build_registry"]


def _kind(name: str) -> str:
    if name in READ_ONLY_TOOLS:
        return "READ"
    if name in COMPUTE_TOOLS:
        return "COMPUTE"
    if name in WRITE_TOOLS:
        return "WRITE"
    if name in INGEST_TOOLS:
        return "INGEST"
    raise KeyError(f"{name} 不属于任何工具集合")  # pragma: no cover - 装配期自洽断言


#: 逐工具的 (input_model, output_model, handler) 三元组。键的集合必须恰好等于
#: `READ_ONLY_TOOLS | COMPUTE_TOOLS | WRITE_TOOLS | INGEST_TOOLS`（26 个）——契约测试
#: `test_every_whitelisted_tool_is_registered` 断言白名单里的每个工具都在此有 spec。
_SPEC_TABLE: dict[str, tuple[type, type, Callable[..., Any]]] = {
    # --- 只读（10） ---
    "get_orders": (m.GetOrdersIn, m.OrderListOut, read.get_orders),
    "get_products": (m.GetProductsIn, m.ProductListOut, read.get_products),
    "get_inventory": (m.GetInventoryIn, m.InventoryOut, read.get_inventory),
    "get_machines": (m.GetMachinesIn, m.MachineListOut, read.get_machines),
    "get_workers": (m.GetWorkersIn, m.WorkerListOut, read.get_workers),
    "get_current_plan": (m.GetCurrentPlanIn, m.PlanHandle, read.get_current_plan),
    "get_preference_rules": (
        m.GetPreferenceRulesIn,
        m.PreferenceRuleListOut,
        read.get_preference_rules,
    ),
    "get_risk_findings": (
        m.GetRiskFindingsIn,
        m.RiskFindingListOut,
        read.get_risk_findings,
    ),
    "get_value_metrics": (m.GetValueMetricsIn, m.ValueMetricsOut, read.get_value_metrics),
    "get_job_details": (m.GetJobDetailsIn, m.JobDetailListOut, read.get_job_details),
    # --- 计算（9） ---
    "generate_schedule": (
        m.GenerateScheduleIn,
        m.PlanHandle,
        compute.generate_schedule,
    ),
    "check_constraints": (
        m.CheckConstraintsIn,
        m.ValidationOut,
        compute.check_constraints,
    ),
    "evaluate_schedule": (
        m.EvaluateScheduleIn,
        m.ObjectiveBreakdownOut,
        compute.evaluate_schedule,
    ),
    "compare_plans": (m.ComparePlansIn, m.ComparePlansOut, compute.compare_plans),
    "get_affected_jobs": (
        m.GetAffectedJobsIn,
        m.AffectedJobsOut,
        compute.get_affected_jobs,
    ),
    "classify_impact": (m.ClassifyImpactIn, m.ImpactOut, compute.classify_impact),
    "run_scenario": (m.RunScenarioIn, m.ScenarioOut, compute.run_scenario),
    "scan_risks": (m.ScanRisksIn, m.RiskFindingListOut, compute.scan_risks),
    "compute_baseline": (
        m.ComputeBaselineIn,
        m.BaselineComparisonOut,
        compute.compute_baseline,
    ),
    # --- 写入（4） ---
    "save_proposed_plan": (
        m.SaveProposedPlanIn,
        m.SaveProposedPlanOut,
        write.save_proposed_plan,
    ),
    "register_disruption": (
        m.RegisterDisruptionIn,
        m.RegisterDisruptionOut,
        write.register_disruption,
    ),
    "propose_preference_rule": (
        m.ProposePreferenceRuleIn,
        m.ProposePreferenceRuleOut,
        write.propose_preference_rule,
    ),
    "save_import_batch": (
        m.SaveImportBatchIn,
        m.SaveImportBatchOut,
        write.save_import_batch,
    ),
    # --- 摄取（3） ---
    "read_uploaded_file_preview": (
        m.ReadPreviewIn,
        m.FilePreviewOut,
        ingest.read_uploaded_file_preview,
    ),
    "propose_column_mapping": (
        m.ProposeColumnMappingIn,
        m.ColumnMappingProposal,
        ingest.propose_column_mapping,
    ),
    "validate_mapping": (
        m.ValidateMappingIn,
        m.ValidateMappingOut,
        ingest.validate_mapping,
    ),
}

#: 逐工具的响应 token 上限覆盖（design.md §2.4）。未列出者取 `ToolSpec` 默认 2,000。
_MAX_TOKENS_OVERRIDE: dict[str, int] = {
    "get_preference_rules": 1_200,
    "get_value_metrics": 800,
}


def build_specs() -> dict[str, ToolSpec]:
    """装配 26 个真实 `ToolSpec`。只读工具 `supports_projection=True`（R22.12）。"""
    specs: dict[str, ToolSpec] = {}
    for name, (input_model, output_model, handler) in _SPEC_TABLE.items():
        kind = _kind(name)
        specs[name] = ToolSpec(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            input_model=input_model,
            output_model=output_model,
            handler=handler,
            max_response_tokens=_MAX_TOKENS_OVERRIDE.get(name, 2_000),
            supports_projection=(kind == "READ"),
        )
    return specs


def build_registry(
    *,
    recorder: ToolCallRecorder,
    audit_write: Callable[..., Any] | None = None,
    handler_timeout_s: float = 30.0,
) -> ToolRegistry:
    """生产装配点：用真实 spec 建一个 `ToolRegistry`（见模块 docstring）。

    `recorder` 由调用方注入（生产传 `DbToolCallRecorder(session)`，测试/冒烟传
    `InMemoryToolCallRecorder`）；`audit_write` 传 `app.db.audit.append` 的适配器，用于
    `TOOL_NOT_PERMITTED` 审计（R22.10）。
    """
    return ToolRegistry(
        build_specs(),
        recorder=recorder,
        audit_write=audit_write,
        handler_timeout_s=handler_timeout_s,
    )


def spec_table_keys() -> Mapping[str, None]:  # pragma: no cover - 便于装配期自检
    """返回 spec 表覆盖的工具名集合（供装配期断言与调试）。"""
    return {name: None for name in _SPEC_TABLE}
