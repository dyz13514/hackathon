"""Property 2：排产输出满足全部 9 类硬约束（任务 2.9，R6.1 / R6.6 / R4.3 / R4.6 / R9.5；
design.md Correctness Properties「Property 2」）。

*For any* `DomainSnapshot`，对 `Scheduling_Core.generate_schedule` 输出的**已排产部分**运行
`Constraint_Validator.validate`，得到的 `violations` 为**空集**（design.md §2552「Property 2」）。

## 为什么这是全系统最承重的正确性主张（tasks.md 任务 2.9：非可选）

排产器（`app/core/scheduler.py`）与校验器（`app/core/validation.py`）是 design.md §3.2 点名
的**两个独立实现**：除 `available_at`（物料语义，R6.3）与 `processing_minutes`（纯算术）两
个 design.md 允许共用的纯函数外，校验器不复用排产器的任何内部函数。这条属性因此是一次
**交叉验证**——排产器负责「排得对」，校验器独立地判定「排得对不对」，两者对同一份计划的
判断必须一致。若排产器把某类硬约束算漏了（例如换型占用没算进机器重叠、或作业跨了班次
边界），校验器会独立地抓到它，让本属性变红；反之若校验器写松了，它就放行了排产器本该被
挡下的坏计划。两处独立实现意味着任一处的 bug 都会让这条属性失败，而不是两者以同一个 bug
一起沉默（design.md §3.2、validation.py 模块 docstring）。

覆盖的九类硬约束（R6.1）：物料充足、机器可用、机器能力、工人可用、工人技能、机器不重叠
（含换型占用区间）、工人不重叠、工序前后序（R4.3）、班次边界（R4.6）。R9.5 要求重排产出
的计划同样零违反——初始生成是重排的退化情形（`freeze=∅`），本属性守住这条基线；R6.6 要求
`is_feasible ⟺ violations 为空`，一并断言。

## 取样口径：三个 scarcity 档全覆盖

`domain_snapshots` 的 `scarcity` 参数调节物料丰俭（tests/generators.py）：

- `ABUNDANT`：物料从不成为瓶颈，可行路径密集——已排产作业多，是本属性的**主战场**
  （排上去的越多，被校验的约束点越多）。
- `TIGHT`：物料时缺时足，`scheduled_jobs` 与 `unschedulable_jobs` 混合出现——校验器要在
  「部分排产」的计划上仍判零违反。
- `INFEASIBLE`：物料刻意置零，需要物料的作业必然缺料——`scheduled_jobs` 可能为空或很小，
  这时属性平凡成立（空集无违反），但仍要确认那些**不需要物料**的作业若排上了也零违反。

三档都取样，因为本属性守的是「**凡是排上的，都合法**」，而在稀缺侧「排上的」这个集合本身
会缩小；只测 `ABUNDANT` 会让 `TIGHT` / `INFEASIBLE` 下「部分排产计划仍零违反」这一分支漏测。

## 断言什么

对每个快照：`candidate = generate_schedule(snapshot)`，`report = validate(candidate, snapshot)`。
`validate` 内部只校验 `candidate.scheduled_jobs`（`unschedulable_jobs` 本就没排入计划，谈
「重叠」「越界」无意义，见 validation.py `validate` docstring），因此直接传整个 candidate 即可，
无需自行过滤。断言两条：

1. `report.violations == ()`——空集，无任何一类硬约束被违反（Property 2 的核心）。
2. `report.is_feasible is True`——`is_feasible ⟺ violations 为空`（R6.6）。这条与第 1 条互为
   表里，一起断言是为了同时钉死 `ValidationReport` 那个布尔与列表的一致性（validation.py
   `ValidationReport` docstring 点名的「某处写成 `> 0` 就静默激活坏计划」正是要防的）。

违反非空时，把每条违反的 `violation_type` 与 `human_description` 一并放进断言消息——
counterexample 收缩后，这份清单直接告诉人「排产器在哪类约束上出了错」。

## 不碰数据库、不碰 LLM

输入是 `domain_snapshots` 产出的**内存**快照，不经数据库（tests/generators.py 模块 docstring：
六条属性守的都是内核纯函数性质，把库拖进来只会把毫秒级属性测试变成集成测试）。
`generate_schedule` 与 `validate` 都是内核纯函数，不触达 Bedrock；`conftest.py` 已把测试期
`LLM_MODE` 强制为 `STUB`，故额度纪律在此平凡满足。
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from app.core.scheduler import generate_schedule
from app.core.snapshot import DomainSnapshot
from app.core.validation import validate
from tests.generators import domain_snapshots

# 任务 2.9 明确要求 `max_examples=100`。快照构造与「排产 + 全量九类校验」两趟纯函数计算
# 在演示规模下是毫秒级，但快照构造本身略重，放宽 deadline 并抑制「过慢」健康检查——慢不是
# 错误，唯一要守的是「凡是排上的都合法」。
_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _describe(report_violations: tuple[object, ...]) -> str:
    """把违反清单渲染成可读的断言消息。counterexample 收缩后，这份清单直接指出出错的约束类。"""
    lines = []
    for v in report_violations:
        vtype = getattr(v, "violation_type", "?")
        desc = getattr(v, "human_description", "")
        lines.append(f"  - [{vtype}] {desc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Property 2（主）：任意 scarcity 档下，排产输出零硬约束违反
# --------------------------------------------------------------------------


@_SETTINGS
@given(
    snapshot=domain_snapshots(scarcity="ABUNDANT")
    | domain_snapshots(scarcity="TIGHT")
    | domain_snapshots(scarcity="INFEASIBLE")
)
def test_scheduled_output_satisfies_all_hard_constraints(snapshot: DomainSnapshot) -> None:
    """**Validates: Requirements 6.1, 6.6, 4.3, 4.6, 9.5**

    对任意内部一致的 `DomainSnapshot`（三个 scarcity 档各占三分之一采样权重），
    `Scheduling_Core.generate_schedule` 产出的已排产部分经 `Constraint_Validator.validate`
    校验，`violations` 为空集、`is_feasible` 为真。

    排产器与校验器**独立实现**（design.md §3.2），因此这是一次交叉验证：排产器排出的每一个
    作业，都必须能通过校验器对九类硬约束（物料 / 机器可用 / 机器能力 / 工人可用 / 工人技能 /
    机器不重叠含换型 / 工人不重叠 / 工序前后序 / 班次边界）的独立判定。任一侧的 bug 都会让
    本属性失败。
    """
    candidate = generate_schedule(snapshot)

    # validate 内部只校验 candidate.scheduled_jobs（unschedulable_jobs 没排入计划），
    # 因此直接传整个 candidate；无需自行过滤已排产部分。
    report = validate(candidate, snapshot)

    assert report.violations == (), (
        "排产器输出的已排产部分违反了硬约束（排产器与校验器独立实现，本属性抓到了其中一侧的 "
        f"bug）：\n{_describe(report.violations)}"
    )
    # is_feasible ⟺ violations 为空（R6.6）：两者一起断言，钉死 ValidationReport 布尔与列表的
    # 一致性（validation.py ValidationReport docstring）。
    assert report.is_feasible is True, (
        "violations 为空但 is_feasible 却为 False —— ValidationReport 的布尔与列表不一致（R6.6）"
    )
