"""`Trace_Recorder`：把一次编排落成 `traces` / `trace_steps` / `tool_calls`（任务 5.12，
design.md Data Models §7、Components §1「共享物表」、R24.1/5/7、R22.11）。

任务 5.7 的 `Orchestrator` 把 `Tracer` 定成一个协议（`begin` / `record_step` / `end`），
并给了一个 `InMemoryTracer` 作接缝，好让 `run()` 在无 DB 时也可跑、可测。本模块交付那个
接缝背后的**真实**记录器：`begin` 写一行 `traces`，`record_step` 写一行 `trace_steps`，
`end` 结算 `traces` 的 `outcome` / `step_count` / token 与美元汇总。计划生成流水线（任务
2.12）也有一个最小 trace 接缝（`plan_generation._record_trace`），本任务同样把它换成对
本模块的调用——`Orchestrator.run` 与流水线的步骤逻辑**一字不改**（与 `Bedrock_Adapter`
的 `BudgetRecorder` 接缝同一思路：换实例，不换调用点）。

## 为什么 `begin` 的签名不带 `trigger_source` / `session_id`，而它们又是 NOT NULL

`Tracer` 协议的 `begin(*, kind, mode, agent)` 是任务 5.7 定死的，本任务**不得**改它
（改了就要动 `Orchestrator.run`）。但 `traces` 表的 `trigger_source` 与 `session_id` 是
NOT NULL（design.md §7）。这两者是**一次编排的运行上下文**，不随每一步变化，因此它们在
**构造** `DbTracer` 时提供，而不是在 `begin` 的逐次调用里。一个 `DbTracer` 实例服务于
一次编排运行：`Orchestrator` 在装配时按请求的触发来源与会话 ID 构造它，`run()` 内部只调
协议的三个方法。这样协议保持不变，而 NOT NULL 列有了来源。

## `mode != REPLAY` 是 `PROJECT_REAL_RUN_CAP = 150` 的计数依据（任务 5.4 消费）

`traces.mode` 的每一行都可能计入真实运行配额：`admin.count_real_runs` 的口径是
`COUNT(*) WHERE mode != 'REPLAY'`。因此本记录器写下的 `mode` 直接决定一次编排是否吃掉
一格配额。`PIPELINE`（流水线）与 `REACT`（ReAct 循环）都算真实运行；只有回放（`REPLAY`，
本项目当前不产生）不算。这条口径不在本模块重写——它属于 `admin` / `budget`，本模块只负责
如实写下每次运行的 `mode`。

## `decision_reason` 是结构化摘要，不是推理链（R24.7）

`trace_steps.decision_reason` 存的是「这一步为什么这样收尾」的结构化短语（工具名、护栏
判定、终止原因），**不是**模型的原始 thought。`Orchestrator` 传进来的 `detail` 已经是这种
摘要（例如 `"final 通过契约校验"`、工具名、`"预算耗尽…"`），本模块原样落库。把原始推理链
挡在 trace 之外是 R24.7 的明确要求，也让 `/traces` 详情页不会泄漏模型的内部独白。

## `tool_calls` 由 `DbToolCallRecorder` 写（R22.11）

`Tool_Registry`（任务 5.1）在每次 `invoke` 后调注入的 `ToolCallRecorder.record`。任务 5.1
给的是 `InMemoryToolCallRecorder`；本模块提供 `DbToolCallRecorder`，把每条 `ToolCallRecord`
落成一行 `tool_calls`，`trace_id` / `step_id` 关联到本次运行的 trace 与步骤。生产装配把它
注入 `ToolRegistry`，`invoke` 的 7 步闸门一字不改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db import models as orm
from app.orchestrator.tracing import TraceHandle
from app.tools.registry import ToolCallRecord

__all__ = [
    "DbToolCallRecorder",
    "DbTraceHandle",
    "DbTracer",
    "new_trace_id",
]


#: `traces.outcome` / ReAct 结果 `outcome` 到「是否成功」的口径。`OK` 与流水线的
#: `SUCCESS` 都算成功收尾；其余（`MAX_STEPS_EXCEEDED` / `TOKEN_BUDGET_EXCEEDED` /
#: `VALIDATION_FAILED` / `ERROR` / `LLM_UNAVAILABLE`）都是非成功终止。本模块不据此做
#: 任何分支——只是把 `outcome` 原样落库；这里列出取值域是为文档完整。


def new_trace_id(now: datetime | None = None) -> str:
    """可读且按时间排序的 trace ID：`TRACE-20260302T081500-3f9ac1b2`。

    与 `db/audit.py::_new_audit_id` 同一形态：时间前缀让 `traces` 表按发生顺序自然排列
    （演示时直接读表很实用），随机后缀保证同一秒内多次运行不撞主键。trace ID 不参与任何
    确定性断言（属性 1 断言的是排产结果，不是行 ID），随机后缀因此安全。
    """
    moment = now if now is not None else datetime.now()  # noqa: DTZ005
    return f"TRACE-{moment:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"


@dataclass
class DbTraceHandle(TraceHandle):
    """`DbTracer` 的运行期句柄，在基类 `TraceHandle` 上补三样落库需要的运行期状态。

    基类字段（`trace_id` / `kind` / `mode` / `agent` / `outcome` / `step_count` / `steps`）
    由 `Orchestrator` 直接读写，保持原义。这里补充：

    - `started_monotonic`：单调时钟起点，用于给每一步算 `duration_ms`（墙上时钟会被系统
      调时干扰，逐步耗时用单调时钟才可靠）。
    - `last_step_monotonic`：上一步收尾的单调时刻，本步耗时 = 现在 − 它。
    - `total_input_tokens` / `total_output_tokens` / `estimated_usd`：token 与美元汇总，
      `end` 时写进 `traces`。P0 无真实 LLM 驱动接线进 `Orchestrator`，因此本记录器不自行
      发明 token 数——它们由调用方（将来接线的驱动/预算作用域）经 `add_usage` 累加；未接线
      时恒为 0，那是这条路径的**正确真值**（流水线确实零 token）。
    """

    started_at: datetime = field(default_factory=lambda: datetime.now())  # noqa: DTZ005
    started_monotonic: float = field(default_factory=monotonic)
    last_step_monotonic: float = field(default_factory=monotonic)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    #: 已写入的 `trace_steps` 行数，用作 `step_index`（0 起，随写入递增）。
    persisted_steps: int = 0
    #: 本次运行最新写入的 `trace_steps.step_id`，供 `DbToolCallRecorder` 关联工具调用。
    last_step_id: str | None = None

    def add_usage(self, *, input_tokens: int, output_tokens: int, usd: Decimal) -> None:
        """把一次 LLM 调用的用量累进本 trace 的汇总（将来接线真实驱动时调用）。"""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.estimated_usd += usd


class DbTracer:
    """DB 版 `Tracer`：`begin` / `record_step` / `end` 写 `traces` / `trace_steps`。

    构造时绑定一次编排运行的上下文：`session`（业务会话，trace 与它同事务落盘，使
    `production_plans.generated_by_trace_id` 的外键有指向）、`trigger_source`、`session_id`。
    `result_ref` 可在运行中经 `set_result_ref` 补上（例如计划生成算出 `plan_id` 后）。

    与 `InMemoryTracer` 行为同构（`begin` 建句柄、`record_step` 追加一步、`end` 定型
    `step_count`），差别只在「落库」。因此把 `Orchestrator` 的 `InMemoryTracer` 换成本类，
    `run()` 一字不改即产出真实的 `traces` / `trace_steps` 行。
    """

    def __init__(
        self,
        session: Session,
        *,
        trigger_source: str,
        session_id: str,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._trigger_source = trigger_source
        self._session_id = session_id
        self._now = now
        #: 最近一次 `begin` 的句柄。`DbToolCallRecorder` 读它取 `last_step_id` 关联工具调用。
        self.current: DbTraceHandle | None = None

    def begin(self, *, kind: str, mode: str, agent: str | None) -> DbTraceHandle:
        """写一行 `traces`（`ended_at` / `outcome` 待 `end` 补），返回运行期句柄。

        `traces` 行**立即 flush**：`production_plans.generated_by_trace_id` 等外键指向它，
        SQLite 在 `foreign_keys = ON` 下逐条 INSERT 就检查，被引用的 trace 必须先在库里。
        """
        started_at = self._now if self._now is not None else datetime.now()  # noqa: DTZ005
        handle = DbTraceHandle(
            trace_id=new_trace_id(started_at),
            kind=kind,
            mode=mode,
            agent=agent,
            started_at=started_at,
        )
        self._session.add(
            orm.Trace(
                trace_id=handle.trace_id,
                kind=kind,
                mode=mode,
                agent=agent,
                trigger_source=self._trigger_source,
                session_id=self._session_id,
                started_at=started_at,
                ended_at=None,
                outcome=None,
                step_count=0,
                total_input_tokens=0,
                total_output_tokens=0,
                estimated_usd=Decimal("0"),
                result_ref=None,
            )
        )
        self._session.flush()
        self.current = handle
        return handle

    def record_step(
        self, trace: TraceHandle, *, step_kind: str, outcome: str, detail: str
    ) -> None:
        """写一行 `trace_steps`（结构化摘要，非推理链，R24.7），并镜像进 `trace.steps`。

        `duration_ms` 用单调时钟测两次 `record_step` 之间的间隔；`decision_reason` 存
        `Orchestrator` 传入的 `detail`（已是结构化摘要）。`input_tokens` / `output_tokens`
        逐步恒为 0——真实 LLM 驱动接线后由那条路径填（本任务不发明 token 数）。
        """
        handle = _as_db_handle(trace)
        now_mono = monotonic()
        duration_ms = int((now_mono - handle.last_step_monotonic) * 1000)
        handle.last_step_monotonic = now_mono

        step_index = handle.persisted_steps
        step_id = f"STEP-{handle.trace_id[6:]}-{step_index:03d}"
        self._session.add(
            orm.TraceStep(
                step_id=step_id,
                trace_id=handle.trace_id,
                step_index=step_index,
                step_kind=step_kind,
                decision_reason=detail,
                input_digest=None,
                output_digest=None,
                duration_ms=max(duration_ms, 0),
                input_tokens=0,
                output_tokens=0,
            )
        )
        self._session.flush()

        handle.persisted_steps += 1
        handle.last_step_id = step_id
        # 与 InMemoryTracer 同构：也在句柄的内存镜像里留一份，供同进程内断言/读取。
        handle.steps.append(
            {"step_kind": step_kind, "outcome": outcome, "detail": detail}
        )
        handle.step_count = handle.persisted_steps

    def end(self, trace: TraceHandle) -> None:
        """结算 `traces`：写 `ended_at` / `outcome` / `step_count` / token 与美元汇总。

        `outcome` 由 `Orchestrator` / 流水线在 `trace.outcome` 上设定（成功为 `OK` /
        `SUCCESS`，失败为各终止码）；本方法把句柄上的最终值落库。未显式设 `outcome`（例如
        `Mode.NONE` 未接线路径）则保持 NULL。
        """
        handle = _as_db_handle(trace)
        row = self._session.get(orm.Trace, handle.trace_id)
        if row is None:  # pragma: no cover - begin 必先于 end，行必然存在
            return
        ended_at = self._now if self._now is not None else datetime.now()  # noqa: DTZ005
        row.ended_at = ended_at
        row.outcome = handle.outcome
        row.step_count = handle.persisted_steps
        row.total_input_tokens = handle.total_input_tokens
        row.total_output_tokens = handle.total_output_tokens
        row.estimated_usd = handle.estimated_usd
        self._session.flush()

    def set_result_ref(self, trace: TraceHandle, result_ref: str) -> None:
        """补写 `traces.result_ref`（plan_id / batch_id / scenario_id），供 `/traces` 回链。

        运行中途才知道结果引用（例如计划生成算出 `plan_id`），因此单列一个方法而不塞进
        `begin`。非 `Orchestrator` 协议的一部分——由知道结果引用的调用方（流水线 / 服务）
        显式调用。
        """
        handle = _as_db_handle(trace)
        row = self._session.get(orm.Trace, handle.trace_id)
        if row is not None:
            row.result_ref = result_ref
            self._session.flush()


def _as_db_handle(trace: TraceHandle) -> DbTraceHandle:
    """把协议里的 `TraceHandle` 收窄回 `DbTracer` 发出的 `DbTraceHandle`。

    `DbTracer.begin` 只发 `DbTraceHandle`，因此 `record_step` / `end` 收到的必是它。这个
    断言把「有人拿别处造的裸 `TraceHandle` 喂给 DB 记录器」这类装配错误在当场暴露，而不是
    让它以「少写了几行 trace_steps」的形式静默发生。
    """
    if not isinstance(trace, DbTraceHandle):  # pragma: no cover - 装配错误
        raise TypeError(
            "DbTracer 只处理自己 begin() 发出的 DbTraceHandle；"
            f"收到 {type(trace).__name__}"
        )
    return trace


@dataclass
class DbToolCallRecorder:
    """DB 版 `ToolCallRecorder`：把每条 `ToolCallRecord` 落成一行 `tool_calls`（R22.11）。

    `Tool_Registry.invoke` 的 7 步闸门在每次调用（含被白名单拒绝、输入非法、超时）后调
    `record`。任务 5.1 给的是 `InMemoryToolCallRecorder`；本类是它的生产对偶，写库而非内存。
    `invoke` 一字不改——只是注入的 recorder 换成本类。

    `step_id` 的关联：`ToolContext.step_id` 若已由调用方置好（ReAct 循环当前传 `None`，把
    工具调用挂在 trace 而非某一步上），则用它；否则回退到本记录器绑定的 `tracer.current`
    最近一步（`last_step_id`），使工具调用与「触发它的那一步」在 `/traces` 详情里能对上。
    """

    session: Session
    #: 绑定的 `DbTracer`，用于在 `ToolCallRecord.step_id` 缺失时回退到当前 trace 的最近一步。
    tracer: DbTracer | None = None

    def record(self, entry: ToolCallRecord) -> None:
        step_id = entry.step_id
        if step_id is None and self.tracer is not None and self.tracer.current is not None:
            step_id = self.tracer.current.last_step_id

        self.session.add(
            orm.ToolCall(
                call_id=f"CALL-{uuid4().hex[:16]}",
                trace_id=entry.trace_id,
                step_id=step_id,
                caller=entry.caller,
                tool_name=entry.tool_name,
                args_digest=entry.args_digest,
                args_json=_json_safe(entry.args_json),
                result_summary=entry.result_summary,
                result_tokens=entry.result_tokens,
                truncated=entry.truncated,
                outcome=entry.outcome,
                duration_ms=entry.duration_ms,
            )
        )
        self.session.flush()


def _json_safe(value: Any) -> Any:
    """`tool_calls.args_json` 是 JSON 列——原样透传 dict（已由记账层构造成可序列化形态）。

    `ToolCallRecord.args_json` 是 `dict[str, Any]`，registry 在构造它时已用原始入参
    （已通过 Pydantic 校验或将被拒绝）。这里保持不变，只在极端非 dict 情形下兜底成字符串，
    避免一次异常的记账把整条业务事务带崩。
    """
    if isinstance(value, dict):
        return value
    return {"value": str(value)}  # pragma: no cover - registry 恒传 dict
