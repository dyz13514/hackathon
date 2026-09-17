"""`Tool_Registry`：Agent 与系统流水线触达任何能力的唯一入口（任务 5.1，R22 全部）。

design.md §2.3 把这里定为「白名单与 2,000 token 上限的单点强制处」。它的价值不在于它做了
多少事，而在于它是**唯一**能做这些事的地方——两种执行形态（确定性流水线 A 与 ReAct 循环 B）
都只能经 `invoke()` 调用内核，因此 `Trace` / `Audit_Log` / 预算记账对两者一视同仁，而白名单、
schema 校验、投影、截断这四道闸门没有任何调用方能绕过。

## `invoke()` 的 7 步为什么顺序固定

顺序不是风格问题，每一步都依赖前一步的结论：

1. **白名单**在最前——不通过则 handler **根本不被调用**（R22.10）。把它放在输入校验之后，
   等于让一个越权调用先跑一遍参数校验，泄漏「这个工具存不存在、要什么参数」这类信息。
2. **输入 schema**——失败作为**观察结果**回给 Agent（R22.2），不是抛异常：Agent 要能读到
   「你少了个必填字段」然后自己改，这是 ReAct 循环自我修正的前提。
3. **执行**（带超时保护）——只有前两关都过了才真正跑 handler。
4. **输出 schema**——防「实现漂移」：handler 哪天多返回了逐 `ScheduledJob` 明细，这一步用
   `output_model`（`PlanHandle` 根本没有明细字段）把它挡在类型层面（ADR-004 的一道防线）。
5. **字段投影**——只读工具按 `fields` 收窄（R22.12）。
6. **token 截断**——硬压到 `max_response_tokens`，超出标 `truncated`（R22.16 / R25.6）。
7. **记账**——写 `tool_calls`（R22.11）：调用方、参数摘要、结果摘要、耗时、token 数。

## 四道结构性保障（design.md §2.3，不是靠约定）

1. `handler` 全部住在 `tools/handlers/` 且不被 `agents/` import——`test_layering.py` 扫描。
2. `TOOL_WHITELIST` 是 `MappingProxyType` 包裹的 `frozenset`：运行期不可变，没有
   `add_tool_for_agent()` 之类的 API。想给某个 Agent 加权限，只能改这段源码。
3. `output_model` 里根本没有 `scheduled_jobs: list[...]` 字段（§2.4 的 `PlanHandle`），且
   契约测试断言任何 `output_model` 的 array 字段都声明 `maxItems`——明细泄漏在类型层面不可能。
4. `save_proposed_plan` 只能写 `status=PENDING_APPROVAL`（枚举参数硬编码），没有工具能写
   `ACTIVE`（R22.9 / R23.4）。

白名单只有 `TOOL_WHITELIST` 这**一层**，按调用方划分。按路径二次收窄（`PATH_TOOL_SUBSET`）
是 prompt caching 降级路径的配套机具，已随 caching 一并移出范围（requirements 第 3 节拒绝清单）。

## 本模块（5.1）与 5.2 的分工

5.1 交付**机具**：`ToolSpec` / `TOOL_WHITELIST` / `invoke()` 的 7 步 / 记账 / 投影 / 截断，
以及工具名集合（`READ_ONLY_TOOLS` 等）。具体的 `input_model` / `output_model` / `handler`
由 5.2 落地。为了机具在 5.1 就可测，注册表按传入的 `specs` 工作（构造时注入），因此契约测试
可以喂进代表性的 fixture spec，而 5.2 落地后换成真 spec 即可，`invoke()` 一字不改。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ValidationError

from app.tools.clamp import clamp_tokens, project

__all__ = [
    "ALL_TOOLS",
    "COMPUTE_TOOLS",
    "INGEST_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_WHITELIST",
    "WRITE_TOOLS",
    "CallerId",
    "InMemoryToolCallRecorder",
    "ToolCallRecord",
    "ToolCallRecorder",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "ToolRegistry",
    "UnknownToolError",
    "args_digest",
    "summarise",
]

# --------------------------------------------------------------------------
# 调用方与工具名集合
# --------------------------------------------------------------------------

#: 四类调用方（design.md §2.2 共享物表）。`SYSTEM_PIPELINE` 是形态 A（确定性流水线），
#: 其余三个是形态 B（ReAct）里的 Agent。白名单按这一维划分。
CallerId = Literal[
    "SYSTEM_PIPELINE",
    "INGESTION_AGENT",
    "PLANNING_AGENT",
    "RISK_MONITOR_AGENT",
]

#: 只读工具（R22.3）。名字集合在此定义，具体 spec 由 5.2 注入。
READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_orders",
        "get_products",
        "get_inventory",
        "get_machines",
        "get_workers",
        "get_current_plan",
        "get_preference_rules",
        "get_risk_findings",
        "get_value_metrics",
        "get_job_details",
    }
)

#: 确定性计算工具（R22.4）。
COMPUTE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "check_constraints",
        "generate_schedule",
        "evaluate_schedule",
        "compare_plans",
        "get_affected_jobs",
        "classify_impact",
        "run_scenario",
        "scan_risks",
        "compute_baseline",
    }
)

#: 写入工具（R22.5）。`propose_preference_rule` 仅 P1 的 DISTIL_PREFERENCE 路线使用。
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "save_proposed_plan",
        "register_disruption",
        "propose_preference_rule",
        "save_import_batch",
    }
)

#: 摄取工具（R22.6）。`save_import_batch` 同时属于写入类，摄取 Agent 也用它——因此它出现在
#: `WRITE_TOOLS` 与 `Ingestion_Agent` 的白名单里，但不进 `INGEST_TOOLS`（此集合专指只有摄取
#: Agent 才碰、系统流水线不碰的三个纯摄取工具，用于从 `SYSTEM_PIPELINE` 白名单里排除）。
INGEST_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "read_uploaded_file_preview",
        "propose_column_mapping",
        "validate_mapping",
    }
)

#: 全部工具名。用于 `SYSTEM_PIPELINE` 白名单（= 全部 − 纯摄取）与契约测试的笛卡尔积表。
ALL_TOOLS: Final[frozenset[str]] = (
    READ_ONLY_TOOLS | COMPUTE_TOOLS | WRITE_TOOLS | INGEST_TOOLS
)

#: 按调用方划分的工具白名单（design.md §2.3）。
#:
#: `MappingProxyType` 包裹 + `frozenset` 值 = 运行期彻底不可变（第 2 道结构性保障）：既没有
#: 改这个映射的 API，`TOOL_WHITELIST["PLANNING_AGENT"].add(...)` 也会抛 `AttributeError`。
#: 想调整权限，只能改这里的源码——这让「扩权」成为一次有意识的、会进 code review 的决定。
#:
#: - `INGESTION_AGENT`：严格 4 个（R22.7）。连「当前有哪些订单」都看不到——它处理最不受信任的
#:   外部文件，与排产逻辑物理隔离（design.md Architecture §3.1）。
#: - `RISK_MONITOR_AGENT`：只读 + `scan_risks`（R22.8），不碰任何写入工具。
#: - `PLANNING_AGENT`：只读 + 计算 + 三个提案/写入工具，但**没有** `save_import_batch`
#:   （那是摄取 Agent 的），也没有任何能置 `ACTIVE` 的工具（R22.9）。
#: - `SYSTEM_PIPELINE`：全部工具减去纯摄取三件——流水线不做列映射，那是 ReAct 路径。
TOOL_WHITELIST: Mapping[CallerId, frozenset[str]] = MappingProxyType(
    {
        "INGESTION_AGENT": frozenset(
            {
                "read_uploaded_file_preview",
                "propose_column_mapping",
                "validate_mapping",
                "save_import_batch",
            }
        ),
        "RISK_MONITOR_AGENT": frozenset(READ_ONLY_TOOLS | {"scan_risks"}),
        "PLANNING_AGENT": frozenset(
            READ_ONLY_TOOLS
            | COMPUTE_TOOLS
            | {"save_proposed_plan", "register_disruption", "propose_preference_rule"}
        ),
        "SYSTEM_PIPELINE": frozenset(ALL_TOOLS - INGEST_TOOLS),
    }
)


# --------------------------------------------------------------------------
# ToolSpec / ToolContext / ToolResult
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整声明（design.md §2.3）。

    `frozen=True`：spec 在注册后不可改。一个可变的 spec 意味着「某次调用后 handler 被换掉」
    这种最难排查的问题成为可能。

    `max_response_tokens` 默认 2,000（R22.16 / R25.6）；`supports_projection` 默认 `False`，
    只有只读工具置真（R22.12）——计算与写入工具返回的是句柄或聚合值，本就紧凑，投影无意义。
    """

    name: str
    kind: Literal["READ", "COMPUTE", "WRITE", "INGEST"]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[BaseModel, "ToolContext"], BaseModel]
    max_response_tokens: int = 2_000
    supports_projection: bool = False


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的执行上下文。

    handler 通过它拿到会话、追踪 ID 与降级标志——而**不是**通过全局状态。这让 handler
    可测（注入一个测试用的 context 即可），也让「同一个 handler 在流水线与 ReAct 里被同样
    地调用」成为事实。

    `session`：类型放宽为 `Any` 是刻意的——`app.tools` 不应硬依赖 `sqlalchemy.orm.Session`
    的具体类型（那会让契约测试也被迫拖进 ORM）。handler（5.2，住在 `tools/handlers/`）自己
    知道它拿到的是什么。`trace_id` 关联记账写入的 `tool_calls.trace_id`。
    """

    trace_id: str
    session: Any = None
    step_id: str | None = None
    degraded_mode: bool = False


@dataclass(frozen=True)
class ToolResult:
    """`invoke()` 的返回。成功与失败是同一个类型的两种形态。

    为什么错误不抛异常而是作为一个 `ok=False` 的结果返回：ReAct 循环要把「工具失败」当作
    **观察结果**喂回给模型（R22.2、Error Handling §3），异常会把它变成需要 try/except 的
    控制流，而那正是我们不想让编排层去做的事——编排层只该看 `result.ok` 然后决定下一步。
    """

    ok: bool
    payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: Any = None
    truncated: bool = False
    tokens: int = 0

    @classmethod
    def success(
        cls, payload: dict[str, Any], *, truncated: bool, tokens: int
    ) -> "ToolResult":
        return cls(ok=True, payload=payload, truncated=truncated, tokens=tokens)

    @classmethod
    def error(cls, code: str, detail: Any = None) -> "ToolResult":
        return cls(ok=False, error_code=code, error_detail=detail)


class UnknownToolError(KeyError):
    """白名单放行了一个工具名，但注册表里没有对应的 `ToolSpec`。

    这**不是**一个可作为观察结果回给 Agent 的错误——它意味着白名单与已注册 spec 集合不一致，
    是配置错误而非调用错误。契约测试断言这两个集合一致，因此它在正确配置下永不发生；一旦发生，
    应当在启动/测试期就炸出来，而不是被 Agent 静默重试。
    """


# --------------------------------------------------------------------------
# 记账
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallRecord:
    """一次工具调用的记账条目（R22.11）。字段对齐 `tool_calls` 表列。"""

    trace_id: str
    step_id: str | None
    caller: str
    tool_name: str
    args_digest: str
    args_json: dict[str, Any]
    result_summary: str
    result_tokens: int
    truncated: bool
    outcome: str
    duration_ms: int


class ToolCallRecorder(Protocol):
    """记账写入面。`invoke()` 只依赖这个协议，不依赖任何具体持久化实现。

    这道抽象是为了让 5.1 的机具**现在**就可测：契约测试注入 `InMemoryToolCallRecorder`，
    断言每次 `invoke`（含被白名单拒绝的调用）都恰好落一条记录；生产装配注入一个写
    `tool_calls` 表的 DB recorder（需要一个已存在的 `Trace`，那是任务 5.7 / 5.12 的语境）。
    """

    def record(self, entry: ToolCallRecord) -> None: ...


@dataclass
class InMemoryToolCallRecorder:
    """把记账条目收集进内存列表。测试与 `DETERMINISTIC_ONLY` 冒烟用。"""

    entries: list[ToolCallRecord] = field(default_factory=list)

    def record(self, entry: ToolCallRecord) -> None:
        self.entries.append(entry)


# --------------------------------------------------------------------------
# 摘要与摘要哈希
# --------------------------------------------------------------------------


def _canonical(payload: Any) -> str:
    """稳定字节序的 JSON（记账摘要与哈希共用），与 clamp 的口径一致。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def args_digest(raw_args: Mapping[str, Any]) -> str:
    """入参的短哈希，落进 `tool_calls.args_digest`（R22.11）。

    存哈希而非原参数本身进 `args_digest` 列：原参数另存 `args_json`。哈希让「同一组参数被
    调了几次」一眼可查（相同参数 → 相同 digest），也让 `TOOL_NOT_PERMITTED` 审计能在不落原始
    参数的前提下留下可比对的指纹（design.md §2.3 的 `args_digest=digest(raw_args)`）。
    """
    return hashlib.sha256(_canonical(dict(raw_args)).encode("utf-8")).hexdigest()[:16]


