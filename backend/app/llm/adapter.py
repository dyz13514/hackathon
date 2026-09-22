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

`assemble_body` 按网关实际接受的 **Ollama `/api/chat`** 形态装配请求体：`system` 块按序
用空行拼成**第一条 `role="system"` 消息**（提示词段 + 工具 schema 段都在这里，静态前缀
因此在最前），`user` 作为**唯一一条 `role="user"` 消息**在后，采样参数进 `options`、
`temperature = 0`。变化的内容只发生在最后一条消息里。

该形态由 `LLM_API_STYLE` 选择（`LlmApiStyle`）：`OLLAMA`（默认，向后兼容）把
`temperature` / `num_predict` 放进 `options`；`OPENAI` 供 OpenAI 兼容服务使用（例如
DeepSeek 的 `POST /chat/completions`）——`messages` 逐字节相同，只把采样参数升到顶层
`temperature` / `max_tokens` 并去掉 `options`（`num_predict` 是 Ollama 专有字段）。
两种形态因此共享同一条 **messages 不变量**，`content_hash` 也不含形态，cassette 与缓存
跨形态同样有效。

这里真正 load-bearing 的是**顺序不变量**（静态前缀在前、变化内容集中在末尾、
`temperature = 0`），而不是具体的传输字段名：`Context_Manager.assemble_messages` 是纯函数，
其输出可被逐字节断言；请求体也因此逐字节确定，`content_hash` 稳定，缓存与 cassette 才能
按哈希命中。

> design.md §2.1 的 JSON 草图记的是早期设想的 `system` 数组形态。团队实际接入的是
> Ollama 兼容网关（`POST /api/chat`，模型 `sonnet4.5:latest`），因此**实现以网关的真实
> 契约**为准；`tests/unit/test_llm_adapter_cache.py` 与 `test_context_manager.py` 按该契约
> 断言。响应解析见 `_parse_response`：认得的不止一种形态，且认不出来会显式报错而不是
> 悄悄返回空文本。

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
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.db import audit
from app.llm.pricing import PRICE
from app.logging_config import log_event
from app.settings import DEFAULT_BEDROCK_MODEL, Settings

if TYPE_CHECKING:
    from app.llm.cassette import Cassette

logger = logging.getLogger(__name__)


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


class LlmApiStyle(str, Enum):
    """请求体的接口形态。**不改变**响应解析、认证与重试/降级逻辑，只决定采样参数放在哪。

    - `OLLAMA`（默认，向后兼容）：团队既有网关的 `POST /api/chat` 契约——`temperature` 与
      `num_predict` 放在 `options` 里。
    - `OPENAI`：OpenAI 兼容服务（例如 DeepSeek 的 `POST /chat/completions`）——同样的
      `model` / `messages` / `stream`，但 `temperature` 与 `max_tokens` 在**顶层**，且不发
      `options`（`num_predict` 是 Ollama 专有字段，对方不认识它，最坏情况会整条请求 400）。

    两种形态的 `messages` 完全一致（静态前缀在前、唯一一条 user 在后），因此 `content_hash`
    不受本枚举影响——cassette 与缓存跨形态仍然有效（`content_hash` 只覆盖 agent/system/
    user/max_tokens/temperature）。
    """

    OLLAMA = "OLLAMA"
    OPENAI = "OPENAI"


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


