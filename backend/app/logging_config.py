"""结构化 JSON 日志到 stdout（R24.8），且凭证与系统提示词永不出现在日志中（R23.10）。

依据 design.md「运维要点」：

> 日志：结构化 JSON 到 stdout，字段含 `trace_id`、`step_index`、`event`（R24.8）；
> 凭证与系统提示词内容永不出现在日志中，由一条 `test_no_secret_in_logs.py` 断言（R23.10）。

## 三条设计决定

1. **只有一个 handler，写 stdout。** Lightsail 上日志由 systemd 收走，进程自己不做
   轮转、不写文件。`uvicorn` 自带的三个 logger 被清空 handler 并改为向 root 传播，
   否则会话里会同时出现人类可读行与 JSON 行两种格式，集中查看时无法解析。

2. **脱敏做两道，都在格式化的最后一步。** 一道按**字段名**（`_DENY_FIELD_PARTS`），
   一道按**取值**（`register_redaction()` 注册的字面量）。两道都要，因为泄漏有两种
   形态：有人把 `api_key=...` 当 extra 传进来（字段名能抓住），或者有人把整个请求体
   拼进 message（只有取值匹配能抓住）。命中一律替换成 `REDACTED` 而不是删除——留下
   痕迹让运维知道「这里有东西被挡住了」，删除会让人误以为字段本来就不存在。

3. **`trace_id` / `step_index` 走 ContextVar。** 编排器在一次运行里会经过几十个日志
   点，逐个显式传参必然漏。`log_scope()` 在 `Orchestrator.run()` 与 `_react_loop`
   的每一步入口各设一次（任务 5.7、5.12），其余调用点什么都不用做就带上了关联字段。
   显式传入的值优先于 ContextVar，因为跨线程的后台任务拿不到调用方的上下文。

## 为什么不用第三方结构化日志库

`structlog` 之类需要在整个仓库统一使用它的 logger 才有意义，而 `uvicorn` /
`sqlalchemy` / `alembic` 都用标准库 logging。走标准库的 `Formatter` 接口，第三方库
的日志与自己的日志会经过同一个脱敏与序列化路径，这正是 R23.10 需要的性质：
**没有绕过点**。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.settings import Settings

#: 脱敏后的占位符。出现在日志里说明有内容被主动挡下，不是字段缺失。
REDACTED = "***REDACTED***"

#: 未经 `log_event()` 分类的记录（多为第三方库日志）统一落在这个 `event` 上。
DEFAULT_EVENT = "LOG_RECORD"

#: 取值脱敏的最短长度。比这更短的「密钥」在日志里做子串替换会误伤大量正常文本
#: （例如口令是 `abc` 时会把 `abcdef` 一起打码），而这类过短密钥本身已被
#: `settings.py` 的强度校验挡在启动之前。
MIN_REDACTABLE_LENGTH = 8

#: 字段名脱敏的片段清单（小写子串匹配）。
#:
#: 刻意**不含** `token`：`input_tokens` / `output_tokens` / `token_usage` 是预算记账
#: 的正常字段（R25.1），打掉它们会让成本可观测性失效。会话令牌相关字段用
#: `session_token` / `authorization` 两个更具体的片段覆盖。
_DENY_FIELD_PARTS: tuple[str, ...] = (
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "session_token",
    "access_key",
    # 系统提示词内容（R23.10）。Agent 的静态提示词常量按 6 段拼装（任务 5.6），
    # 这几个字段名是它可能被顺手记下来的形态。
    "system_prompt",
    "prompt_text",
    "prompt_body",
    "messages",
)

#: `logging.LogRecord` 自带的属性。extra 字段 = record.__dict__ 减去这一集合。
_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "taskName",
        "thread",
        "threadName",
        # 本模块自己的约定字段，单独提取，不再重复进 extra。
        "event",
        "trace_id",
        "step_index",
    }
)

#: 已注册的待脱敏字面量。按长度降序使用，避免长密钥被自己的前缀先替换掉。
_redactions: set[str] = set()

_current_trace_id: ContextVar[str | None] = ContextVar("log_trace_id", default=None)
_current_step_index: ContextVar[int | None] = ContextVar("log_step_index", default=None)


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------


def register_redaction(value: str | None) -> None:
    """把一个字面量登记为「永不出现在日志中」。

    调用点：`configure_logging()` 注册全部凭证；任务 5.6 落地静态提示词后，
    `agents/prompts/` 的段常量在同处注册（提示词是常量，注册一次即覆盖全部轮次）。

    过短或空的取值被忽略——见 `MIN_REDACTABLE_LENGTH` 的理由。
    """
    if value and len(value) >= MIN_REDACTABLE_LENGTH:
        _redactions.add(value)


def clear_redactions() -> None:
    """清空已注册字面量。仅供测试在用例间隔离状态。"""
    _redactions.clear()


def redact(text: str) -> str:
    """把已注册字面量替换成 `REDACTED`。"""
    result = text
    for secret in sorted(_redactions, key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, REDACTED)
    return result


def _is_denied_field(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _DENY_FIELD_PARTS)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """递归按字段名脱敏。

    深度上限 6 层：日志里的嵌套结构再深也已经不可读，而无限递归会被一个自引用的
    dict 挂死——日志代码把进程搞崩是不可接受的失败模式。
    """
    if depth >= 6:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED
                if _is_denied_field(str(key))
                else _sanitize(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1) for item in value]
    return value


# --------------------------------------------------------------------------
# 关联字段的作用域
# --------------------------------------------------------------------------


@contextmanager
def log_scope(
    *, trace_id: str | None = None, step_index: int | None = None
) -> Iterator[None]:
    """在作用域内为全部日志记录附上 `trace_id` / `step_index`。

    只设显式传入的那一项，另一项沿用外层的值：`_react_loop` 的每一步只需要换
    `step_index`，`trace_id` 由外层的 `Orchestrator.run()` 作用域提供。
    """
    trace_token = _current_trace_id.set(trace_id) if trace_id is not None else None
    step_token = _current_step_index.set(step_index) if step_index is not None else None
    try:
        yield
    finally:
        if step_token is not None:
            _current_step_index.reset(step_token)
        if trace_token is not None:
            _current_trace_id.reset(trace_token)


def current_trace_id() -> str | None:
    """当前作用域的 `trace_id`，无则 `None`。"""
    return _current_trace_id.get()


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------


class JsonLogFormatter(logging.Formatter):
    """一条记录一行 JSON。字段顺序固定，便于人眼扫读与 `jq` 取值。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # 三个 design.md 点名要求的字段一律存在（哪怕为 null），这样下游查询
            # 不需要区分「字段缺失」与「值为空」两种情况。
            "event": getattr(record, "event", None) or DEFAULT_EVENT,
            "trace_id": getattr(record, "trace_id", None) or _current_trace_id.get(),
            "step_index": _resolve_step_index(record),
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = REDACTED if _is_denied_field(key) else _sanitize(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # `default=str` 兜住 datetime / Decimal / Path 这类不可 JSON 序列化的取值：
        # 日志格式化失败会让 logging 打印一段 traceback 到 stderr 而丢掉这条记录，
        # 那比一个字符串化的值糟糕得多。
        line = json.dumps(payload, ensure_ascii=False, default=str)
        return redact(line)


def _resolve_step_index(record: logging.LogRecord) -> int | None:
    explicit = getattr(record, "step_index", None)
    return explicit if explicit is not None else _current_step_index.get()


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------

#: 需要交出自己 handler 的第三方 logger。它们默认自带格式化输出，不接管就会有
#: 两种格式并存。
_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "alembic",
    "sqlalchemy.engine",
)


def build_handler() -> logging.StreamHandler[Any]:
    """stdout 上的 JSON handler。"""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    return handler


def configure_logging(settings: Settings) -> None:
    """装配根 logger 并注册凭证脱敏。幂等：重复调用不会叠加 handler。

    在 `create_app()` 里调用，而不是模块导入时：导入期没有配置可读，而日志级别与
    脱敏清单都来自配置。
    """
    register_redaction(settings.session_shared_password.get_secret_value())
    register_redaction(settings.session_secret_key.get_secret_value())
    if settings.bedrock_api_key is not None:
        register_redaction(settings.bedrock_api_key.get_secret_value())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(build_handler())
    root.setLevel(settings.log_level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    message: str = "",
    **fields: Any,
) -> None:
    """记录一个分类事件。业务代码的首选入口。

    `event` 是必需的位置参数，因此「忘了给事件命名」在调用点就写不出来。
    """
    logger.log(level, message or event, extra={"event": event, **fields})
