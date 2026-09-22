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
from typing import Any

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
    LlmApiStyle,
    LlmDisabledError,
    LlmMode,
    LlmRequest,
    LlmResponse,
    LlmResponseFormatError,
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
    """装配成网关的真实契约（Ollama /api/chat），且静态前缀仍在最前。

    设计契约里 load-bearing 的是**顺序不变量**（静态前缀在前、变化的内容集中在末尾、
    `temperature = 0`）与**逐字节确定**，不是传输字段名。网关形态见
    `tests/unit/test_context_manager.py` 的契约 2/5 与 `adapter.py` 的模块 docstring。
    """
    req = _request()
    body = req.assemble_body(model="test-model")

    assert body["model"] == "test-model"
    # 恰两条：一条携带静态前缀的 system，一条承载四块文本的 user（没有历史消息）。
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    # 静态前缀在最前，且逐字节等于 system 块的**按序**拼接（装配不重排）。
    assert body["messages"][0]["content"] == "\n\n".join(req.system)
    # 变化的内容只在 user 那一条里。
    assert body["messages"][1]["content"] == req.user
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert body["options"]["num_predict"] == req.max_tokens
    # 逐字节确定：同请求两次装配结果相同。
    assert body == req.assemble_body(model="test-model")


def test_assemble_body_defaults_to_the_single_configured_model_constant() -> None:
    """模型名只有一个来源：未显式传参时用 `settings.DEFAULT_BEDROCK_MODEL`。"""
    from app.settings import DEFAULT_BEDROCK_MODEL

    assert _request().assemble_body()["model"] == DEFAULT_BEDROCK_MODEL


# --------------------------------------------------------------------------
# 请求体形态：OLLAMA（默认，向后兼容）与 OPENAI（OpenAI 兼容服务，如 DeepSeek）
# --------------------------------------------------------------------------


def test_assemble_body_openai_style_puts_sampling_params_at_the_top_level() -> None:
    """`OPENAI` 形态：`temperature` / `max_tokens` 在顶层，且**不发** `options`。

    DeepSeek 的 `POST /chat/completions` 是 OpenAI 兼容契约：`options` 与 `num_predict`
    都不是它的字段（最坏情况整条请求 400 → 本适配层会判为不可重试错误并立即降级）。
    因此这里逐条断言「采样参数在顶层」且「Ollama 专有字段一个都不出现」。
    """
    req = _request()
    body = req.assemble_body(model="deepseek-chat", style=LlmApiStyle.OPENAI)

    assert body["model"] == "deepseek-chat"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["stream"] is False
    assert body["temperature"] == req.temperature == 0.0
    assert body["max_tokens"] == req.max_tokens
    assert "options" not in body
    assert "num_predict" not in body
    # 逐字节确定。
    assert body == req.assemble_body(model="deepseek-chat", style=LlmApiStyle.OPENAI)


def test_assemble_body_defaults_to_the_ollama_style() -> None:
    """缺省形态仍是 `OLLAMA`：既有网关的请求体逐字节不变（向后兼容）。"""
    req = _request()
    default_body = req.assemble_body(model="m")

    assert default_body == req.assemble_body(model="m", style=LlmApiStyle.OLLAMA)
    assert default_body["options"] == {"temperature": 0.0, "num_predict": req.max_tokens}
    assert "temperature" not in default_body
    assert "max_tokens" not in default_body


def test_api_style_does_not_change_the_content_hash() -> None:
    """两种形态共享同一份 `messages`，因此 `content_hash` 不变——cassette 跨形态仍有效。

    `content_hash` 只覆盖 agent / system / user / max_tokens / temperature，不含请求体形态；
    换形态（或换 OpenAI 兼容服务）不会让既有录制失效，也不会让进程内缓存错位。
    """
    req = _request()

    assert req.content_hash() == req.content_hash()  # 形态不在 LlmRequest 里，哈希必然一致
    ollama = req.assemble_body(model="m", style=LlmApiStyle.OLLAMA)
    openai = req.assemble_body(model="m", style=LlmApiStyle.OPENAI)
    # 两份请求体的 messages 逐字节相同，只有采样参数的位置不同。
    assert ollama["messages"] == openai["messages"]


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


