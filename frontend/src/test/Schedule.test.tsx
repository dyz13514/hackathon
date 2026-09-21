import { fireEvent, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PlanDetail } from '../api/plans';
import { groupByMachine, timeSpan } from '../components/Gantt';

// 用 mock 隔离网络：本套测试守的是「组件拿到计划后画对了什么」，不测后端。
vi.mock('../api/plans', async () => {
  const actual = await vi.importActual<typeof import('../api/plans')>('../api/plans');
  return { ...actual, generatePlan: vi.fn() };
});

import { generatePlan } from '../api/plans';
import { Schedule } from '../routes/Schedule';

const PLAN: PlanDetail = {
  plan_id: 'PLAN-abc',
  production_date: '2026-03-02',
  status: 'PENDING_APPROVAL',
  plan_version: 1,
  origin: 'PLAN_GENERATION',
  input_snapshot_version: 1,
  generated_by_trace_id: 'TRACE-x',
  feasibility: 'PARTIAL',
  scheduled_jobs: [
    {
      job_id: 'ORD-001-OP1',
      order_id: 'ORD-001',
      product_id: 'PRD-HOUSING',
      operation_sequence: 1,
      machine_id: 'CNC-01',
      worker_id: 'W-01',
      start_time: '2026-03-02T08:00:00',
      end_time: '2026-03-02T09:00:00',
      setup_minutes: 20,
      changeover_minutes: 15,
    },
    {
      job_id: 'ORD-002-OP1',
      order_id: 'ORD-002',
      product_id: 'PRD-BRACKET',
      operation_sequence: 1,
      machine_id: 'MILL-01',
      worker_id: 'W-02',
      start_time: '2026-03-02T08:30:00',
      end_time: '2026-03-02T10:00:00',
      setup_minutes: 0,
      changeover_minutes: 0,
    },
  ],
  unschedulable_jobs: [
    {
      job_id: 'ORD-009-OP1',
      order_id: 'ORD-009',
      blocking_reason: 'MATERIAL_INSUFFICIENT',
      unblock_suggestion: { material_id: 'MAT-STEEL-01', shortfall_quantity: 40 },
    },
  ],
  objective_breakdown: {
    components: [],
    total_score: 123.4,
    preference_contributions: [],
    weight_overrides_applied: [],
  },
  baseline_comparison: {
    baseline_plan_id: 'PLAN-base',
    snapshot_version: 1,
    on_time_rate: 0.9,
    baseline_on_time_rate: 0.6,
    total_tardiness_minutes: 47,
    baseline_total_tardiness_minutes: 210,
    late_order_count: 1,
    baseline_late_order_count: 4,
  },
};

afterEach(() => {
  vi.clearAllMocks();
});

describe('Gantt 纯函数', () => {
  it('按机器分组，机器 ID 升序、组内按起始时刻升序', () => {
    const rows = groupByMachine(PLAN.scheduled_jobs);
    expect(rows.map((r) => r.machineId)).toEqual(['CNC-01', 'MILL-01']);
    expect(rows[0]?.jobs).toHaveLength(1);
  });

  it('时间跨度覆盖全部作业', () => {
    const span = timeSpan(PLAN.scheduled_jobs);
    expect(span.min).toBe(Date.parse('2026-03-02T08:00:00'));
    expect(span.max).toBe(Date.parse('2026-03-02T10:00:00'));
  });

  it('空作业集回退到一个非零窗口，不除零', () => {
    const span = timeSpan([]);
    expect(span.max).toBeGreaterThan(span.min);
  });
});

describe('Schedule 视图', () => {
  it('生成后画出甘特图、基线对比与不可排产抽屉', async () => {
    vi.mocked(generatePlan).mockResolvedValue(PLAN);
    render(<Schedule />);

    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));

    // 甘特图（role=img）
    expect(await screen.findByRole('img', { name: 'Schedule Gantt chart' })).toBeInTheDocument();
    // 基线对比区显示按期率与拖期
    expect(screen.getByRole('heading', { name: 'Baseline comparison (vs. FCFS)' })).toBeInTheDocument();
    expect(screen.getByText('90.0%')).toBeInTheDocument();
    expect(screen.getByText('60.0%')).toBeInTheDocument();
    // 不可排产抽屉列出作业与量化解锁条件（R8.6）
    expect(screen.getByRole('heading', { name: /Unschedulable jobs/ })).toBeInTheDocument();
    expect(screen.getByText(/MATERIAL_INSUFFICIENT/)).toBeInTheDocument();
    expect(screen.getByText(/shortfall_quantity=40/)).toBeInTheDocument();
  });

  it('生成失败时显示错误而不是空白', async () => {
    vi.mocked(generatePlan).mockRejectedValue(new Error('boom'));
    render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(generatePlan).mockResolvedValue(PLAN);
    const { container } = render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));
    await screen.findByRole('img', { name: 'Schedule Gantt chart' });

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
