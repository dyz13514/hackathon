"""`Trace` 的开合 seam：`TraceHandle` / `Tracer` 协议 / `InMemoryTracer`（任务 5.7）。

这三样原先住在 `orchestrator.py` 里。抽到本模块是一次**纯结构性**移动，动机是打破一条
import 环：`Trace_Recorder`（任务 5.12，`trace_recorder.py`）需要 `TraceHandle` 来派生
它的 DB 句柄，而 `orchestrator.py` 又 import `app.llm.budget`，`budget` 再 import
`app.api.admin`（`PROJECT_REAL_RUN_CAP` / `count_real_runs` 的唯一来源）——`app.api` 的
包初始化会 import 路由模块，其中 `plans.py` → `plan_generation.py` → `trace_recorder.py`。
若 `trace_recorder` 从 `orchestrator.py` 取 `TraceHandle`，这条链就闭合成环，`budget` 在
半初始化状态下被回读。

把「Trace 的类型与协议」这一层单拎出来（它只依赖标准库，不碰 budget / registry / 契约），
让 `trace_recorder` 从这里取 `TraceHandle` 而不触达 `orchestrator.py`，环即断开。
`orchestrator.py` 仍从本模块**再导出**这三个名字，因此现有 `from app.orchestrator.orchestrator
import TraceHandle, Tracer, InMemoryTracer` 一字不必改（与任务 5.7 的公开面保持一致）。

`InMemoryTracer` 的定位不变：它是 `Trace_Recorder` 落地前的最小实现，让 `Orchestrator.run`
在无 DB 时也可跑、可测；5.12 落地后把注入的实例换成 `DbTracer` 即可，`run()` 一字不改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

__all__ = [
    "InMemoryTracer",
    "TraceHandle",
    "Tracer",
]


@dataclass
class TraceHandle:
    """一次编排的 `Trace`（内存镜像）。字段对齐 `traces` 表列（design.md Data Models §7）。

    `Trace_Recorder`（5.12）会把它落成 `traces` / `trace_steps` 行；`InMemoryTracer` 只在
    内存里累积，使 `run()` 现在就可跑、可断言。`steps` 保留每一步的结构化摘要（`step_kind`
    + `outcome` + `detail`），供终止性测试断言「保留了完整 Trace」（R21.6）。
    """

    trace_id: str
    kind: str
    mode: str
    agent: str | None
    outcome: str | None = None
    step_count: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)


class Tracer(Protocol):
    """`Trace` 的开合 seam（design.md §1：`self.tracer.begin(...)` / `self.tracer.end(...)`）。"""

    def begin(self, *, kind: str, mode: str, agent: str | None) -> TraceHandle: ...

    def record_step(
        self, trace: TraceHandle, *, step_kind: str, outcome: str, detail: str
    ) -> None: ...

    def end(self, trace: TraceHandle) -> None: ...


class InMemoryTracer:
    """内存 `Tracer`（任务 5.12 的 `Trace_Recorder` 之前的最小实现）。

    与 `plan_generation.py` 的最小 trace 接缝同一定位：让编排层现在就有一个能开合的 `Trace`，
    完整落库归 5.12。`end` 把 `outcome` 定型为最后一次记录的 outcome（若未显式设置），
    `step_count` 由记录的步数派生。
    """

    def __init__(self) -> None:
        self.traces: list[TraceHandle] = []

    def begin(self, *, kind: str, mode: str, agent: str | None) -> TraceHandle:
        handle = TraceHandle(
            trace_id=f"TRACE-{uuid4().hex[:16]}", kind=kind, mode=mode, agent=agent
        )
        self.traces.append(handle)
        return handle

    def record_step(
        self, trace: TraceHandle, *, step_kind: str, outcome: str, detail: str
    ) -> None:
        trace.steps.append(
            {"step_kind": step_kind, "outcome": outcome, "detail": detail}
        )
        trace.step_count = len(trace.steps)

    def end(self, trace: TraceHandle) -> None:
        # outcome 已由 run() / _react_loop 设定；若仍为空（例如 Mode.NONE 未接线），保持 None。
        trace.step_count = len(trace.steps)
