"""`Context_Manager.assemble_messages` 的契约测试（任务 5.5，**非可选**，承接原属性 22）。

design.md §2.2 把 `Context_Manager` 称作「本设计中最需要单元测试的组件」——它 load-bearing：
重排路径每一轮输入 = 静态前缀 + `assemble_messages` 输出。若第 4 条契约（折叠）失效，8 步
循环的输入会从 ≈10,000 token 涨到 ≈30,000 token，直接击穿 K-16。因此本文件对 §2.2 的 8 条
契约**逐条**断言，并额外断言输入 token 关于观察条数的增长斜率 ≤ 40 token/条——那是折叠有效
的经济学后果，也是这条不变量的第一道探测（第二道在 `EVAL-015`）。

装配是纯函数（无 I/O / 无随机 / 无时间依赖），因此这些断言全部逐字节可判定，无需任何 mock。
"""

from __future__ import annotations

import copy

import pytest

from app.orchestrator.context_manager import (
    AgentContext,
    ObservationRecord,
    SessionState,
    StaticPrefix,
    TokenUsage,
    assemble_messages,
)
from app.tools.clamp import count_tokens

# --------------------------------------------------------------------------
# 夹具：一个固定的 Planning_Agent 前缀 + 会话状态 + 观察生成器
# --------------------------------------------------------------------------

PREFIX = StaticPrefix(
    agent="PLANNING_AGENT",
    blocks=("<系统提示词 段1-3+段5>", "<工具 schema 段4>"),
)


def _state() -> SessionState:
    """一个非平凡的会话状态：与 design.md §2.2 示例同构（有活动计划、有偏好规则）。"""
    return SessionState(
        session_id="SESS-0001",
        active_plan_id="PLAN-0007",
        pending_plan_id=None,
        last_disruption_id="DSR-0003",
        enabled_preference_rule_ids=["PR-001", "PR-003"],
        token_usage=TokenUsage(input_tokens=8020, output_tokens=410),
        degraded_mode=False,
    )


def _obs(step: int, *, untrusted: bool = False) -> ObservationRecord:
    """第 `step` 步的一条观察。payload 是一段有代表性的句柄 JSON（≈120 token 量级）。"""
    payload = (
        '{"plan_id":"PLAN-%04d","plan_version":1,"feasibility":"PARTIAL",'
        '"objective":{"total_score":4187.5,"late_order_count":2,'
        '"total_tardiness_minutes":315,"total_changeover_minutes":90,'
        '"preference_penalty":60.0},"scheduled_job_count":27,'
        '"unschedulable_count":3,"trace_id":"TRC-%04d"}'
    ) % (step, step)
    return ObservationRecord(
        step_index=step,
        tool_name="get_current_plan",
        outcome="OK",
        key_identifier=f"PLAN-{step:04d}",
        payload_json=payload,
        payload_tokens=count_tokens(payload),
        untrusted=untrusted,
    )


def _ctx(n_obs: int, *, untrusted_last: bool = False) -> AgentContext:
    """一个带 `n_obs` 条观察的上下文。`untrusted_last` 把最后一条标为不受信任。"""
    observations = [_obs(i) for i in range(1, n_obs + 1)]
    if untrusted_last and observations:
        observations[-1] = _obs(n_obs, untrusted=True)
    return AgentContext(
        agent="PLANNING_AGENT",
        task_block="重排今日计划以吸收 DSR-0003 的机器停机，最小化拖期。",
        running_state=_state(),
        observations=observations,
    )


def _user(ctx: AgentContext) -> str:
    """装配后那唯一一条 user 消息的文本。"""
    return assemble_messages(ctx, prefix=PREFIX).user


# --------------------------------------------------------------------------
# 契约 1：system == prefix.blocks，逐字节
# --------------------------------------------------------------------------


def test_contract_1_system_equals_prefix_verbatim() -> None:
    """返回值的 system 逐字节等于 prefix.blocks（同 Agent 各轮不变）。"""
    req_few = assemble_messages(_ctx(1), prefix=PREFIX)
    req_many = assemble_messages(_ctx(20), prefix=PREFIX)

    assert req_few.system == PREFIX.blocks
    # 观察条数从 1 涨到 20，system 一字不变——变化只在 messages。
    assert req_many.system == req_few.system == PREFIX.blocks


# --------------------------------------------------------------------------
# 契约 2：messages 恰 1 条 user，四块按固定顺序
# --------------------------------------------------------------------------


def test_contract_2_single_user_message_with_four_blocks_in_order() -> None:
    """messages 恰 1 条 role=user，由四块按固定顺序拼接。"""
    body = assemble_messages(_ctx(5), prefix=PREFIX).assemble_body()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"

    user = body["messages"][0]["content"]
    # 四块的开标签按固定顺序出现，且各恰好一次。
    order = [
        user.index("<running_state>"),
        user.index("<history>"),
        user.index("<recent_observations>"),
        user.index("<task>"),
    ]
    assert order == sorted(order), "四块顺序必须是 running_state → history → recent → task"
    for tag in ("<running_state>", "<history>", "<recent_observations>", "<task>"):
        assert user.count(tag) == 1, f"{tag} 应恰好出现一次"


