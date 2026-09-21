/**
 * 方案对比视图（design.md Components §6 `/plans/:a/compare/:b`，任务 7.5，R10.1/R10.2）。
 *
 * 三块内容，逐条对应 §6 的 `/plans/:a/compare/:b` 行：
 * 1. 左右并排甘特——A（对照，通常是当前 ACTIVE）与 B（建议）各一张 `<Gantt>`；
 * 2. 逐作业变更标签——`ADDED` / `REMOVED` / `MOVED` / `REASSIGNED` / `UNCHANGED`（R10.1），
 *    附 A/B 两侧的资源与时间，便于逐条核对；
 * 3. 解释面板——每个 `MOVED` / `REASSIGNED` 作业一条 `decision_evidence`（R10.2）：触发原因、
 *    被违反或将被违反的约束、涉及资源。
 *
 * 顶栏是同口径的聚合：churn 比例 + 五类计数（与句柄式 `compare_plans` 工具同源）。反事实
 * （R10.3）属任务 8.4，本端点与本视图暂不含。
 *
 * 数据来源是**确定性**明细端点（`GET /plans/{a}/compare/{b}`），不触发 LLM。计划 id 从路由
 * 参数读取；`a` / `b` 也可作为 props 传入以便测试。加载态、错误态（含 `PLAN_NOT_FOUND`）、
 * 空态都显式呈现，不留白页。
 *
 * 可访问性（R27.9）：变更标签除颜色外带文字；甘特图自带文字摘要（见 `Gantt`）；两张图各有
 * 标题区分 A / B。
 */

import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';

import { ApiError } from '../api/client';
import {
  comparePlans,
  type DecisionEvidence,
  type JobChange,
  type PlanChangeKind,
  type PlanCompare as PlanCompareData,
  type ScheduledJob,
} from '../api/plans';
import { Gantt } from '../components/Gantt';

/** 变更标签的中文文案（除颜色外用文字传达，R27.9）。 */
const CHANGE_LABEL: Record<PlanChangeKind, string> = {
  ADDED: 'Added',
  REMOVED: 'Removed',
  MOVED: 'Rescheduled',
  REASSIGNED: 'Reassigned',
  UNCHANGED: 'Unchanged',
};

/** 从 job_id 尾部的 `-OPn` 推工序号；推不出则回退 0。纯展示用，不参与权威计算。 */
export function operationSequenceFromJobId(jobId: string): number {
  const match = /-OP(\d+)$/i.exec(jobId);
  return match ? Number(match[1]) : 0;
}

/**
 * 把一侧（A 或 B）的变更集折成 `ScheduledJob[]` 喂给 `<Gantt>`。
 *
 * 对比端点只带资源与起止（`{a,b}_machine_id` 等），不带 `setup_minutes` / `changeover_minutes`，
 * 因此这两项取 0（对比图不画换型段）。某侧不存在该作业时（`ADDED` 之于 A、`REMOVED` 之于 B）
 * 跳过——那一侧本就没有它。
 */
export function toGanttJobs(
  changes: readonly JobChange[],
  side: 'a' | 'b',
): ScheduledJob[] {
  const jobs: ScheduledJob[] = [];
  for (const change of changes) {
    const machineId = side === 'a' ? change.a_machine_id : change.b_machine_id;
    const workerId = side === 'a' ? change.a_worker_id : change.b_worker_id;
    const startTime = side === 'a' ? change.a_start_time : change.b_start_time;
    const endTime = side === 'a' ? change.a_end_time : change.b_end_time;
    if (machineId == null || startTime == null || endTime == null) {
      continue;
    }
    jobs.push({
      job_id: change.job_id,
      order_id: change.order_id,
      product_id: '',
      operation_sequence: operationSequenceFromJobId(change.job_id),
      machine_id: machineId,
      worker_id: workerId ?? '',
      start_time: startTime,
      end_time: endTime,
      setup_minutes: 0,
      changeover_minutes: 0,
    });
  }
  return jobs;
}

