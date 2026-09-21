import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { PlanDetail, PlanSummary } from '../api/plans';

// mock 隔离网络：本套测试守的是「审批视图渲染与动作接线」，不测后端。
vi.mock('../api/plans', async () => {
  const actual = await vi.importActual<typeof import('../api/plans')>('../api/plans');
  return {
    ...actual,
    listPending: vi.fn(),
    getPlan: vi.fn(),
    approvePlan: vi.fn(),
    rejectPlan: vi.fn(),
    modifyPlan: vi.fn(),
  };
});

import { approvePlan, getPlan, listPending, modifyPlan } from '../api/plans';
import { Approval } from '../routes/Approval';

const SUMMARY: PlanSummary = {
  plan_id: 'PLAN-abc',
  production_date: '2026-03-02',
  status: 'PENDING_APPROVAL',
  plan_version: 2,
  origin: 'PLAN_GENERATION',
  feasibility: 'PARTIAL',
  input_snapshot_version: 3,
  generated_by_trace_id: 'TRACE-x',
};

const PLAN: PlanDetail = {
  plan_id: 'PLAN-abc',
  production_date: '2026-03-02',
  status: 'PENDING_APPROVAL',
  plan_version: 2,
  origin: 'PLAN_GENERATION',
  input_snapshot_version: 3,
  generated_by_trace_id: 'TRACE-x',
  feasibility: 'PARTIAL',
  scheduled_jobs: [],
  unschedulable_jobs: [
    {
      job_id: 'ORD-009-OP1',
      order_id: 'ORD-009',
      blocking_reason: 'MATERIAL_INSUFFICIENT',
      unblock_suggestion: { shortfall_quantity: 40 },
    },
  ],
  objective_breakdown: {
    components: [
      { name: 'total_tardiness', raw_value: 47, weight: -1.0, weighted_contribution: -47 },
      { name: 'on_time_rate', raw_value: 0.9, weight: 100, weighted_contribution: 90 },
    ],
    total_score: 43,
    preference_contributions: [],
    weight_overrides_applied: [
      {
        rule_id: 'rule-1',
        component: 'total_tardiness_minutes',
        multiplier: 1.5,
        original_weight: 1,
        new_weight: 1.5,
      },
    ],
  },
  baseline_comparison: null,
};

function mockLoaded() {
  vi.mocked(listPending).mockResolvedValue([SUMMARY]);
  vi.mocked(getPlan).mockResolvedValue(PLAN);
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('Approval 视图', () => {
  it('渲染计划摘要、目标评分全分量表与总分（R7.3）', async () => {
    mockLoaded();
    render(<Approval />);

    expect(await screen.findByRole('heading', { name: 'Plan summary' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Objective score breakdown' })).toBeInTheDocument();
    // 全分量逐行
    expect(screen.getByRole('rowheader', { name: 'total_tardiness' })).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'on_time_rate' })).toBeInTheDocument();
    // 权重覆盖标注
    expect(screen.getByText(/rule-1/)).toBeInTheDocument();
  });

  it('醒目标注不可排产作业数与受影响订单（R8.6）', async () => {
    mockLoaded();
    render(<Approval />);
    expect(await screen.findByRole('heading', { name: /Unschedulable jobs: 1/ })).toBeInTheDocument();
    expect(screen.getByText(/ORD-009/)).toBeInTheDocument();
  });

  it('APPROVE 调用后端并显示成功', async () => {
    mockLoaded();
    vi.mocked(approvePlan).mockResolvedValue({ plan_id: 'PLAN-abc', status: 'ACTIVE' });
    render(<Approval />);

    fireEvent.click(await screen.findByRole('button', { name: 'Approve plan PLAN-abc' }));
    await waitFor(() => expect(approvePlan).toHaveBeenCalledWith('PLAN-abc', 2));
    expect(await screen.findByRole('status')).toHaveTextContent('activated');
  });

  it('REJECT 按钮在理由少于 5 字符时禁用（R11.4）', async () => {
    mockLoaded();
    render(<Approval />);
    const rejectBtn = await screen.findByRole('button', { name: 'Reject plan PLAN-abc' });
    expect(rejectBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Rejection reason'), { target: { value: 'Insufficient material, needs to be deferred' } });
    expect(rejectBtn).toBeEnabled();
  });

  it('MODIFY 提交 5 类结构化修改之一', async () => {
    mockLoaded();
    vi.mocked(modifyPlan).mockResolvedValue({
      new_plan_id: 'PLAN-def',
      source_plan_id: 'PLAN-abc',
      status: 'PENDING_APPROVAL',
    });
    render(<Approval />);

    await screen.findByRole('heading', { name: 'Approval actions' });
    fireEvent.change(screen.getByLabelText('Modification type'), { target: { value: 'LOCK_JOB' } });
    fireEvent.change(screen.getByLabelText('Job id'), { target: { value: 'ORD-001-OP1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Submit modification for plan PLAN-abc' }));

    await waitFor(() =>
      expect(modifyPlan).toHaveBeenCalledWith('PLAN-abc', [
        { kind: 'LOCK_JOB', job_id: 'ORD-001-OP1' },
      ]),
    );
    expect(await screen.findByRole('status')).toHaveTextContent('PLAN-def');
  });

  it('STALE_PROPOSAL 显示变化提示、两个版本号与重新生成入口（R12.4）', async () => {
    mockLoaded();
    vi.mocked(approvePlan).mockRejectedValue(
      new ApiError(409, 'STALE_PROPOSAL', 'Changed', {
        proposal_version: 3,
        current_version: 5,
      }),
    );
    render(<Approval />);

    fireEvent.click(await screen.findByRole('button', { name: 'Approve plan PLAN-abc' }));
    const alert = await screen.findByText(/The input data this proposal relies on has changed/);
    expect(alert).toBeInTheDocument();
    // 两个版本号
    const stale = alert.closest('.approval-stale') as HTMLElement;
    expect(stale).toHaveTextContent('Proposal version: 3');
    expect(stale).toHaveTextContent('current data version: 5');
    // 重新生成入口
    expect(screen.getByRole('link', { name: 'Regenerate from latest data' })).toBeInTheDocument();
  });

  it('没有待审批计划时给出空态', async () => {
    vi.mocked(listPending).mockResolvedValue([]);
    render(<Approval />);
    expect(await screen.findByText('There are no plans pending approval.')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    mockLoaded();
    const { container } = render(<Approval />);
    await screen.findByRole('heading', { name: 'Approval actions' });

    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
