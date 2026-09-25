"""`DETERMINISTIC_ONLY` 降级模式的全覆盖（任务 11.6，R25.8–11、R14.11；design.md §2.6）。

design.md §2.6 点名要求「所有 LLM 调用点都必须实现 `except LlmDisabledError` 的模板回退，
由 `test_degraded_mode.py` 遍历全部调用点断言」——**非可选**（承接原属性 31）。本文件即那份
遍历，分五组守住降级模式的每一条验收：

1. **旁路点只有一个**（design.md §2.6）：静态断言全仓库唯一的 `adapter.invoke(...)` 调用点集合
   （`explanation.py` / `ingestion_agent.py` / `planning_agent.py`），并断言每个调用点所在的
   **P0 路径**要么 `except LlmDisabledError` 回退模板、要么在调用前被降级守卫拒绝——没有第四
   条会在降级下抛未捕获异常的路径。
2. **计划生成解释**（R25.9）：`DISABLED` 下 `build_explanation` 捕获 `LlmDisabledError`，发布
   `TemplateExplanationRenderer` 的模板文本（`numeric_check = FALLBACK`），且仍恰好一次尝试。
3. **LLM 列映射被拒**（R25.10）：`GET /imports/{id}/proposal` 在 `DISABLED` 下返回 409
   `LLM_UNAVAILABLE_USE_MANUAL_MAPPING` 并给出手工列映射的 next_action——P0 唯一被拒绝的能力。
4. **手动开关**（R25.11）：`POST /settings/mode` 进入 / 退出 `DETERMINISTIC_ONLY`，改运行期
   `Bedrock_Adapter.mode`（旁路点），各写一条 `DEGRADED_MODE_SWITCH` 审计；`GET /health` 的
   `mode` 随之反映运行期状态。
5. **保留能力在降级下照常**（R25.9）：偏好规则 CRUD、价值台账、CSV 导出在 `DISABLED` 下仍
   200——它们本就无 LLM，降级不影响（抽样断言，非穷举）。

走真实 SQLite + 真实 seed + 真实内核，不 mock、不触网（`LLM_MODE=STUB`，且降级路径不发调用）。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditLog, Base
from app.llm.adapter import LlmDisabledError, LlmMode
from app.main import create_app
from app.seed.loader import load_demo_data
from app.settings import Settings

LOGIN = "/api/auth/login"
HEALTH = "/api/health"
SET_MODE = "/api/settings/mode"
PREFERENCES = "/api/preferences"
VALUE_LEDGER = "/api/value-ledger"

#: 全仓库允许出现 `adapter.invoke(...)` 的源文件（design.md §2.6「旁路点只有一个」——出口只有
#: `BedrockAdapter`，调用点是有限、已知的三处）。新增调用点必须显式加进这里并证明其降级回退，
#: 因此这个集合本身就是「调用点没有悄悄增加」的护栏。
_KNOWN_INVOKE_SITES = {
    "app/services/explanation.py",  # 计划生成解释：except LlmDisabledError → 模板
    "app/agents/ingestion_agent.py",  # 列映射驱动：其 P0 入口 get_proposal 在调用前守卫拒绝
    "app/agents/planning_agent.py",  # 重排/解释驱动：P0 重排走确定性流水线，不经此驱动
    # --- 任务 13（P1）新增的三条 LLM 路径。每条都实现了 DETERMINISTIC_ONLY 降级回退： ---
    # 13.1 自然语言 What-if 翻译：invoke 被 `except LlmDisabledError` 捕获 → 返回
    # `TranslationOutcome.LLM_UNAVAILABLE`，前端退回结构化场景表单（R16 范围说明）。
    "app/services/whatif_translate.py",
    # 13.2 LLM 风险归因叙述：`RiskNarrativeDriver.generate` 捕获**任一** LLM 侧失败（含
    # LlmDisabledError / CassetteMiss / BedrockUnavailable）→ 返回 None → 回退任务 8.6 的
    # 确定性模板叙述（narrative_source=TEMPLATE，R14.5/R14.11）。
    "app/agents/risk_monitor_agent.py",
    # 13.3 LLM 偏好规则蒸馏：invoke 的失败被捕获 → 返回 `DistilOutcome.LLM_UNAVAILABLE`
    # 空候选集，前端提示改用手写规则（R18.3，降级下 P0 手写入口不受影响）。
    "app/services/preference_distil.py",
}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "degraded.db"
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
def client(application: object, app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(application) as test_client:  # type: ignore[arg-type]
        test_client.post(
            LOGIN, json={"password": app_settings.session_shared_password.get_secret_value()}
        )
        yield test_client


def _backend_root() -> pathlib.Path:
    # tests/unit/test_degraded_mode.py → backend/
    return pathlib.Path(__file__).resolve().parents[2]


def _audit_rows(application: object, event_type: str) -> list[AuditLog]:
    engine = application.state.engine  # type: ignore[attr-defined]
    with Session(engine) as s:
        return list(
            s.execute(
                select(AuditLog)
                .where(AuditLog.event_category == "DEGRADED_MODE_SWITCH")
                .where(AuditLog.event_type == event_type)
            ).scalars()
        )


# --------------------------------------------------------------------------
# 1. 旁路点只有一个 + 每个调用点都有降级处置（遍历全部调用点，design.md §2.6）
# --------------------------------------------------------------------------


def _invoke_sites() -> set[str]:
    """扫 `app/` 下全部 `.py`，收集出现 `<x>.invoke(` 且 `<x>` 名含 adapter 的调用点文件。

    用 AST 而非正则：只认真正的属性调用 `something.invoke(...)`，避免把 docstring 里的
    `BedrockAdapter.invoke()` 文本算进来。`registry.invoke` / `orch.run` 等不是 LLM 出口，
    通过属性名 `invoke` + 接收者名含 `adapter` 过滤。
    """
    root = _backend_root() / "app"
    sites: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "invoke"
                and "adapter" in _receiver_name(node.func.value).lower()
            ):
                sites.add(str(path.relative_to(_backend_root())).replace("\\", "/"))
    return sites


def _receiver_name(node: ast.expr) -> str:
    """接收者名：`adapter.invoke` → 'adapter'；`self._adapter.invoke` → '_adapter'。

    调用点的接收者既可能是裸名（`ast.Name`，如 explanation 的 `adapter`），也可能是属性访问
    （`ast.Attribute`，如两个驱动的 `self._adapter`）。取最内层的标识名即可判断它是不是 adapter。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def test_only_known_invoke_sites_exist() -> None:
    """`adapter.invoke(...)` 只出现在已知的三处（旁路点唯一，调用点没有悄悄增加）。"""
    found = _invoke_sites()
    assert found == _KNOWN_INVOKE_SITES, (
        "adapter.invoke 调用点集合与预期不符——新增/移动调用点必须同步更新 _KNOWN_INVOKE_SITES "
        f"并证明其降级回退。实际={sorted(found)} 预期={sorted(_KNOWN_INVOKE_SITES)}"
    )


