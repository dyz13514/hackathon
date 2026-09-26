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

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { getMaterial, updateMaterialAvailability, type MaterialAvailability } from '../api/materials';
import {
  generatePlan, getPlan, getPlanExplanation, listActive, listPending,
  type PlanDetail, type PlanExplanation,
} from '../api/plans';
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

function busiestMachine(plan: PlanDetail): { machineId: string; minutes: number } | null {
  const minutes = new Map<string, number>();
  for (const job of plan.scheduled_jobs) {
    const duration = (Date.parse(job.end_time) - Date.parse(job.start_time)) / 60_000;
    if (Number.isFinite(duration) && duration > 0) {
      minutes.set(job.machine_id, (minutes.get(job.machine_id) ?? 0) + duration);
    }
  }
  const top = [...minutes].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
  return top ? { machineId: top[0], minutes: Math.round(top[1]) } : null;
}

function baselineVerdict(plan: PlanDetail): string | null {
  const comparison = plan.baseline_comparison;
  if (!comparison) return null;
  const rateChange = comparison.on_time_rate - comparison.baseline_on_time_rate;
  const tardinessChange = comparison.total_tardiness_minutes
    - comparison.baseline_total_tardiness_minutes;
  if (Math.abs(rateChange) < 0.000001 && tardinessChange === 0) {
    return 'Conclusion: no measured improvement over FCFS in on-time rate or tardiness.';
  }
  if (rateChange >= 0 && tardinessChange <= 0) {
    return 'Conclusion: this plan improves or matches FCFS on both measured delivery metrics.';
  }
  if (rateChange <= 0 && tardinessChange >= 0) {
    return 'Conclusion: this plan does not beat FCFS on the measured delivery metrics.';
  }
  return 'Conclusion: the delivery metrics trade off against each other; review the baseline table.';
}

