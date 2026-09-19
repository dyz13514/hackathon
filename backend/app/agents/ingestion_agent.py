"""`Ingestion_Agent` 的 ReAct 驱动与 Orchestrator 注入（任务 10.4，design.md §3.1、ADR-001）。

与 `app/agents/planning_agent.py` 逐条同构——`Orchestrator._react_loop` 通过 `AgentDriver`
（`next_turn(ctx) -> AgentTurn | None`）取「下一轮模型输出」，本模块为 `INGESTION_AGENT`
提供两个驱动：

1. **`IngestionAgentDriver`**（真实驱动）：用 `Context_Manager.assemble_messages(ctx, prefix=...)`
   装配请求，交 `BedrockAdapter.invoke()`，把响应文本包成 `AgentTurn`。`STUB` / `REPLAY` 下
   `invoke` 从 cassette 取回（不触网），`LIVE` 才真实调用——本类不判断模式（全仓库唯一 LLM
   出口是 `BedrockAdapter`）。

2. **`ScriptedIngestionDriver`**（脚本化桩驱动）：按预置回合脚本逐轮吐 JSON 字符串，**不触达
   Bedrock、不需要 cassette**。让 ReAct 循环在无真实/录制 LLM 响应时也能端到端测试：典型序列
   `read_uploaded_file_preview → propose_column_mapping → validate_mapping`，最后一轮吐一个
   `{"final": {...}}`（`ColumnMappingProposal` Agent 契约）。这是用户裁决的 approach (b)。

## 为什么 LLM 无法绕过确定性校验

Agent 的 `final`（`ColumnMappingProposal`）只是**提案**，写入 `import_batches.proposed_mapping`
供人工确认——它**永不落库**。落库唯一入口是 `commit_batch(AcceptedMapping)`，其构造函数断言
无 `NEEDS_CONFIRMATION`、无缺必填、`unparsed_cells` 已处置（`app/services/ingestion.py`）。
`validate_mapping` 是确定性工具（R2.8），LLM 不参与校验。因此即便模型提出错误映射，也只能
停在「待确认」，不可能静默落库（K-07）。

## 分层

住在 `app/agents/`，`tests/structure/test_layering.py` 第 ② 条断言它不 import 内核或
`tools/handlers`。因此只 import `app.agents.*`（契约与提示词）、`app.llm.adapter`（LLM 出口）、
`app.orchestrator.context_manager`（装配纯函数）与 `Orchestrator` 的协议类型。桩驱动只吐字符串。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.agents.contracts import ColumnMappingProposal
from app.agents.prompts.ingestion import build_static_prefix
from app.llm.adapter import BedrockAdapter, LlmRequest
from app.orchestrator.context_manager import AgentContext, StaticPrefix, assemble_messages
from app.orchestrator.orchestrator import AgentTurn

#: `INGESTION_AGENT` 在编排里的名字（与 `MAX_AGENT_STEPS` / `TOOL_WHITELIST` 的键一致）。
INGESTION_AGENT = "INGESTION_AGENT"


class IngestionAgentDriver:
    """真实 ReAct 驱动：每轮装配上下文 → 调 `BedrockAdapter.invoke` → 返回 `AgentTurn`。

    `prefix` 是本次运行的静态前缀（`build_static_prefix()`，逐轮逐字节相同）。`STUB` / `REPLAY`
    下 `invoke` 从 cassette 取回响应；`LIVE` 才真实调用。本类不判断模式。
    """

    def __init__(
        self,
        adapter: BedrockAdapter,
        *,
        prefix: StaticPrefix | None = None,
        max_tokens: int = 800,
    ) -> None:
        self._adapter = adapter
        self._prefix = prefix if prefix is not None else build_static_prefix()
        self._max_tokens = max_tokens

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None:
        request: LlmRequest = assemble_messages(ctx, prefix=self._prefix)
        if request.max_tokens != self._max_tokens:
            request = request.model_copy(update={"max_tokens": self._max_tokens})
        response = self._adapter.invoke(request)
        return AgentTurn(raw=response.content)


def build_ingestion_driver(adapter: BedrockAdapter) -> IngestionAgentDriver:
    """列映射路径的真实驱动（输出契约 `ColumnMappingProposal`）。"""
    return IngestionAgentDriver(adapter, prefix=build_static_prefix())


# --------------------------------------------------------------------------
# 脚本化桩驱动：不触达 Bedrock，逐轮返回预置 JSON
# --------------------------------------------------------------------------


@dataclass
class ScriptedIngestionDriver:
    """按预置脚本逐轮产出 ReAct 回合的桩驱动（approach b：ReAct 可用桩测试）。

    `turns` 是一串 JSON 字符串，每次 `next_turn` 弹出下一条。每条要么是
    `{"action": {"tool": ..., "args": {...}}}`（工具调用轮），要么是 `{"final": {...}}`
    （终止轮）。脚本耗尽后返回 `None`（模拟模型停止响应）。它不 import 内核、不触网——只吐
    字符串；真实解析/校验数值由确定性工具 handler 在 `_dispatch_action` 里算出并作为观察回给
    循环，本驱动的 `final` 只携带映射提案。
    """

    turns: Sequence[str]
    _index: int = field(default=0, init=False)

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None:  # noqa: ARG002 - 桩忽略 ctx
        if self._index >= len(self.turns):
            return None
        raw = self.turns[self._index]
        self._index += 1
        return AgentTurn(raw=raw)


def scripted_mapping_final(
    *,
    entity_type: str,
    entity_type_confidence: float,
    columns: list[dict],
    mapping_rationale: str = "",
) -> str:
    """构造一个 `{"final": {...}}` 的 JSON 字符串（`ColumnMappingProposal` Agent 契约形状）。

    供脚本化桩驱动与测试构造终止轮。`columns` 每项形如
    `{"target_field": ..., "source_column": ..., "confidence": ...}`（`MappedColumn`）。
    """
    return json.dumps(
        {
            "final": {
                "entity_type": entity_type,
                "entity_type_confidence": entity_type_confidence,
                "columns": columns,
                "mapping_rationale": mapping_rationale,
            }
        },
        ensure_ascii=False,
    )


def scripted_action(tool: str, **args: object) -> str:
    """构造一个 `{"action": {"tool": ..., "args": {...}}}` 的 JSON 字符串（工具调用轮）。"""
    return json.dumps({"action": {"tool": tool, "args": args}}, ensure_ascii=False)


def ingestion_contracts() -> dict[str, type]:
    """`Orchestrator(contracts=...)` 的注入：列映射路径的输出契约。

    `INGEST_MAPPING` 路由到 `INGESTION_AGENT`，输出契约 `ColumnMappingProposal`（Agent 契约，
    与工具输出模型同名但不同形，见 `contracts.py` 模块 docstring）。
    """
    return {INGESTION_AGENT: ColumnMappingProposal}


__all__ = [
    "INGESTION_AGENT",
    "ColumnMappingProposal",
    "IngestionAgentDriver",
    "ScriptedIngestionDriver",
    "build_ingestion_driver",
    "ingestion_contracts",
    "scripted_action",
    "scripted_mapping_final",
]
