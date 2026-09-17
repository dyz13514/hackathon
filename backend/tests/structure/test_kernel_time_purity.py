"""`app/core/**` 里不得读时钟、不得取随机数（任务 2.1，R5.7）。

design.md Components §3 引言：

> 无 ORM、无 I/O、无 `datetime.now()`（当前时间由入参 `now` 显式传入），因此天然可重现。

`test_layering.py` 已经用 import 图守住了「无 ORM、无 I/O」。时钟与随机数守不住的原因是它们
来自**标准库**：`datetime` 与 `random` 是内核完全正当的依赖，禁不掉包，只能禁调用。

## 为什么这条断言值得单独存在

内核里出现一次 `datetime.now()` 之后：

- 排产结果开始随运行时刻漂移，但**属性 1 仍然通过**——它构造的 `DomainSnapshot` 没变，变的
  是不在入参里的那个东西；
- 审批前的重校验（R6.5）可能在与排产时不同的「现在」上运行，于是刚生成的计划在提交审批时
  被自己否掉，而两次调用的入参完全相同；
- 演示当天的复现请求（「再跑一次给我看」）会得到不同的甘特图。

三种后果都不以测试变红的形式出现，所以扫描必须先于代码存在。

扫描是 AST 而非文本：`datetime.now()`、`datetime.datetime.now()`、`from datetime import
datetime` 后的 `datetime.now()`、以及 `now = datetime.now` 这种取引用后再调用的写法，文本
正则至少漏掉最后一种。
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CORE_PACKAGE = BACKEND_ROOT / "app" / "core"

_SKIP_DIRS = frozenset({"__pycache__", ".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"})

#: 禁用的属性调用名。按**属性名**而非全限定名判定，因此 `datetime.now()`、
#: `datetime.datetime.now()`、`dt.now()` 三种写法一并命中。
#:
#: 代价是误报的可能：一个自定义对象上叫 `now` 的方法也会被拦。这个方向的误报是可接受的
#: ——内核里出现名为 `now` 的**调用**本身就该被看一眼，而 `snapshot.now` 是字段访问，不是
#: 调用，不会命中。
#:
#: `time` 刻意**不在**此列：`start_time.time()` 是取时间分量的正当写法。裸名 `time()`
#: （`from time import time`）由下面那组覆盖，而那种写法没有正当用途。
FORBIDDEN_CALL_ATTRS = frozenset(
    {
        "now",  # datetime.now
        "today",  # date.today / datetime.today
        "utcnow",  # datetime.utcnow
        "monotonic",  # time.monotonic
        "perf_counter",
        "time_ns",
        "random",
        "randint",
        "choice",
        "shuffle",
    }
)

#: 禁用的裸函数名（`from time import time` 之后的 `time()`）与随机源。
FORBIDDEN_CALL_NAMES = frozenset({"time", "time_ns", "random", "randint", "choice", "shuffle"})

#: 内核不得 import 的标准库模块：随机数没有任何确定性用途，禁包比禁调用更彻底。
#: `datetime` / `time` **不**在此列——内核当然要用 `datetime` 类型。
FORBIDDEN_MODULES = frozenset({"random", "secrets", "uuid"})


def _core_files() -> list[Path]:
    if not CORE_PACKAGE.is_dir():
        return []
    return sorted(
        path for path in CORE_PACKAGE.rglob("*.py") if not _SKIP_DIRS.intersection(path.parts)
    )


def _relative(path: Path) -> str:
    return path.relative_to(BACKEND_ROOT).as_posix()


def _clock_offences_in_source(source: str, filename: str) -> list[str]:
    """源码里对时钟 / 随机数的调用与 import，返回人可读的违反清单。"""
    tree = ast.parse(source, filename=filename)
    offences: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CALL_ATTRS:
                offences.append(f"{filename}:{node.lineno} 调用了 .{func.attr}()")
            elif isinstance(func, ast.Name) and func.id in FORBIDDEN_CALL_NAMES:
                offences.append(f"{filename}:{node.lineno} 调用了 {func.id}()")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    offences.append(f"{filename}:{node.lineno} import 了 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_MODULES:
                offences.append(f"{filename}:{node.lineno} import 了 {node.module}")
            if root in {"datetime", "time"}:
                for alias in node.names:
                    if alias.name in FORBIDDEN_CALL_NAMES | FORBIDDEN_CALL_ATTRS:
                        offences.append(
                            f"{filename}:{node.lineno} 从 {node.module} 引入了 {alias.name}"
                        )

    return offences


def test_kernel_never_reads_the_clock() -> None:
    """内核里没有任何一处 `datetime.now()` / `date.today()` / `time()`。

    「现在」是 `DomainSnapshot.now` 这个字段，由入参显式传入（任务 2.1）。
    """
    offences = [
        offence
        for path in _core_files()
        for offence in _clock_offences_in_source(path.read_text(encoding="utf-8"), _relative(path))
    ]
    assert not offences, "确定性内核读了时钟或随机数（R5.7 前提被破坏）：\n" + "\n".join(offences)


def test_scan_sees_the_kernel_package() -> None:
    """路径解析正确：`app/core/snapshot.py` 确实在扫描范围内。

    `_core_files()` 对不存在的目录返回空列表，因此一个写错的根路径会安静地永久通过。
    """
    scanned = {_relative(path) for path in _core_files()}
    assert "app/core/snapshot.py" in scanned


def test_scanner_detects_synthetic_clock_reads() -> None:
    """扫描器确实会抓到四种写法，且不误伤 `snapshot.now` 这样的字段访问。"""
    source = (
        "from datetime import datetime\n"
        "import random\n"
        "def f(snapshot):\n"
        "    a = datetime.now()\n"
        "    b = datetime.datetime.utcnow()\n"
        "    c = random.randint(1, 2)\n"
        "    return a, b, c, snapshot.now\n"
    )
    offences = _clock_offences_in_source(source, "probe.py")

    assert any(".now()" in offence for offence in offences)
    assert any(".utcnow()" in offence for offence in offences)
    assert any("import 了 random" in offence for offence in offences)
    assert any(".randint()" in offence for offence in offences)

    clean = "def g(snapshot):\n    return snapshot.now, snapshot.production_date\n"
    assert _clock_offences_in_source(clean, "clean.py") == []
