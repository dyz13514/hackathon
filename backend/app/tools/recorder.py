"""把工具调用记账写进 `tool_calls` 表的 recorder（任务 5.1，R22.11）。

`ToolRegistry` 只依赖 `ToolCallRecorder` 协议（`registry.py`），不依赖任何持久化实现——
契约测试注入内存 recorder，生产装配注入本模块的 `DbToolCallRecorder`。这样「注册表的 7 步
闸门」与「记账落到哪张表」彻底解耦：前者是纯机具、可在无库环境里测；后者才碰 ORM。

## 为什么放在 `app/tools/` 而不是 `app/db/`

它写库，看起来像持久层的活。但它是 `ToolCallRecorder` 协议的**实现**，与协议同源更便于
一起阅读与演进；且 `tool_calls` 的写入只有这一个入口，没有跨模块复用的仓储价值。分层扫描
（`test_layering.py`）不禁止 `app/tools/` import `app/db`（禁的是 `app/agents/` 与
`app/core/`），因此这里 import ORM 合规。

## 一个前置条件：`tool_calls.trace_id` 是必填外键

因此本 recorder 只能在**已经开了一个 `Trace`** 的语境下使用（任务 5.7 的 `Orchestrator.run`
在入口 `begin()` 一个 trace，随后每次 `invoke` 落一条 `tool_calls`）。`ctx.trace_id` 必须指向
一个已存在的 `traces` 行，否则外键约束在 flush 时报错。这不是本模块要处理的错误——它是调用
契约：谁开的 trace 谁负责它存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import ToolCall
from app.tools.registry import ToolCallRecord

__all__ = ["DbToolCallRecorder"]


def _new_call_id() -> str:
    """可读的调用行 ID。调用 ID 不参与任何确定性断言，随机后缀安全（与全库其余 ID 同规）。"""
    return f"CALL-{uuid4().hex[:16]}"


@dataclass
class DbToolCallRecorder:
    """把 `ToolCallRecord` 写成一行 `tool_calls`。

    绑定一个 `Session`：写入**加入调用方的当前事务**，不自己提交。这与审计写入（走独立连接、
    立即提交，因为「阻断」不能随业务回滚）不同——一次工具调用的记账与它所在的这一步编排是
    同一件事，业务回滚了，这条记账一起回滚是正确的（那次调用在最终状态里等于没发生）。
    """

    session: Session

    def record(self, entry: ToolCallRecord) -> None:
        self.session.add(
            ToolCall(
                call_id=_new_call_id(),
                trace_id=entry.trace_id,
                step_id=entry.step_id,
                caller=entry.caller,
                tool_name=entry.tool_name,
                args_digest=entry.args_digest,
                args_json=entry.args_json,
                result_summary=entry.result_summary,
                result_tokens=entry.result_tokens,
                truncated=entry.truncated,
                outcome=entry.outcome,
                duration_ms=entry.duration_ms,
            )
        )
