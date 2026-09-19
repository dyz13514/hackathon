/**
 * 价值台账视图（design.md Components §6 `/value` 行，任务 7.6 的 K-14 片，R13.12/R13.13）。
 *
 * Task 7.6 明确点名的两块内容（情节 8 / K-14）：
 * 1. **自主 vs 上报比例**——`auto_handled_count` / `escalated_count` 与占比（R13.13）；
 * 2. **每次判定的决定性判据**——逐行列出影响分级裁决的 `impact_class` / `autonomy_level` /
 *    `execution_path` 与触发该等级的具体判据（R13.12）。
 *
 * 数据来自 `GET /api/value-ledger`（只读端点，无需认证），全部由确定性组件从 `impact_assessments`
 * 聚合，不触发 LLM。完整的价值台账（节省时间、token/美元等，design.md §4.4）属任务 11.x；本视图
 * 先交付 Task 7.6 点名的自主性透明度，并顺带展示当前 ACTIVE 计划的基线 KPI（如有）。
 *
 * 可访问性（R27.9）：区块用 `<section aria-labelledby>`；执行路径除颜色外带文字标签；比例
 * 同时以数字与文字表达；刷新按钮可键盘到达并有 `aria-label`。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import {
  getValueLedger,
  VALUE_LEDGER_CSV_URL,
  type AutonomyDecision,
  type KpiRow,
  type MetricLabel,
  type ValueLedger as ValueLedgerData,
} from '../api/valueLedger';

/** 执行路径的中文文案（除颜色外用文字传达，R27.9）。 */
const PATH_LABEL: Record<string, string> = {
  PROPOSED: '自主提案',
  ESCALATED: '上报人工',
  AUTO_APPLIED: '自动应用',
};

function pathLabel(path: string): string {
  return PATH_LABEL[path] ?? path;
}

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/** 标签的图标 + 文字（不仅靠颜色区分，R27.9、R19.4/R19.6/R25.13）。 */
const LABEL_META: Record<MetricLabel, { icon: string; text: string }> = {
  MEASURED: { icon: '✓', text: '实测' },
  ESTIMATED: { icon: '≈', text: '估计（访谈）' },
  PROJECTED: { icon: '⌁', text: '预测' },
};

function LabelBadge({ label }: { label: MetricLabel }) {
  const meta = LABEL_META[label];
  return (
    <span className={`metric-label metric-label-${label}`}>
      <span aria-hidden="true">{meta.icon} </span>
      {meta.text}
    </span>
  );
}

