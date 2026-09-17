"""SQLAlchemy 2.0 声明式模型：design.md「Data Models」§2–§7 的全部 35 张表。

三条贯穿全表的约定（design.md Data Models 开头，R27.4）：

1. **PostgreSQL 兼容**——不用 SQLite 专有类型。JSON 列用 `JSON`（PG 下映射
   `JSONB`），时间列统一 `DateTime`（naive 本地时间，全系统单时区），日期列用
   `Date`，数值列用 `Numeric`。
2. **主键是可读字符串 ID**（`PLAN-0007` 形式），便于演示与审计阅读。唯一例外是
   `input_snapshots.snapshot_version`，它必须是单调递增整数（R12.1）。
3. **`Numeric` 而非 `Float`**——design.md §3.1.4 要求加工时长用 `Decimal` 计算后
   `math.ceil`，「禁止浮点，避免同输入不同结果」。因此凡进入排产算术的数值列
   一律 `Numeric`，SQLAlchemy 默认以 `Decimal` 取出。

## 一个刻意的整数：`production_plans` ↔ `disruptions` 的外键环

`disruptions.active_plan_id` 指向 `production_plans`，而 `production_plans.disruption_id`
指向 `disruptions`——这是 schema 里唯一的外键环。PostgreSQL 的 `REFERENCES` 要求被引用
表已存在，环必须靠 `ALTER TABLE ADD CONSTRAINT` 打破，而 SQLite 不支持那条语句；
`use_alter=True` 会让 `create_all` 在 SQLite 上直接失败。

因此 `production_plans.disruption_id` **不带库级外键**（保留列与语义），环在建表顺序上
自然断开：`production_plans` 先建，`disruptions` 后建并保留它那条 NOT NULL 外键。选择牺牲
可空的一侧而保住非空的一侧，是因为「扰动必须指向一个真实计划」比「计划可以回指扰动」
更需要库级强制。该列的引用完整性由 `services/` 层在写入点校验。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束命名规范。SQLite 上的后续迁移要走 `render_as_batch`（重建表再拷数据），
# 那条路径必须能按名字引用约束；匿名约束会让 batch 迁移在删改约束时失败。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部模型的基类。`metadata` 是 schema 的唯一真源。

    `migrations/env.py` 的 `target_metadata` 指向它；
    `tests/structure/test_schema_matches_models.py` 断言 Alembic 迁移建出的
    schema 与它逐表逐列一致，防止模型与迁移从第一天起就漂移。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------
# 取值域常量
#
# design.md 的 DDL 只对少数列写了 CHECK，其余枚举列是 TEXT + 注释。这里把取值域
# 提成 Python 常量而不是全部下沉成库级 CHECK：取值域的权威校验点在 API 边界的
# Pydantic `Literal`（R27.5），库级 CHECK 会在演示期造成「改一个取值要跑迁移」的
# 摩擦。真正结构性的约束（唯一性、区间、状态唯一）仍然进 DDL——见各表定义。
# --------------------------------------------------------------------------

RECORD_SOURCES = ("SPREADSHEET_IMPORT", "MANUAL_ENTRY", "SEED_DATA")
RECORD_STATUSES = ("ACTIVE", "REVERTED")

MACHINE_STATUSES = ("AVAILABLE", "BUSY", "DOWN", "MAINTENANCE")
DOWNTIME_REASONS = ("BREAKDOWN", "MAINTENANCE")

ORDER_PRIORITIES = ("URGENT", "HIGH", "NORMAL", "LOW")
#: 排产主循环的确定性全序（design.md §3.1.2，R8.5）。
PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

PLAN_STATUSES = ("DRAFT", "PENDING_APPROVAL", "ACTIVE", "REJECTED", "SUPERSEDED")
FEASIBILITIES = ("FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN")
PLAN_ORIGINS = (
    "PLAN_GENERATION",
    "REPLANNING",
    "MODIFY",
    "RISK_MITIGATION",
    "SCENARIO_ADOPTION",
    "AUTO_REVERT",
    "BASELINE",
)
APPROVAL_ACTIONS = ("APPROVE", "REJECT", "MODIFY", "CANCEL")

IMPACT_CLASSES = ("IMPACT_MINOR", "IMPACT_MODERATE", "IMPACT_MAJOR")
AUTONOMY_LEVELS = ("L1", "L2", "L3", "L4", "L5")
#: `AUTO_APPLIED` 在 P0 恒不出现（L4 属 P1），但取值域此刻就保留：
#: 数据模型属 P0，行为属 P1，这样 P1-J 落地时无需迁移（tasks.md 1.2）。
EXECUTION_PATHS = ("AUTO_APPLIED", "PROPOSED", "ESCALATED")

RISK_SEVERITIES = ("INFO", "WARNING", "CRITICAL")
NARRATIVE_SOURCES = ("LLM", "TEMPLATE")

BATCH_STATUSES = ("AWAITING_CONFIRMATION", "COMMITTED", "REVERTED", "ABANDONED")
SNAPSHOT_TRIGGERS = ("IMPORT_COMMIT", "MANUAL_EDIT", "DISRUPTION", "REVERT", "SEED")

TRACE_MODES = ("PIPELINE", "REACT")
TRIGGER_SOURCES = ("PLANNER_UI", "SCHEDULED", "DATA_CHANGE_EVENT", "RISK_SCAN")
TRACE_OUTCOMES = (
    "OK",
    "MAX_STEPS_EXCEEDED",
    "TOKEN_BUDGET_EXCEEDED",
    "LLM_UNAVAILABLE",
    "VALIDATION_FAILED",
    "ERROR",
)
STEP_KINDS = ("LLM_CALL", "TOOL_CALL", "DETERMINISTIC_STAGE", "GUARDRAIL")
TOOL_OUTCOMES = ("OK", "TOOL_NOT_PERMITTED", "TOOL_INPUT_INVALID", "ERROR")

#: design.md §3.1.6 / R6.1 的 9 类阻塞原因。
BLOCKING_REASONS = (
    "MATERIAL_INSUFFICIENT",
    "MACHINE_UNAVAILABLE",
    "MACHINE_CAPABILITY_MISMATCH",
    "WORKER_SKILL_MISMATCH",
    "WORKER_UNAVAILABLE",
    "PREDECESSOR_UNSCHEDULABLE",
    "SHIFT_WINDOW_EXCEEDED",
    "DUE_DATE_UNREACHABLE",
    "INVALID_ROUTING",
)

#: `after_flush` 钩子监听的 11 张规划相关表（任务 1.3，design.md Data Models §6）。
#: 任一表发生 INSERT/UPDATE/DELETE 即推进 `input_snapshot_version`。
PLANNING_RELEVANT_TABLES = frozenset(
    {
        "orders",
        "products",
        "operations",
        "product_materials",
        "materials",
        "incoming_deliveries",
        "machines",
        "machine_downtime",
        "changeover_rules",
        "workers",
        "worker_absences",
    }
)


# 复用的列工厂。`source` / `record_status` / `import_batch_id` / `last_updated_at`
# 这组「来源追溯四件套」出现在 5 张实体表上（R3.3）。
def _pk() -> Mapped[str]:
    return mapped_column(String, primary_key=True)


# --------------------------------------------------------------------------
# §7 可观测性（先定义：traces 被 import_batches 与 production_plans 引用）
# --------------------------------------------------------------------------


class Trace(Base):
    """一次编排的完整记录（R24.1）。`mode` 区分两种执行形态（ADR-002）。"""

    __tablename__ = "traces"

    trace_id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    # REACT 时为 Agent 名，PIPELINE 时为 NULL——形态 A 没有 Agent 在循环里。
    agent: Mapped[str | None] = mapped_column(String)
    trigger_source: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str | None] = mapped_column(String)
    step_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    total_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    total_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    estimated_usd: Mapped[object] = mapped_column(
        Numeric, nullable=False, default=0, server_default=text("0")
    )
    # plan_id / batch_id / scenario_id。刻意不设外键：指向哪张表由 `kind` 决定。
    result_ref: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        # `mode != 'REPLAY'` 的行数是 PROJECT_REAL_RUN_CAP = 150 的计数依据，
        # 启动时要查，因此建索引。
        Index("ix_traces_mode_started", "mode", "started_at"),
    )


class TraceStep(Base):
    """Trace 内的一步。`decision_reason` 是结构化摘要，不是原始推理链（R24.7）。"""

    __tablename__ = "trace_steps"

    step_id: Mapped[str] = _pk()
    trace_id: Mapped[str] = mapped_column(
        String, ForeignKey("traces.trace_id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_kind: Mapped[str] = mapped_column(String, nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(String)
    input_digest: Mapped[str | None] = mapped_column(String)
    output_digest: Mapped[str | None] = mapped_column(String)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, default=0, server_default=text("0"))
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=0, server_default=text("0"))

    __table_args__ = (
        UniqueConstraint("trace_id", "step_index", name="uq_trace_steps_trace_index"),
    )


class ToolCall(Base):
    """每次工具调用的记录（R22.11）。`caller` 是白名单判定的依据。"""

    __tablename__ = "tool_calls"

    call_id: Mapped[str] = _pk()
    trace_id: Mapped[str] = mapped_column(String, ForeignKey("traces.trace_id"), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String, ForeignKey("trace_steps.step_id"))
    caller: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    args_digest: Mapped[str] = mapped_column(String, nullable=False)
    args_json: Mapped[object | None] = mapped_column(JSON)
    result_summary: Mapped[str] = mapped_column(String, nullable=False)
    result_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_tool_calls_trace_id", "trace_id"),)


# --------------------------------------------------------------------------
# §6 快照版本与并发
# --------------------------------------------------------------------------


class InputSnapshot(Base):
    """单调递增的输入版本号（R12.1）。

    **它不记录逐字段差异。** `entity_change_log` 已按 requirements 第 3 节拒绝清单
    移出范围：审批时唯一需要回答的问题是「提案生成之后数据是否变过」，版本号比对
    就能回答（design.md Data Models §6）。

    `sqlite_autoincrement=True` 是必需的：没有它 SQLite 会复用已删除行的 rowid，
    版本号就不再单调。PG 下该参数被忽略，序列天然单调。
    """

    __tablename__ = "input_snapshots"

    snapshot_version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    # 规划相关表的行摘要哈希，用于快速比对。
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = {"sqlite_autoincrement": True}


# --------------------------------------------------------------------------
# §5 摄取与来源追溯
# --------------------------------------------------------------------------


class ImportBatch(Base):
    """一次表格导入（R3.2）。

    `proposed_mapping` 与 `accepted_mapping` 分开存是审批闸门的一部分：LLM 的原始
    提案只供审计与 UI 展示，**只有人工确认后的 `accepted_mapping` 会被 `commit_batch`
    消费**（R3 的人工确认要求）。
    """

    __tablename__ = "import_batches"

    batch_id: Mapped[str] = _pk()
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    # sha256，重复上传检测（R3.6）。
    file_checksum: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    proposed_mapping: Mapped[object] = mapped_column(JSON, nullable=False)
    accepted_mapping: Mapped[object | None] = mapped_column(JSON)
    operator_decisions: Mapped[object | None] = mapped_column(JSON)
    normalisations: Mapped[object | None] = mapped_column(JSON)
    # 行号 / 列名 / 原始值（R2.8）。
    unparsed_cells: Mapped[object] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    # R2.10：公式列只取缓存值，并在报告中标注。
    formula_columns: Mapped[object] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    ingestion_report: Mapped[object | None] = mapped_column(JSON)
    trace_id: Mapped[str | None] = mapped_column(String, ForeignKey("traces.trace_id"))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_batch_checksum", "file_checksum"),)


class ImportRowProvenance(Base):
    """每条落库记录的来源行（R3.3）。

    `overwritten_payload` 存覆盖 `MANUAL_ENTRY` 时的旧值，整批回滚要靠它还原
    （R3.4–5）——没有它，回滚只能删除而不能恢复被覆盖的手工录入。
    """

    __tablename__ = "import_row_provenance"

    id: Mapped[str] = _pk()
    batch_id: Mapped[str] = mapped_column(
        String, ForeignKey("import_batches.batch_id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # 原始行，不受信任（R23.1）。
    raw_row: Mapped[object] = mapped_column(JSON, nullable=False)
    overwritten_payload: Mapped[object | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint(
            "batch_id", "entity_type", "entity_id", name="uq_provenance_batch_entity"
        ),
    )


# --------------------------------------------------------------------------
# §2 领域实体
# --------------------------------------------------------------------------


class Product(Base):
    """产品。`description` 不受信任（R23.1），装配提示词时须被 `<untrusted>` 包裹。"""

    __tablename__ = "products"

    product_id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, nullable=False)
    record_status: Mapped[str] = mapped_column(
        String, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    import_batch_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("import_batches.batch_id")
    )
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Operation(Base):
    """工序（R4.1）。1–3 道，线性链，无返工。

    两条约束**必须在 DDL 里**（tasks.md 1.2）：`CHECK (sequence BETWEEN 1 AND 3)`
    与 `UNIQUE (product_id, sequence)`。后者在结构上阻止重复 sequence（R4.7），
    使 `INVALID_ROUTING` 成为无法通过写入路径构造出来的状态，而不只是运行期校验。
    """

    __tablename__ = "operations"

    operation_id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(
        String, ForeignKey("products.product_id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    required_machine_type: Mapped[str] = mapped_column(String, nullable=False)
    # 可空；非空时须 ⊆ machine.capabilities。
    required_capability: Mapped[str | None] = mapped_column(String)
    required_worker_skill: Mapped[str] = mapped_column(String, nullable=False)
    # 分钟/件。Numeric 而非 Float——见模块 docstring 第 3 条。
    base_processing_time_per_unit: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # 分钟，与 changeover 相加得 ScheduledJob.setup_minutes（R4.4）。
    setup_time: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    __table_args__ = (
        CheckConstraint("sequence BETWEEN 1 AND 3", name="sequence_range"),
        UniqueConstraint("product_id", "sequence", name="uq_operations_product_sequence"),
    )


class Material(Base):
    """物料。可用量 = `quantity_available - reserved_quantity` + 到期在途（R6.3）。"""

    __tablename__ = "materials"

    material_id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    quantity_available: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    reserved_quantity: Mapped[object] = mapped_column(
        Numeric, nullable=False, default=0, server_default=text("0")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    record_status: Mapped[str] = mapped_column(
        String, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    import_batch_id: Mapped[str | None] = mapped_column(String)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IncomingDelivery(Base):
    """在途到货（R6.3）。`confirmed = false` 的 ETA 进入解释的假设清单（R10.4）。"""

    __tablename__ = "incoming_deliveries"

    delivery_id: Mapped[str] = _pk()
    material_id: Mapped[str] = mapped_column(
        String, ForeignKey("materials.material_id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    eta: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_incoming_material_eta", "material_id", "eta"),)


class ProductMaterial(Base):
    """单层 BOM（R4 限定，无多层展开）。复合主键，无代理键。"""

    __tablename__ = "product_materials"

    product_id: Mapped[str] = mapped_column(
        String, ForeignKey("products.product_id"), primary_key=True
    )
    material_id: Mapped[str] = mapped_column(
        String, ForeignKey("materials.material_id"), primary_key=True
    )
    quantity_per_unit: Mapped[Decimal] = mapped_column(Numeric, nullable=False)


class Machine(Base):
    """机器。`rate_multiplier` 是 R4.5 的产能倍率，必须为正。"""

    __tablename__ = "machines"

    machine_id: Mapped[str] = _pk()
    machine_type: Mapped[str] = mapped_column(String, nullable=False)
    # list[str]。JSON 而非关联表：capabilities 只做集合包含判断，不需要查询能力。
    capabilities: Mapped[object] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    available_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    available_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    rate_multiplier: Mapped[object] = mapped_column(
        Numeric, nullable=False, default=1, server_default=text("1.0")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    record_status: Mapped[str] = mapped_column(
        String, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    import_batch_id: Mapped[str | None] = mapped_column(String)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # 除以 rate_multiplier 是 R4.5 的算式，零或负数会让排产除零或产生负工期。
        CheckConstraint("rate_multiplier > 0", name="rate_multiplier_positive"),
    )


class ChangeoverRule(Base):
    """换型规则（R4.4）。

    `specificity` 是查表的排序键：3=精确 `(machine, from, to)`、2=机器默认、
    1=全局。查表按降序取首条（design.md §3.1.4），因此规则集不需要「最长匹配」
    这类运行期推断——优先级已经物化成一个可排序的整数列。
    """

    __tablename__ = "changeover_rules"

    rule_id: Mapped[str] = _pk()
    # NULL = 适用全部机器。
    machine_id: Mapped[str | None] = mapped_column(String, ForeignKey("machines.machine_id"))
    from_product_id: Mapped[str | None] = mapped_column(String)  # NULL = 通配
    to_product_id: Mapped[str | None] = mapped_column(String)  # NULL = 通配
    changeover_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    specificity: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("changeover_minutes >= 0", name="changeover_non_negative"),
        Index("ix_changeover_lookup", "machine_id", "from_product_id", "to_product_id"),
    )


class Worker(Base):
    """工人。单班次建模（Open Question 1 的当前答案）。"""

    __tablename__ = "workers"

    worker_id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    skills: Mapped[object] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    shift_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    shift_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    record_status: Mapped[str] = mapped_column(
        String, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    import_batch_id: Mapped[str | None] = mapped_column(String)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Order(Base):
    """订单。

    `due_date` 与 `promised_date` **明确区分**（Open Question 2）：前者是客户期望日，
    后者是我们对外承诺日（可空，空表示尚未承诺）。R13 的 `IMPACT_MINOR` 判据
    「不改变任何订单的 `promised_date`」因此有明确语义——`promised_date IS NULL`
    的订单不可能触发该判据。

    `notes` 不受信任（R23.1）。`injection_suspected` 由 `Guardrail_Layer` 置位，
    UI 显示徽章；它是数据列而非日志字段，因为徽章要在列表页直接渲染。
    """

    __tablename__ = "orders"

    order_id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(
        String, ForeignKey("products.product_id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    promised_date: Mapped[datetime | None] = mapped_column(DateTime)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[str | None] = mapped_column(String)
    injection_suspected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    record_status: Mapped[str] = mapped_column(
        String, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    import_batch_id: Mapped[str | None] = mapped_column(String)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # 主循环的排序键（design.md §3.1.2）。
        Index("ix_orders_priority_due", "priority", "due_date", "order_id"),
    )


# --------------------------------------------------------------------------
# §3 计划与排产
# --------------------------------------------------------------------------


class ProductionPlan(Base):
    """生产计划。状态机见 design.md Data Models §8。

    两个版本列不是重复：`plan_version` 是业务版本（MODIFY 生成新版本时递增），
    `version` 是乐观并发控制的行版本（R12.7）——
    `UPDATE ... SET version=version+1 WHERE plan_id=? AND version=?`，
    `rowcount == 0` 即 `CONCURRENT_MODIFICATION`。

    `ux_active_per_day` 与 `ux_pending_per_day` 两个**部分唯一索引**是 R11.8 与
    R12.6 的结构性强制：同一 `production_date` 上最多一个 `ACTIVE`、最多一个
    `PENDING_APPROVAL`。这两条必须在 DDL 里（tasks.md 1.2）——放在应用层会在并发
    审批下漏判，而属性 15 要求「任一 production_date 上 ACTIVE 计划数恒 ≤ 1」。

    `BASELINE` 计划也是本表的行（`status = DRAFT`、`origin = 'BASELINE'`），永不进入
    审批流，因此不受这两个部分唯一索引影响。
    """

    __tablename__ = "production_plans"

    plan_id: Mapped[str] = _pk()
    production_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    feasibility: Mapped[str] = mapped_column(String, nullable=False)
    plan_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    input_snapshot_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("input_snapshots.snapshot_version"), nullable=False
    )
    origin: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_plan_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("production_plans.plan_id")
    )
    superseded_by_plan_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("production_plans.plan_id")
    )
    # R24.5：计划可回溯到生成它的那次编排。
    generated_by_trace_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("traces.trace_id")
    )
    # 无库级外键——打破 production_plans ↔ disruptions 的环，见模块 docstring。
    disruption_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 不受信任（R23.1）：由规划员自由输入。
    rejection_reason: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index(
            "ux_active_per_day",
            "production_date",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ux_pending_per_day",
            "production_date",
            unique=True,
            sqlite_where=text("status = 'PENDING_APPROVAL'"),
            postgresql_where=text("status = 'PENDING_APPROVAL'"),
        ),
        Index("ix_plans_date_status", "production_date", "status"),
    )


class ProductionJob(Base):
    """作业：订单 × 工序的展开结果（R4.2）。

    `job_id` 是确定性的 `"{order_id}-OP{sequence}"`（design.md §3.1.1），不是 UUID
    ——属性 1 要求同输入两次运行产出逐字段相同的结果，随机 ID 会直接破坏它。
    """

    __tablename__ = "production_jobs"

    job_id: Mapped[str] = _pk()
    order_id: Mapped[str] = mapped_column(String, ForeignKey("orders.order_id"), nullable=False)
    product_id: Mapped[str] = mapped_column(String, nullable=False)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # R4.2 线性链：每道工序最多一个前驱。
    predecessor_job_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("production_jobs.job_id")
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    required_machine_type: Mapped[str] = mapped_column(String, nullable=False)
    required_worker_skill: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("order_id", "operation_sequence", name="uq_jobs_order_sequence"),
    )


class ScheduledJob(Base):
    """已排产作业：唯一记录「谁、在哪台机器、什么时候」的表。

    `setup_minutes` 与 `changeover_minutes` 都存：前者是 `op.setup_time + changeover`
    的合计（R4.4），后者单独留存供 `total_changeover_minutes` 目标分量使用。只存合计
    的话，目标评分就无法拆出换型分量。

    `UNIQUE (plan_id, job_id)` 与 `CHECK (end_time > start_time)` 必须在 DDL 里
    （tasks.md 1.2）：前者阻止同一计划里一个作业被排两次，后者阻止零长或负长区间
    ——两者都是 `Constraint_Validator` 之外的第二道结构性保险。
    """

    __tablename__ = "scheduled_jobs"

    scheduled_job_id: Mapped[str] = _pk()
    plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_jobs.job_id"), nullable=False
    )
    machine_id: Mapped[str] = mapped_column(
        String, ForeignKey("machines.machine_id"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String, ForeignKey("workers.worker_id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    setup_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    changeover_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # MODIFY 的 LOCK_JOB（R11.5）：重排时该作业进入冻结集。
    locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "job_id", name="uq_scheduled_plan_job"),
        CheckConstraint("end_time > start_time", name="end_after_start"),
        # 硬约束校验要按 (计划, 机器) 扫时间序找重叠，这是它的驱动索引。
        Index("ix_sched_plan_machine_start", "plan_id", "machine_id", "start_time"),
        Index("ix_sched_plan_worker_start", "plan_id", "worker_id", "start_time"),
    )


class UnschedulableJob(Base):
    """不可排产作业及其量化解封条件（R8.3）。

    `unblock_suggestion` 是 JSON 而非若干定型列：9 类 `blocking_reason` 各有不同的
    量化字段（design.md §3.1.6 的表），拉平成列会得到一张大部分为 NULL 的宽表。
    属性 4 断言该 JSON 至少含一个数值型字段。
    """

    __tablename__ = "unschedulable_jobs"

    id: Mapped[str] = _pk()
    plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_jobs.job_id"), nullable=False
    )
    # R6.1 的 9 类之一，取值域见 BLOCKING_REASONS。
    blocking_reason: Mapped[str] = mapped_column(String, nullable=False)
    unblock_suggestion: Mapped[object] = mapped_column(JSON, nullable=False)

    __table_args__ = (UniqueConstraint("plan_id", "job_id", name="uq_unschedulable_plan_job"),)


class ObjectiveBreakdown(Base):
    """目标评分的逐分量拆解。`plan_id` 既是主键也是外键：一个计划恰好一份拆解。"""

    __tablename__ = "objective_breakdowns"

    plan_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("production_plans.plan_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 7 个 ComponentScore。
    components: Mapped[object] = mapped_column(JSON, nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # 本次使用的权重，含偏好规则覆盖后的值——存下来才能复算复核。
    weights: Mapped[object] = mapped_column(JSON, nullable=False)
    # 逐 rule_id 的贡献（R7.6）。
    preference_contributions: Mapped[object] = mapped_column(JSON, nullable=False)
    weight_overrides_applied: Mapped[object] = mapped_column(JSON, nullable=False)


class BaselineComparison(Base):
    """与 FCFS 基线的对比（R5.4、R19.2）。

    `snapshot_version` 独立存一份是刻意的冗余：属性 37 断言它等于
    `plan.input_snapshot_version`，即基线与正式计划**同输入同口径**。不存这一列，
    「口径是否一致」就只能靠推断而不能被断言。
    """

    __tablename__ = "baseline_comparisons"

    plan_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("production_plans.plan_id", ondelete="CASCADE"),
        primary_key=True,
    )
    baseline_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    on_time_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    baseline_on_time_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    total_tardiness_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_total_tardiness_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    late_order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_late_order_count: Mapped[int] = mapped_column(Integer, nullable=False)


# --------------------------------------------------------------------------
# §4 决策、自主与审批
# --------------------------------------------------------------------------


class PlanApproval(Base):
    """审批动作记录（R11.9）。

    `revalidation_result` 存审批时重校验的**完整**结果，不是布尔。R11 要求审批不是
    盖章：批准时要重跑硬约束校验，而「当时校验到了什么」是事后追责的唯一凭据。
    """

    __tablename__ = "plan_approvals"

    approval_id: Mapped[str] = _pk()
    plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String)
    revalidation_result: Mapped[object] = mapped_column(JSON, nullable=False)
    # MODIFY 的 5 类结构化修改。
    modifications: Mapped[object | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_approvals_plan", "plan_id", "timestamp"),)


class Disruption(Base):
    """扰动登记（R9.1 的 5 类）。

    `active_plan_id` 保留库级外键（NOT NULL）——扰动必须指向一个真实计划。
    环的另一侧 `production_plans.disruption_id` 让出了外键，见模块 docstring。
    """

    __tablename__ = "disruptions"

    disruption_id: Mapped[str] = _pk()
    type: Mapped[str] = mapped_column(String, nullable=False)
    # 判别联合，结构化。5 类扰动各有不同载荷，因此是 JSON 而非定型列。
    payload: Mapped[object] = mapped_column(JSON, nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    active_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    # R9.9：扰动可回溯到处理它的那次编排。
    trace_id: Mapped[str | None] = mapped_column(String, ForeignKey("traces.trace_id"))


class MachineDowntime(Base):
    """机器故障 / 保养时间窗，扰动登记后写入。

    `disruption_id` 可空：seed 数据里的计划保养没有对应扰动，而
    `MACHINE_BREAKDOWN` 类扰动登记时会写入一行并回指自己。排产的
    `candidate_machines` 直接查本表排除落在窗内的机器（design.md §3.1.2）。
    """

    __tablename__ = "machine_downtime"

    downtime_id: Mapped[str] = _pk()
    machine_id: Mapped[str] = mapped_column(
        String, ForeignKey("machines.machine_id"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # BREAKDOWN | MAINTENANCE
    reason: Mapped[str] = mapped_column(String, nullable=False)
    disruption_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("disruptions.disruption_id")
    )

    __table_args__ = (
        CheckConstraint("end_time > start_time", name="end_after_start"),
        Index("ix_downtime_machine_start", "machine_id", "start_time"),
    )


class WorkerAbsence(Base):
    """工人缺勤时间窗。与 `MachineDowntime` 同构，同样由扰动登记写入。"""

    __tablename__ = "worker_absences"

    absence_id: Mapped[str] = _pk()
    worker_id: Mapped[str] = mapped_column(String, ForeignKey("workers.worker_id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    disruption_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("disruptions.disruption_id")
    )

    __table_args__ = (
        CheckConstraint("end_time > start_time", name="end_after_start"),
        Index("ix_absence_worker_start", "worker_id", "start_time"),
    )


class ImpactAssessment(Base):
    """影响分级与自主等级裁决（R13.12）。

    `decisive_predicates` 与 `impact_input` 都存，是「决策可复核」的实现：前者是
    触发该等级的具体判据字符串，后者是 `ImpactInput` 的 7 个字段原值。有了后者，
    任何人都能离线复算一遍分级结果，而不必相信当时的输出。

    `execution_path` 保留 `AUTO_APPLIED` 取值但 P0 恒不出现——L4 行为属 P1，
    数据模型属 P0（tasks.md 1.2）。
    """

    __tablename__ = "impact_assessments"

    assessment_id: Mapped[str] = _pk()
    candidate_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    baseline_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    disruption_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("disruptions.disruption_id")
    )
    impact_class: Mapped[str] = mapped_column(String, nullable=False)
    autonomy_level: Mapped[str] = mapped_column(String, nullable=False)
    decisive_predicates: Mapped[object] = mapped_column(JSON, nullable=False)
    impact_input: Mapped[object] = mapped_column(JSON, nullable=False)
    execution_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AutoAppliedChange(Base):
    """L4 自动应用的变更记录。**P1 功能，P0 建表**（tasks.md 1.2）。

    P0 运行期该表恒为空。建表放在 P0 是为了让 P1-J 落地时不需要迁移——
    `snapshot_before` / `snapshot_after` 两个 JSON 列存变更前后完整的
    `scheduled_jobs` 列表，一键回滚靠它们还原。
    """

    __tablename__ = "auto_applied_changes"

    change_id: Mapped[str] = _pk()
    assessment_id: Mapped[str] = mapped_column(
        String, ForeignKey("impact_assessments.assessment_id"), nullable=False
    )
    plan_id_before: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    plan_id_after: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    snapshot_before: Mapped[object] = mapped_column(JSON, nullable=False)
    snapshot_after: Mapped[object] = mapped_column(JSON, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reverted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime)
    revert_plan_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("production_plans.plan_id")
    )


class PlannerDecision(Base):
    """规划员决策（R18.1–2），偏好蒸馏的证据来源。

    与 `plan_approvals` 的区别：后者是审批闸门的执行记录（含重校验结果），本表是
    **可供学习的决策语料**（含当时的目标拆解快照）。分开存是因为两者的读者不同
    ——前者服务审计，后者服务偏好蒸馏，且后者要求至少 2 条同向证据才能生成候选规则
    （R18.10）。
    """

    __tablename__ = "planner_decisions"

    decision_id: Mapped[str] = _pk()
    plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("production_plans.plan_id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String)  # 不受信任
    modifications: Mapped[object | None] = mapped_column(JSON)
    objective_breakdown_snapshot: Mapped[object] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PreferenceRule(Base):
    """偏好规则（R18.5、R18.11）。

    `enabled` 的默认值是 `false`，且这是 R18.4 的硬要求：蒸馏出的候选规则一律未启用，
    只有显式人工确认能启用。属性 10 断言「不存在任何调用序列能使某条规则在没有显式
    人工确认的情况下变为 `enabled = true`」，因此这个默认值不是便利，是安全边界。
    """

    __tablename__ = "preference_rules"

    rule_id: Mapped[str] = _pk()
    human_text: Mapped[str] = mapped_column(String, nullable=False)
    # 4 类判别联合之一。偏好规则只能影响打分，永不放宽硬约束（属性 10a）。
    structured_form: Mapped[object] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # source_decision_ids < 2 条（R18.10）。
    low_evidence: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        # 启用规则数恒 ≤ 20（属性 10）。上限的强制在服务层——SQLite 无法表达
        # 「满足条件的行数 ≤ N」，这里建索引让那次计数是 O(log n)。
        Index("ix_pref_rules_enabled", "enabled"),
    )


class PreferenceRuleSource(Base):
    """`source_decision_ids` 的规范化形式。复合主键天然去重。"""

    __tablename__ = "preference_rule_sources"

    rule_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("preference_rules.rule_id", ondelete="CASCADE"),
        primary_key=True,
    )
    decision_id: Mapped[str] = mapped_column(
        String, ForeignKey("planner_decisions.decision_id"), primary_key=True
    )


class RiskFinding(Base):
    """风险发现（R14.9）。

    `finding_key = sha1(risk_type|entity_type|entity_id)` 且 UNIQUE：同一风险在每日
    扫描中反复出现时更新 `last_seen_at` 而不是插新行，否则风险面板会被重复项淹没。

    `narrative_source` 区分 `LLM` 与 `TEMPLATE`，使降级模式下的叙述来源在 UI 上可辨识
    （R14.11）。
    """

    __tablename__ = "risk_findings"

    finding_id: Mapped[str] = _pk()
    finding_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    risk_type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    affected_order_ids: Mapped[object] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    narrative: Mapped[str | None] = mapped_column(String)
    narrative_source: Mapped[str | None] = mapped_column(String)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    mitigation_plan_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("production_plans.plan_id")
    )

    __table_args__ = (Index("ix_risk_unresolved", "resolved_at", "severity"),)


# --------------------------------------------------------------------------
# §7 可观测性（续）
# --------------------------------------------------------------------------


class AuditLog(Base):
    """审计日志（R24.3–4），**append-only**。

    刻意的减法：**没有 `entry_hash` / `prev_hash` 列**。加密哈希链已按 requirements
    第 3 节拒绝清单移出范围（tasks.md 1.2）——它防御的威胁模型是「有权限的操作者
    事后改写历史」，而演示是单 Planner 单组织，该威胁不在模型内。

    R24.3 要求的「不提供修改或删除接口」由任务 1.4 的两道机制满足：ORM 层只暴露
    `append()`，以及一个拦截针对本表 UPDATE/DELETE 的 SQLAlchemy 监听器。
    """

    __tablename__ = "audit_log"

    audit_id: Mapped[str] = _pk()
    event_category: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    # PLANNER | SYSTEM | <AGENT_NAME>
    actor: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String)
    subject_id: Mapped[str | None] = mapped_column(String)
    payload: Mapped[object] = mapped_column(JSON, nullable=False)
    # 无外键：审计写入走独立连接（AUDIT_BYPASS），可能先于 trace 行提交。
    trace_id: Mapped[str | None] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_audit_category_time", "event_category", "occurred_at"),
        Index("ix_audit_subject", "subject_type", "subject_id"),
    )


class ValueMetric(Base):
    """价值台账（R19.1）。

    `labels` 与 `metrics` 平行：每个指标字段都带 `MEASURED` / `ESTIMATED` /
    `PROJECTED` 标签。R25.13 要求预测值与实测值并列且**可辨识**，把标签和数值放在
    同一行的两个 JSON 列里，保证二者不会在传递中走散。
    """

    __tablename__ = "value_metrics"

    metric_id: Mapped[str] = _pk()
    plan_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("production_plans.plan_id")
    )
    measured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    metrics: Mapped[object] = mapped_column(JSON, nullable=False)
    labels: Mapped[object] = mapped_column(JSON, nullable=False)


class LlmCache(Base):
    """LLM 响应缓存（R25.7）。`content_hash` 是主键，命中即零 token 支出。"""

    __tablename__ = "llm_cache"

    content_hash: Mapped[str] = mapped_column(String, primary_key=True)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    response_json: Mapped[object] = mapped_column(JSON, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    hit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class Setting(Base):
    """特性开关、权重、模式。键值表，避免为每个开关跑一次迁移。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


#: 全部 35 张表的名字，供结构测试与迁移一致性断言使用。
ALL_TABLE_NAMES = frozenset(Base.metadata.tables)
