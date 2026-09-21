import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { PromiseDateResult } from '../api/quotes';

// mock 隔离网络：本套测试守的是「视图提交询价、渲染报价结果」，不测后端。
vi.mock('../api/quotes', () => ({ quotePromiseDate: vi.fn() }));

import { quotePromiseDate } from '../api/quotes';
import { Quote } from '../routes/Quote';

const MET: PromiseDateResult = {
  feasible: true,
  earliest_completion: '2026-03-04T07:43:00',
  desired_due_date: '2026-03-14T08:00:00',
  desired_date_met: true,
  deferred_order_ids: [],
  total_tardiness_minutes: 100,
  active_total_tardiness_minutes: 100,
  total_tardiness_delta_minutes: 0,
  constraint_reason: null,
};

const LATE: PromiseDateResult = {
  feasible: true,
  earliest_completion: '2026-03-02T12:11:00',
  desired_due_date: '2026-03-02T08:00:00',
  desired_date_met: false,
  deferred_order_ids: ['ORD-005'],
  total_tardiness_minutes: 260,
  active_total_tardiness_minutes: 100,
  total_tardiness_delta_minutes: 160,
  constraint_reason: 'Desired due date 2026-03-02 cannot be met; earliest committable completion is 2026-03-02T12:11:00.',
};

function fillAndSubmit(): void {
  fireEvent.change(screen.getByLabelText('Product ID'), { target: { value: 'PRD-BRACKET' } });
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '3' } });
  fireEvent.change(screen.getByLabelText('Desired due date'), {
    target: { value: '2026-03-14T08:00' },
  });
  fireEvent.click(screen.getByRole('button', { name: /Calculate promise date/ }));
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('Quote 视图', () => {
  it('可满足期望交期时显示最早可承诺完工时刻（R17.1）', async () => {
    vi.mocked(quotePromiseDate).mockResolvedValue(MET);
    render(<Quote />);
    fillAndSubmit();
    await waitFor(() => expect(quotePromiseDate).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Desired due date can be met/)).toBeInTheDocument();
    expect(screen.getByText(/Existing orders deferred: none/)).toBeInTheDocument();
  });

  it('期望交期不可满足时显示最早可行时刻 + 约束原因 + 被推迟订单（R17.2/R17.3）', async () => {
    vi.mocked(quotePromiseDate).mockResolvedValue(LATE);
    render(<Quote />);
    fillAndSubmit();
    await screen.findByText(/Desired due date cannot be met/);
    expect(screen.getByText(/Earliest feasible completion/)).toBeInTheDocument();
    expect(screen.getByText(/ORD-005/)).toBeInTheDocument();
    expect(screen.getByText(/160 min \(worse\)/)).toBeInTheDocument();
  });

  it('不可行时显示无法排入 + 约束原因', async () => {
    vi.mocked(quotePromiseDate).mockResolvedValue({
      ...MET,
      feasible: false,
      earliest_completion: null,
      desired_date_met: false,
      constraint_reason: 'No feasible machine/worker/time slot found for its 3 operations.',
    });
    render(<Quote />);
    fillAndSubmit();
    expect(await screen.findByText(/cannot be scheduled/)).toBeInTheDocument();
  });

  it('无 ACTIVE 计划时显示明确提示（不空白）', async () => {
    vi.mocked(quotePromiseDate).mockRejectedValue(
      new ApiError(409, 'NO_ACTIVE_PLAN', 'No active plan'),
    );
    render(<Quote />);
    fillAndSubmit();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/no ACTIVE plan/);
  });

  it('产品不存在时显示 422 原因', async () => {
    vi.mocked(quotePromiseDate).mockRejectedValue(
      new ApiError(422, 'SCENARIO_INVALID_MUTATION', 'Product PRD-X does not exist; cannot quote.'),
    );
    render(<Quote />);
    fillAndSubmit();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/Cannot quote/);
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(quotePromiseDate).mockResolvedValue(MET);
    const { container } = render(<Quote />);
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
