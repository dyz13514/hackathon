"""风险面板端点（design.md Components §5「风险」、§6 `/risks`，任务 8.5 + 8.6，R14）。

两个端点：

- `POST /api/risks/scan`（任务 8.5）—— 手动触发一次确定性风险扫描，去重落库并返回结果。
  写端点（受 `Session_Auth` 保护）。这是 R14.1 三类触发器里的「Planner 手动请求」那条，也是
  最先落地、可独立验证的一条；事件触发（计划激活 / 数据变更）与每日定时触发在其之上接线。
- `GET /api/risks`（任务 8.6）—— 只读列出当前风险发现，按严重度分组供风险面板渲染。

## 数值全部确定性、无 LLM

风险度量与 severity 由 `core/risk.py` 算，叙述由 `core/risk_narrative.py` 的模板渲染
（`narrative_source = TEMPLATE`，R14.5）。本端点不调用任何 LLM。

## 严重度分流（R14.6–7，全部 P0）

`INFO` / `WARNING` 仅入风险面板（本端点返回它们，不生成提案）。`CRITICAL` 由 8.6 的处理
交给 `Orchestrator` 以 `REPLAN` 意图生成缓解提案，再走任务 7.3 的自主等级判定（因此
`CRITICAL` 的缓解提案同样受 L5 约束，不会自动生效）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.core.risk import ROLLING_HORIZON_DAYS
from app.db import models as orm
from app.services.risk_scan import scan_and_persist

router = APIRouter(prefix="/risks", tags=["risks"])


# --------------------------------------------------------------------------
# 响应契约
# --------------------------------------------------------------------------


class RiskFindingOut(BaseModel):
    """一条风险发现（R14.9）。度量/阈值/受影响订单/叙述及其来源徽章供面板渲染。"""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    risk_type: str
    severity: str
    entity_type: str
    entity_id: str
    metric_value: float
    threshold_value: float
    affected_order_ids: list[str]
    narrative: str | None
    narrative_source: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    mitigation_plan_id: str | None


class ScanRisksRequest(BaseModel):
    """`POST /risks/scan` 的请求体。`horizon_days` 可选，默认滚动时域 3 天（R14.2）。"""

    model_config = ConfigDict(extra="forbid")

    horizon_days: int = Field(default=ROLLING_HORIZON_DAYS, ge=1, le=30)


class ScanRisksResponse(BaseModel):
    """`POST /risks/scan` 的响应：本次扫描结果 + 新增/更新计数（幂等可见）。"""

    model_config = ConfigDict(extra="forbid")

    finding_count: int
    inserted: int
    updated: int
    findings: list[RiskFindingOut]


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------


def _to_out(row: orm.RiskFinding) -> RiskFindingOut:
    affected = row.affected_order_ids if isinstance(row.affected_order_ids, list) else []
    return RiskFindingOut(
        finding_id=row.finding_id,
        risk_type=str(row.risk_type),
        severity=str(row.severity),
        entity_type=str(row.entity_type),
        entity_id=str(row.entity_id),
        metric_value=float(row.metric_value),
        threshold_value=float(row.threshold_value),
        affected_order_ids=[str(x) for x in affected],
        narrative=row.narrative,
        narrative_source=row.narrative_source,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        mitigation_plan_id=row.mitigation_plan_id,
    )


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


class RiskListResponse(BaseModel):
    """`GET /risks` 的响应：当前风险发现，按 severity 分组供面板渲染（R14.6）。

    `by_severity` 是 CRITICAL/WARNING/INFO 三组的分桶；`critical` / `warning` / `info` 三个
    计数便于面板顶栏。`findings` 是同一批发现的扁平列表（按 severity 秩 + finding_key 排序）。
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[RiskFindingOut]
    critical_count: int
    warning_count: int
    info_count: int


@router.get(
    "",
    response_model=RiskListResponse,
    summary="列出当前风险发现，按严重度排序（R14.6，供风险面板）",
)
def list_risks(request: Request) -> RiskListResponse:
    """只读端点（无认证，与其余 GET 同口径）：回读全部未解决风险发现，按严重度排序。

    `INFO` / `WARNING` / `CRITICAL` 都返回——面板据 severity 分组渲染（R14.6）；`CRITICAL`
    项的缓解入口由前端据 `severity == 'CRITICAL'` 呈现。排序稳定：CRITICAL 在前，同级按
    `finding_key` 升序（与扫描内核同口径），使面板顺序可复现。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    severity_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    with factory() as db:
        rows = db.execute(
            select(orm.RiskFinding).where(orm.RiskFinding.resolved_at.is_(None))
        ).scalars().all()
    ordered = sorted(
        rows, key=lambda r: (severity_rank.get(str(r.severity), 2), r.finding_key)
    )
    findings = [_to_out(r) for r in ordered]
    return RiskListResponse(
        findings=findings,
        critical_count=sum(1 for r in ordered if str(r.severity) == "CRITICAL"),
        warning_count=sum(1 for r in ordered if str(r.severity) == "WARNING"),
        info_count=sum(1 for r in ordered if str(r.severity) == "INFO"),
    )


@router.post(
    "/scan",
    response_model=ScanRisksResponse,
    summary="手动触发确定性风险扫描并去重落库（R14.1）",
)
def scan_risks_endpoint(
    request: Request, body: ScanRisksRequest, session: PlannerSession
) -> ScanRisksResponse:
    """手动触发一次风险扫描。写端点（受 `Session_Auth` 保护）。

    委派给 `services.risk_scan.scan_and_persist`——它读当前 `ACTIVE` 计划与快照、调确定性
    内核算风险、渲染模板叙述、按 `finding_key` 去重落库并自提交。无 ACTIVE 计划时返回空结果
    （不报错）。`session` 参数使保护关系进 OpenAPI。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    # 任务 13.2：装上 Risk_Monitor_Agent 的 LLM 归因叙述驱动（只读）。它对最高严重度的至多 5 项
    # WARNING+ 风险尝试 LLM 叙述；LIVE 失败标记 LLM_FAILED，缺 adapter 用事实模板。
    driver = _narrative_driver(request)
    with factory() as db:
        result = scan_and_persist(
            db, horizon_days=body.horizon_days, trigger="MANUAL", narrative_driver=driver
        )
        findings = [_to_out(row) for row in result.findings]
        return ScanRisksResponse(
            finding_count=result.finding_count,
            inserted=result.inserted,
            updated=result.updated,
            findings=findings,
        )


def _narrative_driver(request: Request) -> object | None:
    """从 `app.state.llm_adapter` 构造只读的 `RiskNarrativeDriver`；缺 adapter 时返回 None。

    降级模式在驱动内部判断。若启动配置为 LIVE，即便运行期已转为 DISABLED，
    生成失败仍标记 LLM_FAILED；没有驱动时才使用事实模板。
    """
    adapter = getattr(request.app.state, "llm_adapter", None)
    if adapter is None:
        return None
    from app.agents.risk_monitor_agent import RiskNarrativeDriver

    return RiskNarrativeDriver(adapter)
