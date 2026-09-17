"""统一错误响应包（design.md Error Handling §2）。

design.md 规定的形状只有一个：

    {"error": {"code": ..., "message": ..., "details": {...},
               "next_actions": [...], "trace_id": ...}}

放在 `api/` 而不是 `core/` 是分层规则的直接结果：`error_response()` 返回
`JSONResponse`，而 `app/core/**` 不得 import `fastapi`（任务 1.8 的静态断言 ①）。
错误**分类**是 API 边界的概念，确定性内核抛的是自己的领域异常，由路由层翻译。

## `ErrorCode` 为什么不是一次写全

design.md §2 的错误码表有 40 项，绝大多数的抛出点还不存在。一次性写全会得到一份
「哪些码真的在用」无法回答的枚举，而未被使用的成员在重命名与删除时无人察觉。因此
约定：**每个落地任务把自己抛的码加进来**，枚举成员数因此恒等于系统当前真实能返回的
错误种类数。本任务（1.5）只需要 `UNAUTHENTICATED`。

## `next_actions` 是必填的产品主张

design.md：「每个业务性拒绝都必须告诉规划员**下一步能做什么**」。这里把它做成
`error_response()` 的显式参数而不是可选装饰，是为了让「拒绝了但没给出路」在调用点
就显眼。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.logging_config import current_trace_id


class ErrorCode(StrEnum):
    """当前系统真实会返回的错误码。新增抛出点时在此追加对应成员。"""

    # 系统与安全（R23）
    UNAUTHENTICATED = "UNAUTHENTICATED"


class NextAction(BaseModel):
    """一个可执行的下一步。`href` 为空表示动作在当前界面内完成。"""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="动作标识，前端据此决定渲染哪个入口")
    href: str | None = Field(default=None, description="对应的 API 或视图路径")


class ErrorBody(BaseModel):
    """错误包的内容部分。"""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(description="面向规划员的中文说明，不含任何凭证或内部细节")
    details: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[NextAction] = Field(default_factory=list)
    trace_id: str | None = None


class ErrorEnvelope(BaseModel):
    """`{"error": {...}}`。响应体的唯一错误形状。"""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


def error_response(
    *,
    status_code: int,
    code: ErrorCode,
    message: str,
    next_actions: list[NextAction],
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """构造统一错误响应。

    `trace_id` 从 `logging_config` 的 ContextVar 取，因此编排路径里的错误自动带上
    关联 ID，而无需每个抛出点传参（R24.8 的同一套关联字段）。
    """
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or {},
            next_actions=next_actions,
            trace_id=current_trace_id(),
        )
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))
