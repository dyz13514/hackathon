/**
 * 计划端点的类型化客户端（design.md Components §5「计划」分组，任务 2.12）。
 *
 * 形状逐字对应后端 `app/api/plans.py` 的 `PlanDetailOut`。手写而非 OpenAPI 生成：本任务
 * 只落这几个端点，一份紧贴后端契约的手写类型比引入代码生成链更轻。后续端点批量落地时
 * 再切换到生成式客户端（`api/client.ts` 的模块注释）。
 */

import { apiFetch } from './client';

export interface ScheduledJob {
  readonly job_id: string;
  readonly order_id: string;
  readonly product_id: string;
  readonly operation_sequence: number;
  readonly machine_id: string;
  readonly worker_id: string;
  /** ISO 8601 起始时刻（含换型段）。 */
  readonly start_time: string;
  readonly end_time: string;
  readonly setup_minutes: number;
  /** 换型分钟，前端据此画斜纹段。 */
  readonly changeover_minutes: number;
}

export interface UnschedulableJob {
  readonly job_id: string;
  readonly order_id: string;
  readonly blocking_reason: string;
  /** 至少含一个量化字段（R8.3）。键随 blocking_reason 而异，因此是开放字典。 */
  readonly unblock_suggestion: Record<string, unknown>;
}

export interface ComponentScore {
  readonly name: string;
  readonly raw_value: number;
  readonly weight: number;
  readonly weighted_contribution: number;
}

export interface ObjectiveBreakdown {
  readonly components: readonly ComponentScore[];
  readonly total_score: number;
  readonly preference_contributions: readonly string[];
  readonly weight_overrides_applied: readonly string[];
}

export interface BaselineComparison {
  readonly baseline_plan_id: string;
  readonly snapshot_version: number;
  readonly on_time_rate: number;
  readonly baseline_on_time_rate: number;
  readonly total_tardiness_minutes: number;
  readonly baseline_total_tardiness_minutes: number;
  readonly late_order_count: number;
  readonly baseline_late_order_count: number;
}

export interface PlanDetail {
  readonly plan_id: string;
  readonly production_date: string;
  readonly status: string;
  readonly plan_version: number;
  readonly origin: string;
  readonly input_snapshot_version: number;
  readonly generated_by_trace_id: string | null;
  readonly feasibility: string;
  readonly scheduled_jobs: readonly ScheduledJob[];
  readonly unschedulable_jobs: readonly UnschedulableJob[];
  readonly objective_breakdown: ObjectiveBreakdown | null;
  readonly baseline_comparison: BaselineComparison | null;
}

export interface PlanSummary {
  readonly plan_id: string;
  readonly production_date: string;
  readonly status: string;
  readonly plan_version: number;
  readonly origin: string;
  readonly feasibility: string;
  readonly input_snapshot_version: number;
  readonly generated_by_trace_id: string | null;
}

/** 生成一个 PENDING_APPROVAL 计划（形态 A 确定性流水线，R5.1）。 */
export function generatePlan(productionDate?: string): Promise<PlanDetail> {
  return apiFetch<PlanDetail>('/plans/generate', {
    method: 'POST',
    body: JSON.stringify(productionDate ? { production_date: productionDate } : {}),
  });
}

/** 计划全文，含 scheduled_jobs 明细（供甘特图，R5.5）。 */
export function getPlan(planId: string): Promise<PlanDetail> {
  return apiFetch<PlanDetail>(`/plans/${encodeURIComponent(planId)}`);
}

/** 待审批计划列表（R1.1、R12.6）。 */
export function listPending(): Promise<PlanSummary[]> {
  return apiFetch<PlanSummary[]>('/plans/pending');
}

/** 当前生效计划列表（R1.1）。 */
export function listActive(): Promise<PlanSummary[]> {
  return apiFetch<PlanSummary[]>('/plans/active');
}

// --------------------------------------------------------------------------
// 审批动作（design.md Components §5「审批」，任务 3.1/3.2；前端接线任务 3.7）
// --------------------------------------------------------------------------

/** 5 类结构化修改（R11.5）。逐字对应后端 `app/services/approval.py` 的 `Modification`。 */
export type Modification =
  | { readonly kind: 'REASSIGN_MACHINE'; readonly job_id: string; readonly machine_id: string }
  | { readonly kind: 'REASSIGN_WORKER'; readonly job_id: string; readonly worker_id: string }
  | { readonly kind: 'MOVE_TIME'; readonly job_id: string; readonly start_time: string }
  | { readonly kind: 'REMOVE_FROM_PLAN'; readonly job_id: string }
  | { readonly kind: 'LOCK_JOB'; readonly job_id: string };

export interface ApproveResult {
  readonly plan_id: string;
  readonly status: string;
}

export interface RejectResult {
  readonly plan_id: string;
  readonly status: string;
}

export interface ModifyResult {
  readonly new_plan_id: string;
  readonly source_plan_id: string;
  readonly status: string;
}

/** 五步审批闸门：唯一能置 ACTIVE 的路径（R11.3、R12）。`STALE_PROPOSAL` 等错误经 `ApiError` 抛出。 */
export function approvePlan(planId: string, expectedVersion: number): Promise<ApproveResult> {
  return apiFetch<ApproveResult>(`/plans/${encodeURIComponent(planId)}/approve`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

/** 拒绝提案：置 REJECTED，理由必填且 ≥5 字符（R11.4，长度由服务端权威判定）。 */
export function rejectPlan(planId: string, rejectionReason: string): Promise<RejectResult> {
  return apiFetch<RejectResult>(`/plans/${encodeURIComponent(planId)}/reject`, {
    method: 'POST',
    body: JSON.stringify({ rejection_reason: rejectionReason }),
  });
}

/** 5 类结构化修改 → 新的 PENDING_APPROVAL 版本（R11.5–7）。 */
export function modifyPlan(
  planId: string,
  modifications: readonly Modification[],
): Promise<ModifyResult> {
  return apiFetch<ModifyResult>(`/plans/${encodeURIComponent(planId)}/modify`, {
    method: 'POST',
    body: JSON.stringify({ modifications }),
  });
}