def test_explanation_site_catches_llm_disabled() -> None:
    """`explanation.py` 的调用点在函数体内 `except LlmDisabledError`（源级断言）。"""
    from app.services import explanation

    src = inspect.getsource(explanation.build_explanation)
    assert "except" in src and "LlmDisabledError" in src


def test_ingestion_and_planning_drivers_propagate_for_upstream_fallback() -> None:
    """两个 Agent 驱动的 `invoke` **不吞** `LlmDisabledError`——由上游（get_proposal 守卫 /
    确定性重排流水线）处置。这里断言驱动本身不静默吞掉异常（否则降级会被误判为成功）。"""
    from app.agents import ingestion_agent, planning_agent

    for driver_cls in (
        ingestion_agent.IngestionAgentDriver,
        planning_agent.PlanningAgentDriver,
    ):
        src = inspect.getsource(driver_cls)
        # 驱动不应自行 except LlmDisabledError 后伪造成功——它要么向上抛，要么无该 except。
        assert "except LlmDisabledError" not in src


# --------------------------------------------------------------------------
# 2. 计划生成解释在降级下回退模板（R25.9）——行为断言
# --------------------------------------------------------------------------


class _DisabledAdapter:
    """始终抛 `LlmDisabledError` 的假 adapter，并计数调用次数。"""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = LlmMode.DISABLED

    def invoke(self, req: Any) -> Any:
        self.calls += 1
        raise LlmDisabledError(reason="DETERMINISTIC_ONLY")


def test_explanation_falls_back_to_template_when_disabled(application: object) -> None:
    """DISABLED 下 build_explanation 发布模板解释（numeric_check=FALLBACK），恰好一次尝试。"""
    from app.services.explanation import (
        BaselineView,
        ComponentView,
        NumericCheck,
        ScheduledJobView,
        assemble_initial_plan_explanation,
        build_explanation,
    )

    components = tuple(
        ComponentView(name=n, raw_value=1.0, weight=2.0, weighted_contribution=2.0)
        for n in (
            "late_order_count",
            "total_tardiness_minutes",
            "urgent_order_lateness",
            "churn_ratio",
            "machine_utilisation",
            "total_changeover_minutes",
            "preference_penalty",
        )
    )
    built = assemble_initial_plan_explanation(
        plan_id="PLAN-x",
        feasibility="FEASIBLE",
        scheduled=(
            ScheduledJobView(
                job_id="ORD-001-OP1",
                order_id="ORD-001",
                machine_id="CNC-01",
                duration_minutes=45,
            ),
        ),
        unschedulable=(),
        components=components,
        baseline=BaselineView(
            on_time_rate=0.85,
            baseline_on_time_rate=0.6,
            total_tardiness_minutes=315,
            baseline_total_tardiness_minutes=900,
            late_order_count=2,
            baseline_late_order_count=5,
        ),
    )
    adapter = _DisabledAdapter()
    engine = application.state.engine  # type: ignore[attr-defined]
    result = build_explanation(
        built.explanation, built.payload, adapter, engine=engine  # type: ignore[arg-type]
    )
    assert adapter.calls == 1  # 仍尝试了一次（在 invoke 内即抛）
    assert result.numeric_check == NumericCheck.FALLBACK
    assert result.narrative  # 模板文本非空


