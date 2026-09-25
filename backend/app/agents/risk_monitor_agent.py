"""`Risk_Monitor_Agent` 的 LLM 归因叙述驱动（任务 13.2，P1-J ②，R14.5/R14.8/R14.10）。

## 职责与边界（R14.8 只读）

`Risk_Monitor_Agent` 为已由确定性内核算出的风险发现生成一段**归因叙述**——「为什么会发生、
牵连了谁、建议怎么做」。它**只读**：不修改任何生产数据或计划，其工具白名单只含只读工具 +
`scan_risks`（见 `app/tools/registry.py::TOOL_WHITELIST[RISK_MONITOR_AGENT]`）。本模块产出的
是**纯叙述文本**，绝不携带任何可触发写操作的字段，也绝不产生度量数值——度量、severity、
受影响订单全部来自确定性 `RiskFinding`，LLM 只把这些既定事实组织成一段可读的中文归因。

## 为什么不走工具型 ReAct 循环

叙述生成不需要调用任何工具：确定性扫描已经把一条风险的全部事实算好并传给本驱动。因此这里是
一次**单轮**「读懂事实、写一段归因」的调用，而不是多步 tool-calling。这与「只读」是一致的：
连读工具都不必调，写操作更无从谈起。仍复用全仓库唯一的 LLM 出口 `BedrockAdapter.invoke`
（REPLAY/STUB/DISABLED 的分派、cassette、预算记账都在那一层）。

## 回退（R14.5、R14.11）

LLM 不可用（DETERMINISTIC_ONLY → `LlmDisabledError`）、回放缺失、或输出不合格（空串、超长、
或解析不出叙述）时，调用方回退到任务 8.6 的确定性模板叙述，并标 `narrative_source=TEMPLATE`；
成功时标 `LLM`。UI 据此以徽章区分两类叙述来源。

## 分层

住在 `app/agents/`：`test_layering.py` 第 ② 条断言它不 import 内核 / `tools/handlers` / 服务 /
持久层。本模块因此只 import `app.llm.adapter`（LLM 出口）与标准库；`RiskFinding` 的字段以
**普通标量参数**传入（由服务层从内核值对象拆出后传进来），而不是 import `app.core.risk`。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.adapter import BedrockAdapter, LlmRequest

#: `RISK_MONITOR_AGENT` 在编排里的名字（与 `MAX_AGENT_STEPS` / `TOOL_WHITELIST` 的键一致）。
RISK_MONITOR_AGENT = "RISK_MONITOR_AGENT"

#: LLM 单次叙述响应的 token 上限。归因叙述是一小段中文，384 足够且把成本压到最低。
_MAX_RESPONSE_TOKENS = 384

#: 叙述文本的最大长度（与 `RiskNarrative.narrative` 契约的上限口径一致，超长视作不合格→回退）。
_MAX_NARRATIVE_CHARS = 4000


@dataclass(frozen=True)
class RiskFindingFacts:
    """一条风险发现的确定性事实（由服务层从内核 `RiskFinding` 拆出后传入）。

    全部字段都是**已确定的事实**：LLM 不得改动其中任何数值，只据此组织叙述。刻意用普通标量
    而非 import 内核值对象，以满足 Agent 层的分层约束（见模块 docstring）。
    """

    risk_type: str
    severity: str
    entity_type: str
    entity_id: str
    metric_value: float
    threshold_value: float
    affected_order_ids: tuple[str, ...]


def _build_system_prefix() -> tuple[str, ...]:
    """Risk_Monitor_Agent 归因叙述的静态系统前缀（byte-stable）。

    保留 [ROLE]/[AUTHORITY]/[PROTOCOL] 的安全语义：只读、不得臆造数值、不得声明 autonomy/impact、
    只输出一段叙述文本。刻意不给它任何工具 schema——叙述生成不调用工具（只读的最强形式）。
    """
    role = (
        "[ROLE]\n"
        "You are the Risk_Monitor_Agent. Your only responsibility is to write a short English "
        "attribution narrative for a risk finding that has **already been computed by the "
        "deterministic system**: explain where the risk comes from, which orders it involves, "
        "and the recommended next action. You are read-only and do not modify any production "
        "data or plan."
    )
    authority = (
        "[AUTHORITY]\n"
        "You must not change or fabricate any number (metric values, thresholds, and affected "
        "orders are all given by the system; you may only cite them faithfully). You must not "
        "declare autonomy_level or impact_class. You call no tools and output only a narrative."
    )
    protocol = (
        "[PROTOCOL]\n"
        'Output only a single JSON object: {"narrative": "<an English attribution narrative '
        'describing the source, affected orders, and recommended action>"}. '
        "Do not output any characters other than the JSON, do not output a reasoning chain, "
        "and do not invent numbers beyond the given facts."
    )
    prose = "\n\n".join([role, authority, protocol])
    return (prose,)


def _facts_block(facts: RiskFindingFacts) -> str:
    """把一条风险发现的确定性事实渲成给模型的 user 段（byte-stable）。"""
    affected = ", ".join(facts.affected_order_ids) if facts.affected_order_ids else "(none)"
    return (
        "Write an English attribution narrative for the risk finding below "
        "(only cite the given facts, do not invent numbers):\n"
        f"- Risk type: {facts.risk_type}\n"
        f"- Severity: {facts.severity}\n"
        f"- Entity: {facts.entity_type} {facts.entity_id}\n"
        f"- Metric value: {facts.metric_value}\n"
        f"- Threshold: {facts.threshold_value}\n"
        f"- Affected orders: {affected}"
    )


def build_narrative_request(facts: RiskFindingFacts) -> LlmRequest:
    """构造一次归因叙述的 `LlmRequest`（供驱动调用与测试构造 cassette）。"""
    return LlmRequest(
        agent=RISK_MONITOR_AGENT,
        system=_build_system_prefix(),
        user=_facts_block(facts),
        max_tokens=_MAX_RESPONSE_TOKENS,
        temperature=0.0,
    )


class RiskNarrativeDriver:
    """单轮 LLM 归因叙述驱动：装配请求 → `BedrockAdapter.invoke` → 解析出 narrative 文本。

    `generate(facts)` 返回叙述字符串，或 `None` 表示本次不可用/不合格；调用方根据
    启动时模型模式决定标记失败还是使用离线模板。
    在 `STUB`/`REPLAY` 下 `invoke` 从 cassette 取回（不触网）；`DISABLED` 抛 `LlmDisabledError`
    ——由本方法捕获并返回 `None`（回退信号），因此调用方无需分别处理禁用与解析失败。
    """

    def __init__(self, adapter: BedrockAdapter) -> None:
        self._adapter = adapter

    @property
    def live_configured(self) -> bool:
        from app.llm.adapter import LlmMode

        return getattr(self._adapter, "configured_mode", self._adapter.mode) == LlmMode.LIVE

    def generate(self, facts: RiskFindingFacts) -> str | None:
        request = build_narrative_request(facts)
        try:
            response = self._adapter.invoke(request)
        except Exception:  # noqa: BLE001
            # 失败信号：
            # DISABLED 降级抛 LlmDisabledError、REPLAY 回放缺失抛 CassetteMiss、真实调用连续失败
            # 抛 BedrockUnavailableError——任一 LLM 侧失败都返回 None，由调用方如实标记。
            # 只读驱动不吞掉自身的编程错误：invoke 之外没有 try 覆盖，解析在下面独立进行。
            return None
        return _parse_narrative(response.content)


def _parse_narrative(raw: str) -> str | None:
    """从模型输出解析 narrative 文本。非法 JSON / 缺字段 / 空串 / 超长 → None（回退信号）。"""
    import json

    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    narrative = parsed.get("narrative")
    if not isinstance(narrative, str):
        return None
    text = narrative.strip()
    if not text or len(text) > _MAX_NARRATIVE_CHARS:
        return None
    return text


__all__ = [
    "RISK_MONITOR_AGENT",
    "RiskFindingFacts",
    "RiskNarrativeDriver",
    "build_narrative_request",
]
