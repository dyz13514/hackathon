/**
 * 价值台账端点的类型化客户端（design.md Components §5「台账」、§6 `/value`，任务 7.6 的 K-14 片）。
 *
 * 形状逐字对应后端 `app/api/value_ledger.py` 的 `ValueLedgerOut`。手写而非 OpenAPI 生成，理由同
 * `state.ts`：本任务只落这一个端点。
 */

import { apiFetch } from './client';

/** 一次影响分级裁决的可展示摘要（R13.12）。 */
export interface AutonomyDecision {
  readonly assessment_id: string;
  readonly candidate_plan_id: string;
  readonly impact_class: string;
  readonly autonomy_level: string;
  /** 执行路径：`PROPOSED`（自主提案）或 `ESCALATED`（上报人工）。P0 不出现 `AUTO_APPLIED`。 */
  readonly execution_path: string;
  /** 决定该等级的具体判据，形如 `changed_job_count=3 > 2`（R13.12）。 */
  readonly decisive_predicates: readonly string[];
}

/** MEASURED / ESTIMATED / PROJECTED 标签（R19.4、R19.6、R25.13）。 */
export type MetricLabel = 'MEASURED' | 'ESTIMATED' | 'PROJECTED';

/** 完整 ValueMetrics（design.md §4.4，R19.1）。逐字对应后端 `ValueMetricsOut`。 */
export interface ValueMetrics {
  readonly plan_id: string | null;
  readonly measured_at: string;
  readonly plan_generation_seconds: number | null;
  readonly disruption_response_seconds: number | null;
  readonly on_time_rate: number | null;
  readonly total_tardiness_minutes: number | null;
  readonly churn_ratio: number | null;
  readonly manual_steps_eliminated: number;
  readonly auto_handled_count: number;
  readonly escalated_count: number;
  readonly llm_tokens_used: number;
  readonly estimated_usd_cost: number;
  readonly real_run_count: number;
  readonly project_real_run_cap: number;
  readonly real_run_remaining: number;
  readonly baseline_plan_generation_seconds: number | null;
  readonly baseline_disruption_response_seconds: number | null;
  readonly baseline_on_time_rate: number | null;
  readonly baseline_total_tardiness_minutes: number | null;
  readonly projected_hero_demo_usd: number | null;
  readonly projected_build_total_usd: number | null;
  /** 逐字段 MEASURED/ESTIMATED/PROJECTED 标签。 */
  readonly labels: Readonly<Record<string, MetricLabel>>;
}

/** manual_steps_eliminated 口径表的一行（R19.5）。 */
export interface ManualStepEntry {
  readonly action: string;
  readonly label: string;
  readonly rule: string;
  readonly count: number;
}

/** 一行 KPI（引用 K-01…K-18，R19.4）。字段与 CSV 列一致。 */
export interface KpiRow {
  readonly kpi_id: string;
  readonly metric_name: string;
  readonly current_value: string;
  readonly baseline_value: string;
  readonly delta: string;
  readonly target_value: string;
  readonly label: MetricLabel;
  readonly measured_at: string;
}

/**
 * 价值台账：K-14 自主 vs 上报计数 + 逐条判据 + 当前 ACTIVE 计划的基线 KPI（Task 7.6），
 * 以及完整 ValueMetrics / 口径表 / 带标签的 KPI 行（任务 11.4）。逐字对应后端 `ValueLedgerOut`。
 */
export interface ValueLedger {
  readonly auto_handled_count: number;
  readonly escalated_count: number;
  readonly total_decisions: number;
  /** 自主占比 ∈ [0,1]；无裁决时为 0。 */
  readonly auto_handled_ratio: number;
  readonly decisions: readonly AutonomyDecision[];
  readonly active_plan_id: string | null;
  readonly on_time_rate: number | null;
  readonly baseline_on_time_rate: number | null;
  readonly total_tardiness_minutes: number | null;
  readonly baseline_total_tardiness_minutes: number | null;
  // --- 任务 11.4 新增块 ---
  readonly metrics: ValueMetrics;
  readonly manual_steps: readonly ManualStepEntry[];
  readonly kpis: readonly KpiRow[];
}

/** 价值台账 CSV 导出端点的 URL（R19.8）。用 <a href> 直接下载，credentials 由浏览器带上。 */
export const VALUE_LEDGER_CSV_URL = '/api/value-ledger/export.csv';

/** 价值台账当前值（K-14、R13.12/R13.13）。只读端点，无需认证。 */
export function getValueLedger(): Promise<ValueLedger> {
  return apiFetch<ValueLedger>('/value-ledger');
}
