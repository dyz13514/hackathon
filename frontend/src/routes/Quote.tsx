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
          setError('当前没有 ACTIVE 计划。请先生成并批准一个计划后再报价。');
        } else if (err instanceof ApiError && err.code === 'SCENARIO_INVALID_MUTATION') {
          setError(`无法报价：${err.message}`);
        } else {
          setError(
            err instanceof ApiError
              ? `报价失败（${err.code}）：${err.message}`
              : '报价失败：后端服务不可用。',
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
      <h2 id="quote-heading">可承诺交期报价</h2>
      <p className="quote-hint">
        输入产品、数量与期望交期，系统在只读沙箱里模拟把这笔询价排进当前计划，给出最早可承诺完工日
        与对现有订单的影响。报价不会改动任何生产数据或计划。
      </p>

      <form onSubmit={(e) => void onSubmit(e)} className="quote-form">
        <div className="field">
          <label htmlFor="quote-product">产品 ID</label>
          <input
            id="quote-product"
            type="text"
            required
            value={productId}
            onChange={(e) => setProductId(e.target.value)}
            placeholder="例如 PRD-BRACKET"
          />
        </div>
        <div className="field">
          <label htmlFor="quote-quantity">数量</label>
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
          <label htmlFor="quote-due">期望交期</label>
          <input
            id="quote-due"
            type="datetime-local"
            required
            value={desiredDue}
            onChange={(e) => setDesiredDue(e.target.value)}
          />
        </div>
        <button type="submit" disabled={busy} aria-label="计算可承诺交期">
          {busy ? '计算中…' : '计算可承诺交期'}
        </button>
      </form>

      {error && (
        <p role="alert" className="quote-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {result && (
        <section aria-labelledby="quote-result-heading" className="quote-result" role="status">
          <h3 id="quote-result-heading">报价结果</h3>
          {!result.feasible ? (
            <p className="quote-infeasible">
              <span aria-hidden="true">⛔ </span>
              这笔询价在当前产能下**无法排入**。{result.constraint_reason}
            </p>
          ) : result.desired_date_met ? (
            <p className="quote-ok">
              <span aria-hidden="true">✅ </span>
              可满足期望交期。最早可承诺完工时刻：<strong>{fmt(result.earliest_completion)}</strong>
            </p>
          ) : (
            <p className="quote-late">
              <span aria-hidden="true">⚠ </span>
              期望交期无法满足。最早可行完工时刻：
              <strong>{fmt(result.earliest_completion)}</strong>
              {result.constraint_reason && <span>（{result.constraint_reason}）</span>}
            </p>
          )}
          <ul className="quote-impact">
            <li>
              被推迟的既有订单：
              {result.deferred_order_ids.length === 0
                ? '无'
                : result.deferred_order_ids.join('、')}
            </li>
            <li>
              总拖期变化：{result.total_tardiness_delta_minutes} 分钟
              {result.total_tardiness_delta_minutes > 0
                ? '（变差）'
                : result.total_tardiness_delta_minutes < 0
                  ? '（改善）'
                  : '（不变）'}
            </li>
          </ul>
        </section>
      )}
    </section>
  );
}
