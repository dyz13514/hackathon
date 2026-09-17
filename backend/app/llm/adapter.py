"""`Bedrock_Adapter`：全仓库唯一调用 Bedrock 的地方（任务 5.3，R21.10）。

design.md §2.1 把这里定成「唯一出口」。这不是组织上的偏好，而是四条纪律的共同支点：

- `DETERMINISTIC_ONLY` 旁路只需在这一层生效一次；
- token / USD 记账不会漏账（每次真实调用都从这一个函数出去）；
- cassette 录制回放能覆盖所有调用；
- 网关凭证只经一处读取（`tests/structure/test_layering.py` 静态断言网关 URL 与
  `BEDROCK_GATEWAY_URL` 配置项只出现在本文件）。

## `invoke` 的固定次序（design.md §2.1 代码草图）

1. `DISABLED` → 抛 `LlmDisabledError`。调用方**必须**有模板回退（§2.6 的降级模式）。
2. 算 `content_hash`。
3. 内容哈希缓存命中 → 返回 `hit.with_zero_cost()`（R25.7：同输入第二次零支出）。
4. `REPLAY` / `STUB` → 从 cassette 按哈希取；未录制则按模式分别处理（见 `cassette.py`）。
5. 只有 `LIVE` 才装配请求体、`_post_with_retry` 真实调用网关。
6. 真实调用后 `budget.record(usage)`、写缓存、`_maybe_degrade`。

## 静态前缀优先的装配（这条不变量的价值）

`assemble_body` 把 `system` 数组分成两个块：提示词段 1–3 + 段 5 在前，工具 schema
段 4 在后；一切变化的内容都在 `messages` 里，`temperature = 0`。design.md §2.1 说明
这**与 prompt caching 无关**（caching 已移出范围）——保留它是因为前缀是常量使
`Context_Manager.assemble_messages` 成为纯函数，其输出可被逐字节断言。本模块的
`assemble_body` 是同一个不变量在出口侧的落点：给定同一个 `LlmRequest`，请求体逐字节
确定，因此 `content_hash` 稳定，缓存与 cassette 才能按哈希命中。

## 预算记账是一条注入的接缝

`budget.record(...)` 里的 `Token_Budget_Manager` 归任务 5.4。本任务只把它作为一个
可注入的回调接缝留出来（`BudgetRecorder` 协议 + 默认 no-op），5.4 落地时传入真实实现
即可，无需改动 adapter。同理，`gate()`（调用前的预算闸门）也归 5.4——本层不做闸门判定，
只在真实调用后记账。

## 刻意的减法（全部移出范围）

启动探针、`prompt_caching_available`、`MINIMAL_PREFIX`、缓存读写分项记账——tasks.md 5.3
明确点名不实现。本模块因此没有任何「探测缓存是否可用」或「按最小前缀重发」的分支。
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.db import audit
from app.llm.pricing import PRICE

if TYPE_CHECKING:
    from app.llm.cassette import Cassette
    from app.settings import Settings


# --------------------------------------------------------------------------
# 模式
# --------------------------------------------------------------------------


class LlmMode(str, Enum):
    """LLM 调用模式（design.md §2.1）。

    只有 `LIVE` 触达网络。`REPLAY`（本地与 CI 默认）与 `STUB`（未录制用例）都从
    cassette 取，零成本零网络。`DISABLED` 是 `DETERMINISTIC_ONLY` 降级态。
    """

    LIVE = "LIVE"
    REPLAY = "REPLAY"
    STUB = "STUB"
    DISABLED = "DISABLED"


class LlmDisabledError(RuntimeError):
    """`DISABLED` 模式下有人调用了 `invoke`。

    这不是「调用失败」而是「这条路当前不存在」：调用方应当 `except LlmDisabledError`
    并走模板回退（design.md §2.6，`test_degraded_mode.py` 遍历全部调用点断言存在回退）。
    """

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"LLM 调用已禁用（{reason}）：调用方须走确定性模板回退。")


class BedrockUnavailableError(RuntimeError):
    """真实调用连续失败或遇到不可重试错误，且已切入 `DETERMINISTIC_ONLY`。

    只在 `LIVE` 模式下可能抛出。抛出时 adapter 已把 `mode` 置为 `DISABLED` 并写了
    `DEGRADED_MODE_SWITCH` 审计——因此这是「本次失败」的信号，后续调用会直接以
    `LlmDisabledError` 拒绝，调用方两种异常都应回退到模板。
    """


# --------------------------------------------------------------------------
# 值对象：请求 / 响应 / 用量
# --------------------------------------------------------------------------


class LlmUsage(BaseModel):
    """一次调用的 token 用量。`estimated_usd` 由单价表算出，随用量一起冻结。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @property
    def estimated_usd(self) -> str:
        """本次调用的美元估算，字符串形式（`Decimal` 不便直接进 JSON）。"""
        return str(PRICE.cost_of(self))

    @classmethod
    def zero(cls) -> LlmUsage:
        """零用量：缓存命中时的 `with_zero_cost()` 用它。"""
        return cls(input_tokens=0, output_tokens=0)


