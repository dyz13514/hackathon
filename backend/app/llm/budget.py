"""`Token_Budget_Manager` 与成本纪律（任务 5.4，R25.1–4、design.md §2.5、成本章节 §2）。

这一模块把「在 token 预算内运行，且花费被算术封顶」这条目标（G-013、K-10/K-16/K-11）
落成可执行的记账与闸门。它是 `Bedrock_Adapter` 的 `BudgetRecorder` 接缝（任务 5.3）的
真实实现，也是 `Orchestrator`（任务 5.7）在每次 LLM 调用前后调用的对象。本模块保持
**可独立使用**：`Orchestrator` 尚未落地，但 `open_scope / close_scope / record / gate`
四个方法构成完整的 API，可脱离编排层单独构造与测试。

## 恰好两个作用域，不多不少（R25.2 是封闭清单）

`BUDGETS` 只有 `PLAN_GENERATION`（4,000 token / USD 0.02，K-10）与 `REPLANNING`
（14,000 token / USD 0.06，K-16）。这不是「先放两个，以后再加」——requirements 第 3 节
把另外四个候选作用域明确移出范围。因此 `BUDGETS` 用 `MappingProxyType` 冻结，运行期
无法追加，任何「再加一个作用域」的想法都要先改这里的字面量，成为一次有意识的决定。

## `None` 作用域的三处判断（design.md §2.5 的刻意设计）

路由表 12 条入口里有 10 条 `budget_scope` 是 `None`（只有 `GENERATE_PLAN` 与 `REPLAN`
有强制上限）。`open_scope(None)` 因此返回 `None`——一个 no-op 句柄，而不是「假作用域
对象」。假作用域需要一个不会触发的上限（例如 `max_tokens=sys.maxsize`），那个数字会
落进 `traces`，让「这条路径有个很大的预算」这个错误印象变得可查询。返回 `None` 在数据层
留下的就是「没有作用域」这个事实。代价是 `record / gate / close_scope` 三处各有一个
`None` 判断——刻意接受这三行，换来的是调用方（`Orchestrator.run`）无条件调用、无需
在每个新入口上重复「这条路要不要记账」的分支。

`record(usage, None)` 时**照常**写逐次台账、每日累计与项目累计，只跳过作用域累计；
`gate(None)` 时跳过 `DENY_SCOPE` 判定，但仍评估每日与项目上限——因此「无路径上限」
不等于「无边界」。

## `DENY_*` 不抛异常

`gate()` 返回 `DENY_SCOPE / DENY_DAILY / DENY_PROJECT` 是一个**决策**，不是异常。
`Orchestrator` 收到 `DENY_*` 时按 design.md §2.5 的表走「确定性收尾」——返回已完成的
确定性结果 + `TOKEN_BUDGET_EXCEEDED` 标记（R25.3）。抛异常会让「预算到了，优雅收尾」
变成「预算到了，炸一个栈」，把一个可预期的正常路径伪装成故障。

## 成本纪律的两条独立护栏（design.md 成本章节 §2）

- **真实运行硬上限**：`PROJECT_REAL_RUN_CAP = 150`，按 `traces` 表中 `mode != REPLAY`
  的行数**在启动时**强制。达上限则拒绝以 `LLM_MODE=LIVE` 启动——这是启动期检查而非
  运行期祈祷。计数口径与 `GET /health` 的 `real_run_count` 共用 `admin.count_real_runs`，
  不另写一份 SQL（两份 SQL 迟早漂移）。
- **单一项目美元上限**：`PROJECT_USD_CEILING = USD 35`（K-11 的 USD 40 留 USD 5 余量）。
  项目累计达 **90%** 自动切 `DETERMINISTIC_ONLY` 并写 `DEGRADED_MODE_SWITCH` 审计。
  **不实现** USD 28/32/36 三级闸门梯（requirements 第 3 节拒绝清单）——单一天花板足矣。

`PROJECT_REAL_RUN_CAP` 与 `count_real_runs` 都从 `app.api.admin` import 而非在此重定义：
`GET /health` 与本模块回答的是同一个问题（「真实运行用了多少」），必须给出同一个答案。
"""

