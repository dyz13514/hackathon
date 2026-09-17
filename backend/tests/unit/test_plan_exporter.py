"""`Plan_Exporter` 的导出往返与公式转义单元测试（任务 3.6，承接原属性 38）。

design.md Testing Strategy §3 把原属性 38 收敛为本文件：**导出往返 + 公式转义**，与
`EVAL-014`（导出可重读且一致）、`EVAL-213`（公式注入）互补。

覆盖四块：

1. **纯函数**：`escape_formula` 对 6 类危险起始字符各一例 + 安全值 + 空串；
   `expand_unblock_suggestion` 的确定性展开。
2. **xlsx 往返**：生成 → 审批 → 导出 → 用 openpyxl 重开 → 三个工作表齐全、schedule 行与
   计划一致、footer 带 `approved_by` / `approved_at` / `plan_version`。
3. **csv 往返**：同上，重新解析分段与 `#` 前缀页脚行。
4. **公式转义落到导出**：往库里塞一条 `blocking_reason` 值以 `=` 开头的不可排产作业，
   断言导出后该单元格被前置单引号中和，且 xlsx 单元格 `data_type == "s"`。

走真实 SQLite + 真实 seed + 真实内核（不 mock）：导出交付的就是「库里的计划变成了什么字节」。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.main import create_app
from app.seed.loader import load_demo_data
from app.services.exporter import (
    SCHEDULE_HEADERS,
    UNSCHEDULABLE_HEADERS,
    escape_formula,
    expand_unblock_suggestion,
    export_csv,
    export_xlsx,
)
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "exporter.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def application(app_settings: Settings) -> object:
    app = create_app(app_settings)
    Base.metadata.create_all(app.state.engine)
    factory: sessionmaker[Session] = app.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    return app


@pytest.fixture
def client(application: object) -> Iterator[TestClient]:
    app = application  # type: ignore[assignment]
    with TestClient(app) as test_client:  # type: ignore[arg-type]
        password = app.state.settings.session_shared_password.get_secret_value()  # type: ignore[attr-defined]
        test_client.post(LOGIN, json={"password": password})
        yield test_client


def _anonymous(app_settings: Settings) -> TestClient:
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)
    factory: sessionmaker[Session] = application.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    return TestClient(application)


def _generate_and_approve(client: TestClient) -> dict:
    """生成一个提案并批准它，返回生成响应全文。批准后计划为 `ACTIVE`（唯一合法导出前提）。"""
    generated = client.post(GENERATE, json={})
    assert generated.status_code == 200, generated.text
    body = generated.json()
    plan_id = body["plan_id"]

    fetched = client.get(f"/api/plans/{plan_id}").json()
    approve = client.post(
        f"/api/plans/{plan_id}/approve",
        json={"expected_version": 1},
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "ACTIVE"
    del fetched
    return body


# --------------------------------------------------------------------------
# 1. 纯函数：escape_formula
# --------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
def test_escape_formula_prefixes_dangerous_leading_chars(trigger: str) -> None:
    """6 类危险起始字符各前置一个单引号（R20.6）。"""
    value = f"{trigger}cmd|'/C calc'!A1"
    escaped = escape_formula(value)
    assert escaped == "'" + value
    assert escaped.startswith("'")


@pytest.mark.parametrize("safe", ["ORD-001", "CNC-01", "hello", "1.5", "job=in_middle"])
def test_escape_formula_leaves_safe_values_untouched(safe: str) -> None:
    """不以危险字符开头的值原样返回（中间出现 `=` 不触发）。"""
    assert escape_formula(safe) == safe


def test_escape_formula_empty_string_gets_quote_prefix() -> None:
    """空串被转义成 `"'"`：`"" in "=+-@\\t\\r"` 在 Python 里恒为 True，这是 design.md §4.5
    公式定义的字面后果，实现与 EVAL-213 口径一致地保留它。"""
    assert escape_formula("") == "'"


# --------------------------------------------------------------------------
# 2. 纯函数：expand_unblock_suggestion
# --------------------------------------------------------------------------


def test_expand_unblock_suggestion_is_deterministic_and_sorted() -> None:
    """键按字母序展开成 `k=v; k=v`，列表值逗号分隔——同输入同输出。"""
    suggestion = {
        "shortfall_quantity": "40",
        "material_id": "STEEL",
        "qualifying_machine_types": ["CNC", "LATHE"],
    }
    rendered = expand_unblock_suggestion(suggestion)
    assert rendered == (
        "material_id=STEEL; qualifying_machine_types=CNC,LATHE; shortfall_quantity=40"
    )
    # 确定性：再展开一次逐字符相同。
    assert expand_unblock_suggestion(suggestion) == rendered


def test_expand_unblock_suggestion_empty_is_empty_string() -> None:
    assert expand_unblock_suggestion({}) == ""


# --------------------------------------------------------------------------
# 3. xlsx 往返
# --------------------------------------------------------------------------


def test_xlsx_export_has_three_sheets_and_headers(
    client: TestClient, application: object
) -> None:
    """导出的 `.xlsx` 有 schedule / unschedulable / footer 三个工作表，表头为 R20.2/3 的列。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        content = export_xlsx(db, plan_id)

    workbook = load_workbook(io.BytesIO(content))
    assert workbook.sheetnames == ["schedule", "unschedulable", "footer"]

    schedule = workbook["schedule"]
    header_row = tuple(cell.value for cell in schedule[1])
    assert header_row == SCHEDULE_HEADERS

    unsched = workbook["unschedulable"]
    unsched_header = tuple(cell.value for cell in unsched[1])
    assert unsched_header == UNSCHEDULABLE_HEADERS


