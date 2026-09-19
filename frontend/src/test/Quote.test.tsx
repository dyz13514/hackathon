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
  constraint_reason: '期望交期 2026-03-02 无法满足；最早可承诺完工时刻为 2026-03-02T12:11:00。',
};

function fillAndSubmit(): void {
  fireEvent.change(screen.getByLabelText('产品 ID'), { target: { value: 'PRD-BRACKET' } });
  fireEvent.change(screen.getByLabelText('数量'), { target: { value: '3' } });
  fireEvent.change(screen.getByLabelText('期望交期'), {
    target: { value: '2026-03-14T08:00' },
  });
  fireEvent.click(screen.getByRole('button', { name: /计算可承诺交期/ }));
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
    expect(await screen.findByText(/可满足期望交期/)).toBeInTheDocument();
    expect(screen.getByText(/被推迟的既有订单：无/)).toBeInTheDocument();
  });

  it('期望交期不可满足时显示最早可行时刻 + 约束原因 + 被推迟订单（R17.2/R17.3）', async () => {
    vi.mocked(quotePromiseDate).mockResolvedValue(LATE);
    render(<Quote />);
    fillAndSubmit();
    await screen.findByText(/期望交期无法满足/);
    expect(screen.getByText(/最早可承诺完工时刻/)).toBeInTheDocument();
    expect(screen.getByText(/ORD-005/)).toBeInTheDocument();
    expect(screen.getByText(/160 分钟（变差）/)).toBeInTheDocument();
  });

  it('不可行时显示无法排入 + 约束原因', async () => {
    vi.mocked(quotePromiseDate).mockResolvedValue({
      ...MET,
      feasible: false,
      earliest_completion: null,
      desired_date_met: false,
      constraint_reason: '其 3 道工序找不到可行的机器/工人/时间槽。',
    });
    render(<Quote />);
    fillAndSubmit();
    expect(await screen.findByText(/无法排入/)).toBeInTheDocument();
  });

  it('无 ACTIVE 计划时显示明确提示（不空白）', async () => {
    vi.mocked(quotePromiseDate).mockRejectedValue(
      new ApiError(409, 'NO_ACTIVE_PLAN', '无活动计划'),
    );
    render(<Quote />);
    fillAndSubmit();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/没有 ACTIVE 计划/);
  });

  it('产品不存在时显示 422 原因', async () => {
    vi.mocked(quotePromiseDate).mockRejectedValue(
      new ApiError(422, 'SCENARIO_INVALID_MUTATION', '产品 PRD-X 不存在，无法报价。'),
    );
    render(<Quote />);
    fillAndSubmit();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/无法报价/);
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
