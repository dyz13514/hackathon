"""列映射的 `Ingestion_Agent` ReAct 编排入口（任务 10.4，design.md §3.1、Architecture §2.3）。

把上传文件送进**真实的 Orchestrator + Ingestion_Agent** ReAct 路径（`Intent.INGEST_MAPPING`），
产出经契约校验的 `ColumnMappingProposal`（Agent 契约），并按 spec 持久化到
`import_batches.proposed_mapping`。

## 严格复用现有架构（不新建 LLM 出口）

- 驱动：`app/agents/ingestion_agent.py` 的 `IngestionAgentDriver`（真实）/ `ScriptedIngestionDriver`
  （桩），产出 `AgentTurn`。真实驱动经 `BedrockAdapter.invoke`（全仓库唯一 LLM 出口）——`STUB`
  / `REPLAY` 不触网，`LIVE` 才真实调用；本服务不判断模式。
- 编排：`Orchestrator.run(Intent.INGEST_MAPPING, payload, session_id)`，`agent_drivers` 注入
  上面的驱动、`contracts` 注入 `ingestion_contracts()`、`tool_session` 注入一个能按 `upload_id`
  取 `ParsedFile` 的上下文，供三个摄取工具（`read_uploaded_file_preview` /
  `propose_column_mapping` / `validate_mapping`）在 ReAct 步里调用。
- 工具闸门：`ToolRegistry.invoke` 的 7 步（白名单/schema/执行/投影/截断/记账）——不重复。

## LLM 不能绕过确定性校验

Agent 的 `final` 只是**提案**，写入 `proposed_mapping` 供人工确认；**永不落库**。落库唯一入口
是 `commit_batch(AcceptedMapping)`（构造断言无待确认/缺必填/未处置 unparsed）。因此本服务
产出的提案即便有误，也只能停在「待确认」。

## STUB 下的诚实降级

真实驱动在 `STUB` 且无 cassette 时，`BedrockAdapter` 返回占位文本（非 JSON），ReAct 循环因此
走不到合法 `final`（`outcome != OK`）。此时本服务**回退**到确定性提议（`propose_mapping`）作为
`proposed_mapping`，并在返回结果里标注 `agent_outcome`——既不伪造 LIVE cassette，也不谎称
Agent 成功。测试用 `ScriptedIngestionDriver` 注入合法 `final`，证明 Agent 路径端到端被执行。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.agents.ingestion_agent import (
    IngestionAgentDriver,
    ingestion_contracts,
)
from app.llm.adapter import BedrockAdapter
from app.llm.budget import TokenBudgetManager
from app.orchestrator.orchestrator import AgentDriver, Orchestrator
from app.orchestrator.routing import Intent
from app.orchestrator.tracing import InMemoryTracer
from app.services import ingestion as ingestion_svc
from app.services.spreadsheet import ParsedFile
from app.tools.build import build_registry
from app.tools.registry import InMemoryToolCallRecorder

__all__ = ["IngestionRunResult", "MappingToolSession", "run_ingestion_mapping"]


class _IngestMappingPayload(BaseModel):
    """`Intent.INGEST_MAPPING` 的入口载荷（渲成 `<task>` 块）。只带 upload_id。"""

    model_config = ConfigDict(extra="forbid")

    upload_id: str
    entity_type_hint: str | None = None


@dataclass
class MappingToolSession:
    """`tool_session`：让三个摄取工具按 `upload_id` 取到当前上传的 `ParsedFile`。

    满足 `app/tools/handlers/ingest.py::IngestionToolSession` 协议。刻意只暴露 `parsed_for`
    ——摄取工具不需要 DB 会话（列映射全在内存中的已解析文件上跑）。
    """

    parsed: ParsedFile
    upload_id: str

    def parsed_for(self, upload_id: str) -> ParsedFile:
        if upload_id != self.upload_id:
            raise KeyError(upload_id)
        return self.parsed


@dataclass(frozen=True)
class IngestionRunResult:
    """一次列映射运行的结果：提案（写入 proposed_mapping 的形状）+ Agent 运行结局。

    `proposed_mapping` 是要写进 `import_batches.proposed_mapping` 的 dict。`agent_outcome` 是
    `OrchestratorResult.outcome`（`OK` 表示 Agent 真的产出了合法 `final`）。`from_agent` 为
    True 表示提案来自 Agent 的 `final`；False 表示 STUB 无 cassette 时回退到确定性提议。
    """

    proposed_mapping: dict
    agent_outcome: str
    trace_id: str
    from_agent: bool


def run_ingestion_mapping(
    *,
    parsed: ParsedFile,
    upload_id: str,
    adapter: BedrockAdapter,
    entity_type_hint: str | None = None,
    driver: AgentDriver | None = None,
    session_id: str = "ingest",
) -> IngestionRunResult:
    """跑 `Ingestion_Agent` 的列映射 ReAct 路径，返回提案 + 运行结局。

    `driver` 缺省用真实 `IngestionAgentDriver(adapter)`；测试注入 `ScriptedIngestionDriver`
    以在 STUB 环境端到端验证 Agent 路径。Agent 成功（`OK` 且 `final` 是 `ColumnMappingProposal`）
    → 用 `final` 作为 `proposed_mapping`；否则回退到确定性 `propose_mapping`（见模块 docstring）。
    """
    resolved_driver: AgentDriver = (
        driver if driver is not None else IngestionAgentDriver(adapter)
    )
    tool_session = MappingToolSession(parsed=parsed, upload_id=upload_id)
    orch = Orchestrator(
        registry=build_registry(recorder=InMemoryToolCallRecorder()),
        budget=TokenBudgetManager(),
        tracer=InMemoryTracer(),
        agent_drivers={"INGESTION_AGENT": resolved_driver},
        contracts=ingestion_contracts(),
        tool_session=tool_session,
    )
    payload = _IngestMappingPayload(upload_id=upload_id, entity_type_hint=entity_type_hint)
    result = orch.run(Intent.INGEST_MAPPING, payload, session_id=session_id)

    if result.outcome == "OK" and result.final is not None:
        proposed = _agent_final_to_proposed(result.final, parsed, entity_type_hint)
        return IngestionRunResult(
            proposed_mapping=proposed,
            agent_outcome=result.outcome,
            trace_id=result.trace_id,
            from_agent=True,
        )

    # 诚实降级：STUB 无 cassette 时 Agent 走不到合法 final。用确定性提议，不伪造 Agent 成功。
    fallback = ingestion_svc.propose_mapping(parsed, entity_type_hint=entity_type_hint)
    return IngestionRunResult(
        proposed_mapping=fallback,
        agent_outcome=result.outcome,
        trace_id=result.trace_id,
        from_agent=False,
    )


def _agent_final_to_proposed(
    final: object, parsed: ParsedFile, entity_type_hint: str | None
) -> dict:
    """把 Agent 的 `ColumnMappingProposal`（含 `columns`）转成 `proposed_mapping` 的落库形状。

    Agent 契约给出 `entity_type` 与逐列 `MappedColumn`（target_field/source_column/confidence）；
    确定性提议补齐每字段的 `status` / `sample_values` 与归一化提议、缺必填清单——即
    「LLM 定映射、确定性补校验元数据」。这保证写入 `proposed_mapping` 的结构与人工确认 UI 及
    `AcceptedMapping` 消费的形状一致，且状态判定（低置信→NEEDS_CONFIRMATION）由确定性规则给出。
    """
    entity_type = getattr(final, "entity_type", None) or entity_type_hint or "ORDER"
    columns = getattr(final, "columns", []) or []
    header_index = {h: i for i, h in enumerate(parsed.header)}
    required = ingestion_svc.REQUIRED_FIELDS.get(entity_type, [])

    field_mappings: list[dict] = []
    mapped_targets: set[str] = set()
    for col in columns:
        target = getattr(col, "target_field", None)
        source = getattr(col, "source_column", None)
        confidence = float(getattr(col, "confidence", 0.0))
        if target is None or source is None:
            continue
        idx = header_index.get(source)
        samples = (
            ingestion_svc._column_samples(parsed, idx) if idx is not None else []
        )
        status = (
            "NEEDS_CONFIRMATION"
            if target in required and confidence < ingestion_svc._CONFIDENCE_THRESHOLD
            else "AUTO_ACCEPTED"
        )
        field_mappings.append(
            {
                "target_field": target,
                "source_column": source,
                "confidence": confidence,
                "sample_values": samples,
                "status": status,
            }
        )
        mapped_targets.add(target)

    missing = [
        {"target_field": f, "reason": "Agent 未映射且必填"}
        for f in required
        if f not in mapped_targets
    ]
    normalisations = ingestion_svc._propose_normalisations(
        parsed, [ingestion_svc._norm_header(h) for h in parsed.header]
    )
    return {
        "entity_type": entity_type,
        "entity_type_confidence": float(getattr(final, "entity_type_confidence", 0.5)),
        "field_mappings": field_mappings,
        "missing_required_fields": missing,
        "normalisations": normalisations,
    }
