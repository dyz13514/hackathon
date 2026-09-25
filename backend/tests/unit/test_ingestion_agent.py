"""`Ingestion_Agent` 列映射 ReAct 路径的接线证明（任务 10.4，R2/R22.7）。**非可选**。

证明**真实 Orchestrator + Ingestion_Agent ReAct 路径实际被执行**（不触网、不 LIVE、不伪造
cassette），而不是绕过 Agent 直接返回：

1. `Intent.INGEST_MAPPING` 路由到 `INGESTION_AGENT`（REACT，budget_scope=None）。
2. 注入 `ScriptedIngestionDriver`（桩，只吐 JSON）+ `ingestion_contracts()` 后，`Orchestrator.run`
   不再抛 `RouteNotWiredError`，脚本化 ReAct 序列
   `read_uploaded_file_preview → propose_column_mapping → validate_mapping → final` 走到
   `outcome == OK`，`final` 是经契约校验的 `ColumnMappingProposal`。
3. 未注入驱动 → `RouteNotWiredError`（负对照）。
4. `run_ingestion_mapping` 用桩驱动跑通并把 Agent `final` 转成 `proposed_mapping`。
5. 三个摄取工具在 ReAct 步里经 `ToolRegistry` 真正执行，证明工具被调用。

不触网：桩驱动只吐字符串，`BedrockAdapter` 从不被调用（`adapter=None` 传入也能跑桩路径）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.agents.contracts import ColumnMappingProposal
from app.agents.ingestion_agent import (
    INGESTION_AGENT,
    ScriptedIngestionDriver,
    ingestion_contracts,
    scripted_action,
    scripted_mapping_final,
)
from app.db.models import Base
from app.llm.adapter import BedrockAdapter, BedrockUnavailableError, LlmMode
from app.llm.budget import TokenBudgetManager
from app.llm.cassette import Cassette
from app.main import create_app
from app.orchestrator.orchestrator import Orchestrator, RouteNotWiredError
from app.orchestrator.routing import Intent
from app.orchestrator.tracing import InMemoryTracer
from app.services.ingestion_agent_run import (
    IngestionMappingUnavailable,
    LLM_UNAVAILABLE_OUTCOME,
    MappingToolSession,
    run_ingestion_mapping,
)
from app.services.spreadsheet import ParsedFile
from app.settings import Settings
from app.tools.build import build_registry
from app.tools.registry import InMemoryToolCallRecorder


class _Payload(BaseModel):
    upload_id: str = "UP-1"


def _parsed() -> ParsedFile:
    return ParsedFile(
        header=["material_id", "name", "quantity_available", "unit"],
        rows=[
            ["MAT-1", "钢板", "100", "件"],
            ["MAT-2", "螺栓", "500", "箱"],
        ],
    )


def _orchestrator(driver: ScriptedIngestionDriver, parsed: ParsedFile):
    tool_session = MappingToolSession(parsed=parsed, upload_id="UP-1")
    tracer = InMemoryTracer()
    orch = Orchestrator(
        registry=build_registry(recorder=InMemoryToolCallRecorder()),
        budget=TokenBudgetManager(),
        tracer=tracer,
        agent_drivers={INGESTION_AGENT: driver},
        contracts=ingestion_contracts(),
        tool_session=tool_session,
    )
    return orch, tracer


# --------------------------------------------------------------------------
# 接线：未注入 → RouteNotWiredError；注入 → 可跑
# --------------------------------------------------------------------------


def test_unwired_ingestion_agent_raises_route_not_wired() -> None:
    orch = Orchestrator(
        registry=build_registry(recorder=InMemoryToolCallRecorder()),
        budget=TokenBudgetManager(),
        tracer=InMemoryTracer(),
    )
    with pytest.raises(RouteNotWiredError):
        orch.run(Intent.INGEST_MAPPING, _Payload(), session_id="S0")


def test_scripted_react_run_executes_tools_and_reaches_final() -> None:
    """完整 ReAct 序列：预览 → 提议 → 校验 → final。证明工具经 registry 真实执行且走到 OK。"""
    final = scripted_mapping_final(
        entity_type="MATERIAL",
        entity_type_confidence=0.9,
        columns=[
            {"target_field": "material_id", "source_column": "material_id", "confidence": 0.95},
            {"target_field": "name", "source_column": "name", "confidence": 0.9},
            {
                "target_field": "quantity_available",
                "source_column": "quantity_available",
                "confidence": 0.9,
            },
        ],
        mapping_rationale="表头与目标字段一一命中。",
    )
    driver = ScriptedIngestionDriver(
        turns=[
            scripted_action("read_uploaded_file_preview", upload_id="UP-1", max_sample_rows=5),
            scripted_action("propose_column_mapping", upload_id="UP-1"),
            # validate_mapping 需要完整 mapping 对象；用工具输出模型的形状喂一个最小映射。
            scripted_action(
                "validate_mapping",
                upload_id="UP-1",
                mapping={
                    "entity_type": "MATERIAL",
                    "entity_type_confidence": 0.9,
                    "field_mappings": [
                        {
                            "target_field": "quantity_available",
                            "source_column": "quantity_available",
                            "confidence": 0.9,
                            "sample_values": [],
                            "status": "AUTO_ACCEPTED",
                        }
                    ],
                    "missing_required_fields": [],
                    "normalisations": [],
                },
            ),
            final,
        ]
    )
    orch, tracer = _orchestrator(driver, _parsed())
    result = orch.run(Intent.INGEST_MAPPING, _Payload(), session_id="S1")

    assert result.outcome == "OK", result.error_code
    assert isinstance(result.final, ColumnMappingProposal)
    assert result.final.entity_type == "MATERIAL"
    # Trace 记录了工具调用步（证明 read/propose/validate 经 registry 执行）。
    tool_steps = [s for s in tracer.traces[0].steps if s["step_kind"] == "TOOL_CALL"]
    assert len(tool_steps) >= 3
    assert {s["detail"] for s in tool_steps} >= {
        "read_uploaded_file_preview",
        "propose_column_mapping",
        "validate_mapping",
    }


# --------------------------------------------------------------------------
# run_ingestion_mapping：Agent final → proposed_mapping（from_agent=True）
# --------------------------------------------------------------------------


def test_run_ingestion_mapping_uses_agent_final() -> None:
    final = scripted_mapping_final(
        entity_type="MATERIAL",
        entity_type_confidence=0.9,
        columns=[
            {"target_field": "material_id", "source_column": "material_id", "confidence": 0.95},
            {"target_field": "name", "source_column": "name", "confidence": 0.9},
            {
                "target_field": "quantity_available",
                "source_column": "quantity_available",
                "confidence": 0.9,
            },
        ],
    )
    driver = ScriptedIngestionDriver(turns=[final])
    result = run_ingestion_mapping(
        parsed=_parsed(),
        upload_id="UP-1",
        adapter=None,  # 桩驱动不触达 adapter；证明不触网
        driver=driver,
    )
    assert result.from_agent is True
    assert result.agent_outcome == "OK"
    assert result.proposed_mapping["entity_type"] == "MATERIAL"
    targets = {fm["target_field"] for fm in result.proposed_mapping["field_mappings"]}
    assert {"material_id", "name", "quantity_available"} <= targets


def test_run_ingestion_mapping_low_confidence_flags_needs_confirmation() -> None:
    """必填字段低置信（<0.85）→ status NEEDS_CONFIRMATION（K-07：不静默接受）。"""
    final = scripted_mapping_final(
        entity_type="MATERIAL",
        entity_type_confidence=0.9,
        columns=[
            {"target_field": "material_id", "source_column": "material_id", "confidence": 0.5},
        ],
    )
    driver = ScriptedIngestionDriver(turns=[final])
    result = run_ingestion_mapping(parsed=_parsed(), upload_id="UP-1", adapter=None, driver=driver)
    fms = result.proposed_mapping["field_mappings"]
    mid = next(fm for fm in fms if fm["target_field"] == "material_id")
    assert mid["status"] == "NEEDS_CONFIRMATION"


# --------------------------------------------------------------------------
# REPLAY 缺录制：回退确定性提议，不得变成 HTTP 500（回归）
# --------------------------------------------------------------------------


@pytest.fixture
def replay_app_client(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """`LLM_MODE=REPLAY` 且 cassette 目录里**没有** INGESTION_AGENT 录制的应用。

    与演示配置（`LLM_MODE=REPLAY`）同形：`BedrockAdapter` 找不到录制就抛 `CassetteMiss`。
    `valid_env` 默认把模式钉在 `STUB`，这里显式改成 `REPLAY`——两者到达回退的方式不同
    （STUB 返回占位文本 → `outcome != OK`；REPLAY 抛异常），回退结果必须一致。
    """
    valid_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'import_replay.db').as_posix()}")
    valid_env.setenv("LLM_MODE", "REPLAY")
    settings = Settings()  # type: ignore[call-arg]
    application = create_app(settings)
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as client:
        client.post(
            "/api/auth/login",
            json={"password": settings.session_shared_password.get_secret_value()},
        )
        yield client


def test_run_ingestion_mapping_falls_back_when_replay_recording_is_missing(
    tmp_path: Path,
) -> None:
    """真实驱动 + REPLAY 缺录制 → 确定性提议（from_agent=False），**不抛异常**。"""
    adapter = BedrockAdapter(mode=LlmMode.REPLAY, cassette=Cassette(directory=tmp_path))

    result = run_ingestion_mapping(parsed=_parsed(), upload_id="UP-1", adapter=adapter)

    assert result.from_agent is False
    assert result.agent_outcome == LLM_UNAVAILABLE_OUTCOME
    # 异常在 `Orchestrator.run` 返回之前抛出，因此这次运行没有可回传的 trace_id。
    assert result.trace_id is None
    # 回退的是**可用**的提案（识别出实体类型与逐字段映射），不是空壳。
    assert result.proposed_mapping["entity_type"]
    assert result.proposed_mapping["field_mappings"]


def test_import_proposal_endpoint_returns_200_when_replay_recording_is_missing(
    replay_app_client: TestClient,
) -> None:
    """`GET /api/imports/{id}/proposal` 在 REPLAY 缺录制时返回 200 + 确定性提议。

    回归断言：该路径曾经因为 `CassetteMiss` 冒到 API 层而返回 500（Import 页第一个
    步骤即失败）。修好后规划员仍能拿到可人工确认的提案（R2.6/R3.1 的入口不被掐断），
    并且 `from_agent=False` 如实标注这不是 Agent 的输出。
    """
    csv_bytes = b"material_id,name,quantity_available,unit\nMAT-1,Steel,100,pcs\n"
    upload = replay_app_client.post(
        "/api/imports/upload",
        files={"file": ("materials.csv", csv_bytes, "text/csv")},
    )
    assert upload.status_code == 200, upload.text
    upload_id = upload.json()["upload_id"]

    response = replay_app_client.get(f"/api/imports/{upload_id}/proposal")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["from_agent"] is False
    assert body["agent_outcome"] == LLM_UNAVAILABLE_OUTCOME
    assert body["proposal"]["field_mappings"]


def test_live_mapping_failure_is_reported_without_deterministic_proposal(
    replay_app_client: TestClient,
) -> None:
    """LIVE failure must not look like a successful mapping proposal or trigger network."""
    class FailingLiveAdapter:
        mode = LlmMode.LIVE
        configured_mode = LlmMode.LIVE

        def invoke(self, _request):
            raise BedrockUnavailableError("simulated gateway failure")

    replay_app_client.app.state.llm_adapter = FailingLiveAdapter()
    csv_bytes = b"material_id,name,quantity_available\nMAT-1,Steel,100\n"
    uploaded = replay_app_client.post(
        "/api/imports/upload", files={"file": ("materials.csv", csv_bytes, "text/csv")}
    )
    assert uploaded.status_code == 200
    response = replay_app_client.get(f"/api/imports/{uploaded.json()['upload_id']}/proposal")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_GENERATION_FAILED"


def test_live_mapping_service_failure_never_falls_back() -> None:
    class FailingLiveAdapter:
        mode = LlmMode.LIVE
        configured_mode = LlmMode.LIVE

        def invoke(self, _request):
            raise BedrockUnavailableError("simulated gateway failure")

    with pytest.raises(IngestionMappingUnavailable):
        run_ingestion_mapping(parsed=_parsed(), upload_id="UP-1", adapter=FailingLiveAdapter())