function formatClock(iso: string | null): string {
  if (iso == null) {
    return '-';
  }
  const date = new Date(iso);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

/** 「机器 / 工人 @ 起-止」的紧凑单元格文本；某侧缺资源时显示破折号。 */
function sideCell(machineId: string | null, workerId: string | null, start: string | null, end: string | null): string {
  if (machineId == null && start == null) {
    return '-';
  }
  const res = `${machineId ?? '-'} / ${workerId ?? '-'}`;
  return `${res} @ ${formatClock(start)}-${formatClock(end)}`;
}

export interface PlanCompareProps {
  /** 覆盖路由参数（测试注入）。 */
  readonly planIdA?: string;
  readonly planIdB?: string;
}

export function PlanCompare({ planIdA, planIdB }: PlanCompareProps = {}) {
  const params = useParams<{ a: string; b: string }>();
  const idA = planIdA ?? params.a ?? '';
  const idB = planIdB ?? params.b ?? '';

  const [data, setData] = useState<PlanCompareData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!idA || !idB) {
      setError('Missing the two plan ids to compare.');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await comparePlans(idA, idB);
      setData(result);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.code === 'PLAN_NOT_FOUND'
            ? `Plan not found (${err.code}): ${err.message}`
            : `Comparison failed (${err.code}): ${err.message}`
          : 'Comparison failed: network or service unavailable.';
      setError(message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [idA, idB]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-labelledby="compare-heading" className="plan-compare">
      <div className="compare-header">
        <h2 id="compare-heading">Plan comparison</h2>
        <p className="compare-ids">
          A: {idA || 'N/A'} &lt;-&gt; B: {idB || 'N/A'}
        </p>
      </div>

      {loading && <p className="compare-loading">Comparing…</p>}

      {error && !loading && (
        <p role="alert" className="compare-error">
          {error}
        </p>
      )}

      {data && !loading && !error && (
        <div className="compare-body">
          <ul className="compare-summary" aria-label="Change summary">
            <li>
              Churn ratio: <strong>{(data.churn_ratio * 100).toFixed(1)}%</strong>
            </li>
            <li>Added: <strong>{data.added_count}</strong></li>
            <li>Removed: <strong>{data.removed_count}</strong></li>
            <li>Rescheduled: <strong>{data.moved_count}</strong></li>
            <li>Reassigned: <strong>{data.reassigned_count}</strong></li>
            <li>Unchanged: <strong>{data.unchanged_count}</strong></li>
          </ul>

          <div className="compare-gantts">
            <figure aria-labelledby="compare-gantt-a">
              <figcaption id="compare-gantt-a">Plan A (baseline)</figcaption>
              <Gantt jobs={toGanttJobs(data.changes, 'a')} />
            </figure>
            <figure aria-labelledby="compare-gantt-b">
              <figcaption id="compare-gantt-b">Plan B (proposed)</figcaption>
              <Gantt jobs={toGanttJobs(data.changes, 'b')} />
            </figure>
          </div>

          <section aria-labelledby="compare-changes-heading" className="compare-changes">
            <h3 id="compare-changes-heading">Per-job changes ({data.changes.length})</h3>
            <table>
              <thead>
                <tr>
                  <th scope="col">Job</th>
                  <th scope="col">Change</th>
                  <th scope="col">A (baseline)</th>
                  <th scope="col">B (proposed)</th>
                </tr>
              </thead>
              <tbody>
                {data.changes.map((change) => (
                  <tr key={change.job_id} data-change={change.change}>
                    <th scope="row">
                      {change.order_id} · {change.job_id}
                    </th>
                    <td>
                      <span className={`change-tag change-${change.change}`}>
                        {CHANGE_LABEL[change.change]}
                      </span>
                    </td>
                    <td>{sideCell(change.a_machine_id, change.a_worker_id, change.a_start_time, change.a_end_time)}</td>
                    <td>{sideCell(change.b_machine_id, change.b_worker_id, change.b_start_time, change.b_end_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section aria-labelledby="compare-evidence-heading" className="compare-evidence">
            <h3 id="compare-evidence-heading">
              Decision evidence ({data.decision_evidence.length})
            </h3>
            {data.decision_evidence.length === 0 ? (
              <p className="compare-evidence-empty">
                No MOVED / REASSIGNED jobs, so there is no decision evidence.
              </p>
            ) : (
              <ul>
                {data.decision_evidence.map((ev: DecisionEvidence) => (
                  <li key={ev.job_id} className="evidence-item">
                    <p className="evidence-job">{ev.job_id}</p>
                    <p className="evidence-trigger">Trigger: {ev.trigger}</p>
                    <p className="evidence-constraint">Constraint: {ev.constraint}</p>
                    <p className="evidence-resources">
                      Resources involved: {ev.resources.length > 0 ? ev.resources.join(', ') : '(none)'}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
