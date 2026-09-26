/**
 * What-if 视图（design.md Components §6 `/whatif` 行，任务 8.3，R16）。
 *
 * **P0 只有结构化场景表单**：先选 5 类变更之一，再填参数 → 运行推演 → 结果与当前 `ACTIVE`
 * 计划对比 → 「以此场景生成正式提案」（仍走审批，R16.9）。自然语言输入框是 P1（任务 13.1）。
 *
 * 数据来自 `POST /api/scenarios/run`（确定性、无 LLM）；采纳走 `POST /api/scenarios/{id}/adopt`。
 * 加载态、错误态、空态都显式呈现。
 *
 * 可访问性（R27.9）：表单控件有 `<label>`；区块用 `aria-labelledby`；对比数值除颜色外带
 * 文字方向标注（「变差 / 变好」）；按钮可键盘到达并有 `aria-label`。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { getHealth } from '../api/health';
import {
  type AdoptResult,
  type ScenarioMutation,
  type ScenarioResult,
  type TranslateResult,
  adoptScenario,
  runScenario,
  translateScenario,
} from '../api/scenarios';

type MutationKind = ScenarioMutation['kind'];

const KIND_LABEL: Record<MutationKind, string> = {
  ADD_OR_CHANGE_ORDER: 'Add order / change due date',
  SET_MACHINE_UNAVAILABLE: 'Machine unavailable',
  CHANGE_MATERIAL_AVAILABILITY: 'Change material availability',
  SET_WORKER_UNAVAILABLE: 'Worker unavailable',
  CHANGE_ORDER_PRIORITY: 'Change order priority',
};

const KINDS = Object.keys(KIND_LABEL) as MutationKind[];

function deltaLabel(delta: number): string {
  if (delta > 0) return `+${delta} (worse)`;
  if (delta < 0) return `${delta} (better)`;
  return '0 (unchanged)';
}

export function WhatIf() {
  const [kind, setKind] = useState<MutationKind>('SET_MACHINE_UNAVAILABLE');
  const [fields, setFields] = useState<Record<string, string>>({});
  const [result, setResult] = useState<ScenarioResult | null>(null);
  const [adopted, setAdopted] = useState<AdoptResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ---- 任务 13.1 自然语言 What-if 翻译（P1）----
  // `nlEnabled` 由 GET /api/health 的 mode 决定：DETERMINISTIC_ONLY（降级）时隐藏自然语言
  // 输入框、只留结构化表单（R16 范围说明）。默认隐藏，拉到 health 且非降级才显示——这样
  // health 不可达时保守地退回纯结构化表单，不会露出一个会 503 的入口。
  const [nlEnabled, setNlEnabled] = useState(false);
  const [nlQuery, setNlQuery] = useState('');
  const [translation, setTranslation] = useState<TranslateResult | null>(null);
  const [translateError, setTranslateError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getHealth()
      .then((h) => {
        if (!cancelled) setNlEnabled(h.mode !== 'DETERMINISTIC_ONLY');
      })
      .catch(() => {
        if (!cancelled) setNlEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const set = (name: string, value: string) =>
    setFields((prev) => ({ ...prev, [name]: value }));

  const buildMutation = useCallback((): ScenarioMutation => {
    switch (kind) {
      case 'SET_MACHINE_UNAVAILABLE':
        return {
          kind,
          machine_id: fields.machine_id ?? '',
          start_time: fields.start_time ?? '',
          end_time: fields.end_time ?? '',
        };
      case 'SET_WORKER_UNAVAILABLE':
        return {
          kind,
          worker_id: fields.worker_id ?? '',
          start_time: fields.start_time ?? '',
          end_time: fields.end_time ?? '',
        };
      case 'CHANGE_MATERIAL_AVAILABILITY':
        return {
          kind,
          material_id: fields.material_id ?? '',
          quantity_available: Number(fields.quantity_available ?? 0),
        };
      case 'CHANGE_ORDER_PRIORITY':
        return {
          kind,
          order_id: fields.order_id ?? '',
          priority: (fields.priority as 'URGENT' | 'HIGH' | 'NORMAL' | 'LOW') ?? 'NORMAL',
        };
      case 'ADD_OR_CHANGE_ORDER':
        return {
          kind,
          order_id: fields.order_id || null,
          product_id: fields.product_id || null,
          quantity: fields.quantity ? Number(fields.quantity) : null,
          due_date: fields.due_date || null,
        };
    }
  }, [kind, fields]);

  const onRun = useCallback(async () => {
    setBusy(true);
    setError(null);
    setAdopted(null);
    try {
      setResult(await runScenario([buildMutation()]));
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Simulation failed (${err.code}): ${err.message}`
          : 'Simulation failed: backend service unavailable.',
      );
    } finally {
      setBusy(false);
    }
  }, [buildMutation]);

  const onAdopt = useCallback(async () => {
    if (!result) return;
    setBusy(true);
    setError(null);
    try {
      setAdopted(await adoptScenario(result.scenario_id));
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Adoption failed (${err.code}): ${err.message}`
          : 'Adoption failed: backend service unavailable.',
      );
    } finally {
      setBusy(false);
    }
  }, [result]);

  // 翻译：不执行，只把结构化结果放进确认卡（R16.1「执行前必须展示给 Planner 确认」）。
  const onTranslate = useCallback(async () => {
    if (!nlQuery.trim()) return;
    setBusy(true);
    setTranslateError(null);
    setTranslation(null);
    setResult(null);
    setAdopted(null);
    try {
      setTranslation(await translateScenario(nlQuery));
    } catch (err) {
      if (err instanceof ApiError && err.code === 'SCENARIO_CLARIFICATION_REQUIRED') {
        setTranslateError(`${err.message} Example: “What if CNC-01 is unavailable tomorrow from 08:00 to 14:00?”`);
      } else if (err instanceof ApiError && err.code === 'UNSUPPORTED_SCENARIO') {
        const kinds = (err.details.supported_kinds as string[] | undefined) ?? [];
        setTranslateError(
          `${err.message} Supported types: ${kinds.join(', ')}. Please use the structured form below.`,
        );
      } else if (err instanceof ApiError && err.code === 'LLM_UNAVAILABLE_USE_STRUCTURED_FORM') {
        setNlEnabled(false);
        setTranslateError('The LLM is in degraded mode, so natural-language translation is unavailable. Please use the structured form below.');
      } else {
        setTranslateError(
          err instanceof ApiError
            ? `Translation failed (${err.code}): ${err.message}`
            : 'Translation failed: backend service unavailable.',
        );
      }
    } finally {
      setBusy(false);
    }
  }, [nlQuery]);

  // 确认翻译结果 → 回填并执行：把确认过的结构化 mutations 原样交 /scenarios/run（复用 8.3）。
  const onConfirmTranslation = useCallback(async () => {
    if (!translation || translation.mutations.length === 0) return;
    setBusy(true);
    setError(null);
    setAdopted(null);
    try {
      setResult(await runScenario(translation.mutations));
      setTranslation(null);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Simulation failed (${err.code}): ${err.message}`
          : 'Simulation failed: backend service unavailable.',
      );
    } finally {
      setBusy(false);
    }
  }, [translation]);

  return (
    <section aria-labelledby="whatif-heading" className="whatif">
      <h2 id="whatif-heading">What-if simulation</h2>

      {nlEnabled && (
        <section aria-labelledby="whatif-nl-heading" className="whatif-nl">
          <h3 id="whatif-nl-heading">Ask in natural language (optional)</h3>
          <p className="whatif-nl-hint">
            For example, “What if CNC-01 is unavailable tomorrow from 08:00 to 14:00?”. Include the resource ID
            and exact times so the system does not invent them. The translation is shown
            for you to confirm first, and only runs after you confirm — it never changes any plan directly.
          </p>
          <label htmlFor="whatif-nl-query">Natural-language what-if question</label>
          <textarea
            id="whatif-nl-query"
            className="whatif-nl-input"
            rows={2}
            value={nlQuery}
            onChange={(e) => setNlQuery(e.target.value)}
            placeholder="Describe the hypothesis you want to simulate in one sentence…"
          />
          <button
            type="button"
            onClick={() => void onTranslate()}
            disabled={busy || !nlQuery.trim()}
            aria-label="Translate to structured scenario"
          >
            {busy ? 'Translating…' : 'Translate to structured scenario'}
          </button>

          {translateError && (
            <p role="alert" className="whatif-error">
              {translateError}
            </p>
          )}

          {translation && (
            <div className="whatif-translation-card" role="group" aria-label="Translation confirmation card">
              <h4>Translation result (please confirm before running)</h4>
              {translation.injection_suspected && (
                <p role="alert" className="whatif-injection-warning">
                  A suspected prompt-injection pattern was detected in your question; the system treated it as
                  plain data and recorded it for audit — the translation is not influenced by its instructions.
                </p>
              )}
              <p className="whatif-translation-echo">
                Original question (echoed as data):{' '}
                <q>{translation.source_query_echo}</q>
              </p>
              <ol className="whatif-translation-mutations">
                {translation.mutations.map((m, i) => (
                  <li key={i}>
                    <strong>{KIND_LABEL[m.kind]}</strong>
                    <code>{JSON.stringify(m)}</code>
                  </li>
                ))}
              </ol>
              <div className="whatif-translation-actions">
                <button
                  type="button"
                  onClick={() => void onConfirmTranslation()}
                  disabled={busy || translation.mutations.length === 0}
                  aria-label="Confirm and run simulation"
                >
                  Confirm and simulate
                </button>
                <button
                  type="button"
                  onClick={() => setTranslation(null)}
                  disabled={busy}
                  aria-label="Discard translation result"
                >
                  Discard
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      <section aria-labelledby="whatif-form-heading" className="whatif-form">
        <h3 id="whatif-form-heading">Scenario change</h3>
        <label htmlFor="whatif-kind">Change type</label>
        <select
          id="whatif-kind"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value as MutationKind);
            setFields({});
          }}
        >
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {KIND_LABEL[k]}
            </option>
          ))}
        </select>

        <div className="whatif-fields">
          {(kind === 'SET_MACHINE_UNAVAILABLE' || kind === 'SET_WORKER_UNAVAILABLE') && (
            <>
              <FieldInput
                label={kind === 'SET_MACHINE_UNAVAILABLE' ? 'Machine ID' : 'Worker ID'}
                name={kind === 'SET_MACHINE_UNAVAILABLE' ? 'machine_id' : 'worker_id'}
                onChange={set}
              />
              <FieldInput label="Start time (ISO)" name="start_time" onChange={set} />
              <FieldInput label="End time (ISO)" name="end_time" onChange={set} />
            </>
          )}
          {kind === 'CHANGE_MATERIAL_AVAILABILITY' && (
            <>
              <FieldInput label="Material ID" name="material_id" onChange={set} />
              <FieldInput label="Available quantity" name="quantity_available" type="number" onChange={set} />
            </>
          )}
          {kind === 'CHANGE_ORDER_PRIORITY' && (
            <>
              <FieldInput label="Order ID" name="order_id" onChange={set} />
              <label htmlFor="whatif-priority">Priority</label>
              <select
                id="whatif-priority"
                value={fields.priority ?? 'NORMAL'}
                onChange={(e) => set('priority', e.target.value)}
              >
                {['URGENT', 'HIGH', 'NORMAL', 'LOW'].map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </>
          )}
          {kind === 'ADD_OR_CHANGE_ORDER' && (
            <>
              <FieldInput label="Order ID (leave blank to add new)" name="order_id" onChange={set} />
              <FieldInput label="Product ID (required when adding)" name="product_id" onChange={set} />
              <FieldInput label="Quantity" name="quantity" type="number" onChange={set} />
              <FieldInput label="Due date (YYYY-MM-DD)" name="due_date" onChange={set} />
            </>
          )}
        </div>

        <button type="button" onClick={() => void onRun()} disabled={busy} aria-label="Run simulation">
          {busy ? 'Simulating…' : 'Run simulation'}
        </button>
      </section>

      {error && (
        <p role="alert" className="whatif-error">
          {error}
        </p>
      )}

      {result && (
        <section aria-labelledby="whatif-result-heading" className="whatif-result">
          <h3 id="whatif-result-heading">Simulation result (vs. current ACTIVE plan)</h3>
          <ul>
            <li>Feasibility: {result.feasibility}</li>
            <li>Late orders: {result.late_order_count} ({deltaLabel(result.late_order_count_delta)})</li>
            <li>
              Total tardiness (min): {result.total_tardiness_minutes} (
              {deltaLabel(result.total_tardiness_delta_minutes)})
            </li>
            <li>
              New unschedulable jobs:{' '}
              {result.new_unschedulable_jobs.length === 0
                ? 'none'
                : result.new_unschedulable_jobs.join(', ')}
            </li>
            <li>
              Late orders:{' '}
              {result.delayed_order_ids.length === 0 ? 'none' : result.delayed_order_ids.join(', ')}
            </li>
          </ul>
          <button
            type="button"
            onClick={() => void onAdopt()}
            disabled={busy}
            aria-label="Generate formal proposal from this scenario"
          >
            Generate formal proposal from this scenario
          </button>
          {adopted && (
            <p className="whatif-adopted" role="status">
              Proposal {adopted.plan_id} generated ({adopted.status}); go to the Approval view to handle it.
            </p>
          )}
        </section>
      )}
    </section>
  );
}

function FieldInput({
  label,
  name,
  type = 'text',
  onChange,
}: {
  label: string;
  name: string;
  type?: string;
  onChange: (name: string, value: string) => void;
}) {
  const id = `whatif-field-${name}`;
  return (
    <div className="whatif-field">
      <label htmlFor={id}>{label}</label>
      <input id={id} type={type} onChange={(e) => onChange(name, e.target.value)} />
    </div>
  );
}
