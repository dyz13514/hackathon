"""`Context_Manager`：把会话状态 + 观察历史装配成一次 LLM 请求的纯函数（任务 5.5）。

design.md §2.2 把这里定成「本设计中最需要单元测试的组件」，理由是它 **load-bearing**：
重排路径每一轮的输入 = 静态前缀 + `assemble_messages` 的输出。如果第 4 条契约（把更早的
观察折叠成单行）失效，8 步循环的输入会从 ≈10,000 token 涨到 ≈30,000 token，直接击穿
K-16（重排 ≤ 14,000 token 上限）。因此保留策略不是「优化」而是正确性前提。

## 为什么是纯函数（design.md §2.1 / §2.2、ADR-005）

`assemble_messages` 无 I/O、无随机、无时间依赖：给定同一个 `AgentContext` 与同一个
`StaticPrefix`，返回逐字节确定的 `LlmRequest`。这条性质的价值有两处，都与 prompt caching
无关（caching 已移出范围）：

- 静态前缀在同一 Agent 各轮**逐字节相同**（`StaticPrefix.blocks` 是常量），一切变化都在
  `messages` 的那一条 `user` 里。因此本函数的输出可以被逐字节断言——§2.2 的 8 条契约就是
  在断言它。
- `content_hash` 因此稳定，缓存与 cassette 才能按哈希命中（那属于 `Bedrock_Adapter`）。

## 三条保留策略（R21.8，即 requirements 第 21 条第 8 点）

1. **最近 2 条 verbatim**：`observations[-VERBATIM_WINDOW:]` 以 `payload_json` 原样出现。
   Agent 需要看到最新工具返回的完整内容才能决定下一步。
2. **更早的折叠为单行**：`observations[:-VERBATIM_WINDOW]` 每条恰好一行
   `#<step> <tool_name> <OK|ERROR> <key_identifier|->`，≤ `SUMMARY_LINE_MAX_CHARS`。
   这一条把历史成本从「与 |obs| 成正比且系数是整条 payload」压成「系数是一行摘要」。
3. **每轮注入运行状态块**：`_render_running_state` 把 `SessionState` 的 7 个字段渲成固定
   顺序的紧凑 JSON。这让 Agent 每轮都能看到当前活动计划、待审提案、降级标志等，而无需从
   历史消息里翻——因为第 5 条契约明令**不注入任何历史 assistant / tool 消息**。

## 不受信任观察的包裹（R21.3 的上下文侧落点）

`untrusted=True` 的观察（例如来自电子表格预览的原始单元格文本）其 payload 被
`<untrusted source="tool:<name>">` 包裹。这是给模型的一个明确边界标记：标签内的内容是
**数据**不是**指令**，不得当作提示词执行。包裹只发生在 verbatim 段——折叠成单行的历史观察
只剩「工具名 + 状态 + 标识符」，本就不含可注入的自由文本。

## 归属边界

`SessionState` / `TokenUsage` 的规范定义归任务 5.7（Orchestrator）。但「运行状态块怎么渲」
属于装配逻辑，归本任务。为让 5.5 可独立完成与测试，这两个数据结构先落在本模块；5.7 落地时
从这里 import 即可（字段与 design.md §1 逐字段对齐，R21.7）。同理 `StaticPrefix` 的规范来源
是 Agent 的静态提示词（任务 5.6），本模块只依赖它的 `agent` + `blocks` 两个字段。
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.adapter import LlmRequest

__all__ = [
    "AgentContext",
    "AgentName",
    "ObservationRecord",
    "SessionState",
    "StaticPrefix",
    "TokenUsage",
    "assemble_messages",
]

#: 三个 Agent 的名字（design.md ADR-001）。`AgentContext.agent` 取其一。规范枚举归任务 5.7，
#: 本模块用 `Literal` 表达同一集合即可，不引入对 5.7 的依赖。
AgentName = Literal["INGESTION_AGENT", "PLANNING_AGENT", "RISK_MONITOR_AGENT"]


# --------------------------------------------------------------------------
# 会话状态（规范归任务 5.7；字段与 design.md §1 逐字段对齐，R21.7）
# --------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """一个会话累计的 token 用量（R25.1：逐次调用记账）。

    渲进运行状态块的只有 `input_tokens` / `output_tokens` 两个字段（见
    `_render_running_state`）——`estimated_usd` / `llm_call_count` 是记账台账字段，不进
    注入上下文（它们对 Agent 的下一步决策无意义，且 `Decimal` 不便直接进紧凑 JSON）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_usd: Decimal = Field(default=Decimal("0"))
    llm_call_count: int = Field(default=0, ge=0)