class LlmResponseFormatError(BedrockUnavailableError):
    """网关回了 200，但响应体里找不到可用的正文。

    为什么是 `BedrockUnavailableError` 的子类而不是一个新的并列异常：调用方
    （`explanation` / 各 Agent 驱动）已经 catch 了这个类型并各自实现模板回退，
    这是**同一个处置**——「本次 LLM 输出不可用，走确定性回退」。做成子类，那些
    回退一律自动生效，不存在「新异常类型没人接住 → 500」的缺口。

    为什么必须抛而不是返回空串（R25.8「返回不可重试错误 → 切 `DETERMINISTIC_ONLY`」）：
    空正文在这个项目里是**静默的错**——`guard_explanation_numeric_consistency("")` 找不到
    任何数字，因此判定一致，计划解释会以 `numeric_check = PASS` 发布一段空文本。宁可
    回退到确定性模板，也不要把「解析不出来」伪装成「模型说了空话」。

    与 `_post_with_retry` 遇到不可重试 HTTP 状态一样，抛出前已 `_degrade()`，因此基类
    「抛出时已切 `DETERMINISTIC_ONLY`」的承诺对子类同样成立。
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

    def assemble_body(
        self,
        model: str = DEFAULT_BEDROCK_MODEL,
        *,
        style: LlmApiStyle = LlmApiStyle.OLLAMA,
    ) -> dict[str, Any]:
        """按 `style` 装配请求体（默认 `OLLAMA`，与既有网关逐字节兼容）。

        两种形态共享三条不变量：`system` 块**按原序**拼成第一条 `role="system"` 消息
        （静态前缀因此永远在最前）；`user` 是唯一一条 `role="user"` 消息且在最后；
        `stream=False`，采样参数由本对象给出（`temperature` 默认 0，确定性要求）。
        唯一的差别是采样参数的位置：

        - `OLLAMA`：`options.{temperature,num_predict}`（本项目网关的契约）；
        - `OPENAI`：顶层 `temperature` / `max_tokens`，**不带** `options`（OpenAI 兼容服务
          如 DeepSeek 的 `POST /chat/completions`；`num_predict` 对方不认识）。

        给定同一个 `LlmRequest`、`model` 与 `style`，返回逐字节确定。

        注意 `user` 这里承载的是 `Context_Manager` 拼好的四块文本；设计契约中的「恰好一条
        user 消息」说的是它——`role="system"` 只是同一份静态前缀的传输载体，不是历史消息。
        """
        system_text = "\n\n".join(self.system)
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": self.user},
            ],
            "stream": False,
        }
        if style is LlmApiStyle.OPENAI:
            body["temperature"] = self.temperature
            body["max_tokens"] = self.max_tokens
            return body
        body["options"] = {
            "temperature": self.temperature,
            "num_predict": self.max_tokens,
        }
        return body


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
        model: str = DEFAULT_BEDROCK_MODEL,
        style: LlmApiStyle = LlmApiStyle.OLLAMA,
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
        self._model = model
        self._style = style
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
            model=settings.bedrock_model,
            style=LlmApiStyle(settings.llm_api_style),
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
        body = req.assemble_body(model=self._model, style=self._style)
        try:
            payload = self._post_with_retry(body)
        except _RetryExhaustedError as exc:
            self._degrade(reason=exc.reason, trace_id=None)
            raise BedrockUnavailableError(exc.reason) from exc

        try:
            response = self._parse_response(payload)
        except LlmResponseFormatError as exc:
            # 「答了但答不可用」与 HTTP 失败同类：不可重试 → 立即降级（R25.8），
            # 并把原因留在 `DEGRADED_MODE_SWITCH` 审计里便于定位是哪个网关形态变了。
            self._degrade(reason=str(exc), trace_id=None)
            raise

        # 只有走完整条通路（HTTP 成功 **且** 响应可用）才算成功，因此计数器在这里清零：
        # 把「网关一直回不可解析的体」也算进连续失败，才能真正触发降级。
        self._consecutive_failures = 0
        self._record_cassette(req, response)
        return response

    def _record_cassette(self, req: LlmRequest, response: LlmResponse) -> None:
        """把一次真实的 LIVE 响应录进 cassette，供日后 `REPLAY` 回放。

        `cassette.py` 的模块 docstring 与 `Cassette.record` 都把「LIVE 响应由 adapter 录制」
        写成约定；在此之前这段接线缺失，后果是 `LLM_MODE=LIVE` 跑完什么都不会留下，
        `REPLAY` 永远缺录制（`CassetteMiss`）——即真实额度花了、回放素材却没攒下。

        **尽力而为**：生产部署里 `tests/cassettes/` 可能不可写（systemd 的
        `ProtectSystem=strict`），而录制失败绝不能把一次成功的 LLM 调用变成失败。
        写失败只记一条告警，调用照常返回。
        """
        try:
            self._cassette.record(req.content_hash(), req, response)
        except OSError as exc:
            log_event(
                logger,
                "LLM_CASSETTE_RECORD_FAILED",
                level=logging.WARNING,
                message="LIVE 响应未能写入 cassette；本次调用不受影响，但该请求无法离线回放。",
                error=type(exc).__name__,
            )

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
        """网关 JSON → `LlmResponse`：正文必须解析得出来，用量尽力而为。

        两条非对称的严格度，都是有意的：

        - **正文必须有。** 认不出正文（或正文只有空白）→ 抛
          `LlmResponseFormatError`，由 `_invoke_live` 降级。原实现取不到字段时返回空串，
          那会让计划解释以 `numeric_check = PASS` 发布一段空文本——见该异常的 docstring。
        - **用量可以缺。** 用量的用途是告警阈值与台账，缺了按 0 计（宁可低估，也不因为
          网关少给一个计数字段就让整次调用失败）。这里只保证**认得的形态**都被取到，
          不会把一份带用量的响应记成零 —— 那正是本次要修的另一半。

        支持的正文形态（按序尝试，首个命中者胜出，见 `_CONTENT_PATHS`）：

        - `{"message": {"content": "..."}}` —— Ollama `/api/chat`（本项目的网关形态）；
        - `{"response": "..."}` —— Ollama `/api/generate`；
        - `{"content": "..."}` 或 `{"content": [{"type": "text", "text": "..."}]}` ——
          Anthropic / Bedrock `invoke-model` 的 Messages 形态；
        - `{"output_text": "..."}` —— OpenAI Responses；
        - `{"choices": [{"message": {"content": "..."}}]}` / `{"choices": [{"text": ...}]}`
          —— OpenAI Chat Completions / legacy。

        用量形态：Ollama 的 `prompt_eval_count` / `eval_count`、Anthropic 与 OpenAI 共有的
        `usage.input_tokens` / `output_tokens`、OpenAI 的 `usage.prompt_tokens` /
        `completion_tokens`。

        多认几种形态不是「猜」：它让网关换一个前端（或团队换一家代理实现）时不会**静默**
        退化成空解释，而认不出来时一定会留下一条可定位的错误。
        """
        content = _extract_content(payload)
        if content is None or not content.strip():
            raise LlmResponseFormatError(_format_error_message(payload, content))
        return LlmResponse(content=content, usage=_extract_usage(payload))


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


# --------------------------------------------------------------------------
# 响应形态识别：把「认得哪些网关方言」集中成两张表，而不是散在解析逻辑里
# --------------------------------------------------------------------------

#: 正文候选路径（按序尝试，首个命中者胜出）。路径元素是 dict 键，或是 list 下标。
_CONTENT_PATHS: Final[tuple[tuple[str | int, ...], ...]] = (
    ("message", "content"),  # Ollama /api/chat —— 本项目网关的形态
    ("response",),  # Ollama /api/generate
    ("content",),  # 简化 JSON 网关 / Anthropic 的 content 字段
    ("output_text",),  # OpenAI Responses
    ("choices", 0, "message", "content"),  # OpenAI Chat Completions
    ("choices", 0, "text"),  # OpenAI legacy completions
)

#: 用量候选路径：`(输入 token 路径, 输出 token 路径)`，首个命中者胜出。
_USAGE_PATHS: Final[tuple[tuple[tuple[str | int, ...], tuple[str | int, ...]], ...]] = (
    (("prompt_eval_count",), ("eval_count",)),  # Ollama
    (("usage", "input_tokens"), ("usage", "output_tokens")),  # Anthropic / Bedrock
    (("usage", "prompt_tokens"), ("usage", "completion_tokens")),  # OpenAI
)


def _dig(payload: Any, path: tuple[str | int, ...]) -> Any:
    """按路径取值；任一层缺失或类型不符即返回 `None`（不抛异常）。"""
    current: Any = payload
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or key >= len(current):
                return None
            current = current[key]
        else:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
    return current


def _as_text(value: Any) -> str | None:
    """把候选值规整成文本：字符串原样，文本块列表拼接；其余返回 `None`。

    Anthropic 的 `content` 是块列表（`[{"type": "text", "text": "..."}]`），因此需要
    这一层规整；空列表返回 `None` 而不是空串，好让解析继续尝试下一种形态。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text", block.get("content"))
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def _as_int(value: Any) -> int | None:
    """尽力解析非负整数；`bool` 不算（`True` 是 `int` 子类，会悄悄变成 1）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _extract_content(payload: dict[str, Any]) -> str | None:
    """按 `_CONTENT_PATHS` 取正文；一处都取不到返回 `None`。"""
    for path in _CONTENT_PATHS:
        text = _as_text(_dig(payload, path))
        if text is not None:
            return text
    return None


def _extract_usage(payload: dict[str, Any]) -> LlmUsage:
    """按 `_USAGE_PATHS` 取用量；一个计数都认不出时按 0 计（正文仍然可用）。"""
    for input_path, output_path in _USAGE_PATHS:
        input_tokens = _as_int(_dig(payload, input_path))
        output_tokens = _as_int(_dig(payload, output_path))
        if input_tokens is not None or output_tokens is not None:
            return LlmUsage(
                input_tokens=input_tokens or 0, output_tokens=output_tokens or 0
            )
    return LlmUsage.zero()


def _error_hint(payload: dict[str, Any]) -> str | None:
    """从错误信封里取一句人类可读说明（截断，绝不回显整包）。"""
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "type", "code"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    if isinstance(error, str) and error.strip():
        return error.strip()[:200]
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:200]
    return None


def _format_error_message(payload: dict[str, Any], content: str | None) -> str:
    """格式错误的人读信息：给出可定位的线索，但不把整包响应塞进日志/审计。"""
    hint = _error_hint(payload)
    if hint is not None:
        detail = f"网关错误信息：{hint}"
    elif content is not None:
        detail = "正文只有空白字符"
    else:
        detail = f"顶层键={sorted(payload)[:12]}"
    return (
        f"网关响应解析不出正文（{detail}）。"
        "已识别的正文形态：message.content / response / content / output_text / "
        "choices[0].message.content。"
    )