function MaterialFix({ materialId, planId, planSnapshotVersion }: {
  materialId: string; planId: string; planSnapshotVersion: number;
}) {
  const [material, setMaterial] = useState<MaterialAvailability | null>(null);
  const [quantity, setQuantity] = useState('');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void getMaterial(materialId).then((row) => {
      if (cancelled) return;
      setMaterial(row);
      setQuantity(String(row.quantity_available));
    }).catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof ApiError ? cause.message : 'Could not load material.');
    });
    return () => { cancelled = true; };
  }, [materialId]);

  const save = async () => {
    const parsed = Number(quantity);
    if (!Number.isFinite(parsed) || parsed < 0 || reason.trim().length < 5) {
      setError('Enter a non-negative total inventory and a reason of at least 5 characters.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      setMaterial(await updateMaterialAvailability(materialId, parsed, reason.trim()));
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not update material.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="material-fix">
      <p>
        Current {materialId} inventory: {material ? `${formatNumber(material.quantity_available)} ${material.unit}` : 'Loading…'}.
        {' '}Update the source data, then generate a new plan; the saved plan {planId} will not change.
        {' '}The listed shortfall is for this job only; other jobs may still need more material.
      </p>
      {material && material.input_snapshot_version !== planSnapshotVersion && (
        <p role="status">This plan used input version {planSnapshotVersion}; current inputs are version {material.input_snapshot_version}.</p>
      )}
      <label>
        New total available quantity{material ? ` (${material.unit})` : ''}
        <input type="number" min="0" step="any" value={quantity}
          onChange={(event) => setQuantity(event.target.value)} disabled={saving || !material} />
      </label>
      <label>
        Reason for inventory correction
        <input type="text" value={reason} maxLength={200}
          onChange={(event) => setReason(event.target.value)} disabled={saving || !material} />
      </label>
      <button type="button" onClick={() => void save()} disabled={saving || !material}>
        {saving ? 'Saving…' : 'Save inventory correction'}
      </button>
      {error && <p role="alert">{error}</p>}
      {saved && (
        <p role="status">
          Inventory saved. This old plan still shows its original unschedulable job.
          {' '}<a href={`/approval?plan_id=${encodeURIComponent(planId)}`}>Resolve a pending proposal</a>
          {' '}if one exists, then return here and generate a new plan.
        </p>
      )}
    </div>
  );
}

export function Schedule() {
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingPlanId, setPendingPlanId] = useState<string | null>(null);
  const [explanation, setExplanation] = useState<PlanExplanation | null>(null);
  const [explanationError, setExplanationError] = useState<string | null>(null);
  const [explanationLoading, setExplanationLoading] = useState(false);
  const [explanationAttempt, setExplanationAttempt] = useState(0);
  const requestVersion = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const version = requestVersion.current;
    void Promise.all([listActive(), listPending()]).then(async ([active, pending]) => {
      const newest = (plans: typeof active) => [...plans].sort(
        (a, b) => b.production_date.localeCompare(a.production_date),
      )[0];
      const activePlan = newest(active);
      const pendingPlan = newest(pending);
      // A newly generated proposal must be visible even while an older plan remains ACTIVE.
      const selected = pendingPlan ?? activePlan;
      if (!selected) return;
      const detail = await getPlan(selected.plan_id);
      if (cancelled || requestVersion.current !== version) return;
      setPlan(detail);
      setPendingPlanId(pendingPlan?.plan_id ?? null);
    }).catch(() => {
      if (!cancelled && requestVersion.current === version) {
        setError('Could not load saved plans. Refresh the page to retry.');
      }
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!plan) return;
    let cancelled = false;
    setExplanation(null);
    setExplanationError(null);
    setExplanationLoading(true);
    void getPlanExplanation(plan.plan_id).then((result) => {
      if (!cancelled) setExplanation(result);
    }).catch((reason: unknown) => {
      if (!cancelled) {
        setExplanationError(reason instanceof ApiError
          ? `Analysis failed (${reason.code}): ${reason.message}`
          : 'Analysis failed: backend service unavailable.');
      }
    }).finally(() => {
      if (!cancelled) setExplanationLoading(false);
    });
    return () => { cancelled = true; };
  }, [plan?.plan_id, explanationAttempt]);

  const onGenerate = useCallback(async () => {
    requestVersion.current += 1;
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

  const busiest = plan ? busiestMachine(plan) : null;

  return (
    <section aria-labelledby="schedule-heading" className="schedule">
      <div className="schedule-header">
        <h2 id="schedule-heading">Schedule</h2>
        <button type="button" onClick={onGenerate} disabled={loading || pendingPlanId !== null} aria-busy={loading}>
          {loading ? 'Generating…' : pendingPlanId ? 'Plan awaiting approval' : 'Generate today’s plan'}
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
          Import orders and production resources first, then generate a plan for today. No demo plan is loaded automatically.
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

            <section aria-labelledby="plan-analysis-heading" className="plan-analysis">
              <h3 id="plan-analysis-heading">Plan analysis</h3>
              <p>
                Calculated from saved input snapshot {plan.input_snapshot_version}
                {' '}for plan {plan.plan_id}: {plan.scheduled_jobs.length} scheduled jobs,
                {' '}{plan.unschedulable_jobs.length} unschedulable jobs.
                {busiest && (
                  <> Most scheduled machine: {busiest.machineId}
                    {' '}({busiest.minutes} booked minutes).</>
                )}
              </p>
              {plan.baseline_comparison && (
                <>
                  <p>
                    On-time rate versus FCFS: {formatRate(plan.baseline_comparison.on_time_rate)}
                    {' '}vs {formatRate(plan.baseline_comparison.baseline_on_time_rate)};
                    {' '}tardiness {plan.baseline_comparison.total_tardiness_minutes}
                    {' '}vs {plan.baseline_comparison.baseline_total_tardiness_minutes} minutes.
                  </p>
                  <p>{baselineVerdict(plan)}</p>
                </>
              )}
              {explanationLoading && <p role="status">Analyzing this saved plan…</p>}
              {explanationError && (
                <p role="alert">
                  {explanationError}{' '}
                  <button type="button" onClick={() => setExplanationAttempt((value) => value + 1)}>
                    Retry analysis
                  </button>
                </p>
              )}
              {explanation && explanation.llm_mode === 'LIVE' && explanation.numeric_check === 'PASS' && (
                <>
                  <p>LIVE model analysis · numeric check passed</p>
                  <p>{explanation.narrative}</p>
                </>
              )}
              {explanation && (explanation.llm_mode !== 'LIVE' || explanation.numeric_check !== 'PASS') && (
                <p role="status">
                  LIVE model analysis was not run. The schedule and comparisons above are
                  deterministic calculations, not AI-generated conclusions.
                </p>
              )}
              {explanation && (
                <>
                  <p>Confidence: {explanation.confidence.level} — {explanation.confidence.basis}</p>
                  {explanation.assumptions.length > 0 && (
                    <ul>
                      {explanation.assumptions.map((assumption, index) => (
                        <li key={`${assumption.kind}-${index}`}>
                          {assumption.description} (staleness risk: {assumption.stale_risk})
                        </li>
                      ))}
                    </ul>
                  )}
                  <p>
                    Counterfactual: {explanation.counterfactual.kind === 'TRADEOFF'
                      ? `${explanation.counterfactual.component}: ${explanation.counterfactual.current_value} vs ${explanation.counterfactual.counterfactual_value}`
                      : explanation.counterfactual.reason ?? 'No comparable tradeoff for this plan.'}
                  </p>
                </>
              )}
              {plan.generated_by_trace_id && (
                <p>Computation trace: <a href="/traces">{plan.generated_by_trace_id}</a></p>
              )}
            </section>

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
            id="unschedulable-jobs"
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
                    {job.blocking_reason === 'MATERIAL_INSUFFICIENT'
                      && typeof job.unblock_suggestion.material_id === 'string' && (
                        <MaterialFix key={`${plan.plan_id}-${job.job_id}`}
                          materialId={job.unblock_suggestion.material_id}
                          planId={plan.plan_id} planSnapshotVersion={plan.input_snapshot_version} />
                      )}
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