from __future__ import annotations

import threading
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal, get_args

from sqlalchemy import Engine

from app.api.admin import PROJECT_REAL_RUN_CAP, count_real_runs
from app.db import audit
from app.llm.adapter import LlmUsage
from app.llm.pricing import PRICE

__all__ = [
    "BUDGETS",
    "DEFAULT_DAILY_USD_CEILING",
    "PROJECT_REAL_RUN_CAP",
    "PROJECT_USD_CEILING",
    "BudgetScope",
    "BudgetScopeHandle",
    "GateDecision",
    "RealRunCapExceededError",
    "ScopeBudget",
    "TokenBudgetManager",
    "enforce_real_run_cap_on_startup",
]


# --------------------------------------------------------------------------
# 作用域定义：恰好 2 个（R25.2）
# --------------------------------------------------------------------------

#: 两个强制作用域的名字。`Literal` 使拼错的作用域名在 mypy strict 下即报错；
#: 取值集合的封闭性由 `BUDGETS` 的键与本类型的一致性保证（见 `BUDGETS` 之后的断言）。
BudgetScope = Literal["PLAN_GENERATION", "REPLANNING"]


class ScopeBudget:
    """一个作用域的 token 与美元上限。不可变，实例化后不再变化。"""

    __slots__ = ("max_tokens", "max_usd")

    def __init__(self, *, max_tokens: int, max_usd: Decimal) -> None:
        self.max_tokens = max_tokens
        self.max_usd = max_usd


#: 全仓库唯一的作用域预算表（design.md §2.5）。`MappingProxyType` 冻结：运行期不可
#: 追加作用域。改这里的字面量是「引入/移除一个强制作用域」的唯一入口（R25.2 封闭清单）。
BUDGETS: Final = MappingProxyType(
    {
        "PLAN_GENERATION": ScopeBudget(max_tokens=4_000, max_usd=Decimal("0.02")),  # K-10
        "REPLANNING": ScopeBudget(max_tokens=14_000, max_usd=Decimal("0.06")),  # K-16
    }
)

# `BUDGETS` 的键必须与 `BudgetScope` 的取值域逐一对应——两处定义漂移会让「恰好 2 个」
# 这条不变量静默破裂（多一个键即多一个作用域）。import 期断言把漂移变成启动即报错。
assert frozenset(BUDGETS) == frozenset(get_args(BudgetScope)), (
    "BUDGETS 的键与 BudgetScope 取值域不一致：R25.2 要求恰好 2 个作用域"
)


# --------------------------------------------------------------------------
# 成本纪律常量（design.md 成本章节 §2、ADR-011）
# --------------------------------------------------------------------------

#: 单一项目美元上限。K-11 的 USD 40 留 USD 5 余量。**不实现三级闸门梯**。
PROJECT_USD_CEILING: Final = Decimal("35")

#: 项目累计达此比例即自动降级（design.md 成本章节 ③：达 90% 切 DETERMINISTIC_ONLY）。
_PROJECT_DEGRADE_FRACTION: Final = Decimal("0.90")

#: 每日成本上限默认值（R25.4）。达 80% 显示预算告警。
DEFAULT_DAILY_USD_CEILING: Final = Decimal("5.00")

#: 每日告警阈值比例（R25.4：达 80% 告警）。
_DAILY_WARN_FRACTION: Final = Decimal("0.80")


# --------------------------------------------------------------------------
# 闸门决策
# --------------------------------------------------------------------------


class GateDecision(str, Enum):
    """`gate()` 的四种取值。`DENY_*` 是决策而非异常（见模块 docstring）。"""

    ALLOW = "ALLOW"
    DENY_SCOPE = "DENY_SCOPE"
    DENY_DAILY = "DENY_DAILY"
    DENY_PROJECT = "DENY_PROJECT"