class LlmRequest(BaseModel):
    """一次 LLM 请求的全部可变输入（design.md §2.1、§2.2）。

    `system` 是**静态前缀**（两个块：提示词 + 工具 schema），在同一 Agent 各轮逐字节
    相同；`user` 是唯一变化的部分。`content_hash()` 覆盖全部字段，因此同输入必得同哈希，
    这是缓存与 cassette 按哈希命中的前提。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    system: tuple[str, ...] = Field(min_length=1)
    user: str
    max_tokens: int = Field(default=800, gt=0)
    temperature: float = Field(default=0.0, ge=0.0)

    def content_hash(self) -> str:
        """请求内容的稳定哈希（R25.7 的缓存键）。

        用规范化 JSON（`sort_keys` + 紧凑分隔符）再取 sha256：字段顺序、空白差异都不
        影响结果，因此「同一内容」在任何进程、任何平台上都得到同一个键。哈希覆盖
        `agent` 是有意的——不同 Agent 的同一段 user 文本是不同的请求（前缀不同），
        不该共享缓存条目。
        """
        canonical = json.dumps(
            {
                "agent": self.agent,
                "system": list(self.system),
                "user": self.user,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def assemble_body(self) -> dict[str, Any]:
        """静态前缀优先的请求体（design.md §2.1 的 JSON 草图）。

        `system` 数组：`system[0]` 提示词段、`system[1..]` 工具 schema 段——顺序即
        `LlmRequest.system` 的顺序，装配不重排。`messages` 恰好一条 `role="user"`，
        变化内容全在这里。给定同一个 `LlmRequest`，返回逐字节确定。
        """
        return {
            "system": [{"type": "text", "text": block} for block in self.system],
            "messages": [{"role": "user", "content": self.user}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


class LlmResponse(BaseModel):
    """一次 LLM 响应：文本 + 用量。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    usage: LlmUsage

    def with_zero_cost(self) -> LlmResponse:
        """内容不变、用量清零的副本（缓存命中时返回它，R25.7）。

        「第二次零成本」在算术上就是这一步：`usage` 归零后 `PRICE.cost_of` 得
        `Decimal("0")`，记账台账因此不再增加。内容逐字段等于首次，调用方无从区分。
        """
        return self.model_copy(update={"usage": LlmUsage.zero()})


# --------------------------------------------------------------------------
# 注入的接缝：内容哈希缓存 与 预算记账
# --------------------------------------------------------------------------


class ResponseCache(Protocol):
    """内容哈希缓存（`llm_cache` 表，R25.7）。adapter 只依赖这两个方法。"""

    def get(self, content_hash: str) -> LlmResponse | None: ...

    def put(self, content_hash: str, response: LlmResponse) -> None: ...


class BudgetRecorder(Protocol):
    """预算记账接缝。真实实现是 `Token_Budget_Manager.record`（任务 5.4）。

    adapter 只在**真实调用**后调用它一次。5.4 尚未落地，因此默认实现是 no-op
    （见 `_NullBudget`）——这让本任务可独立完成并测试，5.4 落地时替换实例即可。
    """

    def record(self, usage: LlmUsage) -> None: ...


