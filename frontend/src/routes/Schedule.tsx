/**
 * 排产甘特图视图（design.md Components §6 `/schedule`，任务 2.12）。
 *
 * 三块内容，逐条对应 §6 的 `/schedule` 行：
 * 1. 甘特图（`<Gantt>`）——横轴时间纵轴机器，换型斜纹，作业条标 `order_id` 与工序号；
 * 2. 右侧抽屉——不可排产作业清单，附 `blocking_reason` 与量化解锁条件（R8.6）；
 * 3. 基线对比区——按期率与拖期分钟相对 FCFS 基线的差（R5.4、R19.2）。
 *
 * 顶部「生成今日计划」按钮触发形态 A 确定性流水线（`POST /plans/generate`，R5.1）。
 * 加载态、错误态、空态都显式呈现，不留白页。
 *
 * 可访问性（R27.9）：按钮与抽屉可键盘到达；状态（生成中 / 失败 / PARTIAL）除颜色外
 * 附文字；甘特图自带文字摘要（见 `Gantt`）。
 */

import { useCallback, useState } from 'react';

import { ApiError } from '../api/client';
import { generatePlan, getPlan, type PlanDetail } from '../api/plans';
import { Gantt } from '../components/Gantt';

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/** 数值：去掉 `16.00000000000000000000` 这类长尾零，最多保留 2 位小数（值本身不变）。 */
function formatNumber(value: unknown): string {
  if (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '')) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 2,
      });
    }
  }
  return String(value);
}

/** ISO 时间戳 → 本地可读时间；解析不出来就原样显示。 */
function formatTimestamp(value: unknown): string {
  const raw = String(value);
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? raw : parsed.toLocaleString();
}

/** `shortfall_quantity` → 「Shortfall」这类人读标签；未知键退化为「去下划线 + 首字母大写」。 */
function humanizeKey(key: string): string {
  const spaced = key.replace(/_/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function formatSuggestion(suggestion: Record<string, unknown>): string {
  const entries = Object.entries(suggestion);
  if (entries.length === 0) {
    return '(no quantified conditions)';
  }
  // 已知的组合字段先说人话：缺料 → 「Shortfall: 16 kg」+「Material: MAT-…」+「Needed before: …」。
  // 其余键按「人读标签: 值」渲染；下层的数值与时间一个都不改，只改呈现。
  const { material_id: materialId, shortfall_quantity: shortfall, unit } = suggestion;
  const parts: string[] = [];
  if (shortfall !== undefined) {
    parts.push(`Shortfall: ${formatNumber(shortfall)}${unit ? ` ${String(unit)}` : ''}`);
  }
  if (materialId !== undefined) {
    parts.push(`Material: ${String(materialId)}`);
  }
  for (const [key, value] of entries) {
    const consumed =
      key === 'shortfall_quantity' || key === 'material_id' || (key === 'unit' && shortfall !== undefined);
    if (consumed) {
      continue;
    }
    parts.push(
      `${humanizeKey(key)}: ${key === 'needed_before' ? formatTimestamp(value) : formatNumber(value)}`,
    );
  }
  return parts.join(' · ');
}

export function Schedule() {
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingPlanId, setPendingPlanId] = useState<string | null>(null);

  const onGenerate = useCallback(async () => {
    setLoading(true);
    setError(null);
    setPendingPlanId(null);
    try {
      const result = await generatePlan();
      setPlan(result);
      if (result.status === 'PENDING_APPROVAL') setPendingPlanId(result.plan_id);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'PENDING_PLAN_EXISTS') {
        const existingId = err.details.existing_plan_id;
        if (typeof existingId === 'string') {
          setPendingPlanId(existingId);
          try {
            setPlan(await getPlan(existingId));
            return;
          } catch {
            // Existing plan may have changed between the 409 and this read; keep the approval link.
          }
        }
      }
      const message =
        err instanceof ApiError
          ? `Generation failed (${err.code}): ${err.message}`
          : 'Generation failed: network or service unavailable.';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <section aria-labelledby="schedule-heading" className="schedule">
      <div className="schedule-header">
        <h2 id="schedule-heading">Schedule</h2>
        <button type="button" onClick={onGenerate} disabled={loading || plan?.status === 'PENDING_APPROVAL'} aria-busy={loading}>
          {loading ? 'Generating…' : plan?.status === 'PENDING_APPROVAL' ? 'Plan awaiting approval' : 'Generate today’s plan'}
        </button>
      </div>

      {pendingPlanId && (
        <p role="status" className="schedule-pending">
          Plan {pendingPlanId} is awaiting approval. Review its schedule below, then{' '}
          <a href={`/approval?plan_id=${encodeURIComponent(pendingPlanId)}`}>approve or reject it</a>
          {' '}before generating another plan for this date.
        </p>
      )}

      {error && (
        <p role="alert" className="schedule-error">
          {error}
        </p>
      )}

      {!plan && !error && (
        <p className="schedule-empty">
          Click “Generate today’s plan” to run the deterministic scheduling pipeline (consumes no LLM tokens).
        </p>
      )}

      {plan && (
        <div className="schedule-body">
          <div className="schedule-main">
            <p className="schedule-status">
              Plan {plan.plan_id}
              <span className={`feasibility feasibility-${plan.feasibility}`}>
                {' '}
                · Feasibility: {plan.feasibility}
              </span>
              <span> · Status: {plan.status}</span>
            </p>

            <Gantt jobs={plan.scheduled_jobs} />

            {plan.baseline_comparison && (
              <section aria-labelledby="baseline-heading" className="baseline-compare">
                <h3 id="baseline-heading">Baseline comparison (vs. FCFS)</h3>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Metric</th>
                      <th scope="col">This plan</th>
                      <th scope="col">FCFS baseline</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">On-time rate</th>
                      <td>{formatRate(plan.baseline_comparison.on_time_rate)}</td>
                      <td>{formatRate(plan.baseline_comparison.baseline_on_time_rate)}</td>
                    </tr>
                    <tr>
                      <th scope="row">Tardiness (min)</th>
                      <td>{plan.baseline_comparison.total_tardiness_minutes}</td>
                      <td>{plan.baseline_comparison.baseline_total_tardiness_minutes}</td>
                    </tr>
                    <tr>
                      <th scope="row">Late orders</th>
                      <td>{plan.baseline_comparison.late_order_count}</td>
                      <td>{plan.baseline_comparison.baseline_late_order_count}</td>
                    </tr>
                  </tbody>
                </table>
              </section>
            )}
          </div>

          <aside
            className="unschedulable-drawer"
            aria-labelledby="unschedulable-heading"
          >
            <h3 id="unschedulable-heading">
              Unschedulable jobs ({plan.unschedulable_jobs.length})
            </h3>
            {plan.unschedulable_jobs.length === 0 ? (
              <p>All jobs were scheduled.</p>
            ) : (
              <ul>
                {plan.unschedulable_jobs.map((job) => (
                  <li key={job.job_id} className="unschedulable-item">
                    <p className="unschedulable-job">
                      {job.order_id} · {job.job_id}
                    </p>
                    <p className="unschedulable-reason">Reason: {job.blocking_reason}</p>
                    <p className="unschedulable-unblock">
                      Unblock conditions: {formatSuggestion(job.unblock_suggestion)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
