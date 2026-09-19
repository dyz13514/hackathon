/**
 * 瓶颈与产能洞察视图（design.md Components §6 `/insights`，任务 13.5，R15）。
 *
 * 展示当前 `ACTIVE` 计划的：每台机器的利用率 / 承担作业数 / 承担订单价值占比 / 关键机器标识 /
 * 「可用工时 +20% 时 total_tardiness_minutes 的变化量」（由后端沙箱实算，R15.2）；以及按
 * required_worker_skill 聚合的技能缺口。加载态、错误态（含无 ACTIVE 计划）、空态都显式呈现。
 *
 * 可访问性（R27.9）：表格有表头与 scope；关键机器除颜色外带文字（「关键」）与图标；+20% 变化量
 * 带方向文字（改善/变差/不变），不仅靠数值符号。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { type BottleneckInsights, getBottlenecks } from '../api/insights';

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

function deltaLabel(delta: number): string {
  if (delta < 0) return `${delta}（改善）`;
  if (delta > 0) return `+${delta}（变差）`;
  return '0（不变）';
}

export function Insights() {
  const [data, setData] = useState<BottleneckInsights | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getBottlenecks());
    } catch (err) {
      if (err instanceof ApiError && err.code === 'NO_ACTIVE_PLAN') {
        setError('当前没有 ACTIVE 计划。请先生成并批准一个计划后再查看瓶颈洞察。');
      } else {
        setError(
          err instanceof ApiError
            ? `瓶颈洞察不可用（${err.code}）：${err.message}`
            : '瓶颈洞察不可用：后端服务不可用。',
        );
      }
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-labelledby="insights-heading" className="insights">
      <div className="insights-header">
        <h2 id="insights-heading">瓶颈与产能洞察</h2>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          aria-busy={loading}
          aria-label="刷新瓶颈洞察"
        >
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {error && (
        <p role="alert" className="insights-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {data && (
        <>
          <section aria-labelledby="machines-heading" className="insights-machines">
            <h3 id="machines-heading">机器产能（当前 ACTIVE 计划）</h3>
            {data.machines.length === 0 ? (
              <p>当前 ACTIVE 计划没有承担作业的机器。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">机器</th>
                    <th scope="col">利用率</th>
                    <th scope="col">作业数</th>
                    <th scope="col">订单价值占比</th>
                    <th scope="col">关键</th>
                    <th scope="col">+20% 工时→总拖期变化</th>
                  </tr>
                </thead>
                <tbody>
                  {data.machines.map((m) => (
                    <tr key={m.machine_id} data-machine-id={m.machine_id}>
                      <th scope="row">
                        {m.machine_id}
                        <span className="machine-type">（{m.machine_type}）</span>
                      </th>
                      <td>{pct(m.utilisation)}</td>
                      <td>{m.job_count}</td>
                      <td>{pct(m.order_value_share)}</td>
                      <td>
                        {m.is_critical ? (
                          <span className="badge badge-critical" title="无相同能力替代机器">
                            <span aria-hidden="true">⛔ </span>关键
                          </span>
                        ) : (
                          <span className="machine-noncritical">有替代</span>
                        )}
                      </td>
                      <td>{deltaLabel(m.tardiness_delta_if_plus_20pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section aria-labelledby="skills-heading" className="insights-skills">
            <h3 id="skills-heading">技能缺口（按所需工人技能聚合）</h3>
            {data.skill_gaps.length === 0 ? (
              <p>当前计划无技能需求缺口数据。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">技能</th>
                    <th scope="col">所需工时（分钟）</th>
                    <th scope="col">可用工时（分钟）</th>
                    <th scope="col">缺口</th>
                  </tr>
                </thead>
                <tbody>
                  {data.skill_gaps.map((g) => (
                    <tr key={g.skill} data-skill={g.skill}>
                      <th scope="row">{g.skill}</th>
                      <td>{g.required_minutes}</td>
                      <td>{g.available_minutes}</td>
                      <td>
                        {g.gap_minutes > 0 ? (
                          <span className="skill-gap-short">
                            <span aria-hidden="true">⚠ </span>缺 {g.gap_minutes}
                          </span>
                        ) : (
                          <span className="skill-gap-ok">富余 {-g.gap_minutes}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </section>
  );
}