def test_xlsx_schedule_rows_roundtrip_the_plan(
    client: TestClient, application: object
) -> None:
    """schedule 工作表的行数与内容与计划的已排产作业一致，且按 (machine_id, start_time) 有序。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]
    expected_jobs = {job["job_id"] for job in body["scheduled_jobs"]}

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        content = export_xlsx(db, plan_id)

    workbook = load_workbook(io.BytesIO(content))
    schedule = workbook["schedule"]
    data_rows = list(schedule.iter_rows(min_row=2, values_only=True))
    assert len(data_rows) == len(body["scheduled_jobs"])

    exported_job_ids = [row[0] for row in data_rows]
    assert set(exported_job_ids) == expected_jobs

    # 分组 + 组内时间升序：把 (machine_id, start_time) 抽出来断言单调不减。
    machine_start = [(row[5], row[7]) for row in data_rows]
    assert machine_start == sorted(machine_start)


def test_xlsx_footer_carries_approval_metadata(
    client: TestClient, application: object
) -> None:
    """footer 工作表含 plan_id / approved_by / approved_at / plan_version（R20.4）。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        content = export_xlsx(db, plan_id)

    workbook = load_workbook(io.BytesIO(content))
    footer = workbook["footer"]
    footer_map = {
        row[0]: row[1] for row in footer.iter_rows(min_row=2, values_only=True)
    }
    assert footer_map["plan_id"] == plan_id
    # 批准者取自 action=APPROVE 的审批记录（actor 为大写会话主体 "PLANNER"）。
    assert footer_map["approved_by"] == "PLANNER"
    assert footer_map["approved_at"], "已批准计划的 approved_at 不应为空"
    assert footer_map["plan_version"] == 1


def test_xlsx_export_is_byte_stable(client: TestClient, application: object) -> None:
    """同一计划导出两次内容相等（确定性排序 + 确定性展开的直接后果）。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        first = export_xlsx(db, plan_id)
    with factory() as db:
        second = export_xlsx(db, plan_id)

    # openpyxl 写的 xlsx 是 zip，元数据可能带时间戳；比对逻辑内容（各表的单元格值）而非原始字节。
    wb1 = load_workbook(io.BytesIO(first))
    wb2 = load_workbook(io.BytesIO(second))
    for name in wb1.sheetnames:
        rows1 = list(wb1[name].iter_rows(values_only=True))
        rows2 = list(wb2[name].iter_rows(values_only=True))
        assert rows1 == rows2


# --------------------------------------------------------------------------
# 4. csv 往返
# --------------------------------------------------------------------------


def test_csv_export_roundtrips_schedule_and_footer(
    client: TestClient, application: object
) -> None:
    """`.csv` 分段可重新解析：schedule 段行数与计划一致，页脚 `#` 行带审批元数据。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        content = export_csv(db, plan_id)

    text = content.decode("utf-8")
    reader = list(csv.reader(io.StringIO(text)))

    # 段标题存在。
    assert ["# schedule"] in reader
    assert ["# unschedulable"] in reader

    # schedule 段的数据行数 == 计划的已排产作业数。
    sched_idx = reader.index(["# schedule"])
    header_idx = sched_idx + 1
    assert tuple(reader[header_idx]) == SCHEDULE_HEADERS
    unsched_idx = reader.index(["# unschedulable"])
    schedule_data = reader[header_idx + 1 : unsched_idx]
    assert len(schedule_data) == len(body["scheduled_jobs"])

    # 页脚 `#` 行。
    footer_lines = [row[0] for row in reader if row and row[0].startswith("# plan_id=")]
    assert footer_lines == [f"# plan_id={plan_id}"]
    assert any(row and row[0].startswith("# approved_by=PLANNER") for row in reader)
    assert any(row and row[0] == "# plan_version=1" for row in reader)


# --------------------------------------------------------------------------
# 5. 公式转义落到导出（EVAL-213）
# --------------------------------------------------------------------------


