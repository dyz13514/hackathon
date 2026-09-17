"""`Plan_Exporter`：把一个计划导出成车间班组长看得懂的表格（design.md §4.5、R20）。

**归属：确定性** —— 本模块不触达 LLM，只把库里的计划行序列化成 `.xlsx` / `.csv`。

## 两种格式，同一份内容（R20.1）

- `.xlsx`（openpyxl）：三个工作表 `schedule` / `unschedulable` / `footer`。
- `.csv`：一个纯文本文件——`schedule` 段在前，`unschedulable` 段其次，`footer` 以
  `#` 前缀的尾部注释行收尾。CSV 没有「多工作表」的概念，因此用分段 + 注释行表达同一结构。

## Sheet 1 `schedule`（R20.2）

按 `machine_id` 分组、组内按 `start_time` 升序，每行 10 个字段：`job_id`、`order_id`、
`product_id`、`quantity`、`operation_sequence`、`machine_id`、`worker_id`、`start_time`、
`end_time`、`setup_minutes`。分组顺序本身也确定（`machine_id` 升序），因此同一计划两次导出
逐字节相同——这是导出往返测试可断言的前提。

## Sheet 2 `unschedulable`（R20.3）

`job_id`、`order_id`、`blocking_reason`、`unblock_suggestion`。最后一列是**人类可读展开**：
`unblock_suggestion` 在库里是结构化 JSON（9 类 `blocking_reason` 各有不同的量化字段，
design.md §3.1.6），车间班组长不读 JSON，因此这里把它拍平成 `key=value; key=value` 的一行
文本。展开是确定性的（键按字母序），使往返可断言。

## Sheet 3 `footer`（R20.4–5）

`plan_id`、`approved_by`、`approved_at`、`plan_version`，供车间核对版本。`approved_by` /
`approved_at` 来自该计划 `action=APPROVE` 的 `plan_approvals` 行；计划尚未审批（例如导出一个
`PENDING_APPROVAL` 计划）时二者为空字符串。若计划已被 `SUPERSEDED`，额外标注
`SUPERSEDED_BY = <plan_id>`（R20.5）。

## 公式注入防护（R20.6、EVAL-213）

所有**文本**单元格经 `escape_formula`：值以 `=`、`+`、`-`、`@`、制表符或回车开头时，前置一个
单引号，使电子表格软件把它当字面文本而非公式。这是防「订单备注里藏 `=cmd|...`」这类注入的
唯一手段。openpyxl 写入时另外把 `cell.data_type = "s"` 强制为字符串，双保险：即便某个值绕过了
前缀判断，它也不会被当成公式求值。

数值列（`quantity` / `operation_sequence` / `setup_minutes` / `plan_version`）不经
`escape_formula`——它们不是用户可控文本，且以数字形式存在电子表格里对车间更有用。用户可控的
自由文本（`unblock_suggestion` 的展开、`order_id` 等标识符）一律经转义。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as orm

#: 以这些字符开头的文本单元格会被电子表格当成公式求值，必须前置单引号转义（R20.6）。
#: `\t` 与 `\r` 一并纳入：某些软件会在前导空白后仍把 `=` 识别为公式起点。
_FORMULA_TRIGGERS = "=+-@\t\r"

#: Sheet 1 的 10 个列（R20.2，顺序即列顺序）。
SCHEDULE_HEADERS: tuple[str, ...] = (
    "job_id",
    "order_id",
    "product_id",
    "quantity",
    "operation_sequence",
    "machine_id",
    "worker_id",
    "start_time",
    "end_time",
    "setup_minutes",
)

#: Sheet 2 的列（R20.3）。
UNSCHEDULABLE_HEADERS: tuple[str, ...] = (
    "job_id",
    "order_id",
    "blocking_reason",
    "unblock_suggestion",
)

#: `datetime` 在导出里的形状：ISO 8601 到分钟，无时区后缀（演示数据是 naive 本地时刻）。
_TIME_FORMAT = "%Y-%m-%dT%H:%M"


def escape_formula(value: str) -> str:
    """公式注入防护（R20.6、design.md §4.5）。

    `escape_formula(v) = "'" + v if v[:1] in "=+-@\\t\\r" else v`。

    以危险字符开头即前置单引号。这是一个纯函数：同输入同输出，导出往返测试据此断言
    注入文本被中和且可逆识别。

    **空串会被转义成 `"'"`**：`""[:1]` 是空串，而 Python 里「空串 in 任意串」恒为
    `True`，因此空串命中判断被前置单引号。这是 design.md §4.5 公式定义的字面后果，
    刻意保留原样——一个空单元格显示成孤立单引号无害，而偏离 design 的公式会让 EVAL-213
    的断言口径与实现不一致。调用方若要「空即空」，应在传入前自行短路，而不是改这里。
    """
    return "'" + value if value[:1] in _FORMULA_TRIGGERS else value


def expand_unblock_suggestion(suggestion: dict[str, Any]) -> str:
    """把结构化 `unblock_suggestion` 展开成一行人类可读文本（R20.3）。

    形状 `key1=value1; key2=value2`，键按字母序排列使输出确定。列表值渲染成逗号分隔。
    空建议返回空串。这里刻意不做本地化或措辞——车间要的是「缺什么、缺多少」的量化事实，
    键名（`shortfall_quantity` 等）本身就是 design.md §3.1.6 的稳定契约。
    """
    parts: list[str] = []
    for key in sorted(suggestion):
        value = suggestion[key]
        if isinstance(value, list):
            rendered = ",".join(str(item) for item in value)
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return "; ".join(parts)


def _format_datetime(value: datetime) -> str:
    return value.strftime(_TIME_FORMAT)


def _format_quantity(value: Decimal | int | float) -> str:
    """数量渲染：去掉 `Decimal` 的尾随零，整数不带小数点。"""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    normalized = dec.normalize()
    # `normalize()` 对 100 会给出 `1E+2`；转成普通记法。
    text = format(normalized, "f")
    return text


# --------------------------------------------------------------------------
# 计划快照：从库里读出导出所需的一切（一次装配，两种格式共用）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScheduleRow:
    job_id: str
    order_id: str
    product_id: str
    quantity: str
    operation_sequence: int
    machine_id: str
    worker_id: str
    start_time: str
    end_time: str
    setup_minutes: int


@dataclass(frozen=True)
class _UnschedulableRow:
    job_id: str
    order_id: str
    blocking_reason: str
    unblock_suggestion: str


@dataclass(frozen=True)
class _Footer:
    plan_id: str
    approved_by: str
    approved_at: str
    plan_version: int
    superseded_by: str | None


@dataclass(frozen=True)
class _PlanExport:
    schedule: tuple[_ScheduleRow, ...]
    unschedulable: tuple[_UnschedulableRow, ...]
    footer: _Footer


class PlanNotFoundError(Exception):
    """请求导出的计划不存在。API 层翻译成 `PLAN_NOT_FOUND`。"""

    def __init__(self, plan_id: str) -> None:
        super().__init__(plan_id)
        self.plan_id = plan_id


def _load_plan_export(session: Session, plan_id: str) -> _PlanExport:
    """把一个计划读成导出用的中间形状。分组与排序在这里定死（R20.2）。"""
    plan = session.get(orm.ProductionPlan, plan_id)
    if plan is None:
        raise PlanNotFoundError(plan_id)

    # Sheet 1：按 machine_id 分组、组内按 start_time 升序。ORDER BY 直接给出这个顺序。
    sched_rows = list(
        session.execute(
            select(orm.ScheduledJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
            .where(orm.ScheduledJob.plan_id == plan_id)
            .order_by(orm.ScheduledJob.machine_id, orm.ScheduledJob.start_time)
        )
    )
    schedule = tuple(
        _ScheduleRow(
            job_id=sj.job_id,
            order_id=pj.order_id,
            product_id=pj.product_id,
            quantity=_format_quantity(pj.quantity),
            operation_sequence=pj.operation_sequence,
            machine_id=sj.machine_id,
            worker_id=sj.worker_id,
            start_time=_format_datetime(sj.start_time),
            end_time=_format_datetime(sj.end_time),
            setup_minutes=sj.setup_minutes,
        )
        for sj, pj in sched_rows
    )

    # Sheet 2：不可排产作业，按 job_id 排序使输出确定。
    unsched_rows = list(
        session.execute(
            select(orm.UnschedulableJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.UnschedulableJob.job_id == orm.ProductionJob.job_id)
            .where(orm.UnschedulableJob.plan_id == plan_id)
            .order_by(orm.UnschedulableJob.job_id)
        )
    )
    unschedulable = tuple(
        _UnschedulableRow(
            job_id=uj.job_id,
            order_id=pj.order_id,
            blocking_reason=uj.blocking_reason,
            unblock_suggestion=expand_unblock_suggestion(
                uj.unblock_suggestion if isinstance(uj.unblock_suggestion, dict) else {}
            ),
        )
        for uj, pj in unsched_rows
    )

    footer = _build_footer(session, plan)
    return _PlanExport(schedule=schedule, unschedulable=unschedulable, footer=footer)


def _build_footer(session: Session, plan: orm.ProductionPlan) -> _Footer:
    """页脚：plan_id / approved_by / approved_at / plan_version（+ SUPERSEDED_BY）。

    `approved_by` / `approved_at` 取该计划 `action=APPROVE` 的最近一条审批记录。未审批的
    计划（`PENDING_APPROVAL` 等）没有这条记录，二者为空串——导出一个未审批计划是合法的
    （班组长可能想预览），页脚如实反映「尚未批准」。
    """
    approval = session.execute(
        select(orm.PlanApproval)
        .where(orm.PlanApproval.plan_id == plan.plan_id)
        .where(orm.PlanApproval.action == "APPROVE")
        .order_by(orm.PlanApproval.timestamp.desc())
    ).scalars().first()

    approved_by = approval.actor if approval is not None else ""
    approved_at = _format_datetime(approval.timestamp) if approval is not None else ""

    superseded_by = (
        plan.superseded_by_plan_id if plan.status == "SUPERSEDED" else None
    )
    return _Footer(
        plan_id=plan.plan_id,
        approved_by=approved_by,
        approved_at=approved_at,
        plan_version=plan.plan_version,
        superseded_by=superseded_by,
    )


# --------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------


def _write_text_cell(sheet: Worksheet, row: int, col: int, value: str) -> None:
    """写一个文本单元格：经 `escape_formula` + 强制 `data_type = "s"`（R20.6）。"""
    cell = sheet.cell(row=row, column=col, value=escape_formula(value))
    cell.data_type = "s"


def _write_number_cell(sheet: Worksheet, row: int, col: int, value: int) -> None:
    """写一个数值单元格（工序号 / setup 分钟 / 版本号）。不经公式转义（非用户文本）。"""
    sheet.cell(row=row, column=col, value=value)


def export_xlsx(session: Session, plan_id: str) -> bytes:
    """导出为 `.xlsx`：三个工作表 `schedule` / `unschedulable` / `footer`（R20.1–5）。"""
    data = _load_plan_export(session, plan_id)
    workbook = Workbook()

    # 默认工作表复用为 schedule。
    schedule_sheet = workbook.active
    assert schedule_sheet is not None
    schedule_sheet.title = "schedule"
    _fill_schedule_sheet(schedule_sheet, data)

    unsched_sheet = workbook.create_sheet("unschedulable")
    _fill_unschedulable_sheet(unsched_sheet, data)

    footer_sheet = workbook.create_sheet("footer")
    _fill_footer_sheet(footer_sheet, data.footer)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _fill_schedule_sheet(sheet: Worksheet, data: _PlanExport) -> None:
    for col, header in enumerate(SCHEDULE_HEADERS, start=1):
        _write_text_cell(sheet, 1, col, header)
    for i, row in enumerate(data.schedule, start=2):
        _write_text_cell(sheet, i, 1, row.job_id)
        _write_text_cell(sheet, i, 2, row.order_id)
        _write_text_cell(sheet, i, 3, row.product_id)
        _write_text_cell(sheet, i, 4, row.quantity)
        _write_number_cell(sheet, i, 5, row.operation_sequence)
        _write_text_cell(sheet, i, 6, row.machine_id)
        _write_text_cell(sheet, i, 7, row.worker_id)
        _write_text_cell(sheet, i, 8, row.start_time)
        _write_text_cell(sheet, i, 9, row.end_time)
        _write_number_cell(sheet, i, 10, row.setup_minutes)


def _fill_unschedulable_sheet(sheet: Worksheet, data: _PlanExport) -> None:
    for col, header in enumerate(UNSCHEDULABLE_HEADERS, start=1):
        _write_text_cell(sheet, 1, col, header)
    for i, row in enumerate(data.unschedulable, start=2):
        _write_text_cell(sheet, i, 1, row.job_id)
        _write_text_cell(sheet, i, 2, row.order_id)
        _write_text_cell(sheet, i, 3, row.blocking_reason)
        _write_text_cell(sheet, i, 4, row.unblock_suggestion)


def _fill_footer_sheet(sheet: Worksheet, footer: _Footer) -> None:
    """页脚工作表：每行一个 `key`/`value` 对（R20.4–5）。"""
    _write_text_cell(sheet, 1, 1, "field")
    _write_text_cell(sheet, 1, 2, "value")
    _write_text_cell(sheet, 2, 1, "plan_id")
    _write_text_cell(sheet, 2, 2, footer.plan_id)
    _write_text_cell(sheet, 3, 1, "approved_by")
    _write_text_cell(sheet, 3, 2, footer.approved_by)
    _write_text_cell(sheet, 4, 1, "approved_at")
    _write_text_cell(sheet, 4, 2, footer.approved_at)
    _write_text_cell(sheet, 5, 1, "plan_version")
    _write_number_cell(sheet, 5, 2, footer.plan_version)
    if footer.superseded_by is not None:
        _write_text_cell(sheet, 6, 1, "SUPERSEDED_BY")
        _write_text_cell(sheet, 6, 2, footer.superseded_by)


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def export_csv(session: Session, plan_id: str) -> bytes:
    """导出为 `.csv`：schedule 段 + unschedulable 段 + `#` 前缀的页脚注释行（R20.1–5）。

    CSV 无多工作表，因此用分段表达三块内容：每段前一行 `# <section>` 标题，页脚整体为
    `#` 前缀的注释行（车间用的电子表格软件把 `#` 行显示为普通文本，不参与列对齐）。所有文本
    字段经 `escape_formula`——CSV 同样会被电子表格打开，注入面完全一样。
    """
    data = _load_plan_export(session, plan_id)
    buffer = io.StringIO()
    # 统一行结束符，使输出与平台无关、往返可断言。
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow(["# schedule"])
    writer.writerow(list(SCHEDULE_HEADERS))
    for row in data.schedule:
        writer.writerow(
            [
                escape_formula(row.job_id),
                escape_formula(row.order_id),
                escape_formula(row.product_id),
                escape_formula(row.quantity),
                row.operation_sequence,
                escape_formula(row.machine_id),
                escape_formula(row.worker_id),
                escape_formula(row.start_time),
                escape_formula(row.end_time),
                row.setup_minutes,
            ]
        )

    writer.writerow(["# unschedulable"])
    writer.writerow(list(UNSCHEDULABLE_HEADERS))
    for urow in data.unschedulable:
        writer.writerow(
            [
                escape_formula(urow.job_id),
                escape_formula(urow.order_id),
                escape_formula(urow.blocking_reason),
                escape_formula(urow.unblock_suggestion),
            ]
        )

    footer = data.footer
    writer.writerow([f"# plan_id={escape_formula(footer.plan_id)}"])
    writer.writerow([f"# approved_by={escape_formula(footer.approved_by)}"])
    writer.writerow([f"# approved_at={escape_formula(footer.approved_at)}"])
    writer.writerow([f"# plan_version={footer.plan_version}"])
    if footer.superseded_by is not None:
        writer.writerow([f"# SUPERSEDED_BY={escape_formula(footer.superseded_by)}"])

    return buffer.getvalue().encode("utf-8")