def test_contract_2_running_state_block_has_fixed_field_order() -> None:
    """运行状态块字段顺序固定，含 7 个会话字段折算出的键 + 两项 token 用量。"""
    user = _user(_ctx(0))
    # 键按 design.md §2.2 示例的固定顺序出现。
    keys_in_order = [
        '"session_id"',
        '"active_plan_id"',
        '"pending_plan_id"',
        '"last_disruption_id"',
        '"enabled_preference_rule_ids"',
        '"degraded_mode"',
        '"token_usage"',
        '"input_tokens"',
        '"output_tokens"',
    ]
    positions = [user.index(k) for k in keys_in_order]
    assert positions == sorted(positions)
    # 具体值渲对：活动计划、偏好规则数组、降级标志、用量。
    assert '"active_plan_id":"PLAN-0007"' in user
    assert '"pending_plan_id":null' in user
    assert '"enabled_preference_rule_ids":["PR-001","PR-003"]' in user
    assert '"degraded_mode":false' in user
    assert '"input_tokens":8020' in user
    assert '"output_tokens":410' in user
    # 台账字段不进上下文。
    assert "estimated_usd" not in user
    assert "llm_call_count" not in user


# --------------------------------------------------------------------------
# 契约 3：最后 2 条 verbatim（payload_json 原样）
# --------------------------------------------------------------------------


def test_contract_3_last_two_observations_appear_verbatim() -> None:
    """最后 VERBATIM_WINDOW(=2) 条以完整 payload_json 原样出现。"""
    ctx = _ctx(5)
    user = _user(ctx)
    last_two = ctx.observations[-2:]
    for o in last_two:
        assert o.payload_json in user, "最近 2 条的完整 payload 应原样出现"
    # 更早的第 3 条（PLAN-0003）的完整 payload 不应原样出现（它被折叠了）。
    third_from_end = ctx.observations[-3]
    assert third_from_end.payload_json not in user


# --------------------------------------------------------------------------
# 契约 4：更早的每条折叠为单行，格式固定
# --------------------------------------------------------------------------


def test_contract_4_older_observations_folded_to_one_line() -> None:
    """更早的每条恰好折叠为一行 `#<step> <tool> <OK|ERROR> <key|->`。"""
    ctx = _ctx(5)
    user = _user(ctx)
    # 第 1、2、3 条被折叠（5 条 − 2 verbatim = 3 条折叠）。
    for step in (1, 2, 3):
        assert f"#{step} get_current_plan OK PLAN-{step:04d}" in user
    # history 块里折叠行数 == 更早观察数。
    history = user.split("<history>\n", 1)[1].split("\n</history>", 1)[0]
    folded_lines = [ln for ln in history.splitlines() if ln.strip()]
    assert len(folded_lines) == 3


def test_contract_4_missing_key_identifier_uses_dash() -> None:
    """key_identifier 为 None 时折叠行用 `-` 占位，保持列数恒定。"""
    obs = [
        ObservationRecord(
            step_index=1,
            tool_name="scan_risks",
            outcome="ERROR",
            key_identifier=None,
            payload_json="{}",
        ),
        _obs(2),
        _obs(3),
    ]
    ctx = AgentContext(
        agent="PLANNING_AGENT",
        task_block="t",
        running_state=_state(),
        observations=obs,
    )
    user = _user(ctx)
    assert "#1 scan_risks ERROR -" in user


# --------------------------------------------------------------------------
# 契约 5：无任何历史 assistant / tool 角色消息
# --------------------------------------------------------------------------


def test_contract_5_no_historical_assistant_or_tool_messages() -> None:
    """装配后 messages 里只有一条 user，绝无 assistant / tool 角色（无原始历史）。"""
    body = assemble_messages(_ctx(20), prefix=PREFIX).assemble_body()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user"]
    assert "assistant" not in roles
    assert "tool" not in roles


# --------------------------------------------------------------------------
# 契约 6：untrusted 观察被 <untrusted source="tool:..."> 包裹
# --------------------------------------------------------------------------


def test_contract_6_untrusted_payload_is_wrapped() -> None:
    """untrusted=True 的观察其 payload 被 <untrusted source="tool:<name>"> 包裹。"""
    ctx = _ctx(2, untrusted_last=True)
    user = _user(ctx)
    assert '<untrusted source="tool:get_current_plan">' in user
    assert "</untrusted>" in user
    # 被包裹的 payload 仍原样在标签之间。
    wrapped_payload = ctx.observations[-1].payload_json
    segment = user.split('<untrusted source="tool:get_current_plan">\n', 1)[1]
    assert segment.startswith(wrapped_payload)


def test_contract_6_trusted_payload_is_not_wrapped() -> None:
    """受信任观察不加包裹标签。"""
    user = _user(_ctx(2, untrusted_last=False))
    assert "<untrusted" not in user


