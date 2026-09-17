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
import { generatePlan, type PlanDetail } from '../api/plans';
import { Gantt } from '../components/Gantt';

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function formatSuggestion(suggestion: Record<string, unknown>): string {
  const entries = Object.entries(suggestion);
  if (entries.length === 0) {
    return '（无量化条件）';
  }
  return entries.map(([key, value]) => `${key}=${String(value)}`).join('，');
}

export function Schedule() {
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onGenerate = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await generatePlan();
      setPlan(result);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `生成失败（${err.code}）：${err.message}`
          : '生成失败：网络或服务不可用。';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <section aria-labelledby="schedule-heading" className="schedule">
      <div className="schedule-header">
        <h2 id="schedule-heading">排产甘特图</h2>
        <button type="button" onClick={onGenerate} disabled={loading} aria-busy={loading}>
          {loading ? '生成中…' : '生成今日计划'}
        </button>
      </div>

      {error && (
        <p role="alert" className="schedule-error">
          {error}
        </p>
      )}

      {!plan && !error && (
        <p className="schedule-empty">
          点击「生成今日计划」运行确定性排产流水线（不消耗 LLM token）。
        </p>
      )}

      {plan && (
        <div className="schedule-body">
          <div className="schedule-main">
            <p className="schedule-status">
              计划 {plan.plan_id}
              <span className={`feasibility feasibility-${plan.feasibility}`}>
                {' '}
                · 可行性：{plan.feasibility}
              </span>
              <span> · 状态：{plan.status}</span>
            </p>

            <Gantt jobs={plan.scheduled_jobs} />

            {plan.baseline_comparison && (
              <section aria-labelledby="baseline-heading" className="baseline-compare">
                <h3 id="baseline-heading">基线对比（相对 FCFS）</h3>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">指标</th>
                      <th scope="col">本计划</th>
                      <th scope="col">FCFS 基线</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">按期率</th>
                      <td>{formatRate(plan.baseline_comparison.on_time_rate)}</td>
                      <td>{formatRate(plan.baseline_comparison.baseline_on_time_rate)}</td>
                    </tr>
                    <tr>
                      <th scope="row">拖期分钟</th>
                      <td>{plan.baseline_comparison.total_tardiness_minutes}</td>
                      <td>{plan.baseline_comparison.baseline_total_tardiness_minutes}</td>
                    </tr>
                    <tr>
                      <th scope="row">迟交订单数</th>
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
              不可排产作业（{plan.unschedulable_jobs.length}）
            </h3>
            {plan.unschedulable_jobs.length === 0 ? (
              <p>全部作业均已排产。</p>
            ) : (
              <ul>
                {plan.unschedulable_jobs.map((job) => (
                  <li key={job.job_id} className="unschedulable-item">
                    <p className="unschedulable-job">
                      {job.order_id} · {job.job_id}
                    </p>
                    <p className="unschedulable-reason">原因：{job.blocking_reason}</p>
                    <p className="unschedulable-unblock">
                      解锁条件：{formatSuggestion(job.unblock_suggestion)}
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
