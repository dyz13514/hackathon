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
  ADD_OR_CHANGE_ORDER: '新增订单 / 改交期',
  SET_MACHINE_UNAVAILABLE: '机器不可用',
  CHANGE_MATERIAL_AVAILABILITY: '改物料可用量',
  SET_WORKER_UNAVAILABLE: '工人不可用',
  CHANGE_ORDER_PRIORITY: '改订单优先级',
};

const KINDS = Object.keys(KIND_LABEL) as MutationKind[];

function deltaLabel(delta: number): string {
  if (delta > 0) return `+${delta}（变差）`;
  if (delta < 0) return `${delta}（变好）`;
  return '0（不变）';
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
          ? `推演失败（${err.code}）：${err.message}`
          : '推演失败：后端服务不可用。',
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
          ? `采纳失败（${err.code}）：${err.message}`
          : '采纳失败：后端服务不可用。',
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
      if (err instanceof ApiError && err.code === 'UNSUPPORTED_SCENARIO') {
        const kinds = (err.details.supported_kinds as string[] | undefined) ?? [];
        setTranslateError(
          `无法把该提问映射到支持的场景类型。支持的类型：${kinds.join('、')}。请改用下方结构化表单。`,
        );
      } else if (err instanceof ApiError && err.code === 'LLM_UNAVAILABLE_USE_STRUCTURED_FORM') {
        setNlEnabled(false);
        setTranslateError('LLM 处于降级模式，自然语言翻译不可用。请使用下方结构化表单。');
      } else {
        setTranslateError(
          err instanceof ApiError
            ? `翻译失败（${err.code}）：${err.message}`
            : '翻译失败：后端服务不可用。',
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
          ? `推演失败（${err.code}）：${err.message}`
          : '推演失败：后端服务不可用。',
      );
    } finally {
      setBusy(false);
    }
  }, [translation]);

  return (
    <section aria-labelledby="whatif-heading" className="whatif">
      <h2 id="whatif-heading">What-if 推演</h2>

      {nlEnabled && (
        <section aria-labelledby="whatif-nl-heading" className="whatif-nl">
          <h3 id="whatif-nl-heading">用自然语言提问（可选）</h3>
          <p className="whatif-nl-hint">
            例如「如果 CNC-01 明天上午停机 6 小时会怎样」。翻译结果会先展示给你确认，
            确认后才会执行——不会直接改动任何计划。
          </p>
          <label htmlFor="whatif-nl-query">自然语言 What-if 提问</label>
          <textarea
            id="whatif-nl-query"
            className="whatif-nl-input"
            rows={2}
            value={nlQuery}
            onChange={(e) => setNlQuery(e.target.value)}
            placeholder="用一句话描述你想推演的假设……"
          />
          <button
            type="button"
            onClick={() => void onTranslate()}
            disabled={busy || !nlQuery.trim()}
            aria-label="翻译为结构化场景"
          >
            {busy ? '翻译中…' : '翻译为结构化场景'}
          </button>

          {translateError && (
            <p role="alert" className="whatif-error">
              <span aria-hidden="true">⚠ </span>
              {translateError}
            </p>
          )}

          {translation && (
            <div className="whatif-translation-card" role="group" aria-label="翻译结果确认卡">
              <h4>翻译结果（请确认后执行）</h4>
              {translation.injection_suspected && (
                <p role="alert" className="whatif-injection-warning">
                  <span aria-hidden="true">⚠ </span>
                  你的提问中检测到疑似提示注入模式；系统已作为普通数据处理并记入审计，翻译不受其指令影响。
                </p>
              )}
              <p className="whatif-translation-echo">
                原始提问（作为数据回显）：
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
                  aria-label="确认并执行推演"
                >
                  确认并推演
                </button>
                <button
                  type="button"
                  onClick={() => setTranslation(null)}
                  disabled={busy}
                  aria-label="放弃翻译结果"
                >
                  放弃
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      <section aria-labelledby="whatif-form-heading" className="whatif-form">
        <h3 id="whatif-form-heading">场景变更</h3>
        <label htmlFor="whatif-kind">变更类型</label>
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
                label={kind === 'SET_MACHINE_UNAVAILABLE' ? '机器 ID' : '工人 ID'}
                name={kind === 'SET_MACHINE_UNAVAILABLE' ? 'machine_id' : 'worker_id'}
                onChange={set}
              />
              <FieldInput label="开始时间 (ISO)" name="start_time" onChange={set} />
              <FieldInput label="结束时间 (ISO)" name="end_time" onChange={set} />
            </>
          )}
          {kind === 'CHANGE_MATERIAL_AVAILABILITY' && (
            <>
              <FieldInput label="物料 ID" name="material_id" onChange={set} />
              <FieldInput label="可用量" name="quantity_available" type="number" onChange={set} />
            </>
          )}
          {kind === 'CHANGE_ORDER_PRIORITY' && (
            <>
              <FieldInput label="订单 ID" name="order_id" onChange={set} />
              <label htmlFor="whatif-priority">优先级</label>
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
              <FieldInput label="订单 ID（留空=新增）" name="order_id" onChange={set} />
              <FieldInput label="产品 ID（新增必填）" name="product_id" onChange={set} />
              <FieldInput label="数量" name="quantity" type="number" onChange={set} />
              <FieldInput label="交期 (YYYY-MM-DD)" name="due_date" onChange={set} />
            </>
          )}
        </div>

        <button type="button" onClick={() => void onRun()} disabled={busy} aria-label="运行推演">
          {busy ? '推演中…' : '运行推演'}
        </button>
      </section>

      {error && (
        <p role="alert" className="whatif-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {result && (
        <section aria-labelledby="whatif-result-heading" className="whatif-result">
          <h3 id="whatif-result-heading">推演结果（对比当前 ACTIVE 计划）</h3>
          <ul>
            <li>可行性：{result.feasibility}</li>
            <li>迟交订单数：{result.late_order_count}（{deltaLabel(result.late_order_count_delta)}）</li>
            <li>
              总拖期分钟：{result.total_tardiness_minutes}（
              {deltaLabel(result.total_tardiness_delta_minutes)}）
            </li>
            <li>
              新增不可排产作业：
              {result.new_unschedulable_jobs.length === 0
                ? '无'
                : result.new_unschedulable_jobs.join('、')}
            </li>
            <li>
              迟交订单：
              {result.delayed_order_ids.length === 0 ? '无' : result.delayed_order_ids.join('、')}
            </li>
          </ul>
          <button
            type="button"
            onClick={() => void onAdopt()}
            disabled={busy}
            aria-label="以此场景生成正式提案"
          >
            以此场景生成正式提案
          </button>
          {adopted && (
            <p className="whatif-adopted" role="status">
              已生成提案 {adopted.plan_id}（{adopted.status}），请到审批界面处理。
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