class InMemoryResponseCache:
    """进程内内容哈希缓存的默认实现。

    真实的 `llm_cache` 表持久化归 5.4/5.12 的记账链路；本任务的缓存语义（同输入
    第二次零成本）用内存实现即可完整验证，且不给内核测试引入 DB 依赖。
    """

    def __init__(self) -> None:
        self._store: dict[str, LlmResponse] = {}

    def get(self, content_hash: str) -> LlmResponse | None:
        return self._store.get(content_hash)

    def put(self, content_hash: str, response: LlmResponse) -> None:
        self._store[content_hash] = response


class _NullBudget:
    """no-op 记账。5.4 的 `Token_Budget_Manager` 落地前的默认接缝。"""

    def record(self, usage: LlmUsage) -> None:  # noqa: ARG002 - 接缝占位
        return None


# --------------------------------------------------------------------------
# 重试与降级参数
# --------------------------------------------------------------------------

#: 单次 HTTP 请求超时（秒）。R27.12。
_REQUEST_TIMEOUT_S: Final = 30.0

#: 重试退避间隔（秒）。最多 2 次重试 → 首次失败等 1s，再失败等 4s（design.md §2.1）。
_RETRY_BACKOFF_S: Final = (1.0, 4.0)

#: 连续失败到此次数即切 `DETERMINISTIC_ONLY`（R25.8）。一次 invoke 内最多 3 次尝试
#: （1 次 + 2 次重试），全败即达阈值。
_DEGRADE_AFTER_CONSECUTIVE_FAILURES: Final = 3

#: 可重试的 HTTP 状态码：网关 5xx 与 429 是「稍后可能成功」的抖动。4xx（除 429）是
#: 请求本身的问题，重试无意义 → 直接判为不可重试，立即降级。
_RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})


