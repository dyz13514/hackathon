"""计划端点的 API 测试（任务 2.12，design.md Components §5）。

覆盖四个端点与认证边界：

- `POST /plans/generate` —— 需要会话；返回 `PENDING_APPROVAL` 计划，含 R5.5 六项；
  未认证得到 401（写端点受 `Session_Auth` 保护，R23.12）。
- `GET /plans/{plan_id}` —— 生成后可回读，形状与生成响应一致；不存在返回 `PLAN_NOT_FOUND`。
- `GET /plans/pending` —— 生成后列出该计划。
- `GET /plans/active` —— 无 ACTIVE 计划时为空（本任务不激活计划）。

走真实 SQLite 文件与真实 seed 数据（经 `POST /demo/reset` 铺入），不 mock：这几个端点
交付的就是「HTTP 层把内核结果序列化成了什么、认证有没有真的挡住写请求」。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.db.session import create_db_engine
from app.main import create_app
from app.seed.loader import load_demo_data
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "plans-api.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    """已建表、已铺 seed、已登录的客户端。

    seed 直接经会话工厂写入（不经 `POST /demo/reset`，那条路径在 test_demo_reset 已覆盖），
    因此这里聚焦计划端点本身。
    """
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)
    factory: sessionmaker[Session] = application.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    with TestClient(application) as test_client:
        test_client.post(
            LOGIN, json={"password": app_settings.session_shared_password.get_secret_value()}
        )
        yield test_client


def _anonymous(app_settings: Settings) -> TestClient:
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)
    factory: sessionmaker[Session] = application.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    return TestClient(application)


# --------------------------------------------------------------------------
# POST /plans/generate
# --------------------------------------------------------------------------


def test_local_empty_database_requires_real_scheduling_inputs(
    valid_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    valid_env.setenv("APP_ENV", "LOCAL")
    valid_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    settings = Settings()  # type: ignore[call-arg]
    application = create_app(settings)
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as test_client:
        test_client.post(
            LOGIN, json={"password": settings.session_shared_password.get_secret_value()}
        )
        response = test_client.post(GENERATE, json={})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "SCHEDULING_INPUTS_MISSING"
    with application.state.session_factory() as db:
        assert db.query(orm.ProductionPlan).count() == 0


def test_generate_returns_pending_plan_with_all_six_fields(client: TestClient) -> None:
    """生成返回 200 + `PENDING_APPROVAL` + R5.5 六项齐全。"""
    response = client.post(GENERATE, json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "PENDING_APPROVAL"
    assert body["feasibility"] in {"FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"}
    assert isinstance(body["scheduled_jobs"], list)
    assert isinstance(body["unschedulable_jobs"], list)
    assert len(body["objective_breakdown"]["components"]) == 7
    assert body["baseline_comparison"]["snapshot_version"] >= 1
    assert body["generated_by_trace_id"].startswith("TRACE-")


def _plan_ids_for_date(app_settings: Settings, production_date: str) -> list[str]:
    """该生产日上全部计划 ID（按 ID 排序）。用于断言「拒绝发生在进入流水线之前」。"""
    engine = create_db_engine(app_settings)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT plan_id FROM production_plans "
                    "WHERE production_date = :pd ORDER BY plan_id"
                ),
                {"pd": production_date},
            ).all()
        return [str(row[0]) for row in rows]
    finally:
        engine.dispose()


def _status_counts(app_settings: Settings, production_date: str) -> dict[str, int]:
    """该生产日上各状态的计划数。用于断言「一日至多一个 ACTIVE / PENDING」的不变量。"""
    engine = create_db_engine(app_settings)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT status, COUNT(*) FROM production_plans "
                    "WHERE production_date = :pd GROUP BY status"
                ),
                {"pd": production_date},
            ).all()
        return {str(row[0]): int(row[1]) for row in rows}
    finally:
        engine.dispose()


def _count_rows(app_settings: Settings, sql: str, params: dict[str, object]) -> int:
    """执行一条 COUNT 查询（供「历史记录没被删除」这类断言）。"""
    engine = create_db_engine(app_settings)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(sql), params).scalar_one())
    finally:
        engine.dispose()


def _jobs_per_plan(app_settings: Settings, production_date: str) -> dict[str, int]:
    """该生产日每个计划的 `scheduled_jobs` 行数（「历史计划没有被掏空」的证据）。"""
    engine = create_db_engine(app_settings)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT p.plan_id, COUNT(sj.job_id) FROM production_plans p "
                    "LEFT JOIN scheduled_jobs sj ON sj.plan_id = p.plan_id "
                    "WHERE p.production_date = :pd GROUP BY p.plan_id"
                ),
                {"pd": production_date},
            ).all()
        return {str(row[0]): int(row[1]) for row in rows}
    finally:
        engine.dispose()


def _mitigation_plan_links(app_settings: Settings) -> list[str]:
    """全部非空 `risk_findings.mitigation_plan_id`（外键仍指向活着的计划的证据）。"""
    engine = create_db_engine(app_settings)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT mitigation_plan_id FROM risk_findings "
                    "WHERE mitigation_plan_id IS NOT NULL"
                )
            ).all()
        return sorted(str(row[0]) for row in rows)
    finally:
        engine.dispose()


def test_second_generate_with_the_ui_request_shape_returns_pending_plan_exists(
    client: TestClient, app_settings: Settings
) -> None:
    """UI 的请求形状（空 body）下，重复生成必须返回 409 `PENDING_PLAN_EXISTS`。

    **回归**：前端 `generatePlan()` 不带日期时发送 `{}`，因此 `GeneratePlanIn.production_date`
    为 `None`，而前置检查原先直接拿它查库——`production_date == None` 渲染成 `IS NULL`，恒不
    命中，守卫被绕过；第二次点击于是进入流水线，在按生产日清理旧计划时撞外键
    （`plan_approvals` / `risk_findings` 仍引用它们）或撞 `ux_pending_per_day`，对外表现为
    500 而不是文档化的业务错误。

    三处断言：① 拿到 `PENDING_PLAN_EXISTS` 而不是数据库错误；② 拒绝发生在**进入流水线之前**
    （该生产日的计划集合逐字段不变：既没有新增，也没有被清理）；③ 带可执行的下一步入口。
    """
    first = client.post(GENERATE, json={})
    assert first.status_code == 200, first.text
    first_body = first.json()
    first_plan_id = first_body["plan_id"]
    baseline_plan_id = first_body["baseline_comparison"]["baseline_plan_id"]
    production_date = first_body["production_date"]

    before = _plan_ids_for_date(app_settings, production_date)
    assert first_plan_id in before
    assert baseline_plan_id in before

    # 前端 `generatePlan()` 未选日期时的真实请求体就是 `{}`。
    second = client.post(GENERATE, json={})

    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "PENDING_PLAN_EXISTS"
    assert error["details"]["existing_plan_id"] == first_plan_id
    assert any(action["action"] == "approve" for action in error["next_actions"])
    # 被拒绝且没有进入流水线：该生产日的计划集合完全不变。
    assert _plan_ids_for_date(app_settings, production_date) == before


def test_second_generate_with_explicit_date_also_returns_pending_plan_exists(
    client: TestClient,
) -> None:
    """显式传 `production_date` 时同样返回 409——守卫对两种请求形状都生效。"""
    first = client.post(GENERATE, json={})
    assert first.status_code == 200, first.text

    second = client.post(
        GENERATE, json={"production_date": first.json()["production_date"]}
    )

    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "PENDING_PLAN_EXISTS"



def test_generate_after_approval_succeeds_and_keeps_history(
    client: TestClient, app_settings: Settings
) -> None:
    """生命周期回归：**生成 → 批准 → 再生成**，且历史计划与相关记录原样保留。

    **回归的失败形态**：流水线落库前先 `DELETE FROM production_plans WHERE production_date
    = :pd`（连带删 `scheduled_jobs` / `objective_breakdowns` / `baseline_comparisons`）。批准
    之后该生产日已有被 `plan_approvals`、`risk_findings` 等引用的计划，这条 DELETE 当场撞外键
    → `FOREIGN KEY constraint failed` → 生成接口 500，只能靠重置演示数据绕开。

    修复后的语义：旧计划原地留作历史（仍是 `ACTIVE`，作业行 / 基线 / 审批记录都还在），新计划
    以 `PENDING_APPROVAL` 落库；一日至多一个 `ACTIVE` 与一个 `PENDING` 的不变量照旧成立。
    """
    # ① 生成
    first = client.post(GENERATE, json={})
    assert first.status_code == 200, first.text
    first_body = first.json()
    first_id = first_body["plan_id"]
    baseline_id = first_body["baseline_comparison"]["baseline_plan_id"]
    production_date = first_body["production_date"]
    scheduled_job_ids = [job["job_id"] for job in first_body["scheduled_jobs"]]
    assert scheduled_job_ids, "seed 数据下应能排出作业"

    # ② 批准（`Approval_Service` 是唯一能置 ACTIVE 的路径）
    approved = client.post(
        f"/api/plans/{first_id}/approve",
        json={"expected_version": first_body["plan_version"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ACTIVE"

    # 批准会触发风险扫描，可能自动开出一个待审的缓解提案（R14.7）。R12.6 要求同一生产日先
    # 处置既有 PENDING 才能再生成，因此按正常流程把它拒掉、腾出待审位——这是业务动作而不是
    # 数据清理：被拒的提案依然是可查询的历史（status=REJECTED）。
    for row in client.get("/api/plans/pending").json():
        rejected = client.post(
            f"/api/plans/{row['plan_id']}/reject",
            json={"rejection_reason": "Auto-proposed mitigation not needed for this run."},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "REJECTED"

    # 两个「既有历史」快照：这正是线上那次失败的现场条件——同一生产日已经躺着多份历史计划
    # （一份 ACTIVE、一份被拒的提案、若干 DRAFT 基线），且它们被 `plan_approvals` /
    # `risk_findings.mitigation_plan_id` 引用。旧的「清理当日计划」在这种现场必撞外键。
    plans_before = _plan_ids_for_date(app_settings, production_date)
    jobs_before = _jobs_per_plan(app_settings, production_date)
    links_before = _mitigation_plan_links(app_settings)
    statuses_before = _status_counts(app_settings, production_date)
    assert len(plans_before) >= 4, plans_before
    assert statuses_before.get("ACTIVE") == 1, statuses_before
    assert statuses_before.get("DRAFT", 0) >= 2, statuses_before  # 基线计划
    assert links_before, "批准触发的风险扫描应把 CRITICAL 链到缓解提案上"

    # ③ 再生成：修复前这里就是那记 500（FOREIGN KEY constraint failed）
    second = client.post(GENERATE, json={})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["plan_id"] != first_id
    assert second_body["production_date"] == production_date
    assert second_body["status"] == "PENDING_APPROVAL"

    # ④ 历史计划一份都没少（新计划只增不减）
    plans_after = _plan_ids_for_date(app_settings, production_date)
    assert set(plans_before) <= set(plans_after), (
        f"既有计划被删掉了：{sorted(set(plans_before) - set(plans_after))}"
    )
    assert sorted(set(plans_after) - set(plans_before)) == sorted(
        [second_body["plan_id"], second_body["baseline_comparison"]["baseline_plan_id"]]
    ), "新生成应当且只应当新增「提案 + 基线」两份计划"

    # ⑤ 历史行没有被掏空：每个既有计划的 `scheduled_jobs` 行数逐份不变
    jobs_after = _jobs_per_plan(app_settings, production_date)
    for plan_id, count in jobs_before.items():
        assert jobs_after.get(plan_id) == count, (
            f"计划 {plan_id} 的作业行被改动了：{count} → {jobs_after.get(plan_id)}"
        )

    # ⑥ 被外键引用的记录仍然有效：风险发现的缓解提案链接原样指向仍存在的计划
    links_after = _mitigation_plan_links(app_settings)
    assert links_after == links_before
    assert set(links_after) <= set(plans_after), "缓解提案链接指向了不存在的计划"

    # ⑦ 旧计划仍可读、仍是 ACTIVE、作业行齐全、基线仍可读
    old = client.get(f"/api/plans/{first_id}")
    assert old.status_code == 200, old.text
    old_body = old.json()
    assert old_body["status"] == "ACTIVE"
    # 逐作业仍齐全（生成响应按排产顺序、回读端点按 job_id 排序，故比对集合而非顺序）
    assert sorted(job["job_id"] for job in old_body["scheduled_jobs"]) == sorted(
        scheduled_job_ids
    )
    assert old_body["baseline_comparison"]["baseline_plan_id"] == baseline_id
    assert client.get(f"/api/plans/{baseline_id}").status_code == 200

    # ⑧ 审批记录仍可查：`plan_approvals` 行没被「清理当日计划」连带删掉
    assert (
        _count_rows(
            app_settings,
            "SELECT COUNT(*) FROM plan_approvals WHERE plan_id = :pid",
            {"pid": first_id},
        )
        == 1
    )

    # ⑨ 不变量：该生产日恰一个 ACTIVE、恰一个 PENDING
    counts = _status_counts(app_settings, production_date)
    assert counts.get("ACTIVE") == 1, counts
    assert counts.get("PENDING_APPROVAL") == 1, counts


def test_generate_scheduled_jobs_carry_operation_and_changeover(client: TestClient) -> None:
    """已排产作业带工序号与换型分钟（供甘特图，R5.5 / R4.4）。"""
    body = client.post(GENERATE, json={}).json()
    assert body["scheduled_jobs"], "seed 数据下应能排出作业"
    job = body["scheduled_jobs"][0]
    assert set(job) == {
        "job_id",
        "order_id",
        "product_id",
        "operation_sequence",
        "machine_id",
        "worker_id",
        "start_time",
        "end_time",
        "setup_minutes",
        "changeover_minutes",
    }
    assert job["operation_sequence"] >= 1


def test_generate_requires_authentication(app_settings: Settings) -> None:
    """未认证的生成请求得到 401（写端点受 `Session_Auth` 保护，R23.12）。"""
    with _anonymous(app_settings) as anonymous:
        response = anonymous.post(GENERATE, json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_generate_rejects_unknown_fields(client: TestClient) -> None:
    """请求体不接受 `status`（或任何多余字段）——状态由流水线硬编码（R11.8）。"""
    response = client.post(GENERATE, json={"status": "ACTIVE"})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /plans/{plan_id}
# --------------------------------------------------------------------------


def test_get_plan_roundtrips_the_generated_plan(client: TestClient) -> None:
    """生成后按 ID 回读，关键字段与生成响应一致。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    fetched = client.get(f"/api/plans/{plan_id}")
    assert fetched.status_code == 200
    body = fetched.json()

    assert body["plan_id"] == plan_id
    assert body["status"] == "PENDING_APPROVAL"
    assert body["feasibility"] == generated["feasibility"]
    assert len(body["scheduled_jobs"]) == len(generated["scheduled_jobs"])
    assert body["baseline_comparison"]["baseline_plan_id"] == (
        generated["baseline_comparison"]["baseline_plan_id"]
    )


