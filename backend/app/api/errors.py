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

    # 计划生成（R5，任务 2.12）。字符串值与内核异常的 `code` 常量对齐，使
    # `DataIntegrityError.code` / `InvalidRoutingError.code` 能直接映射过来。
    DATA_INTEGRITY_ERROR = "DATA_INTEGRITY_ERROR"
    INVALID_ROUTING = "INVALID_ROUTING"
    #: 请求的计划不存在（`GET /plans/{id}` 等）。
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"

    # 审批闭环（R11、R12，任务 3.1）。四个都由 `Approval_Service.approve()` 产生，
    # API 层（任务 3.2）把 `ApprovalService` 返回的结构化结果翻译到这里；服务层本身不
    # import fastapi（分层规则），因此这些成员是「服务结果 → HTTP 错误」的翻译目标。
    #: 计划当前状态不是 `PENDING_APPROVAL`，无法审批（R11.2、状态机表外迁移）。
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    #: 提案生成之后输入数据已变化（`current_input_snapshot_version` 与
    #: `plan.input_snapshot_version` 不等，R12.2–3）。载荷只有两个版本号。
    STALE_PROPOSAL = "STALE_PROPOSAL"
    #: 激活前重校验发现硬约束违反（R11.3、R12.5、R6.5）。附违反清单，状态保持
    #: `PENDING_APPROVAL`。
    REVALIDATION_FAILED = "REVALIDATION_FAILED"
    #: 乐观并发失败：另一线程已推进该计划的行版本（R12.7）。
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"

    # REJECT / MODIFY（R11.4–7、R12.6，任务 3.2）。由 `Approval_Service.reject()` /
    # `modify()` 的结构化结果翻译而来。
    #: `rejection_reason` 不足 5 字符（R11.4）。
    REASON_TOO_SHORT = "REASON_TOO_SHORT"
    #: `MODIFY` 的某条修改指向计划里不存在的作业。
    JOB_NOT_IN_PLAN = "JOB_NOT_IN_PLAN"
    #: `MODIFY` 后重校验发现硬约束违反（R11.6）。附违反清单，原计划状态不变。
    MODIFICATION_REVALIDATION_FAILED = "MODIFICATION_REVALIDATION_FAILED"
    #: 目标生产日已存在另一个 `PENDING_APPROVAL`（`ux_pending_per_day` 冲突，R12.6）。
    PENDING_PLAN_EXISTS = "PENDING_PLAN_EXISTS"

    # 审批绕过防护（R11.8、R22.9、R23.4、EVAL-207，任务 3.3）。
    #: `PATCH /plans/{id}` 的请求体里出现了 `status` 键——试图绕过审批直接改计划状态。
    #: 返回 `403 FORBIDDEN` 并写审计。计划状态**只能**经 `Approval_Service` 迁移
    #: （design.md §8 状态机：任何 → `ACTIVE` 的其他路径「无组件被允许」）。
    PLAN_STATUS_WRITE_FORBIDDEN = "PLAN_STATUS_WRITE_FORBIDDEN"
    #: `PATCH /plans/{id}` 请求体含不被接受的字段（`PlanUpdateIn` 的 `extra="forbid"`）。
    PLAN_UPDATE_INVALID = "PLAN_UPDATE_INVALID"

    # 计划导出（R20，任务 3.6）。
    #: `POST /plans/{id}/export?format=...` 的 `format` 不是 `xlsx` / `csv`。
    EXPORT_FORMAT_UNSUPPORTED = "EXPORT_FORMAT_UNSUPPORTED"

    # 可观测性（R24，任务 5.12）。
    #: `GET /traces/{trace_id}` 请求的 Trace 不存在。
    TRACE_NOT_FOUND = "TRACE_NOT_FOUND"

    # 扰动与重排（R9，任务 7.4）。
    #: `POST /disruptions` 登记时不存在 `ACTIVE` 计划（R9.8）。扰动是对当前生效计划的干扰，
    #: 没有生效计划就无从谈影响与重排。
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
    #: `GET /disruptions/{id}/impact` 请求的扰动不存在或尚无影响分析。
    DISRUPTION_NOT_FOUND = "DISRUPTION_NOT_FOUND"

    # What-if 场景（R16，任务 8.3）。
    #: `POST /scenarios/run` 的某条 `ScenarioMutation` 指向不存在的实体或参数非法（R16.2）。
    SCENARIO_INVALID_MUTATION = "SCENARIO_INVALID_MUTATION"
    #: `POST /scenarios/{id}/adopt` 的场景不存在或已过期（进程内暂存失效，R16.9）。
    SCENARIO_NOT_FOUND = "SCENARIO_NOT_FOUND"

    # 电子表格摄取（R2/R3，任务 10）。安全闸门四类（R23.7）：
    MACRO_NOT_ALLOWED = "MACRO_NOT_ALLOWED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_ROWS = "TOO_MANY_ROWS"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    #: `GET /imports/{upload_id}/*` 的上传不存在或已过期。
    UPLOAD_NOT_FOUND = "UPLOAD_NOT_FOUND"
    #: `POST /imports/{upload_id}/confirm` 的映射未满足落库前置（低置信/缺必填/未处置，R2.9）。
    IMPORT_MAPPING_INCOMPLETE = "IMPORT_MAPPING_INCOMPLETE"
    #: `POST /imports/{batch_id}/revert` 的批次不存在。
    IMPORT_BATCH_NOT_FOUND = "IMPORT_BATCH_NOT_FOUND"

    # 偏好规则（R18，任务 11.1）。四道结构性保障之一在 API 边界（design.md §4.3、EVAL-206）：
    #: `structured_form` 指向硬约束开关或非软目标 `component`（如 `allow_shift_overflow`）。
    #: Pydantic 的判别联合先拦一层（返回 422 校验错误），本码是服务层再判一道的显式出口——
    #: 偏好规则**永远不能**放宽硬约束，越界即拒（R18.8）。
    PREFERENCE_RULE_OUT_OF_SCOPE = "PREFERENCE_RULE_OUT_OF_SCOPE"
    #: 启用状态的 `PreferenceRule` 已达 20 条上限（R18.11）。要求先停用既有规则再启用。
    PREFERENCE_RULE_LIMIT_REACHED = "PREFERENCE_RULE_LIMIT_REACHED"
    #: `GET/PATCH/DELETE /api/preferences/{rule_id}` 的规则不存在。
    PREFERENCE_RULE_NOT_FOUND = "PREFERENCE_RULE_NOT_FOUND"


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