class BedrockAdapter:
    """LLM 出口。`invoke` 的次序严格照 design.md §2.1。"""

    def __init__(
        self,
        *,
        mode: LlmMode,
        cassette: Cassette,
        gateway_url: str | None = None,
        api_key: str | None = None,
        cache: ResponseCache | None = None,
        budget: BudgetRecorder | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.mode = mode
        self._cassette = cassette
        # 网关 URL 与密钥只在 LIVE 下用到。它们从这里进入，不在别处读 —— 静态扫描
        # 断言 BEDROCK_GATEWAY_URL 只被本文件引用（见 from_settings）。
        self._gateway_url = gateway_url
        self._api_key = api_key
        self._cache: ResponseCache = cache if cache is not None else InMemoryResponseCache()
        self._budget: BudgetRecorder = budget if budget is not None else _NullBudget()
        self._http_client = http_client
        self._consecutive_failures = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        cassette: Cassette,
        cache: ResponseCache | None = None,
        budget: BudgetRecorder | None = None,
        http_client: httpx.Client | None = None,
    ) -> BedrockAdapter:
        """按配置装配。网关地址与密钥在此从 `settings` 读入——本仓库唯一的读取点。"""
        api_key = (
            settings.bedrock_api_key.get_secret_value()
            if settings.bedrock_api_key is not None
            else None
        )
        return cls(
            mode=LlmMode(settings.llm_mode),
            cassette=cassette,
            gateway_url=settings.bedrock_gateway_url,
            api_key=api_key,
            cache=cache,
            budget=budget,
            http_client=http_client,
        )

    def invoke(self, req: LlmRequest) -> LlmResponse:
        """执行一次 LLM 调用，次序见模块与本类 docstring。"""
        if self.mode is LlmMode.DISABLED:
            raise LlmDisabledError(reason="DETERMINISTIC_ONLY")

        key = req.content_hash()

        hit = self._cache.get(key)
        if hit is not None:
            # R25.7：同输入第二次零支出。不记账、不写 cassette、不触网。
            return hit.with_zero_cost()

        if self.mode in (LlmMode.REPLAY, LlmMode.STUB):
            response = self._cassette.get_or_fail(key, mode=self.mode, request=req)
            self._cache.put(key, response)
            return response

        # 只有 LIVE 到这里。
        response = self._invoke_live(req)
        self._budget.record(response.usage)
        self._cache.put(key, response)
        return response

    # -- LIVE 专用 --------------------------------------------------------

    def _invoke_live(self, req: LlmRequest) -> LlmResponse:
        """真实调用网关，含超时、重试与降级。仅 `LIVE` 模式下走到这里。"""
        body = req.assemble_body()
        try:
            payload = self._post_with_retry(body)
        except _RetryExhaustedError as exc:
            self._degrade(reason=exc.reason, trace_id=None)
            raise BedrockUnavailableError(exc.reason) from exc
        self._consecutive_failures = 0
        return self._parse_response(payload)

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST 到网关，30s 超时 + 最多 2 次重试（1s / 4s）。

        连续 3 次尝试全败（1 + 2 次重试）或遇不可重试错误 → 抛 `_RetryExhaustedError`，
        由 `_invoke_live` 转成降级。退避是固定间隔而非指数抖动：演示规模下调用稀疏，
        固定值可复现、可在测试中断言。
        """
        if self._gateway_url is None:
            raise _RetryExhaustedError(reason="LIVE 模式缺少 BEDROCK_GATEWAY_URL")

        headers = {"content-type": "application/json"}
        if self._api_key is not None:
            headers["authorization"] = f"Bearer {self._api_key}"

        last_reason = "未知错误"
        # 尝试次数 = 1 次首发 + len(_RETRY_BACKOFF_S) 次重试。
        for attempt in range(len(_RETRY_BACKOFF_S) + 1):
            try:
                response = self._do_post(body, headers)
            except httpx.HTTPError as exc:
                # 网络层错误（超时、连接失败）一律可重试。
                self._consecutive_failures += 1
                last_reason = f"网关请求异常：{type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return _json_of(response)
                self._consecutive_failures += 1
                last_reason = f"网关返回 HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE_STATUS:
                    # 不可重试错误：立即停止，交由调用方降级。
                    raise _RetryExhaustedError(reason=last_reason)

            if self._consecutive_failures >= _DEGRADE_AFTER_CONSECUTIVE_FAILURES:
                raise _RetryExhaustedError(reason=last_reason)

            if attempt < len(_RETRY_BACKOFF_S):
                time.sleep(_RETRY_BACKOFF_S[attempt])

        raise _RetryExhaustedError(reason=last_reason)

    def _do_post(self, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        """发一次 POST。抽出来是为了让测试可注入 `http_client`。"""
        assert self._gateway_url is not None  # _post_with_retry 已校验
        if self._http_client is not None:
            return self._http_client.post(self._gateway_url, json=body, headers=headers)
        return httpx.post(
            self._gateway_url, json=body, headers=headers, timeout=_REQUEST_TIMEOUT_S
        )

    def _degrade(self, *, reason: str, trace_id: str | None) -> None:
        """切入 `DETERMINISTIC_ONLY` 并写 `DEGRADED_MODE_SWITCH` 审计（R25.8）。"""
        self.mode = LlmMode.DISABLED
        audit.append(
            event_category="DEGRADED_MODE_SWITCH",
            event_type="ENTER_DETERMINISTIC_ONLY",
            actor="SYSTEM",
            payload={"reason": reason, "trigger": "BEDROCK_FAILURE"},
            trace_id=trace_id,
        )

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> LlmResponse:
        """把网关 JSON 解析成 `LlmResponse`。

        字段名按网关约定：`content` 为文本，`usage.input_tokens` / `output_tokens`
        为用量。缺字段按 0 计（保守：宁可低估用量也不让解析崩掉，实际用量以网关账单
        为准，本地估算只用于告警阈值）。
        """
        usage_raw = payload.get("usage") or {}
        return LlmResponse(
            content=str(payload.get("content", "")),
            usage=LlmUsage(
                input_tokens=int(usage_raw.get("input_tokens", 0)),
                output_tokens=int(usage_raw.get("output_tokens", 0)),
            ),
        )


class _RetryExhaustedError(RuntimeError):
    """内部信号：重试用尽或遇不可重试错误。不外泄，由 `_invoke_live` 转成降级。"""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _json_of(response: httpx.Response) -> dict[str, Any]:
    """把响应体解析成 dict；非 JSON 或非对象一律视为不可重试的坏响应。"""
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise _RetryExhaustedError(reason="网关返回非 JSON 响应") from exc
    if not isinstance(data, dict):
        raise _RetryExhaustedError(reason="网关返回的 JSON 不是对象")
    return data
