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

/**
 * 价值台账：K-14 自主 vs 上报计数 + 逐条判据 + 当前 ACTIVE 计划的基线 KPI（如有）。
 * 逐字对应后端 `ValueLedgerOut`。
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
}

/** 价值台账当前值（K-14、R13.12/R13.13）。只读端点，无需认证。 */
export function getValueLedger(): Promise<ValueLedger> {
  return apiFetch<ValueLedger>('/value-ledger');
}
