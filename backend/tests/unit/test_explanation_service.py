"""`app/services/explanation.py` 的示例测试（任务 5.11，design.md §2.1、R10.7、R25.9、R21.12）。

覆盖解释调用的编排：

1. **恰好 1 次调用、且不发送工具 schema**：`invoke` 被调用一次，`LlmRequest.system` 只有一个
   块（提示词 prose，无工具 schema 块）——省下 ≈2,200 token 是 K-10 成立的关键。
2. **数值一致 → 发布 LLM 文本**（`numeric_check = PASS`）：文本里的数字都能匹配回载荷。
3. **数值不一致 → 回退模板**（`numeric_check = FALLBACK`，R10.7）：文本编造一个载荷外的
   数字，护栏阻止发布并回退模板解释、写 `EXPLANATION_NUMERIC_MISMATCH` 审计。
4. **降级模式 → 回退模板**（R25.9）：`LlmDisabledError` 时发布模板解释，仍恰好一次尝试。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select

from app.core.explain import Explanation
from app.db import audit
from app.db.models import AuditLog, Base
from app.db.session import create_db_engine
from app.llm.adapter import LlmDisabledError, LlmRequest, LlmResponse, LlmUsage
from app.services.explanation import (
    BaselineView,
    ComponentView,
    NumericCheck,
    ScheduledJobView,
    assemble_initial_plan_explanation,
    build_explanation,
    build_explanation_system,
)
from app.settings import Settings


class FakeAdapter:
    """记录调用的假 adapter。`invoke` 返回预置响应或抛预置异常，并保存收到的请求。"""

    def __init__(
        self, *, content: str = "", raise_exc: Exception | None = None
    ) -> None:
        self._content = content
        self._raise = raise_exc
        self.calls: list[LlmRequest] = []

    def invoke(self, req: LlmRequest) -> LlmResponse:
        self.calls.append(req)
        if self._raise is not None:
            raise self._raise
        return LlmResponse(
            content=self._content,
            usage=LlmUsage(input_tokens=1200, output_tokens=300),
        )


@pytest.fixture
def audit_engine(tmp_path: Path) -> Iterator[Engine]:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'audit.db').as_posix()}",
        session_shared_password=SecretStr("pw"),
        session_secret_key=SecretStr("k" * 32),
    )
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    audit.set_audit_engine(engine)
    yield engine
    audit.set_audit_engine(None)
    engine.dispose()


def _components() -> tuple[ComponentView, ...]:
    names = (
        "late_order_count",
        "total_tardiness_minutes",
        "urgent_order_lateness",
        "churn_ratio",
        "machine_utilisation",
        "total_changeover_minutes",
        "preference_penalty",
    )
    return tuple(
        ComponentView(name=n, raw_value=1.0, weight=2.0, weighted_contribution=2.0)
        for n in names
    )


def _inputs() -> tuple[Explanation, dict[str, Any]]:
    result = assemble_initial_plan_explanation(
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
        components=_components(),
        baseline=BaselineView(
            on_time_rate=0.85,
            baseline_on_time_rate=0.6,
            total_tardiness_minutes=315,
            baseline_total_tardiness_minutes=900,
            late_order_count=2,
            baseline_late_order_count=5,
        ),
    )
    return result.explanation, result.payload


# --------------------------------------------------------------------------
# 1. 恰好 1 次调用，不发送工具 schema
# --------------------------------------------------------------------------


def test_explanation_system_has_no_tool_schema_block() -> None:
    """解释调用的 system 只有一个提示词块——**没有工具 schema 块**（K-10 的关键）。"""
    system = build_explanation_system()
    assert len(system) == 1
    prose = system[0]
    # 提示词段齐全，但不含 [TOOLS] 段（那会带来 ≈2,200 token 的工具 schema）。
    assert "[ROLE]" in prose
    assert "[OUTPUT]" in prose
    assert "[TOOLS]" not in prose
    assert "<tool " not in prose


def test_build_explanation_invokes_exactly_once(audit_engine: Engine) -> None:
    """`build_explanation` 恰好调用 `invoke` 一次（design.md §2.1：恰好 1 次）。"""
    explanation, payload = _inputs()
    adapter = FakeAdapter(content="计划把作业排在 CNC-01，准时率 0.85。")
    build_explanation(explanation, payload, adapter, engine=audit_engine)  # type: ignore[arg-type]
    assert len(adapter.calls) == 1
    # 请求的 system 就是不带工具 schema 的那一个块。
    assert len(adapter.calls[0].system) == 1


# --------------------------------------------------------------------------
# 2. 数值一致 → 发布 LLM 文本
# --------------------------------------------------------------------------


def test_consistent_numbers_publish_llm_text(audit_engine: Engine) -> None:
    """文本里的数字都能匹配回载荷 → 发布 LLM 文本，numeric_check = PASS。"""
    explanation, payload = _inputs()
    # 315 分钟、0.85 准时率都在载荷里。
    content = "总拖期 315 分钟，准时率 0.85，比基线 0.6 高。"
    adapter = FakeAdapter(content=content)
    result = build_explanation(explanation, payload, adapter, engine=audit_engine)  # type: ignore[arg-type]

    assert result.numeric_check is NumericCheck.PASS
    assert result.narrative == content
    assert result.fallback_reason is None


# --------------------------------------------------------------------------
# 3. 数值不一致 → 回退模板（R10.7）
# --------------------------------------------------------------------------


def test_inconsistent_numbers_fall_back_to_template(audit_engine: Engine) -> None:
    """文本编造一个载荷外的数字 → 回退模板，numeric_check = FALLBACK，写审计（R10.7）。"""
    explanation, payload = _inputs()
    # 999999 不在载荷任何桶里——编造。
    adapter = FakeAdapter(content="总拖期 999999 分钟，完全离谱。")
    result = build_explanation(explanation, payload, adapter, engine=audit_engine)  # type: ignore[arg-type]

    assert result.numeric_check is NumericCheck.FALLBACK
    assert result.fallback_reason == "EXPLANATION_NUMERIC_MISMATCH"
    # 模板文本天然通过比对（数字全来自结构化真值）：不含编造的 999999。
    assert "999999" not in result.narrative

    with audit_engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_category == "EXPLANATION_NUMERIC_MISMATCH")
        ).scalar_one()
    assert count == 1


# --------------------------------------------------------------------------
# 4. 降级模式 → 回退模板（R25.9）
# --------------------------------------------------------------------------


def test_disabled_mode_falls_back_to_template(audit_engine: Engine) -> None:
    """Bedrock 禁用（DETERMINISTIC_ONLY）→ 发布模板解释（R25.9）。"""
    explanation, payload = _inputs()
    adapter = FakeAdapter(raise_exc=LlmDisabledError(reason="DETERMINISTIC_ONLY"))
    result = build_explanation(explanation, payload, adapter, engine=audit_engine)  # type: ignore[arg-type]

    assert result.numeric_check is NumericCheck.FALLBACK
    assert result.fallback_reason is not None
    assert result.fallback_reason.startswith("LLM_UNAVAILABLE")
    assert "置信度" in result.narrative  # 模板文本渲染了结构化证据
