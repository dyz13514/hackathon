"""审计事件类别常量（R24.4，design.md Data Models §7）。

`audit_log` 有两个分类维度，职责不同：

- **`event_category`**——闭集合，取值只能是本模块定义的常量。R24.4 逐条点名了 14 类，
  design.md 又补了 3 类（`AGENT_RESERVED_KEY_DROPPED`、`EXPLANATION_NUMERIC_MISMATCH`、
  `STALE_PROPOSAL_REJECTED`），成本纪律再加 1 类（`PROJECT_BUDGET_CEILING`，任务 5.4），
  运维侧再加 1 类（`DEMO_RESET`，见其常量注释），合计 19 类。`audit.append()` 在运行期校验取值。
- **`event_type`**——开放字符串，同一类别下的具体事件（例如 `APPROVAL_ACTION` 类别下的
  `APPROVE` / `REJECT` / `MODIFY`）。它是可以自由取值的那一维。

**为什么类别是闭集合。** `GET /api/audit-log` 按类别筛选（任务 5.12），而 `ix_audit_category_time`
索引也建在这一列上。调用点即兴造值（`"approval"`、`"APPROVAL"`、`"APPROVALS"` 各写一遍）会让
筛选静默漏记录——审计日志漏记录比报错更糟，因为没人会发现。因此新增类别必须先在本模块
显式添加常量：这让「引入一个新审计类别」成为一次有意识的决定，而不是顺手的字符串字面量。

本模块**只有常量**，不 import `sqlalchemy` 或任何应用模块，因此任何层都可以安全引用它。
"""

from __future__ import annotations

from typing import Final, Literal, get_args

# --------------------------------------------------------------------------
# R24.4 逐条点名的 14 类
# --------------------------------------------------------------------------

#: 表格导入（R3.2）。
DATA_IMPORT: Final = "DATA_IMPORT"
#: 列映射的人工确认，逐字段记录原建议与选定值（R3.7）。
MAPPING_CONFIRMATION: Final = "MAPPING_CONFIRMATION"
#: 计划生成（R5.1）。
PLAN_GENERATION: Final = "PLAN_GENERATION"
#: 扰动登记（R9.1）。
DISRUPTION_REGISTERED: Final = "DISRUPTION_REGISTERED"
#: 影响分级，载荷含 `decisive_predicates`（R13.12）。
IMPACT_CLASSIFICATION: Final = "IMPACT_CLASSIFICATION"
#: 审批动作 APPROVE / REJECT / MODIFY / CANCEL（R11.9）。
APPROVAL_ACTION: Final = "APPROVAL_ACTION"
#: L4 自动应用（P1；P0 运行期不出现，但类别此刻就在）。
AUTO_APPLY: Final = "AUTO_APPLY"
#: 自动回滚（P1，同上）。
AUTO_REVERT: Final = "AUTO_REVERT"
#: 偏好规则的新增 / 启用 / 停用（R18）。
PREFERENCE_RULE_CHANGE: Final = "PREFERENCE_RULE_CHANGE"
#: 目标权重变更（R7.4）。
WEIGHT_CHANGE: Final = "WEIGHT_CHANGE"
#: 不受信任输入命中注入模式（R23.3）。**不阻断业务流程**，只留痕。
PROMPT_INJECTION_SUSPECTED: Final = "PROMPT_INJECTION_SUSPECTED"
#: 越权工具调用被白名单拒绝（R22.7）。
TOOL_NOT_PERMITTED: Final = "TOOL_NOT_PERMITTED"
#: 沙箱内的写尝试被引擎级监听器阻断（R16.5）。这条记录本身要靠 `AUDIT_BYPASS` 才写得进去。
SANDBOX_WRITE_BLOCKED: Final = "SANDBOX_WRITE_BLOCKED"
#: 切入 / 切出 `DETERMINISTIC_ONLY`（R25.9）。
DEGRADED_MODE_SWITCH: Final = "DEGRADED_MODE_SWITCH"

# --------------------------------------------------------------------------
# design.md 补充的 3 类（tasks.md 1.4 点名要求）
# --------------------------------------------------------------------------