# --------------------------------------------------------------------------
# 作用域句柄：可变累加器
# --------------------------------------------------------------------------


class BudgetScopeHandle:
    """一个已开启作用域的运行期累加器。

    只由 `TokenBudgetManager.open_scope` 构造（非 `None` 时），生命周期到 `close_scope`。
    `exceeds_limit()` 是 `gate()` 判 `DENY_SCOPE` 的依据：token 或美元任一越过 `ScopeBudget`
    即算越限（两条都是 K-10/K-16 的一部分，不能只看一条）。
    """

    __slots__ = ("budget", "input_tokens", "output_tokens", "scope_name", "trace_id", "usd")

    def __init__(self, *, scope_name: BudgetScope, budget: ScopeBudget, trace_id: str) -> None:
        self.scope_name = scope_name
        self.budget = budget
        self.trace_id = trace_id
        self.input_tokens = 0
        self.output_tokens = 0
        self.usd = Decimal("0")

    @property
    def total_tokens(self) -> int:
        """本作用域累计 token（输入 + 输出），与 `ScopeBudget.max_tokens` 对照。"""
        return self.input_tokens + self.output_tokens

    def add(self, usage: LlmUsage) -> None:
        """把一次调用的用量累进本作用域。"""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.usd += PRICE.cost_of(usage)

    def exceeds_limit(self) -> bool:
        """token 或美元任一达到/越过上限即为真（K-10/K-16）。"""
        return self.total_tokens >= self.budget.max_tokens or self.usd >= self.budget.max_usd


# --------------------------------------------------------------------------
# 每日 / 项目累计器
# --------------------------------------------------------------------------


class _UsdAccumulator:
    """一个带上限的美元累加器。线程安全（并发 `record` 下累计不丢）。"""

    __slots__ = ("_lock", "ceiling", "total")

    def __init__(self, *, ceiling: Decimal) -> None:
        self.ceiling = ceiling
        self.total = Decimal("0")
        self._lock = threading.Lock()

    def add(self, amount: Decimal) -> None:
        with self._lock:
            self.total += amount

    def exceeded(self) -> bool:
        """累计达到/越过上限即为真。"""
        return self.total >= self.ceiling

    def fraction(self) -> Decimal:
        """已用比例。上限为 0 时返回 0（避免除零，且 0 上限语义即「无预算」）。"""
        if self.ceiling <= 0:
            return Decimal("0")
        return self.total / self.ceiling


# --------------------------------------------------------------------------
# 真实运行硬上限（启动期强制）
# --------------------------------------------------------------------------


class RealRunCapExceededError(RuntimeError):
    """`traces` 中真实运行数已达 `PROJECT_REAL_RUN_CAP`，拒绝以 `LLM_MODE=LIVE` 启动。

    继承 `RuntimeError`：这不是「参数错」，而是「这条路（真实调用）此刻不该再走」。
    抛出即意味着进程不应以 LIVE 继续启动——与 `settings.ConfigurationError` 的语义一致，
    但单立一类，因为它的成因（配额耗尽）与修复动作（决定是否抬高配额）都与配置缺失不同。
    """