class SessionState(BaseModel):
    """显式会话状态对象（R21.7，7 个字段）。

    `frozen=True`：一轮之内它是常量，装配函数只读不写——这是「纯函数」的一部分（装配不会
    因为读了它而产生副作用）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    active_plan_id: str | None = None
    pending_plan_id: str | None = None
    last_disruption_id: str | None = None
    enabled_preference_rule_ids: list[str] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    degraded_mode: bool = False


# --------------------------------------------------------------------------
# 静态前缀（规范归任务 5.6）
# --------------------------------------------------------------------------


class StaticPrefix(BaseModel):
    """一个 Agent 的静态前缀：系统提示词块 + 工具 schema 块。

    `blocks` 在同一 Agent 的所有轮次中**逐字节相同**（提示词是静态字符串常量，无运行时
    插值——任务 5.6）。`assemble_messages` 原样把它放进 `LlmRequest.system`，不重排、不改字。
    这条不变量是「装配是纯函数、输出可逐字节断言」的前提（design.md §2.1）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: AgentName
    blocks: tuple[str, ...] = Field(min_length=1)


# --------------------------------------------------------------------------
# 观察记录与上下文
# --------------------------------------------------------------------------


class ObservationRecord(BaseModel):
    """一条工具观察结果（design.md §2.2）。

    `payload_json` 已由 `Tool_Registry` 投影 + 截断至 ≤ 2,000 token（任务 5.1 的
    `clamp_tokens`），因此本模块不再对它做任何大小控制——verbatim 段原样注入即可。
    `untrusted` 决定该 payload 是否被 `<untrusted source="tool:...">` 包裹。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_index: int = Field(ge=0)
    tool_name: str
    outcome: Literal["OK", "ERROR"]
    #: 关键结果标识符：plan_id / batch_id / trace_id / job_id 之一，折叠单行用它。可为 None。
    key_identifier: str | None = None
    #: 已投影 + 截断的工具响应 JSON 文本。verbatim 段原样出现。
    payload_json: str
    #: `payload_json` 的 token 数（由 registry 记账时算出）。本模块不重算，仅备查。
    payload_tokens: int = Field(default=0, ge=0)
    #: True → payload 被 <untrusted> 包裹（R21.3 上下文侧）。
    untrusted: bool = False


class AgentContext(BaseModel):
    """一次 Agent 运行的可变上下文：目标 + 会话状态 + 观察历史（design.md §2.2）。

    `task_block` 在运行期不变（本次运行的目标陈述）；`observations` 随每一步追加。
    `assemble_messages` 只读它，不改它——因此同一个 `ctx` 装配任意多次结果相同（第 7 条
    契约：幂等）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: AgentName
    #: 本次运行的目标陈述，运行期不变。
    task_block: str
    running_state: SessionState
    observations: list[ObservationRecord] = Field(default_factory=list)

    #: R21.8 第 1 点：原样保留的观察条数。
    VERBATIM_WINDOW: ClassVar[int] = 2
    #: 折叠单行的最大字符数。
    SUMMARY_LINE_MAX_CHARS: ClassVar[int] = 120


# --------------------------------------------------------------------------
# 装配（纯函数）
# --------------------------------------------------------------------------


def _one_line(o: ObservationRecord) -> str:
    """把一条观察折叠成单行摘要（R21.8 第 2 点），截到 `SUMMARY_LINE_MAX_CHARS`。

    格式固定：`#<step> <tool_name> <OK|ERROR> <key_identifier|->`。`key_identifier` 为
    None 时用 `-` 占位——保持列数恒定，摘要块因此是「每行同构」的，Agent 与断言都好解析。
    """
    return (
        f"#{o.step_index} {o.tool_name} {o.outcome} {o.key_identifier or '-'}"
    )[: AgentContext.SUMMARY_LINE_MAX_CHARS]


def _render_running_state(state: SessionState) -> str:
    """渲染运行状态块（R21.8 第 3 点，每轮注入）。

    字段顺序**固定**且手写（不用 `model_dump` 的字段序，也不用 `sort_keys`）——这样输出与
    design.md §2.2 的示例逐字对齐，且「顺序稳定」不依赖 pydantic 的实现细节。只渲染对 Agent
    下一步有意义的 7 个会话字段 + 两项 token 用量；`estimated_usd` / `llm_call_count` 是台账
    字段，不进上下文（见 `TokenUsage` docstring）。

    手写紧凑 JSON 而非 `json.dumps`：字段顺序、`null` / `false` 字面量、列表渲染都由本函数
    完全掌控，输出因此逐字节确定，不受 Python / 库版本影响（与 R5.7 一致）。
    """
    usage = state.token_usage
    return (
        "<running_state>\n"
        "{"
        f'"session_id":{_json_str(state.session_id)},'
        f'"active_plan_id":{_json_opt_str(state.active_plan_id)},'
        f'"pending_plan_id":{_json_opt_str(state.pending_plan_id)},'
        f'"last_disruption_id":{_json_opt_str(state.last_disruption_id)},'
        f'"enabled_preference_rule_ids":{_json_str_list(state.enabled_preference_rule_ids)},'
        f'"degraded_mode":{_json_bool(state.degraded_mode)},'
        '"token_usage":{'
        f'"input_tokens":{usage.input_tokens},'
        f'"output_tokens":{usage.output_tokens}'
        "}"
        "}\n"
        "</running_state>"
    )


