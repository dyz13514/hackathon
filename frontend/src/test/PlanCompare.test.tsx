import { render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { PlanCompare as PlanCompareData } from '../api/plans';

// mock 隔离网络：本套测试守的是「视图拿到对比结果后画对了什么」，不测后端。
vi.mock('../api/plans', async () => {
  const actual = await vi.importActual<typeof import('../api/plans')>('../api/plans');
  return { ...actual, comparePlans: vi.fn() };
});

import { comparePlans } from '../api/plans';
import { operationSequenceFromJobId, PlanCompare, toGanttJobs } from '../routes/PlanCompare';

const COMPARE: PlanCompareData = {
  plan_id_a: 'PLAN-active',
  plan_id_b: 'PLAN-revised',
  churn_ratio: 0.5,
  added_count: 1,
  removed_count: 1,
  moved_count: 1,
  reassigned_count: 1,
  unchanged_count: 1,
  changes: [
    {
      job_id: 'ORD-001-OP1',
      order_id: 'ORD-001',
      change: 'UNCHANGED',
      a_machine_id: 'CNC-01',
      a_worker_id: 'W-01',
      a_start_time: '2026-03-02T08:00:00',
      a_end_time: '2026-03-02T09:00:00',
      b_machine_id: 'CNC-01',
      b_worker_id: 'W-01',
      b_start_time: '2026-03-02T08:00:00',
      b_end_time: '2026-03-02T09:00:00',
    },
    {
      job_id: 'ORD-002-OP1',
      order_id: 'ORD-002',
      change: 'REASSIGNED',
      a_machine_id: 'CNC-01',
      a_worker_id: 'W-01',
      a_start_time: '2026-03-02T09:00:00',
      a_end_time: '2026-03-02T10:00:00',
      b_machine_id: 'CNC-02',
      b_worker_id: 'W-01',
      b_start_time: '2026-03-02T09:00:00',
      b_end_time: '2026-03-02T10:00:00',
    },
    {
      job_id: 'ORD-003-OP1',
      order_id: 'ORD-003',
      change: 'MOVED',
      a_machine_id: 'MILL-01',
      a_worker_id: 'W-02',
      a_start_time: '2026-03-02T08:00:00',
      a_end_time: '2026-03-02T09:00:00',
      b_machine_id: 'MILL-01',
      b_worker_id: 'W-02',
      b_start_time: '2026-03-02T10:00:00',
      b_end_time: '2026-03-02T11:00:00',
    },
    {
      job_id: 'ORD-004-OP1',
      order_id: 'ORD-004',
      change: 'REMOVED',
      a_machine_id: 'MILL-01',
      a_worker_id: 'W-02',
      a_start_time: '2026-03-02T11:00:00',
      a_end_time: '2026-03-02T12:00:00',
      b_machine_id: null,
      b_worker_id: null,
      b_start_time: null,
      b_end_time: null,
    },
    {
      job_id: 'ORD-005-OP1',
      order_id: 'ORD-005',
      change: 'ADDED',
      a_machine_id: null,
      a_worker_id: null,
      a_start_time: null,
      a_end_time: null,
      b_machine_id: 'CNC-02',
      b_worker_id: 'W-03',
      b_start_time: '2026-03-02T12:00:00',
      b_end_time: '2026-03-02T13:00:00',
    },
  ],
  decision_evidence: [
    {
      job_id: 'ORD-002-OP1',
      trigger: 'Resource reassignment',
      constraint: 'MACHINE_UNAVAILABLE',
      resources: ['machine:CNC-01→CNC-02'],
    },
    {
      job_id: 'ORD-003-OP1',
      trigger: 'Start time adjustment',
      constraint: 'MACHINE_DOWNTIME',
      resources: ['machine:MILL-01'],
    },
  ],
};

function renderView(idA = 'PLAN-active', idB = 'PLAN-revised') {
  return render(
    <MemoryRouter>
      <PlanCompare planIdA={idA} planIdB={idB} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('PlanCompare 纯函数', () => {
  it('operationSequenceFromJobId 从 -OPn 取工序号，取不到回退 0', () => {
    expect(operationSequenceFromJobId('ORD-001-OP3')).toBe(3);
    expect(operationSequenceFromJobId('ORD-001')).toBe(0);
  });

  it('toGanttJobs(a) 跳过 A 中不存在的作业（ADDED）', () => {
    const jobs = toGanttJobs(COMPARE.changes, 'a');
    const ids = jobs.map((j) => j.job_id);
    expect(ids).toContain('ORD-004-OP1'); // REMOVED 在 A 有
    expect(ids).not.toContain('ORD-005-OP1'); // ADDED 在 A 无
  });

  it('toGanttJobs(b) 跳过 B 中不存在的作业（REMOVED）', () => {
    const jobs = toGanttJobs(COMPARE.changes, 'b');
    const ids = jobs.map((j) => j.job_id);
    expect(ids).toContain('ORD-005-OP1'); // ADDED 在 B 有
    expect(ids).not.toContain('ORD-004-OP1'); // REMOVED 在 B 无
  });

  it('toGanttJobs 使用对应侧的机器（REASSIGNED 两侧机器不同）', () => {
    const a = toGanttJobs(COMPARE.changes, 'a').find((j) => j.job_id === 'ORD-002-OP1');
    const b = toGanttJobs(COMPARE.changes, 'b').find((j) => j.job_id === 'ORD-002-OP1');
    expect(a?.machine_id).toBe('CNC-01');
    expect(b?.machine_id).toBe('CNC-02');
  });
});

describe('PlanCompare 视图', () => {
  it('渲染变更摘要、两张甘特、逐作业变更与决策证据', async () => {
    vi.mocked(comparePlans).mockResolvedValue(COMPARE);
    renderView();

    // 摘要计数（R10.1 聚合）
    expect(await screen.findByText('50.0%')).toBeInTheDocument();

    // 两张甘特（A 对照 / B 建议）
    expect(screen.getByText('Plan A (baseline)')).toBeInTheDocument();
    expect(screen.getByText('Plan B (proposed)')).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: 'Schedule Gantt chart' })).toHaveLength(2);

    // 逐作业变更标签（R10.1）
    const changesTable = screen.getByRole('heading', { name: /Per-job changes/ });
    expect(changesTable).toBeInTheDocument();
    expect(screen.getByText('Reassigned')).toBeInTheDocument();
    expect(screen.getByText('Rescheduled')).toBeInTheDocument();
    expect(screen.getByText('Added')).toBeInTheDocument();
    expect(screen.getByText('Removed')).toBeInTheDocument();

    // 决策证据（R10.2）：每个 MOVED/REASSIGNED 一条
    const evidenceSection = screen
      .getByRole('heading', { name: /Decision evidence/ })
      .closest('section') as HTMLElement;
    expect(within(evidenceSection).getByText(/Resource reassignment/)).toBeInTheDocument();
    expect(within(evidenceSection).getByText(/Start time adjustment/)).toBeInTheDocument();
    expect(within(evidenceSection).getByText(/MACHINE_UNAVAILABLE/)).toBeInTheDocument();
    expect(within(evidenceSection).getByText(/machine:CNC-01→CNC-02/)).toBeInTheDocument();
  });

  it('PLAN_NOT_FOUND 时显示错误而不是空白', async () => {
    vi.mocked(comparePlans).mockRejectedValue(
      new ApiError(404, 'PLAN_NOT_FOUND', 'Plan not found'),
    );
    renderView();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/PLAN_NOT_FOUND/);
  });

  it('缺少任一计划 id 时给出提示，不发请求', async () => {
    renderView('PLAN-active', '');
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(vi.mocked(comparePlans)).not.toHaveBeenCalled();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(comparePlans).mockResolvedValue(COMPARE);
    const { container } = renderView();
    await screen.findByText('50.0%');

    const results = await axe.run(container, {
      rules: {
        // jsdom 不渲染真实布局，颜色对比类规则会误报，关掉这一类；结构性规则保留。
        'color-contrast': { enabled: false },
      },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