class _BodyCapturingClient:
    """返回预置成功响应的假 client，并记录最后一次请求体。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.calls = 0
        self.last_body: dict[str, Any] | None = None
        self._payload = payload

    def post(self, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ARG002
        self.calls += 1
        body = kwargs.get("json")
        self.last_body = body if isinstance(body, dict) else None
        return httpx.Response(200, json=self._payload)


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


def test_live_sends_the_configured_model_and_static_prefix_first(tmp_path: Path) -> None:
    """`model` 从构造参数进请求体；请求体形态是网关契约（system 在前，user 在后）。"""
    client = _BodyCapturingClient({"message": {"content": "ok"}})
    adapter = BedrockAdapter(
        mode=LlmMode.LIVE,
        cassette=Cassette(directory=tmp_path),
        gateway_url="http://gateway.internal/api/chat",
        api_key="test-key",
        model="custom-model:latest",
        http_client=client,  # type: ignore[arg-type]
    )
    adapter.invoke(_request())

    assert client.last_body is not None
    assert client.last_body["model"] == "custom-model:latest"
    assert [m["role"] for m in client.last_body["messages"]] == ["system", "user"]


def test_from_settings_passes_bedrock_model_through(tmp_path: Path) -> None:
    """`BEDROCK_MODEL` 配置生效：`from_settings` 把它交给 adapter，进而在请求体里出现。"""
    settings = Settings(  # type: ignore[call-arg]
        database_url=f"sqlite:///{(tmp_path / 's.db').as_posix()}",
        session_shared_password="pw",
        session_secret_key="k" * 32,
        llm_mode="LIVE",
        bedrock_gateway_url="http://gateway.internal/api/chat",
        bedrock_api_key="test-key",  # type: ignore[arg-type]
        bedrock_model="from-env:latest",
    )
    client = _BodyCapturingClient({"message": {"content": "ok"}})
    adapter = BedrockAdapter.from_settings(
        settings, cassette=Cassette(directory=tmp_path), http_client=client  # type: ignore[arg-type]
    )
    adapter.invoke(_request())

    assert client.last_body is not None
    assert client.last_body["model"] == "from-env:latest"


def test_from_settings_plumbs_openai_style_into_the_live_request(tmp_path: Path) -> None:
    """`LLM_API_STYLE=OPENAI` 端到端生效：LIVE 真实请求体是 OpenAI 形态，响应按 OpenAI 形态解析。

    这是 DeepSeek（`POST https://api.deepseek.com/chat/completions`）的实际契约：请求体顶层
    带 `temperature` / `max_tokens`、不带 `options`；响应是
    `choices[0].message.content` + `usage.prompt_tokens/completion_tokens`。
    """
    settings = Settings(  # type: ignore[call-arg]
        database_url=f"sqlite:///{(tmp_path / 'deepseek.db').as_posix()}",
        session_shared_password="pw",
        session_secret_key="k" * 32,
        llm_mode="LIVE",
        bedrock_gateway_url="https://api.deepseek.com/chat/completions",
        bedrock_api_key="test-key",  # type: ignore[arg-type]
        bedrock_model="deepseek-chat",
        llm_api_style="OPENAI",
    )
    client = _BodyCapturingClient(
        {
            "choices": [{"message": {"role": "assistant", "content": "Real DeepSeek-style reply"}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120},
        }
    )
    adapter = BedrockAdapter.from_settings(
        settings, cassette=Cassette(directory=tmp_path), http_client=client  # type: ignore[arg-type]
    )

    response = adapter.invoke(_request())

    assert response.content == "Real DeepSeek-style reply"
    assert response.usage.input_tokens == 900
    assert response.usage.output_tokens == 120
    assert client.last_body is not None
    assert client.last_body["model"] == "deepseek-chat"
    assert client.last_body["temperature"] == 0.0
    assert "max_tokens" in client.last_body
    assert "options" not in client.last_body


# --------------------------------------------------------------------------
# 响应解析：认得的形态都不能被解析成空文本 / 零用量
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "content", "input_tokens", "output_tokens"),
    [
        # Ollama /api/chat —— 本项目网关的真实形态
        (
            {
                "message": {"role": "assistant", "content": "A"},
                "prompt_eval_count": 11,
                "eval_count": 7,
            },
            "A",
            11,
            7,
        ),
        # Ollama /api/generate
        ({"response": "B", "prompt_eval_count": 2, "eval_count": 3}, "B", 2, 3),
        # 简化 JSON 网关形态（adapter 早期契约，也是文档里写的形态）
        (
            {"content": "C", "usage": {"input_tokens": 12, "output_tokens": 3}},
            "C",
            12,
            3,
        ),
        # Anthropic / Bedrock：content 是文本块列表
        (
            {
                "content": [{"type": "text", "text": "D"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            "D",
            1,
            2,
        ),
        # OpenAI Chat Completions
        (
            {
                "choices": [{"message": {"content": "E"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5},
            },
            "E",
            4,
            5,
        ),
        # OpenAI legacy completions / Responses
        ({"choices": [{"text": "F"}]}, "F", 0, 0),
        ({"output_text": "G"}, "G", 0, 0),
    ],
)
def test_parse_response_reads_every_known_gateway_shape(
    payload: dict[str, Any], content: str, input_tokens: int, output_tokens: int
) -> None:
    """七种形态都解析出正文与用量——「有效响应被静默解析成空文本」是不可接受的。"""
    parsed = BedrockAdapter._parse_response(payload)
    assert parsed.content == content
    assert parsed.usage.input_tokens == input_tokens
    assert parsed.usage.output_tokens == output_tokens


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "shape"},
        {"detail": "内部错误"},
        {"message": {"role": "assistant"}},  # 有 message 但没有 content
        {"message": {"content": ""}},  # 正文为空
        {"message": {"content": "   \n  "}},  # 正文只有空白
    ],
)
def test_parse_response_raises_instead_of_returning_empty_text(
    payload: dict[str, Any],
) -> None:
    """认不出正文一律抛错——空正文会被数值护栏判成「一致」，从而发布空解释。"""
    with pytest.raises(LlmResponseFormatError):
        BedrockAdapter._parse_response(payload)


def test_live_unparsable_response_degrades_and_reports_the_cause(
    tmp_path: Path, audit_engine: Engine
) -> None:
    """网关 200 但正文不可解析 → 抛 LlmResponseFormatError、切降级、原因可定位。"""
    _ = audit_engine  # 降级写 DEGRADED_MODE_SWITCH 审计，需可用的审计库
    client = _BodyCapturingClient({"detail": "upstream model unavailable"})
    adapter = _live_adapter(client, tmp_path)

    with pytest.raises(LlmResponseFormatError) as excinfo:
        adapter.invoke(_request())

    # 报错信息带上网关给的线索，且已切降级（后续调用直接拒绝）。
    assert "upstream model unavailable" in str(excinfo.value)
    assert client.calls == 1  # 不可重试：不浪费额度重试
    assert adapter.mode is LlmMode.DISABLED
    with pytest.raises(LlmDisabledError):
        adapter.invoke(_request())


# --------------------------------------------------------------------------
# LIVE → cassette 录制（cassette.py 的约定：LIVE 录、REPLAY 放）
# --------------------------------------------------------------------------


def test_live_success_is_recorded_and_replayable_afterwards(tmp_path: Path) -> None:
    """LIVE 成功响应写进 cassette，随后 REPLAY 能离线回放同一条（R26.5）。"""
    client = _BodyCapturingClient(
        {
            "message": {"content": "录制内容"},
            "prompt_eval_count": 30,
            "eval_count": 9,
        }
    )
    live = _live_adapter(client, tmp_path)
    req = _request()
    assert live.invoke(req).content == "录制内容"

    # 录制文件按请求哈希命名（cassette 的约定），因此任何机器上都定位到同一个文件。
    assert (tmp_path / f"{req.content_hash()}.json").exists()

    replay = BedrockAdapter(mode=LlmMode.REPLAY, cassette=Cassette(directory=tmp_path))
    replayed = replay.invoke(req)
    assert replayed.content == "录制内容"
    assert replayed.usage.input_tokens == 30
    assert replayed.usage.output_tokens == 9


def test_live_cassette_write_failure_does_not_fail_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cassette 目录不可写（部署时 `ProtectSystem=strict`）时，调用仍然成功。"""
    cassette = Cassette(directory=tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(cassette, "record", _boom)
    adapter = BedrockAdapter(
        mode=LlmMode.LIVE,
        cassette=cassette,
        gateway_url="http://gateway.internal/api/chat",
        http_client=_BodyCapturingClient({"message": {"content": "ok"}}),  # type: ignore[arg-type]
    )
    assert adapter.invoke(_request()).content == "ok"
