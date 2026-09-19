"""`Risk_Scanner` 的服务层：读库 → 确定性扫描 → 模板叙述 → 去重落库（任务 8.5，R14.1–4、R14.9）。

## 分工

内核 `app/core/risk.py` 算度量与 severity（纯函数、无会话）；本模块负责有副作用的那半：

1. 读当前 `ACTIVE` 计划的已排产作业（`load_plan_candidate`）与冻结快照（`load_snapshot`）；
2. 调 `core.risk.scan(snapshot, scheduled_jobs, horizon_days)` 得到一组 `RiskFinding`；
3. 用 `render_template_narrative` 渲染叙述（`narrative_source = TEMPLATE`，R14.5/8.6）；
4. 按 `finding_key` **去重落库**（R14.9）：已存在则 `UPDATE last_seen_at, metric_value,
   threshold_value, severity, narrative, affected_order_ids`；不存在则 `INSERT` 一行。

## 幂等（R14.9）

同一份数据两次扫描产出相同的 `finding_key` 集合，因此第二次扫描只更新时间戳与度量，不新增
行——风险面板不会被重复项淹没。这条由 `scan_and_persist` 的 upsert-by-key 保证，并由
`test_risk_scan` 的「连续两次扫描行数不变」断言守住。

## 无 `ACTIVE` 计划时

P0 的 5 类风险都依赖 `ACTIVE` 计划的已排产作业（利用率 / SPOF / 班次 / 物料消耗）或订单
完工，因此无计划时扫描产出空集——`scan_and_persist` 返回 `[]`，不报错（触发器可能在任何时刻
调用，包括还没有 ACTIVE 计划的演示开场）。

## 不在这里做的事

- **不发起缓解提案**：`CRITICAL` 触发 `REPLAN` 走自主等级判定属编排层（8.6 的 API 决定是否
  接线；本服务只落库风险，返回结果供上层判断）。
- **不参与调用方事务的提交**：`scan_and_persist` 自己 `commit()`——它是一次独立的读→写闭环，
  由触发器（事件 / 定时 / 手动端点）各自调用，语义上是「扫描并记录当前风险」这一原子动作。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.risk import ROLLING_HORIZON_DAYS, RiskFinding, scan
from app.core.risk_narrative import render_template_narrative
from app.db import models as orm
from app.logging_config import log_event
from app.seed.dataset import DEMO_ANCHOR
from app.services.replanning import load_plan_candidate
from app.services.snapshot_loader import load_snapshot

logger = logging.getLogger(__name__)

__all__ = ["ScanResult", "scan_and_persist"]


class ScanResult:
    """一次扫描的结果摘要：本次算出的 findings + 新增/更新计数（供端点响应与日志）。"""

    def __init__(
        self,
        findings: list[orm.RiskFinding],
        *,
        inserted: int,
        updated: int,
        mitigation_plan_ids: list[str] | None = None,
    ) -> None:
        self.findings = findings
        self.inserted = inserted
        self.updated = updated
        #: 本次为 CRITICAL 风险新生成的缓解提案 plan_id（R14.7）。无则空列表。
        self.mitigation_plan_ids = mitigation_plan_ids or []

    @property
    def finding_count(self) -> int:
        return len(self.findings)


def scan_and_persist(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_days: int = ROLLING_HORIZON_DAYS,
    trigger: str = "MANUAL",
) -> ScanResult:
    """扫描当前 `ACTIVE` 计划的风险并去重落库，返回 `ScanResult`。**自提交。**

    `now` 默认 `DEMO_ANCHOR`（与生成/重排端点同一演示时钟口径，使风险时域落在演示数据的
    时间坐标系里）。`trigger` 只进日志，用于区分手动 / 事件 / 定时来源。
    """
    resolved_now = now if now is not None else DEMO_ANCHOR

    active = session.execute(
        select(orm.ProductionPlan).where(orm.ProductionPlan.status == "ACTIVE")
    ).scalars().first()

    if active is None:
        # 无 ACTIVE 计划：无可扫描的排产上下文，返回空结果（不是错误，见模块 docstring）。
        log_event(logger, "RISK_SCAN_SKIPPED_NO_ACTIVE_PLAN", trigger=trigger)
        return ScanResult([], inserted=0, updated=0)

    snapshot = load_snapshot(session, now=resolved_now, production_date=active.production_date)
    candidate = load_plan_candidate(session, active.plan_id)

    findings = scan(snapshot, candidate.scheduled_jobs, horizon_days=horizon_days)

    inserted, updated, rows = _persist(session, findings, now=resolved_now)
    session.commit()

    # CRITICAL → 缓解提案（R14.7）：INFO / WARNING 仅入面板、绝不生成提案。CRITICAL 走确定性
    # 再优化产出一个 RISK_MITIGATION 提案并链接 mitigation_plan_id。幂等：已链接（且提案仍
    # PENDING/ACTIVE）的 CRITICAL 不重复生成。这一步在 findings 提交之后进行，各自独立事务。
    mitigation_ids = _propose_mitigations_for_critical(
        session,
        active_plan_id=active.plan_id,
        now=resolved_now,
    )

    log_event(
        logger,
        "RISK_SCAN_COMPLETED",
        trigger=trigger,
        finding_count=len(findings),
        inserted=inserted,
        updated=updated,
        mitigations_proposed=len(mitigation_ids),
    )
    return ScanResult(
        rows, inserted=inserted, updated=updated, mitigation_plan_ids=mitigation_ids
    )


def _propose_mitigations_for_critical(
    session: Session,
    *,
    active_plan_id: str,
    now: datetime,
) -> list[str]:
    """为未解决的 CRITICAL 风险生成一个 RISK_MITIGATION 提案（R14.7）。**每次扫描至多一个。**

    严重度分流（R14.6–7）：只看 `severity == 'CRITICAL'` 且 `resolved_at is None` 的发现；
    `INFO` / `WARNING` 一律不进入本函数，因此不可能产生提案。

    ## 为什么每次扫描至多一个提案

    缓解是一次**计划级的确定性再优化**（`run_risk_mitigation` 对整份快照重排），它一次性
    应对当前全部 CRITICAL 风险——不是「一条风险一个提案」。这也与结构不变量一致：
    `ux_pending_per_day` 部分唯一索引规定同一生产日至多一个 `PENDING_APPROVAL` 计划
    （属性 15、R11.8/R12.6），因此一次扫描本就只能落一个待审提案。生成后，把**所有**当前
    未链接的 CRITICAL 发现都链到这一个提案。

    ## 幂等（R14.9 的提案侧）

    若已存在**任一** CRITICAL 发现链接到一个仍在库的提案，说明本轮 CRITICAL 已有在办缓解，
    直接跳过——重复扫描不为同一批未解决 CRITICAL 风险重复生成提案。
    """
    from app.orchestrator.pipelines.replan_deterministic import run_risk_mitigation
    from app.services.replanning import load_plan_candidate

    criticals = session.execute(
        select(orm.RiskFinding)
        .where(
            orm.RiskFinding.severity == "CRITICAL",
            orm.RiskFinding.resolved_at.is_(None),
        )
        .order_by(orm.RiskFinding.finding_key)  # 确定性顺序
    ).scalars().all()

    if not criticals:
        return []

    # 幂等守卫：已有任一 CRITICAL 链接到仍存在的提案 → 本轮已有在办缓解，不重复生成。
    for finding in criticals:
        if finding.mitigation_plan_id is not None:
            existing = session.get(orm.ProductionPlan, finding.mitigation_plan_id)
            if existing is not None:
                return []

    # 结构守卫：同一生产日已存在一个 PENDING_APPROVAL 计划时，不能再落第二个
    # （`ux_pending_per_day` 部分唯一索引，属性 15）。此时已有待审提案在办，缓解再优化无处落地
    # ——跳过而不是撞唯一约束崩掉。风险仍在面板呈现（CRITICAL），待现有提案处置后下次扫描再议。
    active_plan_row_orm = session.get(orm.ProductionPlan, active_plan_id)
    if active_plan_row_orm is not None:
        pending_exists = session.execute(
            select(orm.ProductionPlan.plan_id).where(
                orm.ProductionPlan.production_date == active_plan_row_orm.production_date,
                orm.ProductionPlan.status == "PENDING_APPROVAL",
            )
        ).scalars().first()
        if pending_exists is not None:
            log_event(
                logger,
                "RISK_MITIGATION_SKIPPED_PENDING_EXISTS",
                existing_pending_plan_id=pending_exists,
            )
            return []

    # 记住待链接的 CRITICAL 的 finding_id（下面 run_risk_mitigation 内部会 load_snapshot →
    # expunge_all()，把当前持有的 ORM 实例从会话里剥离；因此不能在其后直接改这批对象，须重取）。
    critical_ids = [f.finding_id for f in criticals]

    # 生成一个计划级缓解提案（确定性再优化 + 分级 + 自主判定 + 落库；自 commit）。
    active_plan = load_plan_candidate(session, active_plan_id)
    snapshot = load_snapshot(session, now=now)
    result = run_risk_mitigation(
        session,
        active_plan_id=active_plan_id,
        active_plan=active_plan,
        snapshot=snapshot,
        finding_id=critical_ids[0],  # 代表性 finding（提案是计划级的）
        now=now,
        session_id="risk-scan",
    )
    new_plan_id = result.plan.plan_id

    # 重取 CRITICAL 行（上一步的 expunge_all/commit 已让先前实例失效），链到这一个提案后提交。
    for finding_id in critical_ids:
        row = session.get(orm.RiskFinding, finding_id)
        if row is not None:
            row.mitigation_plan_id = new_plan_id
    session.commit()

    log_event(
        logger,
        "RISK_MITIGATION_PROPOSED",
        mitigation_plan_id=new_plan_id,
        critical_count=len(criticals),
        impact_class=result.impact.impact_class,
        autonomy_level=result.impact.autonomy_level,
    )
    return [new_plan_id]


def _persist(
    session: Session,
    findings: tuple[RiskFinding, ...],
    *,
    now: datetime,
) -> tuple[int, int, list[orm.RiskFinding]]:
    """按 `finding_key` upsert 落库（R14.9）。返回 (新增数, 更新数, 全部对应行)。

    去重是本函数的核心：`finding_key` 上有 UNIQUE 约束，同一风险重复出现只更新既有行的
    `last_seen_at` / `metric_value` 等，不新增。`first_seen_at` 只在新增时写，之后不动
    ——它记录「这个风险第一次被看见是什么时候」。
    """
    inserted = 0
    updated = 0
    rows: list[orm.RiskFinding] = []
    for finding in findings:
        narrative = render_template_narrative(finding)
        existing = session.execute(
            select(orm.RiskFinding).where(orm.RiskFinding.finding_key == finding.finding_key)
        ).scalars().first()
        if existing is None:
            row = orm.RiskFinding(
                finding_id=f"RISK-{uuid.uuid4().hex[:12]}",
                finding_key=finding.finding_key,
                risk_type=finding.risk_type.value,
                severity=finding.severity,
                entity_type=finding.entity_type,
                entity_id=finding.entity_id,
                metric_value=finding.metric_value,
                threshold_value=finding.threshold_value,
                affected_order_ids=list(finding.affected_order_ids),
                narrative=narrative.text,
                narrative_source=narrative.source,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(row)
            inserted += 1
            rows.append(row)
        else:
            # 重复出现：只更新可变字段与 last_seen_at，保留 first_seen_at 与 finding_id。
            existing.severity = finding.severity
            existing.metric_value = finding.metric_value
            existing.threshold_value = finding.threshold_value
            existing.affected_order_ids = list(finding.affected_order_ids)
            existing.narrative = narrative.text
            existing.narrative_source = narrative.source
            existing.last_seen_at = now
            updated += 1
            rows.append(existing)
    session.flush()
    return inserted, updated, rows
