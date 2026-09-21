/**
 * 审批视图（design.md Components §6 `/approval` 行，任务 3.7）。
 *
 * 逐条对应 §6 的 `/approval` 行与 R7.3 / R8.6 / R11 / R12.4：
 * 1. 计划摘要（plan_id、状态、可行性、input_snapshot_version）；
 * 2. `objective_breakdown` 全分量与权重表（R7.3）——逐分量的原始值、权重、加权贡献；
 * 3. 不可排产作业数与受影响订单的醒目标注（R8.6）；
 * 4. `APPROVE` / `REJECT`（必填理由）/ `MODIFY`（5 类结构化表单，R11.5）；
 * 5. `STALE_PROPOSAL` 时显示「该提案所依赖的输入数据已在提案生成之后发生变化」加两个
 *    版本号，并给「基于最新数据重新生成」入口（R12.4）。
 *
 * 数据流：`GET /plans/pending` 取待审批清单 → 选中一个 → `GET /plans/{id}` 取全文。审批
 * 动作打到 `POST /plans/{id}/approve|reject|modify`（服务端强制审批规则，R11.2）。前端
 * 不做任何授权判断——按钮点了之后，成败由后端说了算。
 *
 * 可访问性（R27.9）：全部控件有 `aria-label`、可键盘到达（原生 button / input / select）；
 * 状态信息（陈旧提案、成功、失败、不可排产标注）除颜色外都附图标字符与文字。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import {
  approvePlan,
  getPlan,
  listPending,
  modifyPlan,
  rejectPlan,
  type Modification,
  type PlanDetail,
  type PlanSummary,
} from '../api/plans';

const MODIFICATION_KINDS = [
  { value: 'REASSIGN_MACHINE', label: 'Reassign machine' },
  { value: 'REASSIGN_WORKER', label: 'Reassign worker' },
  { value: 'MOVE_TIME', label: 'Move time' },
  { value: 'REMOVE_FROM_PLAN', label: 'Remove from plan' },
  { value: 'LOCK_JOB', label: 'Lock job' },
] as const;

type ModificationKind = (typeof MODIFICATION_KINDS)[number]['value'];

interface StaleInfo {
  readonly proposalVersion: number | null;
  readonly currentVersion: number | null;
}

function affectedOrderIds(plan: PlanDetail): string[] {
  return Array.from(new Set(plan.unschedulable_jobs.map((job) => job.order_id))).sort();
}

/** 依据当前修改类型构造一条 `Modification`；缺参数返回 null（表单未填全）。 */
function buildModification(
  kind: ModificationKind,
  jobId: string,
  target: string,
): Modification | null {
  const job = jobId.trim();
  if (!job) {
    return null;
  }
  const value = target.trim();
  switch (kind) {
    case 'REASSIGN_MACHINE':
      return value ? { kind, job_id: job, machine_id: value } : null;
    case 'REASSIGN_WORKER':
      return value ? { kind, job_id: job, worker_id: value } : null;
    case 'MOVE_TIME':
      return value ? { kind, job_id: job, start_time: value } : null;
    case 'REMOVE_FROM_PLAN':
      return { kind, job_id: job };
    case 'LOCK_JOB':
      return { kind, job_id: job };
    default:
      return null;
  }
}

