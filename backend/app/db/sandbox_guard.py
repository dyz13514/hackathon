"""`Scenario_Sandbox` 的第 2 层隔离：引擎事件级 DML 拦截（任务 8.1，design.md §3.7、ADR-009）。

## 两层里的第 2 层

沙箱隔离是**两层**（ADR-009）：

- **第 1 层（不可达）**在 `services/snapshot_loader.load_sandbox_snapshot` + `core/snapshot`：
  快照读完即 `expunge_all()` 且 `frozen=True`，因此沙箱里既没有连着会话的 ORM 对象、也改不动
  快照。写入尝试几乎**不可达**。
- **第 2 层（可检测）= 本模块**：一个挂在 `Engine` 类上的 `before_cursor_execute` 监听器。
  当 `SANDBOX_ACTIVE` 为真且 `AUDIT_BYPASS` 为假时，任何 DML 语句（`INSERT` / `UPDATE` /
  `DELETE` / `REPLACE` / `CREATE` / `DROP` / `ALTER`）在到达游标前被拦下并抛 `SandboxWriteBlocked`。

为什么两层都要：第 1 层让写入不可达，但「不可达」本身不产生可断言的信号——R16.5 与 EVAL-204
要求写入尝试被**检测**到。第 2 层就是那个信号：即使将来某次改动让沙箱代码路径上重新出现了一个
真实 `Session`，SQL 语句到达游标时仍会被拦下，并留下一条 `SANDBOX_WRITE_BLOCKED` 审计。

## 为什么监听器挂在 `Engine` 类上而不是某个实例上

与 `app/db/audit.py` 的审计不可变监听器同一理由：沙箱里任何引擎都不该发生写入，包括测试临时
建的引擎、以及将来某人新建的引擎。绑到单个实例就等于给绕过留了一个门。默认状态下
`SANDBOX_ACTIVE` 为假，监听器对**全部**正常应用写入直接 `return`——它只在 `sandbox_guard(...)`
语境内（`SANDBOX_ACTIVE` 为真）才拦截，因此进程级挂载不影响任何常规写路径。

## 与 `AUDIT_BYPASS` 的协作

拦截条件里的 `AUDIT_BYPASS` 来自 `app/db/audit.py`——**必须是同一个 ContextVar**。审计写入
（包括本模块记录 `SANDBOX_WRITE_BLOCKED` 那一次）走 `audit.append()`，它在写入前把 `AUDIT_BYPASS`
置真。若这里读的是另一个同名 ContextVar，那条「阻断」审计本身就会被沙箱拦住——「记录阻断」这件
事被阻断，恰是最难发现的失效。因此本模块从 `app.db.audit` import `AUDIT_BYPASS`，不新建。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

from sqlalchemy import Engine, event

from app.db.audit import AUDIT_BYPASS, append
from app.logging_config import log_event

logger = logging.getLogger(__name__)

__all__ = [
    "SANDBOX_ACTIVE",
    "SandboxWriteBlocked",
    "sandbox_active",
    "sandbox_guard",
]

#: 沙箱推演进行中的标记。默认假：监听器对全部常规写入直接放行，只有 `sandbox_guard(...)`
#: 语境内为真，此时任何 DML 被拦。ContextVar 而非全局 bool：并发请求各有各的沙箱状态，
#: 一个请求进沙箱不该让另一个请求的写入被误拦。
SANDBOX_ACTIVE: ContextVar[bool] = ContextVar("SANDBOX_ACTIVE", default=False)

#: 匹配 DML/DDL 语句的起始动词（design.md §3.7 的 `^(INSERT|UPDATE|DELETE|REPLACE|CREATE|
#: DROP|ALTER)`）。沙箱里**任何**写都不该发生，因此不看目标表——与审计监听器（只护 audit_log
#: 一张表）不同：那里放行其它表的写，这里连一次写尝试都不允许。`\b` 确保只匹配完整动词，
#: `SELECT ... AS updated_flag` 这种以关键字为列别名的读语句不被误伤（它以 SELECT 起头）。
_DML_RE: Final = re.compile(
    r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b",
    re.IGNORECASE,
)


class SandboxWriteBlocked(RuntimeError):
    """沙箱推演中出现了一次 DML 写尝试，已被引擎级监听器拦下（R16.5、EVAL-204）。

    继承 `RuntimeError` 而非 `ValueError`：这不是「参数不对」，而是「这条路在沙箱里不存在」。
    它**终止该次模拟**（R16 第 5 条），不应被业务代码捕获后重试——捕获它唯一正确的方式是改掉
    发出该写语句的代码。`statement` 只留前 200 字符，避免把可能含大 payload 的语句整条带进
    异常与日志。
    """

    def __init__(self, *, statement: str) -> None:
        self.statement = statement[:200]
        super().__init__(
            f"沙箱内拒绝 DML（design.md §3.7 第 2 层）：Scenario_Sandbox 对生产数据只读，"
            f"任何写尝试都会终止模拟并记 SANDBOX_WRITE_BLOCKED。被拒语句：{self.statement}"
        )


@event.listens_for(Engine, "before_cursor_execute")
def _block_dml_in_sandbox(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """在语句到达驱动之前，若处于沙箱语境则拦下任何 DML（第 2 层）。

    放行的两种情况：① 不在沙箱语境（`SANDBOX_ACTIVE` 为假）——这是绝大多数调用，常规写入
    完全不受影响；② 审计旁路（`AUDIT_BYPASS` 为真）——记录 `SANDBOX_WRITE_BLOCKED` 那次
    INSERT 本身必须放行，否则「记录阻断」会被阻断。其余情况下，DML 语句抛 `SandboxWriteBlocked`。
    """
    if not SANDBOX_ACTIVE.get() or AUDIT_BYPASS.get():
        return
    if _DML_RE.match(statement):
        raise SandboxWriteBlocked(statement=statement)


@contextmanager
def sandbox_active() -> Iterator[None]:
    """把 `SANDBOX_ACTIVE` 置真的最小语境；退出时**恢复原值**而不是硬置假。

    用 `reset(token)`：嵌套（沙箱内又进一层沙箱）时内层退出不该把外层的沙箱状态清掉。这是
    `sandbox_guard` 的底座——后者在此之上加了「捕获 → 审计 → 重抛」。单独暴露它，供只需要
    「在这段代码里禁写」而不需要 scenario_id 审计语义的内部测试钩子使用。
    """
    token = SANDBOX_ACTIVE.set(True)
    try:
        yield
    finally:
        SANDBOX_ACTIVE.reset(token)


@contextmanager
def sandbox_guard(scenario_id: str) -> Iterator[None]:
    """沙箱推演的写保护语境（design.md §3.7）。

    块内 `SANDBOX_ACTIVE` 为真，任何 DML 触发 `SandboxWriteBlocked`。捕获到该异常时：

    1. 用 `audit.append()`（走 `AUDIT_BYPASS` 标记的独立连接）写一条 `SANDBOX_WRITE_BLOCKED`
       审计——独立连接是关键，否则这条 INSERT 会被本模块的监听器自己拦住（除非 `AUDIT_BYPASS`
       放行，而 `append` 正是这么做的）；
    2. 重抛，**终止该次模拟**（R16 第 5 条）。

    审计写在 `finally` 复位 `SANDBOX_ACTIVE` 之前完成也无妨：`append` 靠 `AUDIT_BYPASS` 放行，
    不依赖 `SANDBOX_ACTIVE` 的值。`scenario_id` 进审计 `subject_id`，使「哪次模拟尝试了写」可查。
    """
    token = SANDBOX_ACTIVE.set(True)
    try:
        yield
    except SandboxWriteBlocked as blocked:
        # 记录阻断，再重抛以终止模拟（R16 第 5 条）。审计 INSERT 走 `append`，它在写入前置
        # `AUDIT_BYPASS`，因此本模块的监听器会放行这一次写——即便此刻 `SANDBOX_ACTIVE` 仍为真。
        # 不在此处复位 `SANDBOX_ACTIVE`：复位统一交给 `finally`，避免对同一 token 复位两次
        # （`ContextVar.reset` 二次调用会抛）。
        append(
            event_category="SANDBOX_WRITE_BLOCKED",
            event_type="DML_BLOCKED",
            actor="SYSTEM",
            payload={"scenario_id": scenario_id, "statement": blocked.statement},
            subject_type="Scenario",
            subject_id=scenario_id,
        )
        log_event(
            logger,
            "SANDBOX_WRITE_BLOCKED",
            level=logging.WARNING,
            scenario_id=scenario_id,
        )
        raise
    finally:
        # 无论正常退出、被 SandboxWriteBlocked 终止、还是任何其它异常，都恰好复位一次，
        # 不泄漏沙箱语境到语境外的后续写入。
        SANDBOX_ACTIVE.reset(token)