def enforce_real_run_cap_on_startup(engine: Engine, *, llm_mode: str) -> int:
    """启动期真实运行配额检查。返回当前真实运行数。

    只在 `LLM_MODE=LIVE` 时强制：`REPLAY` / `STUB` / `DISABLED` 都不触网、不消耗额度，
    因此配额对它们无意义（design.md 成本章节 ①）。达上限时写一条 `PROJECT_BUDGET_CEILING`
    审计再抛 `RealRunCapExceededError`——「为什么这次没起来」在日志里要有答案。

    计数口径 `mode != REPLAY` 与 `GET /health` 完全一致（共用 `count_real_runs`）。
    这是**启动期**检查而非运行期：配额耗尽后重启一百次也不会自己好，让它在演示开始前
    就以清晰的错误暴露，胜过第一次真实调用时才炸。
    """
    if llm_mode != "LIVE":
        return count_real_runs(engine)

    current = count_real_runs(engine)
    if current >= PROJECT_REAL_RUN_CAP:
        audit.append(
            event_category="PROJECT_BUDGET_CEILING",
            event_type="REAL_RUN_CAP_EXCEEDED",
            actor="SYSTEM",
            payload={
                "real_run_count": current,
                "cap": PROJECT_REAL_RUN_CAP,
                "attempted_mode": "LIVE",
            },
            engine=engine,
        )
        raise RealRunCapExceededError(
            f"真实运行数已达上限（{current} ≥ {PROJECT_REAL_RUN_CAP}）："
            f"拒绝以 LLM_MODE=LIVE 启动。请改用 REPLAY，或有意识地抬高 "
            f"PROJECT_REAL_RUN_CAP 后重试。"
        )
    return current


# --------------------------------------------------------------------------
# Token_Budget_Manager
# --------------------------------------------------------------------------


