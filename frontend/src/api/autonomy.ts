/**
 * L4 自动应用记录与一键回滚端点的类型化客户端（任务 13.4，R13.9/R13.10）。
 *
 * 形状逐字对应后端 `app/api/autonomy.py`。顶栏通知区用 `listChanges` 呈现未回滚的自动应用记录，
 * 并提供 `revertChange` 一键回滚入口。回滚经后端 `Approval_Service.activate_internal()` 的完整
 * 硬约束重校验——回滚也不会产生违规计划（R13.10）。
 */

import { apiFetch } from './client';

/** 一条 L4 自动应用记录（R13.9）。 */
export interface AutoAppliedChange {
  readonly change_id: string;
  readonly assessment_id: string;
  readonly plan_id_before: string;
  readonly plan_id_after: string;
  readonly applied_at: string;
  readonly reverted: boolean;
  readonly reverted_at: string | null;
  readonly revert_plan_id: string | null;
}

export interface AutoAppliedChangeList {
  readonly changes: readonly AutoAppliedChange[];
}

export interface RevertResult {
  readonly change_id: string;
  readonly revert_plan_id: string;
  readonly superseded_plan_id: string;
  readonly status: string;
}

/** 列出全部自动应用记录（最近的在前）。只读，供顶栏通知区。 */
export function listAutoAppliedChanges(): Promise<AutoAppliedChangeList> {
  return apiFetch<AutoAppliedChangeList>('/autonomy/changes');
}

/**
 * 一键回滚一个自动应用变更（写端点，需已登录会话）。
 *
 * 后端从 `snapshot_before` 重建计划并经完整重校验重新激活。可能返回 404（记录不存在）、
 * 409（已回滚过）、422（重建计划重校验失败，回滚中止）——均以 `ApiError` 抛出。
 */
export function revertAutoAppliedChange(changeId: string): Promise<RevertResult> {
  return apiFetch<RevertResult>(`/autonomy/changes/${encodeURIComponent(changeId)}/revert`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}
