"""`Ingestion_Agent` 的静态提示词与前缀（任务 5.6，design.md §3.1、ADR-001）。

`Ingestion_Agent` 处理**最不受信任的输入**（外部文件的全部单元格），与排产逻辑**物理隔离**
（design.md §3.1 四道机制）。[1 ROLE] 段明确它「不是排产器」，其白名单严格 4 个摄取/写入工具
——连「当前有哪些订单」都看不到（`build_tools_block` 从不可变的 `TOOL_WHITELIST` 取集合，因此
[TOOLS] 段里不可能出现任何读排产实体的工具）。输出契约是 `ColumnMappingProposal`。
"""

from __future__ import annotations

from app.agents.contracts import ColumnMappingProposal
from app.agents.prompts.shared import static_prefix
from app.orchestrator.context_manager import StaticPrefix

__all__ = ["ROLE_BLOCK", "build_static_prefix"]

#: [1 ROLE]——每 Agent 不同。一句话职责 + 「你不是排产器」（design.md §3.2）。
ROLE_BLOCK = (
    "[ROLE]\n"
    "你是 Ingestion_Agent，负责把规划员上传的电子表格（可能很脏）映射为结构化字段，"
    "并对单位/日期归一提出建议。你不是排产器，也看不到任何订单、机器、工人或计划数据；"
    "你唯一的产物是一份供人工确认的列映射提案。"
)


def build_static_prefix() -> StaticPrefix:
    """装配 `Ingestion_Agent` 的静态前缀（block0 提示词、block1 工具 schema）。"""
    return static_prefix(
        "INGESTION_AGENT",
        role_block=ROLE_BLOCK,
        output_contract=ColumnMappingProposal,
    )