export function Approval() {
  const [pending, setPending] = useState<readonly PlanSummary[]>([]);
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stale, setStale] = useState<StaleInfo | null>(null);

  // REJECT 理由。
  const [rejectionReason, setRejectionReason] = useState('');
  // MODIFY 表单三段。
  const [modKind, setModKind] = useState<ModificationKind>('REASSIGN_MACHINE');
  const [modJobId, setModJobId] = useState('');
  const [modTarget, setModTarget] = useState('');

  const loadPlanDetail = useCallback(async (planId: string) => {
    try {
      const detail = await getPlan(planId);
      setPlan(detail);
    } catch (err) {
      setPlan(null);
      setError(
        err instanceof ApiError ? `Failed to load plan (${err.code}): ${err.message}` : 'Failed to load plan.',
      );
    }
  }, []);

  const loadPending = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listPending();
      setPending(list);
      if (list.length > 0) {
        await loadPlanDetail(list[0]!.plan_id);
      } else {
        setPlan(null);
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Failed to load pending list (${err.code}): ${err.message}`
          : 'Failed to load pending list: backend unavailable.',
      );
    } finally {
      setLoading(false);
    }
  }, [loadPlanDetail]);

  useEffect(() => {
    void loadPending();
  }, [loadPending]);

  const resetActionState = useCallback(() => {
    setError(null);
    setNotice(null);
    setStale(null);
  }, []);

  const onApprove = useCallback(async () => {
    if (!plan) {
      return;
    }
    resetActionState();
    try {
      const result = await approvePlan(plan.plan_id, plan.plan_version);
      setNotice(`✔ Plan ${result.plan_id} activated (${result.status}).`);
      await loadPending();
    } catch (err) {
      if (err instanceof ApiError && err.code === 'STALE_PROPOSAL') {
        setStale({
          proposalVersion: (err.details.proposal_version as number) ?? null,
          currentVersion: (err.details.current_version as number) ?? null,
        });
        return;
      }
      setError(
        err instanceof ApiError ? `Approval failed (${err.code}): ${err.message}` : 'Approval failed.',
      );
    }
  }, [plan, resetActionState, loadPending]);

  const onReject = useCallback(async () => {
    if (!plan) {
      return;
    }
    resetActionState();
    try {
      const result = await rejectPlan(plan.plan_id, rejectionReason);
      setNotice(`✔ Plan ${result.plan_id} rejected (${result.status}).`);
      setRejectionReason('');
      await loadPending();
    } catch (err) {
      setError(
        err instanceof ApiError ? `Rejection failed (${err.code}): ${err.message}` : 'Rejection failed.',
      );
    }
  }, [plan, rejectionReason, resetActionState, loadPending]);

  const onModify = useCallback(async () => {
    if (!plan) {
      return;
    }
    resetActionState();
    const modification = buildModification(modKind, modJobId, modTarget);
    if (!modification) {
      setError('Please enter the job id and the target value required for this modification type.');
      return;
    }
    try {
      const result = await modifyPlan(plan.plan_id, [modification]);
      setNotice(
        `✔ Created new pending version ${result.new_plan_id} (from plan ${result.source_plan_id}).`,
      );
      setModJobId('');
      setModTarget('');
      await loadPending();
    } catch (err) {
      setError(
        err instanceof ApiError ? `Modification failed (${err.code}): ${err.message}` : 'Modification failed.',
      );
    }
  }, [plan, modKind, modJobId, modTarget, resetActionState, loadPending]);

  const breakdown = plan?.objective_breakdown ?? null;
  const affected = plan ? affectedOrderIds(plan) : [];
  const targetLabel =
    modKind === 'REASSIGN_MACHINE'
      ? 'Target machine id'
      : modKind === 'REASSIGN_WORKER'
        ? 'Target worker id'
        : modKind === 'MOVE_TIME'
          ? 'Target start time (ISO 8601)'
          : '';

  return (
    <section aria-labelledby="approval-heading" className="approval">
      <div className="approval-header">
        <h2 id="approval-heading">Approval</h2>
        <button
          type="button"
          onClick={() => void loadPending()}
          disabled={loading}
          aria-busy={loading}
          aria-label="Refresh pending list"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {notice && (
        <p role="status" className="approval-notice">
          {notice}
        </p>
      )}

      {error && (
        <p role="alert" className="approval-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {stale && (
        <div role="alert" className="approval-stale">
          <p>
            <span aria-hidden="true">⚠ </span>
            The input data this proposal relies on has changed since the proposal was generated.
          </p>
          <p className="approval-stale-versions">
            Proposal version: {stale.proposalVersion ?? 'unknown'} · current data version:{' '}
            {stale.currentVersion ?? 'unknown'}
          </p>
          <a className="approval-regenerate" href="/schedule" aria-label="Regenerate from latest data">
            Regenerate from latest data
          </a>
        </div>
      )}

      {!loading && pending.length === 0 && !error && (
        <p className="approval-empty">There are no plans pending approval.</p>
      )}

      {plan && (
        <div className="approval-body">
          <section aria-labelledby="summary-heading" className="approval-summary">
            <h3 id="summary-heading">Plan summary</h3>
            <p>
              <strong>{plan.plan_id}</strong> · status {plan.status} · feasibility{' '}
              <span className={`feasibility feasibility-${plan.feasibility}`}>
                {plan.feasibility}
              </span>{' '}
              · input data version {plan.input_snapshot_version} · v{plan.plan_version}
            </p>
          </section>

          <section
            aria-labelledby="unschedulable-heading"
            className={
              plan.unschedulable_jobs.length > 0
                ? 'approval-unschedulable approval-unschedulable-warn'
                : 'approval-unschedulable'
            }
          >
            <h3 id="unschedulable-heading">
              <span aria-hidden="true">{plan.unschedulable_jobs.length > 0 ? '⚠ ' : '✔ '}</span>
              Unschedulable jobs: {plan.unschedulable_jobs.length}
            </h3>
            {affected.length > 0 ? (
              <p className="approval-affected">Affected orders ({affected.length}): {affected.join(', ')}</p>
            ) : (
              <p>All jobs scheduled; no affected orders.</p>
            )}
          </section>

          <section aria-labelledby="breakdown-heading" className="approval-breakdown">
            <h3 id="breakdown-heading">Objective score breakdown</h3>
            {breakdown ? (
              <>
                <table>
                  <caption className="table-caption">
                    Per-component raw value, weight and weighted contribution (R7.3)
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Component</th>
                      <th scope="col">Raw value</th>
                      <th scope="col">Weight</th>
                      <th scope="col">Weighted contribution</th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdown.components.map((component) => (
                      <tr key={component.name}>
                        <th scope="row">{component.name}</th>
                        <td>{component.raw_value}</td>
                        <td>{component.weight}</td>
                        <td>{component.weighted_contribution}</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr>
                      <th scope="row" colSpan={3}>
                        Total score
                      </th>
                      <td>{breakdown.total_score}</td>
                    </tr>
                  </tfoot>
                </table>
                {breakdown.weight_overrides_applied.length > 0 && (
                  <p className="approval-overrides">
                    Weight overrides applied:{' '}
                    {breakdown.weight_overrides_applied
                      .map((o) => `${o.component} ×${o.multiplier} (${o.rule_id})`)
                      .join(', ')}
                  </p>
                )}
                {breakdown.preference_contributions.length > 0 && (
                  <div className="approval-preferences">
                    <h4>Preference rule impact (R18.7)</h4>
                    <ul>
                      {breakdown.preference_contributions.map((c) => (
                        <li key={c.rule_id}>
                          <span className="pref-rule-text">{c.human_text}</span> ({c.rule_id}):{' '}
                          affected jobs {c.violating_job_ids.join(', ') || '—'}, penalty{' '}
                          {c.weighted_contribution} min-equivalent
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            ) : (
              <p>This plan has no objective score breakdown.</p>
            )}
          </section>

          <section aria-labelledby="actions-heading" className="approval-actions">
            <h3 id="actions-heading">Approval actions</h3>

            <div className="approval-action">
              <button
                type="button"
                onClick={() => void onApprove()}
                aria-label={`Approve plan ${plan.plan_id}`}
              >
                Approve
              </button>
            </div>

            <div className="approval-action">
              <label htmlFor="rejection-reason">Rejection reason (required, at least 5 characters)</label>
              <textarea
                id="rejection-reason"
                value={rejectionReason}
                onChange={(event) => setRejectionReason(event.target.value)}
                aria-label="Rejection reason"
                rows={2}
              />
              <button
                type="button"
                onClick={() => void onReject()}
                disabled={rejectionReason.trim().length < 5}
                aria-label={`Reject plan ${plan.plan_id}`}
              >
                Reject
              </button>
            </div>

            <div className="approval-action approval-modify">
              <h4>Modify</h4>
              <label htmlFor="mod-kind">Modification type</label>
              <select
                id="mod-kind"
                value={modKind}
                onChange={(event) => setModKind(event.target.value as ModificationKind)}
                aria-label="Modification type"
              >
                {MODIFICATION_KINDS.map((kind) => (
                  <option key={kind.value} value={kind.value}>
                    {kind.label}
                  </option>
                ))}
              </select>

              <label htmlFor="mod-job">Job id</label>
              <input
                id="mod-job"
                type="text"
                value={modJobId}
                onChange={(event) => setModJobId(event.target.value)}
                aria-label="Job id"
              />

              {targetLabel && (
                <>
                  <label htmlFor="mod-target">{targetLabel}</label>
                  <input
                    id="mod-target"
                    type="text"
                    value={modTarget}
                    onChange={(event) => setModTarget(event.target.value)}
                    aria-label={targetLabel}
                  />
                </>
              )}

              <button
                type="button"
                onClick={() => void onModify()}
                aria-label={`Submit modification for plan ${plan.plan_id}`}
              >
                Submit modification
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
