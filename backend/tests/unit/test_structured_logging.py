"""结构化 JSON 日志的形状（R24.8、tasks.md 1.7）。

design.md「运维要点」只点名三个字段：`trace_id`、`step_index`、`event`。这里断言的正是
它们的存在性与来源优先级，以及「一条记录一行合法 JSON」——后者是集中查看能否解析的
前提，也是最容易被一个多行 message 悄悄破坏的性质。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO

import pytest

from app.logging_config import (
    DEFAULT_EVENT,
    JsonLogFormatter,
    clear_redactions,
    log_event,
    log_scope,
)

REQUIRED_FIELDS = ("event", "trace_id", "step_index")


@pytest.fixture
def sink() -> Iterator[tuple[logging.Logger, StringIO]]:
    clear_redactions()
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("tests.structured_logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, stream
    finally:
        logger.handlers = []
        logger.propagate = True


def _records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_every_record_is_one_line_of_json_with_the_three_required_fields(
    sink: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = sink

    log_event(logger, "PLAN_GENERATED", plan_id="PLAN-0007")
    logger.warning("多行\nmessage\n也必须留在一行里")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2, "换行的 message 不得把一条记录拆成两行"
    for record in _records(stream):
        for field in REQUIRED_FIELDS:
            assert field in record


def test_unclassified_records_get_the_default_event(
    sink: tuple[logging.Logger, StringIO],
) -> None:
    """第三方库的日志没有 `event`，落在 `DEFAULT_EVENT` 上而不是缺字段。"""
    logger, stream = sink
    logger.info("uvicorn 之类的库日志")
    assert _records(stream)[0]["event"] == DEFAULT_EVENT


def test_log_scope_attaches_trace_id_and_step_index(
    sink: tuple[logging.Logger, StringIO],
) -> None:
    """作用域内的调用点不必传参就带上关联字段，退出后不残留。"""
    logger, stream = sink

    with log_scope(trace_id="TRC-0001"):
        log_event(logger, "RUN_STARTED")
        with log_scope(step_index=3):
            log_event(logger, "TOOL_INVOKED", tool_name="generate_schedule")
    log_event(logger, "AFTER_SCOPE")

    started, tool, after = _records(stream)
    assert (started["trace_id"], started["step_index"]) == ("TRC-0001", None)
    assert (tool["trace_id"], tool["step_index"]) == ("TRC-0001", 3)
    assert tool["tool_name"] == "generate_schedule"
    assert (after["trace_id"], after["step_index"]) == (None, None)


def test_explicit_fields_win_over_the_scope(
    sink: tuple[logging.Logger, StringIO],
) -> None:
    """后台任务拿不到调用方的 ContextVar，必须能显式覆盖。"""
    logger, stream = sink

    with log_scope(trace_id="TRC-OUTER", step_index=1):
        log_event(logger, "REPLAYED", trace_id="TRC-INNER", step_index=9)

    record = _records(stream)[0]
    assert (record["trace_id"], record["step_index"]) == ("TRC-INNER", 9)


def test_non_serialisable_values_do_not_break_the_line(
    sink: tuple[logging.Logger, StringIO],
) -> None:
    """`Decimal` / `datetime` / 自定义对象走 `default=str`，不让格式化失败丢掉记录。"""
    from datetime import UTC, datetime
    from decimal import Decimal

    logger, stream = sink

    log_event(
        logger,
        "COST_RECORDED",
        estimated_usd=Decimal("0.0312"),
        at=datetime(2025, 3, 1, 8, 30, tzinfo=UTC),
        opaque=object(),
    )

    record = _records(stream)[0]
    assert record["estimated_usd"] == "0.0312"
    assert str(record["at"]).startswith("2025-03-01")