def test_get_unknown_plan_returns_plan_not_found(client: TestClient) -> None:
    response = client.get("/api/plans/PLAN-nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /plans/{plan_id}/explanation —— 结构化解释 + numeric_check（R10，任务 5.11）
# --------------------------------------------------------------------------


def test_get_explanation_returns_structured_evidence_and_numeric_check(
    client: TestClient,
) -> None:
    """解释端点返回结构化证据 + 叙述 + numeric_check（R10、design.md §2.1）。

    测试环境 `LLM_MODE=STUB`：未录制的解释调用返回固定占位文本（不含数字，因此数值比对
    通过），`numeric_check = PASS`。结构化字段是确定性真值：初始计划无 delta，故
    `decision_evidence` 为空、`counterfactual.kind = NO_TRADEOFF`、置信度 LOW（3 条假设）。
    """
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.get(f"/api/plans/{plan_id}/explanation")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["plan_id"] == plan_id
    assert body["numeric_check"] in {"PASS", "FALLBACK"}
    assert body["narrative"]
    # 初始计划：无逐作业决策证据，反事实为单值 NO_TRADEOFF（R10.3 恰好 1 项）。
    assert body["decision_evidence"] == []
    assert body["counterfactual"]["kind"] == "NO_TRADEOFF"
    assert body["counterfactual"]["reason"]
    # 三条可能过期的假设 → 置信度 LOW（R10.4 / R10.5）。
    assert len(body["assumptions"]) == 3
    assert body["confidence"]["level"] == "LOW"
    assert body["confidence"]["basis"]


def test_get_explanation_for_unknown_plan_is_404(client: TestClient) -> None:
    response = client.get("/api/plans/PLAN-nope/explanation")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /plans/pending & /plans/active
# --------------------------------------------------------------------------


def test_pending_lists_the_generated_plan(client: TestClient) -> None:
    generated = client.post(GENERATE, json={}).json()
    pending = client.get("/api/plans/pending").json()
    ids = {plan["plan_id"] for plan in pending}
    assert generated["plan_id"] in ids
    assert all(plan["status"] == "PENDING_APPROVAL" for plan in pending)


def test_active_is_empty_before_any_approval(client: TestClient) -> None:
    """本任务不激活计划，因此生成后 `/plans/active` 仍为空。"""
    client.post(GENERATE, json={})
    active = client.get("/api/plans/active").json()
    assert active == []


def test_baseline_plan_is_not_listed_as_pending(client: TestClient) -> None:
    """基线计划（DRAFT / BASELINE）永不进审批流，因此不出现在 pending 列表（design.md §8）。"""
    generated = client.post(GENERATE, json={}).json()
    baseline_id = generated["baseline_comparison"]["baseline_plan_id"]
    pending_ids = {plan["plan_id"] for plan in client.get("/api/plans/pending").json()}
    assert baseline_id not in pending_ids


# --------------------------------------------------------------------------
# PATCH /plans/{id} —— 审批绕过防护（R11.8 / R22.9 / R23.4，任务 3.3）
# --------------------------------------------------------------------------


def test_patch_with_status_key_is_forbidden_403(client: TestClient) -> None:
    """请求体含 `status` 键即 `403 FORBIDDEN`——试图绕过审批直接改计划状态（EVAL-207）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={"status": "ACTIVE"})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PLAN_STATUS_WRITE_FORBIDDEN"


def test_patch_status_does_not_change_plan_state(client: TestClient) -> None:
    """403 之后计划状态保持 `PENDING_APPROVAL`——绕过尝试没有任何副作用。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    client.patch(f"/api/plans/{plan_id}", json={"status": "ACTIVE"})

    after = client.get(f"/api/plans/{plan_id}").json()
    assert after["status"] == "PENDING_APPROVAL"


def test_patch_status_null_is_still_forbidden(client: TestClient) -> None:
    """`status` 键存在即拒，与它的值无关（`null` 也不行）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={"status": None})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PLAN_STATUS_WRITE_FORBIDDEN"


def test_patch_writes_audit_on_forbidden_status(client: TestClient, app_settings: Settings) -> None:
    """403 的同时写一条 `PLAN_STATUS_WRITE_FORBIDDEN` 审计（留痕绕过尝试，R24.4）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    client.patch(f"/api/plans/{plan_id}", json={"status": "ACTIVE"})

    engine = create_db_engine(app_settings)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type FROM audit_log "
                "WHERE subject_id = :sid AND event_type = 'PLAN_STATUS_WRITE_FORBIDDEN'"
            ),
            {"sid": plan_id},
        ).fetchall()
    assert len(rows) == 1


def test_patch_unknown_field_is_422(client: TestClient) -> None:
    """非 `status` 的多余字段走常规校验：`PlanUpdateIn` 的 `extra="forbid"` → 422。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={"feasibility": "FEASIBLE"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLAN_UPDATE_INVALID"


def test_patch_empty_body_is_noop_200(client: TestClient) -> None:
    """空请求体是「无字段可更新」的 no-op，返回 200（P0 计划无可就地写字段）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={})
    assert response.status_code == 200
    assert response.json()["updated_fields"] == []


def test_patch_requires_authentication(app_settings: Settings) -> None:
    """未认证的 PATCH 得到 401（写端点受 `Session_Auth` 保护，R23.12）。"""
    with _anonymous(app_settings) as anonymous:
        response = anonymous.patch("/api/plans/PLAN-x", json={"status": "ACTIVE"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
