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
import { getValueLedger, type AutonomyDecision, type ValueLedger as ValueLedgerData } from '../api/valueLedger';

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

      {error && (
        <p role="alert" className="value-ledger-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {data && (
        <div className="value-ledger-body">
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
