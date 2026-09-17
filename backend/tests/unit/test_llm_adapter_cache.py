"""`Bedrock_Adapter` 与 cassette 录制回放（任务 5.3）。

核心断言（**非可选**，承接原属性 30，design.md Correctness Properties 表第 30 行）：
**同一 `content_hash` 两次调用，第二次 `usage` 为零且内容与第一次逐字段相同（R25.7）。**

其余用例覆盖 design.md §2.1 代码草图的其余分支：`DISABLED` 抛 `LlmDisabledError`、
`STUB` 未录制返回占位、`REPLAY` 未录制抛 `CassetteMiss`、静态前缀优先的装配逐字节确定、
`content_hash` 对内容稳定、`_post_with_retry` 连续失败切降级。

测试全程不触网：`STUB` / `REPLAY` 从 cassette 取，`LIVE` 用注入的假 `httpx.Client`。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine

from app.db import audit
from app.db.models import Base
from app.db.session import create_db_engine
from app.llm.adapter import (
    BedrockAdapter,
    BedrockUnavailableError,
    InMemoryResponseCache,
    LlmDisabledError,
    LlmMode,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)
from app.llm.cassette import Cassette, CassetteMiss
from app.llm.pricing import PRICE
from app.settings import Settings


@pytest.fixture
def audit_engine(tmp_path: Path) -> Iterator[Engine]:
    """文件库 + 建表 + 指向审计引擎：让 `_degrade` 的 DEGRADED_MODE_SWITCH 能落盘。

    降级路径写审计（R25.8），审计走独立引擎（`db/audit.py`）。测试里把它指向一个
    真实的临时库文件——`:memory:` 每个连接是独立库，审计的独立连接会看不到表。
    """
    settings = Settings(  # type: ignore[call-arg]
        database_url=f"sqlite:///{(tmp_path / 'audit.db').as_posix()}",
        session_shared_password="pw",
        session_secret_key="k" * 32,
    )
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    audit.set_audit_engine(engine)
    yield engine
    audit.set_audit_engine(None)
    engine.dispose()


def _request(user: str = "解释这个计划为什么把 JOB-004 排在最后。") -> LlmRequest:
    """一个固定的请求：静态前缀两块 + 变化的 user 文本。"""
    return LlmRequest(
        agent="PLANNING_AGENT",
        system=("<系统提示词 段1-3+段5>", "<工具 schema 段4>"),
        user=user,
    )


def _record(cassette: Cassette, req: LlmRequest, *, content: str) -> LlmResponse:
    """把一条响应录进 cassette，返回该响应。"""
    response = LlmResponse(
        content=content, usage=LlmUsage(input_tokens=1200, output_tokens=300)
    )
    cassette.record(req.content_hash(), req, response)
    return response


# --------------------------------------------------------------------------
# 核心：同内容第二次零成本（承接属性 30）
# --------------------------------------------------------------------------


def test_second_call_same_content_hash_is_zero_cost(tmp_path: Path) -> None:
    """同一 `content_hash` 两次调用：第二次 usage 为零，内容逐字段相同（R25.7）。"""
    cassette = Cassette(directory=tmp_path)
    req = _request()
    recorded = _record(cassette, req, content="因为 CNC-01 在该时段被更高优先级订单占满。")

    cache = InMemoryResponseCache()
    adapter = BedrockAdapter(mode=LlmMode.REPLAY, cassette=cassette, cache=cache)

    first = adapter.invoke(req)
    second = adapter.invoke(req)

    # 第一次来自 cassette，用量非零。
    assert first.content == recorded.content
    assert first.usage.input_tokens == 1200
    assert first.usage.output_tokens == 300

    # 第二次命中内容哈希缓存：用量清零，成本恰为 0。
    assert second.usage.input_tokens == 0
    assert second.usage.output_tokens == 0
    assert PRICE.cost_of(second.usage) == PRICE.cost_of(LlmUsage.zero())
    assert str(PRICE.cost_of(second.usage)) == "0.00"

    # 内容逐字段相同——调用方无从区分两次。
    assert second.content == first.content


def test_cache_hit_does_not_touch_cassette_again(tmp_path: Path) -> None:
    """缓存命中后即便录制被删除，第二次调用仍成功（证明未再读 cassette）。"""
    cassette = Cassette(directory=tmp_path)
    req = _request()
    _record(cassette, req, content="内容 A")

    adapter = BedrockAdapter(mode=LlmMode.REPLAY, cassette=cassette)
    first = adapter.invoke(req)

    # 删掉录制文件；若第二次还去读 cassette 就会 CassetteMiss。
    (tmp_path / f"{req.content_hash()}.json").unlink()

    second = adapter.invoke(req)
    assert second.content == first.content
    assert second.usage.input_tokens == 0


# --------------------------------------------------------------------------
# content_hash 与静态前缀装配
# --------------------------------------------------------------------------


def test_content_hash_stable_for_same_content() -> None:
    """同内容 → 同哈希；user 文本变一个字 → 哈希变。"""
    assert _request().content_hash() == _request().content_hash()
    assert _request("A").content_hash() != _request("B").content_hash()


def test_content_hash_includes_agent() -> None:
    """不同 Agent 的同一段 user 是不同请求（前缀不同），不共享缓存条目。"""
    base = _request()
    other = base.model_copy(update={"agent": "INGESTION_AGENT"})
    assert base.content_hash() != other.content_hash()


def test_assemble_body_static_prefix_first_and_deterministic() -> None:
    """静态前缀优先：system 两块按序在前，messages 恰 1 条 user，temperature=0。"""
    body = _request().assemble_body()
    assert [block["text"] for block in body["system"]] == [
        "<系统提示词 段1-3+段5>",
        "<工具 schema 段4>",
    ]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["temperature"] == 0
    # 逐字节确定：同请求两次装配结果相同。
    assert body == _request().assemble_body()


# --------------------------------------------------------------------------
# 模式分支
# --------------------------------------------------------------------------


def test_disabled_mode_raises(tmp_path: Path) -> None:
    """DISABLED 抛 LlmDisabledError（调用方须走模板回退）。"""
    adapter = BedrockAdapter(mode=LlmMode.DISABLED, cassette=Cassette(directory=tmp_path))
    with pytest.raises(LlmDisabledError):
        adapter.invoke(_request())


def test_stub_mode_returns_placeholder_when_unrecorded(tmp_path: Path) -> None:
    """STUB 未录制 → 返回零用量的占位响应，不抛错、不触网。"""
    adapter = BedrockAdapter(mode=LlmMode.STUB, cassette=Cassette(directory=tmp_path))
    response = adapter.invoke(_request())
    assert "[STUB]" in response.content
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0


def test_replay_mode_raises_when_unrecorded(tmp_path: Path) -> None:
    """REPLAY 未录制 → CassetteMiss（缺口必须被人处理，不静默）。"""
    adapter = BedrockAdapter(mode=LlmMode.REPLAY, cassette=Cassette(directory=tmp_path))
    with pytest.raises(CassetteMiss):
        adapter.invoke(_request())


def test_replay_mode_returns_recorded_response(tmp_path: Path) -> None:
    """REPLAY 命中 → 返回录制内容与用量。"""
    cassette = Cassette(directory=tmp_path)
    req = _request()
    _record(cassette, req, content="录制内容")
    adapter = BedrockAdapter(mode=LlmMode.REPLAY, cassette=cassette)
    response = adapter.invoke(req)
    assert response.content == "录制内容"


# --------------------------------------------------------------------------
# LIVE 重试与降级（不触真实网络，注入假 client）
# --------------------------------------------------------------------------


class _AlwaysFailClient:
    """总是返回 500 的假 client：模拟网关抖动。"""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, url: str, **_: object) -> httpx.Response:  # noqa: ARG002
        self.calls += 1
        return httpx.Response(500, json={"error": "boom"})


class _OkClient:
    """返回一个成功响应的假 client。"""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, url: str, **_: object) -> httpx.Response:  # noqa: ARG002
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "content": "真实响应",
                "usage": {"input_tokens": 900, "output_tokens": 120},
            },
        )


def _live_adapter(client: object, tmp_path: Path) -> BedrockAdapter:
    return BedrockAdapter(
        mode=LlmMode.LIVE,
        cassette=Cassette(directory=tmp_path),
        gateway_url="http://gateway.internal/invoke-model",
        api_key="test-key",
        http_client=client,  # type: ignore[arg-type]
    )


def test_live_success_records_usage(tmp_path: Path) -> None:
    """LIVE 成功：解析出内容与用量，一次调用不重试。成功路径不写审计。"""
    client = _OkClient()
    adapter = _live_adapter(client, tmp_path)
    response = adapter.invoke(_request())
    assert response.content == "真实响应"
    assert response.usage.input_tokens == 900
    assert client.calls == 1


def test_live_three_failures_degrade_to_disabled(
    tmp_path: Path, audit_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续 3 次失败（1 + 2 重试）→ 切 DISABLED 并抛 BedrockUnavailableError（R25.8）。"""
    _ = audit_engine  # 降级写 DEGRADED_MODE_SWITCH 审计，需可用的审计库
    # 别在测试里真的 sleep 1s/4s。
    monkeypatch.setattr("app.llm.adapter.time.sleep", lambda _s: None)

    client = _AlwaysFailClient()
    adapter = _live_adapter(client, tmp_path)

    with pytest.raises(BedrockUnavailableError):
        adapter.invoke(_request())

    assert client.calls == 3  # 首发 + 2 次重试
    assert adapter.mode is LlmMode.DISABLED

    # 降级后再调用直接以 LlmDisabledError 拒绝。
    with pytest.raises(LlmDisabledError):
        adapter.invoke(_request())