export function ValueLedger() {
  const [data, setData] = useState<ValueLedgerData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getValueLedger();
      setData(result);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `台账不可用（${err.code}）：${err.message}`
          : '台账不可用：后端服务不可用。';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-labelledby="value-ledger-heading" className="value-ledger">
      <div className="value-ledger-header">
        <h2 id="value-ledger-heading">价值台账</h2>
        <div className="value-ledger-actions">
          <a
            className="value-ledger-export"
            href={VALUE_LEDGER_CSV_URL}
            aria-label="导出价值台账为 CSV"
          >
            导出 CSV
          </a>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            aria-busy={loading}
            aria-label="刷新价值台账"
          >
            {loading ? '加载中…' : '刷新'}
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="value-ledger-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {data && (
        <div className="value-ledger-body">
          {/* --- KPI 表：当前值 / 基线值 / 差值 / 目标值 + 标签（R19.4） --- */}
          <section aria-labelledby="kpi-heading" className="value-ledger-kpis">
            <h3 id="kpi-heading">KPI（K-01 至 K-18）</h3>
            <table>
              <caption className="sr-only">每项 KPI 的当前值、基线值、差值、目标值与标签</caption>
              <thead>
                <tr>
                  <th scope="col">KPI</th>
                  <th scope="col">指标</th>
                  <th scope="col">当前值</th>
                  <th scope="col">基线值</th>
                  <th scope="col">差值</th>
                  <th scope="col">目标值</th>
                  <th scope="col">标签</th>
                </tr>
              </thead>
              <tbody>
                {data.kpis.map((row: KpiRow) => (
                  <tr key={row.kpi_id} data-label={row.label}>
                    <th scope="row">{row.kpi_id}</th>
                    <td>{row.metric_name}</td>
                    <td>{row.current_value || '—'}</td>
                    <td>{row.baseline_value || '—'}</td>
                    <td>{row.delta || '—'}</td>
                    <td>{row.target_value}</td>
                    <td>
                      <LabelBadge label={row.label} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* --- 实测累计 vs 预测（两列，R25.13） --- */}
          <section aria-labelledby="cost-heading" className="value-ledger-cost">
            <h3 id="cost-heading">成本：实测累计 vs 预测（两列并排）</h3>
            <table>
              <thead>
                <tr>
                  <th scope="col">口径</th>
                  <th scope="col">
                    实测累计 <LabelBadge label="MEASURED" />
                  </th>
                  <th scope="col">
                    预测 <LabelBadge label="PROJECTED" />
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">LLM 累计 token</th>
                  <td>{data.metrics.llm_tokens_used}</td>
                  <td>—</td>
                </tr>
                <tr>
                  <th scope="row">估算成本（USD）</th>
                  <td>{data.metrics.estimated_usd_cost.toFixed(4)}</td>
                  <td>
                    一次演示 ≈ {data.metrics.projected_hero_demo_usd}（K-17）；构建+排练 ≈{' '}
                    {data.metrics.projected_build_total_usd}（K-18）
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="value-ledger-quota" role="status">
              真实运行配额：已用 <strong>{data.metrics.real_run_count}</strong> /{' '}
              {data.metrics.project_real_run_cap}，剩余{' '}
              <strong>{data.metrics.real_run_remaining}</strong> 次。
            </p>
          </section>

          {/* --- manual_steps_eliminated 口径表（R19.5，原样展示） --- */}
          <section aria-labelledby="manual-steps-heading" className="value-ledger-manual-steps">
            <h3 id="manual-steps-heading">
              消除的人工步骤（共 {data.metrics.manual_steps_eliminated} 步）
            </h3>
            <table>
              <caption className="sr-only">manual_steps_eliminated 的计数口径表</caption>
              <thead>
                <tr>
                  <th scope="col">动作</th>
                  <th scope="col">计 1 步的条件</th>
                  <th scope="col">计数</th>
                </tr>
              </thead>
              <tbody>
                {data.manual_steps.map((entry) => (
                  <tr key={entry.action}>
                    <th scope="row">{entry.label}</th>
                    <td>{entry.rule}</td>
                    <td>{entry.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section aria-labelledby="autonomy-ratio-heading" className="autonomy-ratio">
            <h3 id="autonomy-ratio-heading">自主处理 vs 上报人工（K-14）</h3>
            <ul className="autonomy-counts">
              <li>
                自主处理：<strong>{data.auto_handled_count}</strong>
              </li>
              <li>
                上报人工：<strong>{data.escalated_count}</strong>
              </li>
              <li>
                裁决总数：<strong>{data.total_decisions}</strong>
              </li>
              <li>
                自主占比：<strong>{formatRate(data.auto_handled_ratio)}</strong>
              </li>
            </ul>
            {data.total_decisions === 0 && (
              <p className="autonomy-empty">尚无影响分级裁决——登记一次扰动后这里会出现记录。</p>
            )}
          </section>

          <section aria-labelledby="decisions-heading" className="autonomy-decisions">
            <h3 id="decisions-heading">每次判定的决定性判据（{data.decisions.length}）</h3>
            {data.decisions.length === 0 ? (
              <p>暂无裁决记录。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">裁决</th>
                    <th scope="col">影响等级</th>
                    <th scope="col">自主等级</th>
                    <th scope="col">执行路径</th>
                    <th scope="col">决定性判据</th>
                  </tr>
                </thead>
                <tbody>
                  {data.decisions.map((d: AutonomyDecision) => (
                    <tr key={d.assessment_id} data-execution-path={d.execution_path}>
                      <th scope="row">{d.candidate_plan_id}</th>
                      <td>{d.impact_class}</td>
                      <td>{d.autonomy_level}</td>
                      <td>
                        <span className={`path-tag path-${d.execution_path}`}>
                          {pathLabel(d.execution_path)}
                        </span>
                      </td>
                      <td>
                        {d.decisive_predicates.length > 0 ? (
                          <ul className="predicate-list">
                            {d.decisive_predicates.map((p) => (
                              <li key={p}>{p}</li>
                            ))}
                          </ul>
                        ) : (
                          '（无）'
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {data.active_plan_id && data.on_time_rate != null && (
            <section aria-labelledby="ledger-kpi-heading" className="value-ledger-kpi">
              <h3 id="ledger-kpi-heading">当前生效计划的基线对比</h3>
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
                    <td>{formatRate(data.on_time_rate)}</td>
                    <td>
                      {data.baseline_on_time_rate != null
                        ? formatRate(data.baseline_on_time_rate)
                        : '—'}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row">拖期分钟</th>
                    <td>{data.total_tardiness_minutes ?? '—'}</td>
                    <td>{data.baseline_total_tardiness_minutes ?? '—'}</td>
                  </tr>
                </tbody>
              </table>
            </section>
          )}
        </div>
      )}
    </section>
  );
}
