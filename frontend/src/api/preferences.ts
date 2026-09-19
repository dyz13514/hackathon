/**
 * 偏好规则管理端点的类型化客户端（design.md Components §5「偏好」、§6 `/preferences`，任务 11.1，R18）。
 *
 * 形状逐字对应后端 `app/api/preferences.py`。手写而非 OpenAPI 生成，理由同 `valueLedger.ts`：
 * 本任务只落这一组端点。
 *
 * **显式启用**（tasks.md 11.1 第 3 点）在客户端也体现为独立函数：`createPreference` 的入参里没有
 * `enabled`，启用/停用只经 `enablePreference` / `disablePreference` 两个专门调用——UI 因此无法在
 * 一次「创建」或「编辑」里静默启用规则。
 */

import { apiFetch } from './client';

/** 4 类偏好规则的判别键，逐字对应后端 `PreferenceForm`。 */
export type PreferenceKind =
  | 'AVOID_MACHINE_FOR_ORDER'
  | 'AVOID_MACHINE_FOR_PRODUCT'
  | 'PREFER_WORKER_FOR_SKILL'
  | 'ADJUST_OBJECTIVE_WEIGHT';

/** 可被 ADJUST_OBJECTIVE_WEIGHT 作用的 6 个软目标分量（后端 `SoftWeightKey`）。 */
export type SoftWeightKey =
  | 'late_order_count'
  | 'total_tardiness_minutes'
  | 'urgent_order_lateness'
  | 'churn_ratio'
  | 'machine_utilisation'
  | 'total_changeover_minutes';

export interface AvoidMachineForOrderForm {
  readonly kind: 'AVOID_MACHINE_FOR_ORDER';
  readonly order_id: string;
  readonly machine_id: string;
  readonly weight_delta?: number;
}

export interface AvoidMachineForProductForm {
  readonly kind: 'AVOID_MACHINE_FOR_PRODUCT';
  readonly product_id: string;
  readonly machine_id: string;
  readonly weight_delta?: number;
}

export interface PreferWorkerForSkillForm {
  readonly kind: 'PREFER_WORKER_FOR_SKILL';
  readonly skill: string;
  readonly worker_id: string;
  readonly weight_delta?: number;
}

export interface AdjustObjectiveWeightForm {
  readonly kind: 'ADJUST_OBJECTIVE_WEIGHT';
  readonly component: SoftWeightKey;
  readonly multiplier: number;
}

export type PreferenceForm =
  | AvoidMachineForOrderForm
  | AvoidMachineForProductForm
  | PreferWorkerForSkillForm
  | AdjustObjectiveWeightForm;

/** 一条偏好规则，逐字对应后端 `PreferenceRuleOut`。 */
export interface PreferenceRule {
  readonly rule_id: string;
  readonly human_text: string;
  readonly structured_form: PreferenceForm;
  readonly kind: PreferenceKind | string;
  readonly enabled: boolean;
  /** source_decision_ids < 2，界面提示证据不足（R18.10）。 */
  readonly low_evidence: boolean;
  readonly created_at: string;
  readonly updated_at: string;
  readonly created_by: string;
  readonly source_decision_ids: readonly string[];
}

/** `GET /preferences` 响应：规则列表 + 启用计数与 20 条上限。 */
export interface PreferenceList {
  readonly items: readonly PreferenceRule[];
  readonly total: number;
  readonly enabled_count: number;
  readonly max_enabled: number;
}

/** `GET /preferences/{id}/affected-jobs` 响应。 */
export interface AffectedJobs {
  readonly rule_id: string;
  readonly plan_id: string | null;
  readonly job_ids: readonly string[];
}

/** 创建请求体。**无 `enabled` 字段**：创建即未启用（R18.4）。 */
export interface CreatePreferenceInput {
  readonly human_text: string;
  readonly structured_form: PreferenceForm;
  readonly source_decision_ids?: readonly string[];
}

/** 编辑请求体。**无 `enabled` 字段**：通用编辑不能启用/停用。 */
export interface UpdatePreferenceInput {
  readonly human_text?: string;
  readonly structured_form?: PreferenceForm;
  readonly source_decision_ids?: readonly string[];
}

export function listPreferences(enabledOnly = false): Promise<PreferenceList> {
  const query = enabledOnly ? '?enabled_only=true' : '';
  return apiFetch<PreferenceList>(`/preferences${query}`);
}

export function createPreference(input: CreatePreferenceInput): Promise<PreferenceRule> {
  return apiFetch<PreferenceRule>('/preferences', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updatePreference(
  ruleId: string,
  input: UpdatePreferenceInput,
): Promise<PreferenceRule> {
  return apiFetch<PreferenceRule>(`/preferences/${encodeURIComponent(ruleId)}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
}

/** 显式启用——独立可审计动作。达 20 条上限时抛 ApiError(code=PREFERENCE_RULE_LIMIT_REACHED)。 */
export function enablePreference(ruleId: string): Promise<PreferenceRule> {
  return apiFetch<PreferenceRule>(`/preferences/${encodeURIComponent(ruleId)}/enable`, {
    method: 'POST',
  });
}

/** 停用——下一次排产完全忽略该规则（R18.9）。 */
export function disablePreference(ruleId: string): Promise<PreferenceRule> {
  return apiFetch<PreferenceRule>(`/preferences/${encodeURIComponent(ruleId)}/disable`, {
    method: 'POST',
  });
}

export function deletePreference(ruleId: string): Promise<void> {
  return apiFetch<void>(`/preferences/${encodeURIComponent(ruleId)}`, {
    method: 'DELETE',
  });
}

export function getAffectedJobs(ruleId: string): Promise<AffectedJobs> {
  return apiFetch<AffectedJobs>(`/preferences/${encodeURIComponent(ruleId)}/affected-jobs`);
}

/**
 * 一条蒸馏出的候选规则（任务 13.3）。**恒 `enabled=false`**——落库时已创建，待人工逐条
 * `enablePreference` 确认。`low_evidence` 为真时界面提示证据不足（R18.10）。
 */
export interface DistilledCandidate {
  readonly rule_id: string;
  readonly human_text: string;
  readonly structured_form: PreferenceForm;
  readonly source_decision_ids: readonly string[];
  readonly enabled: boolean;
  readonly low_evidence: boolean;
}

/** `POST /preferences/distil` 响应（任务 13.3）。 */
export interface DistilResult {
  /** DISTILLED / NO_EVIDENCE / LLM_UNAVAILABLE。 */
  readonly outcome: string;
  readonly candidates: readonly DistilledCandidate[];
  readonly injection_suspected: boolean;
  readonly considered_decision_ids: readonly string[];
}

/**
 * 从历史决策蒸馏候选偏好规则（任务 13.3，R18.3）。候选一律 `enabled=false`，仍须逐条
 * `enablePreference` 人工确认。写端点。
 */
export function distilPreferences(): Promise<DistilResult> {
  return apiFetch<DistilResult>('/preferences/distil', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}