def summarise(payload: Mapping[str, Any]) -> str:
    """结果摘要，落进 `tool_calls.result_summary`（R22.11）。

    不是完整结果——完整结果的 token 数已单独记在 `result_tokens`。摘要给的是「一眼能认出
    这次调用返回了什么」：顶层键名，加上若干标识符类字段的值（`plan_id` / `batch_id` /
    `trace_id` 这类，与 `Context_Manager` 折叠单行时取的 `key_identifier` 同源）。摘要本身
    也截到 200 字符，避免记账反而成了体积来源。
    """
    keys = sorted(payload.keys())
    id_bits = [
        f"{key}={payload[key]}"
        for key in ("plan_id", "batch_id", "trace_id", "scenario_id")
        if key in payload
    ]
    summary = "keys=" + ",".join(keys)
    if id_bits:
        summary += " " + " ".join(id_bits)
    return summary[:200]


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


class ToolRegistry:
    """`invoke()` 的宿主。构造时注入 spec 集合与记账器，此后不可增删 spec。

    `specs` 在构造时冻结成一个内部 dict：注册表实例一旦建成，`self._specs` 不再改动
    （没有 `register()` 之类的方法）。这与 `TOOL_WHITELIST` 的不可变性是同一思路——能力集合
    是启动期决定的常量，不是运行期状态。
    """

    def __init__(
        self,
        specs: Mapping[str, ToolSpec],
        *,
        recorder: ToolCallRecorder,
        audit_write: Callable[..., Any] | None = None,
        handler_timeout_s: float = 30.0,
    ) -> None:
        self._specs: dict[str, ToolSpec] = dict(specs)
        self._recorder = recorder
        self._handler_timeout_s = handler_timeout_s
        # 审计写入注入进来而非 import：`app.db.audit.append` 拖着 ORM，契约测试不该被迫连库。
        # 生产装配传入 `functools.partial(append, ...)` 的适配器（见 `_audit_not_permitted`）。
        self._audit_write = audit_write

    @property
    def tool_names(self) -> frozenset[str]:
        """已注册的工具名集合。契约测试用它与白名单的并集比对。"""
        return frozenset(self._specs)

    def _audit_not_permitted(
        self, *, caller: str, tool_name: str, digest: str, trace_id: str
    ) -> None:
        """写一条 `TOOL_NOT_PERMITTED` 审计（R22.10）。

        `audit_write` 未注入时静默跳过——契约测试关注的是「handler 不被调用、返回错误码、
        记账落一条」，审计写入本身在 `test_audit_immutable` / 审计单元测试里单独覆盖，这里
        不强制拖库。生产装配必然注入它（否则越权尝试不留痕，违反 R22.10），装配处的测试保证
        这一点。
        """
        if self._audit_write is None:
            return
        self._audit_write(
            event_category="TOOL_NOT_PERMITTED",
            event_type="TOOL_CALL_DENIED",
            actor=caller,
            payload={"tool": tool_name, "args_digest": digest},
            trace_id=trace_id,
        )

    def invoke(
        self, caller: CallerId, tool_name: str, raw_args: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """7 步闸门。见模块 docstring 的顺序说明。"""
        raw = dict(raw_args)
        digest = args_digest(raw)
        started = monotonic()

        # ① 白名单（注册表级强制，非文档约定）— R22.10
        if tool_name not in TOOL_WHITELIST[caller]:
            self._audit_not_permitted(
                caller=caller, tool_name=tool_name, digest=digest, trace_id=ctx.trace_id
            )
            self._record(
                ctx,
                caller=caller,
                tool_name=tool_name,
                digest=digest,
                raw=raw,
                summary="denied",
                tokens=0,
                truncated=False,
                outcome="TOOL_NOT_PERMITTED",
                started=started,
            )
            # handler 在此之前一步都没被触达——白名单是最前的闸门。
            return ToolResult.error("TOOL_NOT_PERMITTED", f"{caller} 无权调用 {tool_name}")

        spec = self._specs.get(tool_name)
        if spec is None:
            # 白名单放行但无 spec：配置不一致，炸出来而不是回给 Agent（见 UnknownToolError）。
            raise UnknownToolError(
                f"{tool_name} 在 {caller} 的白名单内但未注册 ToolSpec；"
                f"白名单与已注册工具集合必须一致（见契约测试）"
            )

        # ② 输入 schema —— 失败作为观察结果回给 Agent（R22.2），不抛异常
        try:
            args = spec.input_model.model_validate(raw)
        except ValidationError as exc:
            self._record(
                ctx,
                caller=caller,
                tool_name=tool_name,
                digest=digest,
                raw=raw,
                summary="input_invalid",
                tokens=0,
                truncated=False,
                outcome="TOOL_INPUT_INVALID",
                started=started,
            )
            return ToolResult.error("TOOL_INPUT_INVALID", exc.errors())

        # ③ 执行（超时保护）
        try:
            out = spec.handler(args, ctx)
        except TimeoutError:
            self._record(
                ctx,
                caller=caller,
                tool_name=tool_name,
                digest=digest,
                raw=raw,
                summary="timeout",
                tokens=0,
                truncated=False,
                outcome="TOOL_TIMEOUT",
                started=started,
            )
            return ToolResult.error("TOOL_TIMEOUT", f"{tool_name} 执行超时")

        # ④ 输出 schema（防实现漂移泄漏明细行）—— ADR-004 的类型层防线
        validated = spec.output_model.model_validate(out)
        full = validated.model_dump(mode="json")

        # ⑤ 字段投影（只读工具且请求了 fields 时）— R22.12
        requested_fields = getattr(args, "fields", None) if spec.supports_projection else None
        payload = project(full, requested_fields)

        # ⑥ 硬截断至 max_response_tokens，标 truncated — R22.16 / R25.6
        payload, truncated, n_tokens = clamp_tokens(payload, spec.max_response_tokens)

        # ⑦ 记账 — R22.11
        self._record(
            ctx,
            caller=caller,
            tool_name=tool_name,
            digest=digest,
            raw=raw,
            summary=summarise(payload),
            tokens=n_tokens,
            truncated=truncated,
            outcome="OK",
            started=started,
        )
        return ToolResult.success(payload, truncated=truncated, tokens=n_tokens)

    def _record(
        self,
        ctx: ToolContext,
        *,
        caller: str,
        tool_name: str,
        digest: str,
        raw: dict[str, Any],
        summary: str,
        tokens: int,
        truncated: bool,
        outcome: str,
        started: float,
    ) -> None:
        """把一条记账条目交给注入的 recorder。

        **每一次 `invoke` 都记一条**，包括被白名单拒绝、输入非法、超时的调用——`tool_calls`
        是可观测性的底座，「谁在什么时候试图调什么」比「成功的调用」更值得留痕（越权尝试尤其
        如此，R22.10）。`outcome` 列区分这几种收尾。
        """
        self._recorder.record(
            ToolCallRecord(
                trace_id=ctx.trace_id,
                step_id=ctx.step_id,
                caller=caller,
                tool_name=tool_name,
                args_digest=digest,
                args_json=raw,
                result_summary=summary,
                result_tokens=tokens,
                truncated=truncated,
                outcome=outcome,
                duration_ms=int((monotonic() - started) * 1000),
            )
        )