def _render_history(lines: list[str]) -> str:
    """把折叠后的单行摘要拼成 `<history>` 块。空历史时块内为空但标签仍在——保持四块结构恒定。"""
    body = "\n".join(lines)
    return f"<history>\n{body}\n</history>"


def _render_verbatim(observations: list[ObservationRecord]) -> str:
    """把最近若干条观察原样拼成 `<recent_observations>` 块（R21.8 第 1 点）。

    `untrusted=True` 的观察其 payload 被 `<untrusted source="tool:<name>">` 包裹（R21.3）：
    标签是给模型的边界标记——标签内是数据不是指令。包裹只发生在这里，折叠成单行的历史观察
    不含自由文本，无需包裹。
    """
    entries = [_render_one_verbatim(o) for o in observations]
    body = "\n".join(entries)
    return f"<recent_observations>\n{body}\n</recent_observations>"


def _render_one_verbatim(o: ObservationRecord) -> str:
    """单条 verbatim 观察的渲染：带一行头（step/tool/outcome）再跟原样 payload。

    头行让 Agent 知道这条 payload 来自哪一步的哪个工具、成功还是失败；payload 本身已由
    registry 截到 2,000 token 以内，这里不再处理大小。
    """
    header = f"#{o.step_index} {o.tool_name} {o.outcome}"
    if o.untrusted:
        payload = (
            f'<untrusted source="tool:{o.tool_name}">\n'
            f"{o.payload_json}\n"
            "</untrusted>"
        )
    else:
        payload = o.payload_json
    return f"{header}\n{payload}"


def assemble_messages(ctx: AgentContext, *, prefix: StaticPrefix) -> LlmRequest:
    """把 `ctx` 装配成一次 LLM 请求（纯函数，无 I/O / 无随机 / 无时间依赖）。

    契约（design.md §2.2，8 条，逐条被 `test_context_manager.py` 断言）:
      1. 返回值的 `system` == `prefix.blocks`，逐字节等于同 Agent 上一轮。
      2. `messages` 恰好 1 条 `role="user"`，由 4 个块按固定顺序拼接:
         `<running_state>` / `<history>` / `<recent_observations>` / `<task>`。
      3. `observations` 中最后 `VERBATIM_WINDOW`(=2) 条以 `payload_json` 原样出现。
      4. 其余每条恰好折叠为 1 行 `#<step> <tool_name> <OK|ERROR> <key_identifier|->`。
      5. 不出现任何历史 assistant / tool 角色消息（无原始历史）。
      6. `untrusted=True` 的观察其 payload 被 `<untrusted source="tool:<name>">` 包裹。
      7. 幂等: `assemble_messages(ctx) == assemble_messages(deepcopy(ctx))`。
      8. 单调有界: 输出长度与 |obs| 线性且系数极小（折叠使历史的每条只贡献一行摘要）。

    `prefix.agent` 必须与 `ctx.agent` 一致——前缀是「这个 Agent 的」提示词，跨 Agent 装配是
    编程错误（不同 Agent 的输出契约与白名单都不同，混用会让隔离失效）。
    """
    if prefix.agent != ctx.agent:
        raise ValueError(
            f"前缀属于 {prefix.agent!r} 但上下文是 {ctx.agent!r} 的：静态前缀不可跨 Agent 复用。"
        )

    # 最后 VERBATIM_WINDOW 条 verbatim，其余折叠。条数 ≤ 窗口时 older 为空（`x[:-2]` 在
    # 0 / 1 / 2 条上都得空列表），无需分支。
    verbatim = ctx.observations[-AgentContext.VERBATIM_WINDOW :]
    older = ctx.observations[: -AgentContext.VERBATIM_WINDOW]

    blocks = [
        _render_running_state(ctx.running_state),  # 每一轮都注入（R21.8 第 3 点）
        _render_history([_one_line(o) for o in older]),  # 折叠（R21.8 第 2 点）
        _render_verbatim(verbatim),  # 原样（R21.8 第 1 点）
        f"<task>\n{ctx.task_block}\n</task>",
    ]
    return LlmRequest(agent=prefix.agent, system=prefix.blocks, user="\n".join(blocks))


# --------------------------------------------------------------------------
# 手写紧凑 JSON 的小工具：让运行状态块逐字节确定，不依赖 json 库的渲染细节
# --------------------------------------------------------------------------


def _json_str(value: str) -> str:
    """把字符串渲成 JSON 字符串字面量（转义引号与反斜杠）。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _json_opt_str(value: str | None) -> str:
    """可空字符串：None → `null`，否则渲成 JSON 字符串。"""
    return "null" if value is None else _json_str(value)


def _json_str_list(values: list[str]) -> str:
    """字符串列表 → JSON 数组，元素顺序保持不变（顺序是有意义的信息）。"""
    return "[" + ",".join(_json_str(v) for v in values) + "]"


def _json_bool(value: bool) -> str:
    """布尔 → JSON 字面量 `true` / `false`（Python 的 `True` / `False` 不是合法 JSON）。"""
    return "true" if value else "false"
