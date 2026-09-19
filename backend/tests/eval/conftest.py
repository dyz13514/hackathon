"""评估套件的共享夹具与骨架纪律（任务 12.1）。

评估套件与 `tests/` 其余部分共享同一套「测试永不消耗 Bedrock 额度」的纪律，但有两点
不同，因此单独给一份 conftest：

1. **模式是 `REPLAY` 而非 `STUB`**。`make eval` 以 `LLM_MODE=REPLAY` 运行（design.md
   Testing Strategy §4）：按请求哈希回放 `tests/cassettes/` 下的录制，零网络、零成本。
   顶层 `tests/conftest.py` 把 `LLM_MODE` 钉在 `STUB` 是给属性/单元测试用的；评估套件要
   的是「和录制逐字节一致的回放」，所以这里显式覆盖为 `REPLAY`。真实网关只在
   `make eval-live`（`LLM_MODE=LIVE`）里触达，且计入 `PROJECT_REAL_RUN_CAP` 配额。

2. **每个用例自动带上 `eval` 标记**。`make eval` 用 `-m eval` 选择用例；本目录下的一切
   都属于评估套件，因此用 `pytest_collection_modifyitems` 自动打标，避免每个用例手写
   `@pytest.mark.eval`。

本文件只提供**骨架**：一套在内存库上跑演示数据的确定性夹具，供 12.2 起的 EVAL 用例复用。
它本身不断言任何 EVAL 结果——那些是后续子任务的事。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import audit
from app.db.models import Base
from app.db.session import create_db_engine, create_session_factory, session_scope
from app.seed import dataset
from app.seed.loader import load_demo_data
from app.settings import Settings

#: 演示数据的时间锚点与生产日期，与单元测试保持同一口径（可复现）。
EVAL_NOW = dataset.DEMO_ANCHOR
EVAL_PRODUCTION_DATE = dataset.DEMO_ANCHOR.date()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """给本目录下收集到的每个用例自动打上 `eval` 标记。

    这样 `make eval`（`pytest -m eval`）只跑评估套件，而 `make test` 通过
    `--ignore=tests/eval` 把它们排除——两条命令的边界因此由**目录**决定，用例作者不必
    记得手写标记。仅对本 `tests/eval/` 目录下的项生效，不影响别处。
    """
    eval_root = Path(__file__).parent
    for item in items:
        try:
            item_path = Path(str(item.fspath))
        except Exception:  # pragma: no cover - 防御性：路径不可解析时不打标
            continue
        if eval_root in item_path.parents or item_path.parent == eval_root:
            item.add_marker(pytest.mark.eval)


@pytest.fixture(autouse=True)
def _replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """评估套件在 `REPLAY` 下运行；未显式设置时兜底为 `REPLAY`，绝不 `LIVE`。

    `make eval` 会在环境里注入 `LLM_MODE=REPLAY`；直接 `pytest tests/eval` 时环境可能没有
    该变量，这里兜底设为 `REPLAY`。唯一例外是 `make eval-live` 显式设的 `LLM_MODE=LIVE`，
    此时不覆盖——那是刻意的真实调用路径。
    """
    if os.environ.get("LLM_MODE") != "LIVE":
        monkeypatch.setenv("LLM_MODE", "REPLAY")


def _eval_settings(db_path: str) -> Settings:
    """评估用例的 `Settings`：内存/临时库 + REPLAY。"""
    return Settings(
        database_url=f"sqlite:///{db_path}",
        session_shared_password=SecretStr("test-shared-password"),
        session_secret_key=SecretStr("test-secret-key-that-is-long-enough-32"),
        llm_mode="LIVE" if os.environ.get("LLM_MODE") == "LIVE" else "REPLAY",
    )


@pytest.fixture
def eval_engine(tmp_path: Path) -> Iterator[Engine]:
    """建表 + 绑定审计引擎的临时库引擎（与单元测试同构，便于用例复用）。"""
    eng = create_db_engine(_eval_settings((tmp_path / "eval.db").as_posix()))
    Base.metadata.create_all(eng)
    audit.set_audit_engine(eng)
    yield eng
    audit.set_audit_engine(None)
    eng.dispose()


@pytest.fixture
def eval_factory(eval_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(eval_engine)


@pytest.fixture
def eval_seeded(eval_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """载入演示数据后的 session 工厂——EVAL 用例的确定性输入起点。"""
    with session_scope(eval_factory) as session:
        load_demo_data(session)
    return eval_factory



# --------------------------------------------------------------------------
# eval-report：逐用例状态 + 断言明细（R26.4，design.md Testing Strategy §4）
# --------------------------------------------------------------------------
#
# `make eval-report` 设 `EVAL_REPORT=<路径>` 后运行整套评估。下面的钩子在设了该变量时
# 收集每个用例的通过状态与失败明细，会话结束时把它们汇总写成 Markdown。未设该变量时
# （`make eval` / CI 常规运行）钩子零开销、不产出任何文件——报告是一个可选产物，而不是
# 每次评估都要落盘的副作用。
#
# 断言明细来源于失败时的 `longrepr`（pytest 已经把断言差异格式化好了），因此不需要用例
# 作者做任何额外埋点。用例的 `EVAL-xxx` 编号从测试的 docstring 首行或函数名推断，用于让
# 报告按业务编号而非文件路径组织。

from dataclasses import dataclass, field  # noqa: E402


@dataclass
class _EvalOutcome:
    nodeid: str
    label: str
    outcome: str  # "passed" / "failed" / "skipped"
    detail: str = ""


@dataclass
class _EvalReport:
    outcomes: list[_EvalOutcome] = field(default_factory=list)


def _report_path(config: pytest.Config) -> str | None:
    """`EVAL_REPORT` 指向的报告路径；未设则表示本次运行不生成报告。"""
    return os.environ.get("EVAL_REPORT")


def pytest_configure(config: pytest.Config) -> None:
    if _report_path(config):
        config._eval_report = _EvalReport()  # type: ignore[attr-defined]


def _label_for(item_or_report_nodeid: str) -> str:
    """从 nodeid 里取一个人类可读的用例标签（函数名部分）。"""
    return item_or_report_nodeid.split("::")[-1]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    store: _EvalReport | None = getattr(item.config, "_eval_report", None)
    if store is None or report.when != "call":
        return
    detail = ""
    if report.failed and report.longrepr is not None:
        detail = str(report.longrepr)
    store.outcomes.append(
        _EvalOutcome(
            nodeid=report.nodeid,
            label=_label_for(report.nodeid),
            outcome=report.outcome,
            detail=detail,
        )
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    store: _EvalReport | None = getattr(session.config, "_eval_report", None)
    path = _report_path(session.config)
    if store is None or path is None:
        return
    _write_report(Path(path), store)


def _write_report(path: Path, store: _EvalReport) -> None:
    passed = sum(1 for o in store.outcomes if o.outcome == "passed")
    failed = sum(1 for o in store.outcomes if o.outcome == "failed")
    skipped = sum(1 for o in store.outcomes if o.outcome == "skipped")
    total = len(store.outcomes)

    lines: list[str] = []
    lines.append("# 评估套件报告（eval_report.md）")
    lines.append("")
    lines.append(
        "由 `make eval-report`（`LLM_MODE=REPLAY`）生成。逐用例通过状态 + 失败断言明细（R26.4）。"
    )
    lines.append("")
    lines.append(f"- 合计：{total}")
    lines.append(f"- 通过：{passed}")
    lines.append(f"- 失败：{failed}")
    lines.append(f"- 跳过：{skipped}")
    lines.append("")
    lines.append("| 状态 | 用例 | 节点 |")
    lines.append("|------|------|------|")
    _icon = {"passed": "✅ 通过", "failed": "❌ 失败", "skipped": "⏭️ 跳过"}
    for o in store.outcomes:
        lines.append(f"| {_icon.get(o.outcome, o.outcome)} | `{o.label}` | `{o.nodeid}` |")
    lines.append("")

    failures = [o for o in store.outcomes if o.outcome == "failed"]
    if failures:
        lines.append("## 失败断言明细")
        lines.append("")
        for o in failures:
            lines.append(f"### `{o.label}`")
            lines.append("")
            lines.append("```")
            lines.append(o.detail.rstrip())
            lines.append("```")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