def _inject_unschedulable(
    application: object, plan_id: str
) -> tuple[str, str]:
    """往计划里塞一条 blocking_reason 以 `=` 开头的不可排产作业，返回 (job_id, 注入原文)。

    复用计划里已存在的一个 production_job（外键要求 job_id 存在于 production_jobs）；若计划
    全可排产，则退回取任意一个 job。注入原文模拟「订单备注里藏公式」的注入面（R20.6）。
    """
    injection = "=HYPERLINK(\"http://evil\")"
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        job = db.execute(select(orm.ProductionJob)).scalars().first()
        assert job is not None
        # 避免与既有 unschedulable 行的 (plan_id, job_id) 唯一约束撞车：先删同键行。
        existing = db.execute(
            select(orm.UnschedulableJob).where(
                orm.UnschedulableJob.plan_id == plan_id,
                orm.UnschedulableJob.job_id == job.job_id,
            )
        ).scalars().first()
        if existing is not None:
            db.delete(existing)
            db.flush()
        db.add(
            orm.UnschedulableJob(
                id=f"UNS-inject-{job.job_id}",
                plan_id=plan_id,
                job_id=job.job_id,
                blocking_reason=injection,
                unblock_suggestion={"note": injection, "minutes_needed": 10},
            )
        )
        db.commit()
    return job.job_id, injection


def test_xlsx_escapes_injected_formula_and_forces_string_type(
    client: TestClient, application: object
) -> None:
    """注入的 `=`-起始文本在 xlsx 里被前置单引号，且单元格强制为字符串类型（R20.6）。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]
    job_id, injection = _inject_unschedulable(application, plan_id)

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        content = export_xlsx(db, plan_id)

    workbook = load_workbook(io.BytesIO(content))
    unsched = workbook["unschedulable"]
    target = None
    for row in unsched.iter_rows(min_row=2):
        if row[0].value == job_id:
            target = row
            break
    assert target is not None, "注入的不可排产作业应出现在 unschedulable 工作表"

    blocking_cell = target[2]
    # 前置单引号中和公式。
    assert blocking_cell.value == "'" + injection
    # 强制字符串类型（第二道防线）。
    assert blocking_cell.data_type == "s"


def test_csv_escapes_injected_formula(
    client: TestClient, application: object
) -> None:
    """CSV 同样中和注入文本（CSV 也会被电子表格软件打开）。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]
    _job_id, injection = _inject_unschedulable(application, plan_id)

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        content = export_csv(db, plan_id)

    text = content.decode("utf-8")
    # CSV writer 会给含双引号的字段整体加引号并把内部 `"` 翻倍，因此不能对原始文本做子串断言；
    # 解析回来后按单元格值断言：注入被中和成前置单引号的形式（'=...）。
    reader = list(csv.reader(io.StringIO(text)))
    escaped_cells = [cell for row in reader for cell in row if cell == "'" + injection]
    assert escaped_cells, "注入文本应以前置单引号的形式出现在 CSV 里"


# --------------------------------------------------------------------------
# 6. 端点：POST /plans/{id}/export
# --------------------------------------------------------------------------


def test_export_endpoint_returns_xlsx(client: TestClient) -> None:
    """`POST /plans/{id}/export?format=xlsx` 返回 OOXML 二进制。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    response = client.post(f"/api/plans/{plan_id}/export?format=xlsx")
    assert response.status_code == 200, response.text
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # xlsx 是 zip：以 PK 魔数开头。
    assert response.content[:2] == b"PK"
    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["schedule", "unschedulable", "footer"]


def test_export_endpoint_returns_csv(client: TestClient) -> None:
    """`?format=csv` 返回文本 CSV。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    response = client.post(f"/api/plans/{plan_id}/export?format=csv")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "# schedule" in response.text


def test_export_endpoint_defaults_to_xlsx(client: TestClient) -> None:
    """省略 `format` 默认 xlsx。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    response = client.post(f"/api/plans/{plan_id}/export")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


def test_export_endpoint_rejects_unknown_format(client: TestClient) -> None:
    """未知格式返回 422 `EXPORT_FORMAT_UNSUPPORTED`（不静默回退）。"""
    body = _generate_and_approve(client)
    plan_id = body["plan_id"]

    response = client.post(f"/api/plans/{plan_id}/export?format=pdf")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EXPORT_FORMAT_UNSUPPORTED"


def test_export_unknown_plan_returns_plan_not_found(client: TestClient) -> None:
    response = client.post("/api/plans/PLAN-nope/export?format=xlsx")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_export_requires_authentication(app_settings: Settings) -> None:
    """未认证的导出得到 401（写触发式读端点，受 `Session_Auth` 保护，R23.12）。"""
    with _anonymous(app_settings) as anonymous:
        response = anonymous.post("/api/plans/PLAN-x/export?format=xlsx")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