class TokenBudgetManager:
    """token / 成本记账与降级触发（design.md §2.5）。

    实现 `BudgetRecorder` 协议（任务 5.3 的接缝）：`Bedrock_Adapter` 在每次真实调用后
    调用 `record(usage)`——注意 adapter 的接缝只传 `usage`，因此本类提供的 `record`
    有一个默认 `scope=None` 的重载语义（见 `record`）。`Orchestrator`（任务 5.7）则用
    完整的 `open_scope / gate / record(usage, scope) / close_scope` 四方法。

    记账全在内存 + `traces` 派生：本项目没有专门的预算台账表（`llm_cache` 只缓存响应），
    每日与项目累计在进程内维护。项目美元的**权威**累计是 `traces.estimated_usd` 之和
    （`GET /health` 读它）；本类的 `project_total` 是进程内的运行期镜像，用于即时闸门判定
    与降级触发，两者在 `record` 处同步推进。
    """

    def __init__(
        self,
        *,
        daily_ceiling: Decimal = DEFAULT_DAILY_USD_CEILING,
        project_ceiling: Decimal = PROJECT_USD_CEILING,
        actor: str = "SYSTEM",
    ) -> None:
        self.daily = _UsdAccumulator(ceiling=daily_ceiling)
        self.project_total = _UsdAccumulator(ceiling=project_ceiling)
        self._actor = actor
        # 逐次调用台账：(input, output, usd) 三元组，供 UI 逐条查看（R25.1）。
        self.ledger: list[tuple[int, int, Decimal]] = []
        # 降级只触发一次：切入 DETERMINISTIC_ONLY 后 project_total 仍会增长（在途调用
        # 收尾），但审计只写一条，避免同一次越线刷屏。
        self._degraded = False
        self._ledger_lock = threading.Lock()

    # -- 作用域生命周期 --------------------------------------------------

    def open_scope(
        self, scope_name: BudgetScope | None, trace_id: str
    ) -> BudgetScopeHandle | None:
        """开一个作用域。`None` 返回 no-op 句柄（`None` 本身）。

        `Orchestrator.run()` 无条件调用此方法——路由表 10 条入口传 `None`，2 条传作用域名。
        返回 `None` 即「不建作用域行」，它一路传给 `record / gate / close_scope`，三者都对
        `None` 有定义。
        """
        if scope_name is None:
            return None
        return BudgetScopeHandle(
            scope_name=scope_name, budget=BUDGETS[scope_name], trace_id=trace_id
        )

    def close_scope(self, scope: BudgetScopeHandle | None) -> None:
        """结算一个作用域。`None` 为 no-op（无作用域可结算）。

        本项目无作用域合计表，因此结算不写 DB——作用域累计已在 `scope` 对象上，
        `Orchestrator` 在收尾时从它读取 token/USD 汇总写入 `traces`。这里保留方法与
        `None` 判断，是为了让四方法的对称 API 完整，`Orchestrator` 的 `finally` 可无条件调用。
        """
        if scope is None:
            return
        # no-op：作用域汇总由调用方从 handle 读取写入 traces。保留 None 判断（design.md §2.5）。

    # -- 记账 ------------------------------------------------------------

    def record(self, usage: LlmUsage, scope: BudgetScopeHandle | None = None) -> None:
        """记一次调用的用量。三件事在 `scope is None` 时**照常**发生（design.md §2.5）：
        逐次台账、每日累计、项目累计；被跳过的只有作用域累计。

        `scope` 默认 `None` 让本方法同时满足 `BudgetRecorder` 协议（adapter 只传 `usage`）
        与 `Orchestrator` 的显式作用域记账。记完项目累计后立即检查降级阈值——90% 触发
        （design.md 成本章节 ③）。
        """
        cost = PRICE.cost_of(usage)
        with self._ledger_lock:
            self.ledger.append((usage.input_tokens, usage.output_tokens, cost))
        if scope is not None:
            scope.add(usage)
        self.daily.add(cost)
        self.project_total.add(cost)
        self._maybe_degrade_on_ceiling()

    # -- 闸门 ------------------------------------------------------------

    def gate(self, scope: BudgetScopeHandle | None) -> GateDecision:
        """每次 LLM 调用前问一次。返回 `ALLOW / DENY_SCOPE / DENY_DAILY / DENY_PROJECT`。

        `scope is None` 时跳过 `DENY_SCOPE`，仍评估每日与项目上限——「无路径上限」的路径
        依然可能被 `DENY_DAILY / DENY_PROJECT` 拒绝。判定顺序固定（作用域 → 每日 → 项目），
        使同一状态总给同一决策。`DENY_*` 不抛异常，由 `Orchestrator` 走确定性收尾。
        """
        if scope is not None and scope.exceeds_limit():
            return GateDecision.DENY_SCOPE  # 只可能来自 2 个作用域
        if self.daily.exceeded():
            return GateDecision.DENY_DAILY
        if self.project_total.exceeded():
            return GateDecision.DENY_PROJECT
        return GateDecision.ALLOW

    # -- 告警与降级 ------------------------------------------------------

    def daily_warning_active(self) -> bool:
        """每日成本达 80% 即为真（R25.4：达 80% 在 Web_UI 显示预算告警）。"""
        return self.daily.fraction() >= _DAILY_WARN_FRACTION

    def _maybe_degrade_on_ceiling(self) -> None:
        """项目累计达 90% 时切 `DETERMINISTIC_ONLY` 并写审计，只触发一次。

        降级本身是把 `LLM_MODE` 切到 `DISABLED` 的运行期事件（P0 的旁路点只有一个：
        `Bedrock_Adapter` 的模式，design.md §2.6）。本类不持有 adapter 引用，因此这里
        只负责**记录**这次降级决策（`DEGRADED_MODE_SWITCH`）；实际把 adapter 切到
        `DISABLED` 由 `Orchestrator` 在读到降级标记后执行（任务 5.7）。`degraded` 属性
        让编排层可查询是否已越线。
        """
        if self._degraded:
            return
        if self.project_total.fraction() < _PROJECT_DEGRADE_FRACTION:
            return
        self._degraded = True
        audit.append(
            event_category="DEGRADED_MODE_SWITCH",
            event_type="ENTER_DETERMINISTIC_ONLY",
            actor=self._actor,
            payload={
                "reason": "PROJECT_USD_CEILING_90PCT",
                "trigger": "PROJECT_BUDGET",
                "project_usd_spent": str(self.project_total.total),
                "project_usd_ceiling": str(self.project_total.ceiling),
            },
        )

    @property
    def degraded(self) -> bool:
        """项目美元上限已达 90% 并触发降级。`Orchestrator` 读它决定是否旁路 adapter。"""
        return self._degraded