# --------------------------------------------------------------------------
# 契约 7：幂等（对 deepcopy 的 ctx 装配结果相同）
# --------------------------------------------------------------------------


def test_contract_7_idempotent() -> None:
    """assemble_messages(ctx) == assemble_messages(deepcopy(ctx))；且同 ctx 多次装配相同。"""
    ctx = _ctx(7)
    once = assemble_messages(ctx, prefix=PREFIX)
    again = assemble_messages(copy.deepcopy(ctx), prefix=PREFIX)
    assert once == again
    # 同一个 ctx 连续两次装配也逐字段相同（装配不改 ctx，纯函数）。
    assert assemble_messages(ctx, prefix=PREFIX) == once


# --------------------------------------------------------------------------
# 契约 8 + 增长斜率：单调有界，斜率 ≤ 40 token/条
# --------------------------------------------------------------------------


def test_contract_8_growth_slope_is_bounded() -> None:
    """输入 token 关于观察条数的增长斜率 ≤ 40 token/条（折叠有效的经济学后果）。

    对 0 / 1 / 2 / 3 / 20 条观察实测输入 token，做最小二乘线性回归取斜率上界。折叠若失效
    （更早的观察也整条注入），每条会贡献 ≈120+ token，斜率会立刻冲破 40。
    """
    counts = [0, 1, 2, 3, 20]
    tokens = [_input_tokens(n) for n in counts]

    slope = _least_squares_slope(counts, tokens)
    assert slope <= 40, (
        f"增长斜率 {slope:.2f} token/条 超过 40 上界；折叠可能失效。实测点={list(zip(counts, tokens))}"
    )
    # 单调不减：加观察不会让输入变短（sanity）。
    assert tokens == sorted(tokens)


def test_contract_8_snapshots_are_deterministic() -> None:
    """0/1/2/3/20 条的装配快照逐字节确定（同输入两次装配结果相同）。"""
    for n in (0, 1, 2, 3, 20):
        ctx = _ctx(n)
        assert _user(ctx) == _user(_ctx(n))


def test_snapshot_zero_observations_has_empty_history_and_recent() -> None:
    """0 条观察：history 与 recent_observations 块存在但内容为空，四块结构不塌。"""
    user = _user(_ctx(0))
    assert "<history>\n\n</history>" in user
    assert "<recent_observations>\n\n</recent_observations>" in user


def test_snapshot_one_observation_is_verbatim_no_history() -> None:
    """1 条观察：它在 verbatim 段原样出现，history 段为空（无可折叠的更早观察）。"""
    ctx = _ctx(1)
    user = _user(ctx)
    assert ctx.observations[0].payload_json in user
    history = user.split("<history>\n", 1)[1].split("\n</history>", 1)[0]
    assert history == ""


def test_snapshot_twenty_observations_folds_eighteen() -> None:
    """20 条观察：18 条折叠为单行，最后 2 条 verbatim。"""
    ctx = _ctx(20)
    user = _user(ctx)
    history = user.split("<history>\n", 1)[1].split("\n</history>", 1)[0]
    folded = [ln for ln in history.splitlines() if ln.strip()]
    assert len(folded) == 18
    # 最后两条（#19、#20）不在折叠段，而以完整 payload 出现。
    assert ctx.observations[-1].payload_json in user
    assert ctx.observations[-2].payload_json in user
    assert "#18 get_current_plan OK PLAN-0018" in user  # 第 18 条被折叠
    assert "#19 get_current_plan OK PLAN-0019" not in history  # 第 19 条不在折叠段


# --------------------------------------------------------------------------
# 跨 Agent 前缀复用是编程错误
# --------------------------------------------------------------------------


def test_prefix_agent_must_match_context_agent() -> None:
    """前缀属于的 Agent 与上下文的 Agent 不一致时拒绝装配。"""
    wrong_prefix = StaticPrefix(agent="INGESTION_AGENT", blocks=("x",))
    with pytest.raises(ValueError, match="不可跨 Agent 复用"):
        assemble_messages(_ctx(1), prefix=wrong_prefix)


# --------------------------------------------------------------------------
# 辅助：输入 token 计数与线性回归
# --------------------------------------------------------------------------


def _input_tokens(n_obs: int) -> int:
    """一次请求的输入 token 估计 = system 前缀 + user 消息，用 clamp.count_tokens 度量。

    与 `Bedrock_Adapter` 的记账口径一致（同一个 count_tokens 上界估计）。斜率断言度量的是
    「user 消息随观察增长」的部分——system 是常量，不随 n_obs 变。
    """
    req = assemble_messages(_ctx(n_obs), prefix=PREFIX)
    return count_tokens(list(req.system)) + count_tokens(req.user)


def _least_squares_slope(xs: list[int], ys: list[int]) -> float:
    """最小二乘直线斜率。纯算术，无外部依赖（回归本身也要确定性）。"""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var = sum((x - mean_x) ** 2 for x in xs)
    return cov / var
