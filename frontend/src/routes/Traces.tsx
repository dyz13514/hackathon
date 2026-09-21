/**
 * Trace 查看器视图（design.md Components §6 `/traces` 行，任务 5.12）。
 *
 * 两栏，逐条对应 §6 的 `/traces` 行与 R24：
 * 1. **列表**（左）——可按 Agent / 触发类型 / 时间窗筛选（R24.2）；每项标注 `mode`、
 *    `outcome`、步数、token 汇总，点击载入详情。
 * 2. **详情**（右）——顶部标注 `mode = PIPELINE | REACT` 及运行元数据；逐步显示
 *    `step_kind`、`decision_reason`（结构化摘要，非推理链，R24.7）、耗时、token，以及每步
 *    关联的工具调用（工具名、输入摘要 `args_digest`、输出摘要 `result_summary`、耗时、
 *    token）（R24.1、R22.11）。
 *
 * 数据来自只读端点 `GET /api/traces`、`GET /api/traces/{id}`（无需认证）。加载态、错误态、
 * 空态都显式呈现，不留白页。
 *
 * 可访问性（R27.9）：列表是可键盘到达的按钮列；筛选控件有 `<label>`；`mode` / `outcome`
 * 除样式外都带文字；详情区用 `<section aria-labelledby>` 分块。JSX 默认转义文本，
 * `decision_reason` / `result_summary`（可能源自不受信任输入的摘要）因此渲染为纯文本。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import {
  getTrace,
  listTraces,
  type TraceDetail,
  type TraceStep,
  type TraceSummary,
} from '../api/traces';

const TRIGGER_SOURCES = ['PLANNER_UI', 'SCHEDULED', 'DATA_CHANGE_EVENT', 'RISK_SCAN'] as const;
const AGENTS = ['INGESTION_AGENT', 'PLANNING_AGENT', 'RISK_MONITOR_AGENT'] as const;

function formatTimestamp(iso: string | null): string {
  if (!iso) {
    return '—';
  }
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

function errorText(err: unknown, prefix: string): string {
  return err instanceof ApiError
    ? `${prefix} (${err.code}): ${err.message}`
    : `${prefix}: network or service unavailable.`;
}

function StepView({ step }: { step: TraceStep }) {
  return (
    <li className="trace-step">
      <p className="trace-step-head">
        <strong>#{step.step_index}</strong> · <span className="trace-step-kind">{step.step_kind}</span>
        {' · '}
        {step.duration_ms} ms · tokens {step.input_tokens}/{step.output_tokens}
      </p>
      {step.decision_reason && (
        <p className="trace-step-reason">Decision summary: {step.decision_reason}</p>
      )}
      {step.tool_calls.length > 0 && (
        <ul className="trace-tool-calls">
          {step.tool_calls.map((call) => (
            <li key={call.call_id} className="trace-tool-call">
              <span className="trace-tool-name">{call.tool_name}</span>
              {' · '}
              <span className={`trace-outcome trace-outcome-${call.outcome}`}>{call.outcome}</span>
              {' · '}
              in {call.args_digest} · out {call.result_summary}
              {' · '}
              {call.duration_ms} ms · tokens {call.result_tokens}
              {call.truncated && <span className="badge badge-truncated"> truncated</span>}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function TraceDetailView({ detail }: { detail: TraceDetail }) {
  return (
    <section aria-labelledby="trace-detail-heading" className="trace-detail">
      <h3 id="trace-detail-heading">
        {detail.trace_id}
        <span className={`badge badge-mode badge-mode-${detail.mode}`} aria-label={`Execution mode: ${detail.mode}`}>
          {' '}
          mode = {detail.mode}
        </span>
      </h3>
      <p className="trace-detail-meta">
        Intent {detail.kind} · trigger {detail.trigger_source}
        {detail.agent && <> · agent {detail.agent}</>} · outcome{' '}
        <span className={`trace-outcome trace-outcome-${detail.outcome ?? 'NONE'}`}>
          {detail.outcome ?? 'in progress'}
        </span>
      </p>
      <p className="trace-detail-meta">
        Start {formatTimestamp(detail.started_at)} · end {formatTimestamp(detail.ended_at)} · steps{' '}
        {detail.step_count} · tokens {detail.total_input_tokens}/{detail.total_output_tokens} · est. USD{' '}
        {detail.estimated_usd.toFixed(6)}
        {detail.result_ref && <> · result ref {detail.result_ref}</>}
      </p>

      <h4>Steps ({detail.steps.length})</h4>
      {detail.steps.length === 0 ? (
        <p>No steps recorded for this run.</p>
      ) : (
        <ol className="trace-steps">
          {detail.steps.map((step) => (
            <StepView key={step.step_id} step={step} />
          ))}
        </ol>
      )}

      {detail.unassigned_tool_calls.length > 0 && (
        <>
          <h4>Tool calls not linked to a step ({detail.unassigned_tool_calls.length})</h4>
          <ul className="trace-tool-calls">
            {detail.unassigned_tool_calls.map((call) => (
              <li key={call.call_id} className="trace-tool-call">
                <span className="trace-tool-name">{call.tool_name}</span> ·{' '}
                <span className={`trace-outcome trace-outcome-${call.outcome}`}>{call.outcome}</span> ·
                in {call.args_digest} · out {call.result_summary} · {call.duration_ms} ms
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function Traces() {
  const [traces, setTraces] = useState<readonly TraceSummary[]>([]);
  const [selected, setSelected] = useState<TraceDetail | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [agent, setAgent] = useState('');
  const [triggerSource, setTriggerSource] = useState('');

  const loadList = useCallback(async () => {
    setLoading(true);
    setListError(null);
    try {
      const rows = await listTraces({
        agent: agent || undefined,
        triggerSource: triggerSource || undefined,
      });
      setTraces(rows);
    } catch (err) {
      setListError(errorText(err, 'Failed to load trace list'));
    } finally {
      setLoading(false);
    }
  }, [agent, triggerSource]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const onSelect = useCallback(async (traceId: string) => {
    setDetailError(null);
    try {
      setSelected(await getTrace(traceId));
    } catch (err) {
      setSelected(null);
      setDetailError(errorText(err, 'Failed to load trace detail'));
    }
  }, []);

  return (
    <section aria-labelledby="traces-heading" className="traces">
      <div className="traces-header">
        <h2 id="traces-heading">Trace Viewer</h2>
        <div className="traces-filters">
          <label>
            Agent
            <select value={agent} onChange={(e) => setAgent(e.target.value)}>
              <option value="">All</option>
              {AGENTS.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
          <label>
            Trigger type
            <select value={triggerSource} onChange={(e) => setTriggerSource(e.target.value)}>
              <option value="">All</option>
              {TRIGGER_SOURCES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => void loadList()}
            disabled={loading}
            aria-busy={loading}
            aria-label="Refresh trace list"
          >
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {listError && (
        <p role="alert" className="traces-error">
          {listError}
        </p>
      )}

      <div className="traces-body">
        <section aria-labelledby="traces-list-heading" className="traces-list">
          <h3 id="traces-list-heading">Runs ({traces.length})</h3>
          {traces.length === 0 && !listError ? (
            <p className="traces-empty">No traces yet. Records appear here after you generate a plan or run orchestration.</p>
          ) : (
            <ul>
              {traces.map((trace) => (
                <li key={trace.trace_id}>
                  <button
                    type="button"
                    className={
                      selected?.trace_id === trace.trace_id
                        ? 'trace-list-item is-active'
                        : 'trace-list-item'
                    }
                    onClick={() => void onSelect(trace.trace_id)}
                    aria-label={`View trace ${trace.trace_id}`}
                  >
                    <span className="trace-list-id">{trace.trace_id}</span>
                    <span className={`badge badge-mode badge-mode-${trace.mode}`}>{trace.mode}</span>
                    <span className="trace-list-kind">{trace.kind}</span>
                    <span className={`trace-outcome trace-outcome-${trace.outcome ?? 'NONE'}`}>
                      {trace.outcome ?? 'in progress'}
                    </span>
                    <span className="trace-list-meta">
                      {trace.step_count} steps · {formatTimestamp(trace.started_at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="traces-detail-pane">
          {detailError && (
            <p role="alert" className="traces-error">
              {detailError}
            </p>
          )}
          {selected ? (
            <TraceDetailView detail={selected} />
          ) : (
            !detailError && <p className="traces-empty">Select a run on the left to view its step-by-step detail.</p>
          )}
        </div>
      </div>
    </section>
  );
}
