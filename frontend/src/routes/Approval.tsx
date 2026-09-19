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
  { value: 'REASSIGN_MACHINE', label: '改派机器' },
  { value: 'REASSIGN_WORKER', label: '改派工人' },
  { value: 'MOVE_TIME', label: '移动时间' },
  { value: 'REMOVE_FROM_PLAN', label: '移出本计划' },
  { value: 'LOCK_JOB', label: '锁定作业' },
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
        err instanceof ApiError ? `加载计划失败（${err.code}）：${err.message}` : '加载计划失败。',
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
          ? `加载待审批清单失败（${err.code}）：${err.message}`
          : '加载待审批清单失败：后端不可用。',
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
      setNotice(`✔ 计划 ${result.plan_id} 已激活（${result.status}）。`);
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
        err instanceof ApiError ? `审批失败（${err.code}）：${err.message}` : '审批失败。',
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
      setNotice(`✔ 计划 ${result.plan_id} 已拒绝（${result.status}）。`);
      setRejectionReason('');
      await loadPending();
    } catch (err) {
      setError(
        err instanceof ApiError ? `拒绝失败（${err.code}）：${err.message}` : '拒绝失败。',
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
      setError('请填写作业编号与该修改类型所需的目标值。');
      return;
    }
    try {
      const result = await modifyPlan(plan.plan_id, [modification]);
      setNotice(
        `✔ 已生成新的待审批版本 ${result.new_plan_id}（源计划 ${result.source_plan_id}）。`,
      );
      setModJobId('');
      setModTarget('');
      await loadPending();
    } catch (err) {
      setError(
        err instanceof ApiError ? `修改失败（${err.code}）：${err.message}` : '修改失败。',
      );
    }
  }, [plan, modKind, modJobId, modTarget, resetActionState, loadPending]);

  const breakdown = plan?.objective_breakdown ?? null;
  const affected = plan ? affectedOrderIds(plan) : [];
  const targetLabel =
    modKind === 'REASSIGN_MACHINE'
      ? '目标机器编号'
      : modKind === 'REASSIGN_WORKER'
        ? '目标工人编号'
        : modKind === 'MOVE_TIME'
          ? '目标起始时间（ISO 8601）'
          : '';

  return (
    <section aria-labelledby="approval-heading" className="approval">
      <div className="approval-header">
        <h2 id="approval-heading">审批</h2>
        <button
          type="button"
          onClick={() => void loadPending()}
          disabled={loading}
          aria-busy={loading}
          aria-label="刷新待审批清单"
        >
          {loading ? '加载中…' : '刷新'}
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
            该提案所依赖的输入数据已在提案生成之后发生变化。
          </p>
          <p className="approval-stale-versions">
            提案版本：{stale.proposalVersion ?? '未知'} · 当前数据版本：
            {stale.currentVersion ?? '未知'}
          </p>
          <a className="approval-regenerate" href="/schedule" aria-label="基于最新数据重新生成">
            基于最新数据重新生成
          </a>
        </div>
      )}

      {!loading && pending.length === 0 && !error && (
        <p className="approval-empty">当前没有待审批的计划。</p>
      )}

      {plan && (
        <div className="approval-body">
          <section aria-labelledby="summary-heading" className="approval-summary">
            <h3 id="summary-heading">计划摘要</h3>
            <p>
              <strong>{plan.plan_id}</strong> · 状态 {plan.status} · 可行性{' '}
              <span className={`feasibility feasibility-${plan.feasibility}`}>
                {plan.feasibility}
              </span>{' '}
              · 输入数据版本 {plan.input_snapshot_version} · v{plan.plan_version}
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
              不可排产作业：{plan.unschedulable_jobs.length}
            </h3>
            {affected.length > 0 ? (
              <p className="approval-affected">受影响订单（{affected.length}）：{affected.join('、')}</p>
            ) : (
              <p>全部作业均已排产，无受影响订单。</p>
            )}
          </section>

          <section aria-labelledby="breakdown-heading" className="approval-breakdown">
            <h3 id="breakdown-heading">目标评分拆解</h3>
            {breakdown ? (
              <>
                <table>
                  <caption className="table-caption">
                    逐分量原始值、权重与加权贡献（R7.3）
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">分量</th>
                      <th scope="col">原始值</th>
                      <th scope="col">权重</th>
                      <th scope="col">加权贡献</th>
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
                        总分
                      </th>
                      <td>{breakdown.total_score}</td>
                    </tr>
                  </tfoot>
                </table>
                {breakdown.weight_overrides_applied.length > 0 && (
                  <p className="approval-overrides">
                    已应用权重覆盖：
                    {breakdown.weight_overrides_applied
                      .map((o) => `${o.component} ×${o.multiplier}（${o.rule_id}）`)
                      .join('、')}
                  </p>
                )}
                {breakdown.preference_contributions.length > 0 && (
                  <div className="approval-preferences">
                    <h4>偏好规则影响（R18.7）</h4>
                    <ul>
                      {breakdown.preference_contributions.map((c) => (
                        <li key={c.rule_id}>
                          <span className="pref-rule-text">{c.human_text}</span>（{c.rule_id}）：
                          影响作业 {c.violating_job_ids.join('、') || '—'}，惩罚{' '}
                          {c.weighted_contribution} 分钟等价
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            ) : (
              <p>该计划无目标评分拆解。</p>
            )}
          </section>

          <section aria-labelledby="actions-heading" className="approval-actions">
            <h3 id="actions-heading">审批动作</h3>

            <div className="approval-action">
              <button
                type="button"
                onClick={() => void onApprove()}
                aria-label={`批准计划 ${plan.plan_id}`}
              >
                批准（APPROVE）
              </button>
            </div>

            <div className="approval-action">
              <label htmlFor="rejection-reason">拒绝理由（必填，至少 5 个字符）</label>
              <textarea
                id="rejection-reason"
                value={rejectionReason}
                onChange={(event) => setRejectionReason(event.target.value)}
                aria-label="拒绝理由"
                rows={2}
              />
              <button
                type="button"
                onClick={() => void onReject()}
                disabled={rejectionReason.trim().length < 5}
                aria-label={`拒绝计划 ${plan.plan_id}`}
              >
                拒绝（REJECT）
              </button>
            </div>

            <div className="approval-action approval-modify">
              <h4>修改（MODIFY）</h4>
              <label htmlFor="mod-kind">修改类型</label>
              <select
                id="mod-kind"
                value={modKind}
                onChange={(event) => setModKind(event.target.value as ModificationKind)}
                aria-label="修改类型"
              >
                {MODIFICATION_KINDS.map((kind) => (
                  <option key={kind.value} value={kind.value}>
                    {kind.label}
                  </option>
                ))}
              </select>

              <label htmlFor="mod-job">作业编号</label>
              <input
                id="mod-job"
                type="text"
                value={modJobId}
                onChange={(event) => setModJobId(event.target.value)}
                aria-label="作业编号"
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
                aria-label={`提交对计划 ${plan.plan_id} 的修改`}
              >
                提交修改
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