#: Agent 输出里出现保留键并被剥离（R23.5、R13.11）。`impact_class` / `autonomy_level` /
#: `plan_status` 这类字段只能由确定性组件产生，模型声称拥有它们即被记录。
AGENT_RESERVED_KEY_DROPPED: Final = "AGENT_RESERVED_KEY_DROPPED"
#: 解释文本里的数字在载荷中找不到对应（R10.7），随后回退模板解释。
EXPLANATION_NUMERIC_MISMATCH: Final = "EXPLANATION_NUMERIC_MISMATCH"
#: 审批时发现提案依赖的输入版本已变（R12.3）。载荷只有两个版本号。
STALE_PROPOSAL_REJECTED: Final = "STALE_PROPOSAL_REJECTED"

# --------------------------------------------------------------------------
# 运维（tasks.md 1.6）
# --------------------------------------------------------------------------

#: 达到项目级真实运行硬上限（`PROJECT_REAL_RUN_CAP = 150`），拒绝以 `LLM_MODE=LIVE`
#: 启动（design.md 成本章节 ②、ADR-011）。**为什么单立一类而不并进 `DEGRADED_MODE_SWITCH`。**
#: 降级切换记的是「运行期从 LLM 切到确定性」这件事；真实运行配额耗尽记的是「启动被拒绝」
#: ——两件事发生在不同时刻、指向不同的运维动作（前者等 Bedrock 恢复，后者要人决定是否
#: 抬高配额），混进同一类别会让「为什么这次没起来」的排查落到错误的记录上。项目美元
#: 上限达 90% 的自动降级仍走 `DEGRADED_MODE_SWITCH`（它确实是一次运行期切换）。
PROJECT_BUDGET_CEILING: Final = "PROJECT_BUDGET_CEILING"

#: 一键重置演示数据（R28.8）。`POST /api/demo/reset` 清空业务表并重放 seed，
#: **但不清空 `audit_log`**——append-only 的语义不允许删条目，因此「数据被整体换掉了」
#: 这件事只能靠补写一条记录来表达，而这条记录是重置前后审计流的唯一接缝。
#:
#: **为什么新增一个类别而不是复用既有的。** 候选里最像的是 `DATA_IMPORT`，但重置不是
#: 导入：`GET /api/audit-log` 按类别筛选（任务 5.12），把重置混进导入类别会让「这批数据
#: 从哪来」的查询给出错误答案——演示当天最需要区分的恰是「规划员导入的」与「一键重置
#: 铺回去的」。类别是闭集合正是为了让这种混淆不会靠一个字符串字面量悄悄发生（见模块
#: docstring），因此这里按那条规矩显式添加。
DEMO_RESET: Final = "DEMO_RESET"


#: 静态可校验的类别类型。`append()` 的形参用它，拼错的字面量在 mypy strict 下就报错，
#: 不必等到运行期。
AuditCategory = Literal[
    "DATA_IMPORT",
    "MAPPING_CONFIRMATION",
    "PLAN_GENERATION",
    "DISRUPTION_REGISTERED",
    "IMPACT_CLASSIFICATION",
    "APPROVAL_ACTION",
    "AUTO_APPLY",
    "AUTO_REVERT",
    "PREFERENCE_RULE_CHANGE",
    "WEIGHT_CHANGE",
    "PROMPT_INJECTION_SUSPECTED",
    "TOOL_NOT_PERMITTED",
    "SANDBOX_WRITE_BLOCKED",
    "DEGRADED_MODE_SWITCH",
    "AGENT_RESERVED_KEY_DROPPED",
    "EXPLANATION_NUMERIC_MISMATCH",
    "STALE_PROPOSAL_REJECTED",
    "PROJECT_BUDGET_CEILING",
    "DEMO_RESET",
]

#: 运行期校验用的集合。从 `AuditCategory` 派生而非另写一份，两处定义会漂移。
AUDIT_EVENT_CATEGORIES: Final[frozenset[str]] = frozenset(get_args(AuditCategory))
