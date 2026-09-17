"""`PlanDelta` 与 `compute_plan_delta`（任务 7.2，design.md §3.5）。

从两个 `PlanCandidate`（当前 ACTIVE 计划与候选计划）计算变更集与 `churn_ratio`。

## 五个分组的定义（design.md §3.5 伪代码）

设 a = active 计划的 {job_id: ScheduledJob}，b = candidate 的 {job_id: ScheduledJob}：

- `added`      = b - a（候选有、active 无）
- `removed`    = a - b（active 有、候选无）
- `moved`      = a ∩ b 中 start_time 不同 **且** machine_id 相同 **且** worker_id 相同
- `reassigned` = a ∩ b 中 machine_id 不同 **或** worker_id 不同（优先于 moved）
- `unchanged`  = 其余共有作业

`reassigned` 优先于 `moved`：machine/worker 变了就算 reassigned，哪怕时间也变了——
两分组互斥，不会重复计数。

## churn_ratio 的分母是**并集**而非 |ACTIVE|（design.md §3.5）

分母 = |a ∪ b|。插入加急订单时 b > a，分子可达 |a ∪ b| = 1.0 最大，K-05（≤0.20）仍有意义；
若分母取 |a|，插单后比率可超过 1，目标失去意义。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.scheduler import PlanCandidate, ScheduledJob


@dataclass(frozen=True)
class PlanDelta:
    """两份计划之间的变更集（design.md §3.5）。

    字段全部是 `tuple[str, ...]`（job_id 列表）或 `float`（比率），无 ORM / I/O 依赖。
    `frozen=True` 保证消费方不能改写 delta 后重新传给 `classify_impact`。

    `changed_job_count` = len(added) + len(removed) + len(moved) + len(reassigned)，
    是 `ImpactInput` 同名字段的直接来源。
    """

    added: tuple[str, ...]
    """候选计划新增、active 中不存在的 job_id（新增作业）。"""
    removed: tuple[str, ...]
    """active 计划中存在、候选中不存在的 job_id（被移出的作业）。"""
    moved: tuple[str, ...]
    """两者都有、同机器同工人、但 start_time 不同的 job_id。"""
    reassigned: tuple[str, ...]
    """两者都有、machine_id 或 worker_id 发生变化的 job_id（优先于 moved）。"""
    unchanged: tuple[str, ...]
    """两者都有且完全未变的 job_id。"""
    churn_ratio: float
    """(added + removed + moved + reassigned) / |a ∪ b|；0.0 当两者均空。"""

    @property
    def changed_job_count(self) -> int:
        """触发影响分级的「变更作业数」= added + removed + moved + reassigned。"""
        return len(self.added) + len(self.removed) + len(self.moved) + len(self.reassigned)


def compute_plan_delta(active: PlanCandidate, cand: PlanCandidate) -> PlanDelta:
    """从两份 `PlanCandidate` 计算 `PlanDelta`（design.md §3.5 伪代码实现）。

    逐步：
    1. 分别建 `{job_id: ScheduledJob}` 字典。
    2. 按集合运算分出 added / removed / common。
    3. common 内按 machine_id / worker_id 先判 reassigned，再判 moved，其余 unchanged。
    4. churn_ratio 分母取并集大小。

    全程不碰 ORM、不碰 I/O，符合内核分层规则（`test_layering.py` 第 ① 条）。
    """
    a: dict[str, ScheduledJob] = {sj.job_id: sj for sj in active.scheduled_jobs}
    b: dict[str, ScheduledJob] = {sj.job_id: sj for sj in cand.scheduled_jobs}

    a_keys = set(a)
    b_keys = set(b)

    added = sorted(b_keys - a_keys)
    removed = sorted(a_keys - b_keys)
    common = sorted(a_keys & b_keys)

    moved_list: list[str] = []
    reassigned_list: list[str] = []
    unchanged_list: list[str] = []

    for job_id in common:
        sj_a = a[job_id]
        sj_b = b[job_id]
        if sj_a.machine_id != sj_b.machine_id or sj_a.worker_id != sj_b.worker_id:
            # machine または worker が変わった → reassigned（優先）
            reassigned_list.append(job_id)
        elif sj_a.start_time != sj_b.start_time:
            # 同 machine・同 worker、時間だけ変化 → moved
            moved_list.append(job_id)
        else:
            unchanged_list.append(job_id)

    union_size = len(a_keys | b_keys)
    churn_numerator = len(added) + len(removed) + len(moved_list) + len(reassigned_list)
    churn_ratio = churn_numerator / union_size if union_size > 0 else 0.0

    return PlanDelta(
        added=tuple(added),
        removed=tuple(removed),
        moved=tuple(moved_list),
        reassigned=tuple(reassigned_list),
        unchanged=tuple(unchanged_list),
        churn_ratio=churn_ratio,
    )
