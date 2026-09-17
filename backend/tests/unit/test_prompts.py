"""Agent 静态提示词与前缀的契约测试（任务 5.6，design.md §3.1–§3.2、R21.9、R23）。

断言的四组不变量：
1. **静态前缀逐字节稳定**：同一 Agent 多次装配得到逐字节相同的 `StaticPrefix`（§3.2 的核心
   性质，也是 `Context_Manager` 成为纯函数的前提）。
2. **[AUTHORITY] 段禁止清单**：四个数值键名与两个自治声明词都出现在段 2（R23、R13.11）。
3. **段 2/3/5 是共享常量**：三个 Agent 的 prose 块里 [AUTHORITY]/[PROTOCOL]/[DATA_RULES]
   三段逐字节相同（§3.2「[2][3][5] 三段是共享常量」）。
4. **[TOOLS] 段与白名单一致 / 物理隔离**：Ingestion 的工具 schema 只含 4 个摄取/写入工具，
   看不到任何读排产实体的工具（design.md §3.1 机制 1）。

装配是纯函数（无 I/O），因此全部逐字节可判定，无需 mock。
"""

from __future__ import annotations

from app.agents.prompts import ingestion, planning, risk_monitor
from app.agents.prompts.shared import (
    AUTHORITY_BLOCK,
    DATA_RULES_BLOCK,
    PROTOCOL_BLOCK,
    tool_input_models_for,
)
from app.tools.registry import TOOL_WHITELIST

# --------------------------------------------------------------------------
# 1. 静态前缀逐字节稳定
# --------------------------------------------------------------------------


def test_ingestion_prefix_is_byte_stable() -> None:
    """同一 Agent 多次装配 → 逐字节相同的 blocks。"""
    a = ingestion.build_static_prefix()
    b = ingestion.build_static_prefix()
    assert a.blocks == b.blocks
    assert a.agent == "INGESTION_AGENT"
    assert len(a.blocks) == 2  # (prose, tools)


def test_planning_prefixes_are_byte_stable_per_contract() -> None:
    """Planning 两条路径各自稳定，且彼此的 [OUTPUT] 段不同（契约不同）。"""
    replan_a = planning.build_replan_prefix()
    replan_b = planning.build_replan_prefix()
    explain = planning.build_explanation_prefix()

    assert replan_a.blocks == replan_b.blocks
    assert replan_a.agent == "PLANNING_AGENT"
    # [4] 工具块相同（同一白名单），[0] prose 块因 [OUTPUT] 契约不同而不同。
    assert replan_a.blocks[1] == explain.blocks[1]
    assert replan_a.blocks[0] != explain.blocks[0]


def test_risk_monitor_prefix_is_byte_stable() -> None:
    a = risk_monitor.build_static_prefix()
    b = risk_monitor.build_static_prefix()
    assert a.blocks == b.blocks
    assert a.agent == "RISK_MONITOR_AGENT"


# --------------------------------------------------------------------------
# 2. [AUTHORITY] 段禁止清单
# --------------------------------------------------------------------------


def test_authority_block_forbids_time_and_resource_values() -> None:
    """段 2 明令不得输出 start_time/end_time/machine_id/worker_id 数值（R23、design.md §3.2）。"""
    for key in ("start_time", "end_time", "machine_id", "worker_id"):
        assert key in AUTHORITY_BLOCK, f"[AUTHORITY] 段缺少对 {key} 的禁令"


def test_authority_block_forbids_autonomy_and_impact_declarations() -> None:
    """段 2 明令不得声明 autonomy_level / impact_class（R13.11）。"""
    assert "autonomy_level" in AUTHORITY_BLOCK
    assert "impact_class" in AUTHORITY_BLOCK


def test_authority_block_present_in_every_agent_prose() -> None:
    """每个 Agent 的 prose 块里都含 [AUTHORITY] 段。"""
    prose_blocks = [
        ingestion.build_static_prefix().blocks[0],
        planning.build_replan_prefix().blocks[0],
        risk_monitor.build_static_prefix().blocks[0],
    ]
    for prose in prose_blocks:
        assert AUTHORITY_BLOCK in prose


# --------------------------------------------------------------------------
# 3. 段 2/3/5 共享常量
# --------------------------------------------------------------------------


def test_segments_2_3_5_are_shared_verbatim_across_agents() -> None:
    """三段共享常量在三个 Agent 的 prose 块里逐字节相同（design.md §3.2）。"""
    proses = [
        ingestion.build_static_prefix().blocks[0],
        planning.build_replan_prefix().blocks[0],
        risk_monitor.build_static_prefix().blocks[0],
    ]
    for shared in (AUTHORITY_BLOCK, PROTOCOL_BLOCK, DATA_RULES_BLOCK):
        for prose in proses:
            assert shared in prose


def test_role_blocks_differ_across_agents() -> None:
    """[1 ROLE] 段每 Agent 不同（§3.2「差异只在 [1][4][6]」）。"""
    roles = {ingestion.ROLE_BLOCK, planning.ROLE_BLOCK, risk_monitor.ROLE_BLOCK}
    assert len(roles) == 3


# --------------------------------------------------------------------------
# 4. [TOOLS] 段与白名单一致 / Ingestion 物理隔离
# --------------------------------------------------------------------------


def test_tools_block_matches_whitelist_exactly() -> None:
    """每个 Agent 的 [TOOLS] 段恰好含其白名单的每个工具，一个不多一个不少。"""
    for agent in ("INGESTION_AGENT", "PLANNING_AGENT", "RISK_MONITOR_AGENT"):
        models = tool_input_models_for(agent)  # type: ignore[arg-type]
        assert set(models) == set(TOOL_WHITELIST[agent])  # type: ignore[index]


def test_ingestion_tools_block_is_physically_isolated() -> None:
    """Ingestion 的 [TOOLS] 段只含 4 个摄取/写入工具，看不到任何读排产实体的工具。

    design.md §3.1 机制 1：其白名单里没有任何能读 Order/Machine/Worker/Plan 的工具，
    因此文件里的指令文本不可能作用于排产上下文。
    """
    prefix = ingestion.build_static_prefix()
    tools_block = prefix.blocks[1]
    expected = {
        "read_uploaded_file_preview",
        "propose_column_mapping",
        "validate_mapping",
        "save_import_batch",
    }
    assert set(tool_input_models_for("INGESTION_AGENT")) == expected
    # 排产实体读取工具的名字绝不出现在 Ingestion 的工具块里。
    for forbidden in ("get_orders", "get_machines", "get_workers", "get_current_plan"):
        assert f'name="{forbidden}"' not in tools_block


def test_tools_block_is_byte_stable() -> None:
    """[TOOLS] 段本身逐字节稳定（schema 用 sort_keys 规范化，工具按名排序）。"""
    from app.agents.prompts.shared import build_tools_block

    assert build_tools_block("PLANNING_AGENT") == build_tools_block("PLANNING_AGENT")
