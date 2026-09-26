/**
 * 可承诺交期报价视图（design.md Components §6 `/quote`，任务 13.6，R17）。
 *
 * 规划员/销售填入 `product_id` + `quantity` + 期望交期，提交后由后端**只读沙箱**实算最早可承诺
 * 完工日、被推迟的既有订单、总拖期变化；期望交期不可满足时显示最早可行日期与具体约束原因（R17.3）。
 * 报价不改动任何生产数据或计划（R17.4）。加载态、错误态（含无 ACTIVE 计划 / 产品不存在）、
 * 结果态都显式呈现。
 *
 * 可访问性（R27.9）：表单控件有 `<label>`；可行/不可行、满足/不满足期望除颜色外带文字与图标；
 * 错误用 `role="alert"`、结果用 `role="status"`。
 */

import { type FormEvent, useCallback, useState } from 'react';

import { ApiError } from '../api/client';
import { type PromiseDateResult, quotePromiseDate } from '../api/quotes';

function fmt(iso: string | null): string {
  if (iso == null) return '—';
  return new Date(iso).toLocaleString();
}

export function Quote() {
  const [productId, setProductId] = useState('');
  const [quantity, setQuantity] = useState('1');
  const [desiredDue, setDesiredDue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PromiseDateResult | null>(null);

  const onSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setError(null);
      setResult(null);
      try {
        // datetime-local 值形如 "2026-03-10T08:00"，补秒后作为 ISO 传后端。
        const iso = desiredDue.length === 16 ? `${desiredDue}:00` : desiredDue;
        setResult(
          await quotePromiseDate({
            product_id: productId.trim(),
            quantity: Number(quantity),
            desired_due_date: iso,
          }),
        );
      } catch (err) {
        if (err instanceof ApiError && err.code === 'NO_ACTIVE_PLAN') {
          setError('There is no ACTIVE plan. Generate and approve a plan before requesting a quote.');
        } else if (err instanceof ApiError && err.code === 'SCENARIO_INVALID_MUTATION') {
          setError(`Cannot quote: ${err.message}`);
        } else {
          setError(
            err instanceof ApiError
              ? `Quote failed (${err.code}): ${err.message}`
              : 'Quote failed: backend service unavailable.',
          );
        }
      } finally {
        setBusy(false);
      }
    },
    [productId, quantity, desiredDue],
  );

  return (
    <section aria-labelledby="quote-heading" className="quote">
      <h2 id="quote-heading">Promise-date quote</h2>
      <p className="quote-hint">
        Enter a product, quantity and desired due date. The system simulates fitting this inquiry into the
        current plan inside a read-only sandbox and returns the earliest committable completion date and the
        impact on existing orders. Quoting does not modify any production data or plan.
      </p>

      <form onSubmit={(e) => void onSubmit(e)} className="quote-form">
        <div className="field">
          <label htmlFor="quote-product">Product ID</label>
          <input
            id="quote-product"
            type="text"
            required
            value={productId}
            onChange={(e) => setProductId(e.target.value)}
            placeholder="e.g. your product ID"
          />
        </div>
        <div className="field">
          <label htmlFor="quote-quantity">Quantity</label>
          <input
            id="quote-quantity"
            type="number"
            min={1}
            step="1"
            required
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="quote-due">Desired due date</label>
          <input
            id="quote-due"
            type="datetime-local"
            required
            value={desiredDue}
            onChange={(e) => setDesiredDue(e.target.value)}
          />
        </div>
        <button type="submit" disabled={busy} aria-label="Calculate promise date">
          {busy ? 'Calculating…' : 'Calculate promise date'}
        </button>
      </form>

      {error && (
        <p role="alert" className="quote-error">
          {error}
        </p>
      )}

      {result && (
        <section aria-labelledby="quote-result-heading" className="quote-result" role="status">
          <h3 id="quote-result-heading">Quote result</h3>
          {!result.feasible ? (
            <p className="quote-infeasible">
              This inquiry <strong>cannot be scheduled</strong> under current capacity. {result.constraint_reason}
            </p>
          ) : result.desired_date_met ? (
            <p className="quote-ok">
              Desired due date can be met. Earliest committable completion:{' '}
              <strong>{fmt(result.earliest_completion)}</strong>
            </p>
          ) : (
            <p className="quote-late">
              Desired due date cannot be met. Earliest feasible completion:{' '}
              <strong>{fmt(result.earliest_completion)}</strong>
              {result.constraint_reason && <span> ({result.constraint_reason})</span>}
            </p>
          )}
          <ul className="quote-impact">
            <li>
              Existing orders deferred:{' '}
              {result.deferred_order_ids.length === 0
                ? 'none'
                : result.deferred_order_ids.join(', ')}
            </li>
            <li>
              Total tardiness change: {result.total_tardiness_delta_minutes} min
              {result.total_tardiness_delta_minutes > 0
                ? ' (worse)'
                : result.total_tardiness_delta_minutes < 0
                  ? ' (better)'
                  : ' (unchanged)'}
            </li>
          </ul>
        </section>
      )}
    </section>
  );
}