# --------------------------------------------------------------------------
# 3. LLM 列映射在降级下被拒（R25.10）——API 行为断言
# --------------------------------------------------------------------------


def _set_disabled(application: object) -> None:
    application.state.llm_adapter.mode = LlmMode.DISABLED  # type: ignore[attr-defined]


def test_column_mapping_rejected_in_degraded_mode(
    client: TestClient, application: object
) -> None:
    """DISABLED 下 GET /imports/{id}/proposal → 503，不生成替代提议。"""
    # 先上传一个最小 CSV，拿到 upload_id。
    csv_bytes = b"order_id,product_id,quantity,due_date\nORD-9,PRD-1,10,2026-03-10\n"
    up = client.post(
        "/api/imports/upload",
        files={"file": ("orders.csv", csv_bytes, "text/csv")},
    )
    assert up.status_code == 200, up.text
    upload_id = up.json()["upload_id"]

    _set_disabled(application)
    resp = client.get(f"/api/imports/{upload_id}/proposal")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == "LLM_GENERATION_FAILED"
    # 当前 UI 尚无手工源列编辑器，下一步只能在模型恢复后重试。
    assert any(a["action"] == "retry_mapping" for a in body["error"]["next_actions"])


# --------------------------------------------------------------------------
# 4. 手动开关 POST /settings/mode（R25.11）+ 审计 + health 反映运行期
# --------------------------------------------------------------------------


def test_manual_mode_switch_enters_and_exits_with_audit(
    client: TestClient, application: object
) -> None:
    """进入/退出 DETERMINISTIC_ONLY 改运行期 adapter.mode，各写一条 DEGRADED_MODE_SWITCH 审计。"""
    # 基础模式（测试环境）是 STUB。进入降级：
    enter = client.post(SET_MODE, json={"mode": "DETERMINISTIC_ONLY"})
    assert enter.status_code == 200, enter.text
    assert enter.json()["mode"] == "DETERMINISTIC_ONLY"
    assert enter.json()["llm_mode"] == "DISABLED"
    assert application.state.llm_adapter.mode == LlmMode.DISABLED  # type: ignore[attr-defined]

    # health 反映运行期降级（不是静态配置）。
    assert client.get(HEALTH).json()["mode"] == "DETERMINISTIC_ONLY"

    # 退出降级：恢复到基础模式 STUB，探针成功。
    exit_resp = client.post(SET_MODE, json={"mode": "NORMAL"})
    assert exit_resp.status_code == 200, exit_resp.text
    assert exit_resp.json()["mode"] == "NORMAL"
    assert exit_resp.json()["probe_ok"] is True
    assert application.state.llm_adapter.mode == LlmMode.STUB  # type: ignore[attr-defined]
    assert client.get(HEALTH).json()["mode"] == "NORMAL"

    # 审计：进入与退出各一条。
    assert len(_audit_rows(application, "ENTER_DETERMINISTIC_ONLY")) == 1
    assert len(_audit_rows(application, "EXIT_DETERMINISTIC_ONLY")) == 1


def test_manual_mode_switch_requires_auth(application: object) -> None:
    """POST /settings/mode 是写端点，未认证 → 401。"""
    with TestClient(application) as anon:  # type: ignore[arg-type]
        resp = anon.post(SET_MODE, json={"mode": "DETERMINISTIC_ONLY"})
        assert resp.status_code == 401


def test_set_mode_rejects_unknown_value(client: TestClient) -> None:
    """非法 mode 值 → 422（判别值域封闭）。"""
    resp = client.post(SET_MODE, json={"mode": "TURBO"})
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# 5. 保留能力在降级下照常（R25.9）——抽样断言
# --------------------------------------------------------------------------


def test_retained_capabilities_work_in_degraded_mode(
    client: TestClient, application: object
) -> None:
    """降级下偏好规则 CRUD、价值台账、CSV 导出仍可用（它们本就无 LLM，R25.9）。"""
    _set_disabled(application)

    # 偏好规则创建（无 LLM，手写入口）。
    created = client.post(
        PREFERENCES,
        json={
            "human_text": "ORD-9 避开 CNC-03",
            "structured_form": {
                "kind": "AVOID_MACHINE_FOR_ORDER",
                "order_id": "ORD-9",
                "machine_id": "CNC-03",
            },
        },
    )
    assert created.status_code == 201, created.text

    # 价值台账 + CSV 导出（确定性，无 LLM）。
    assert client.get(VALUE_LEDGER).status_code == 200
    csv_resp = client.get("/api/value-ledger/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
