import { fireEvent, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { PlanDetail } from '../api/plans';
import { groupByMachine, timeSpan } from '../components/Gantt';

// 用 mock 隔离网络：本套测试守的是「组件拿到计划后画对了什么」，不测后端。
vi.mock('../api/plans', async () => {
  const actual = await vi.importActual<typeof import('../api/plans')>('../api/plans');
  return {
    ...actual, generatePlan: vi.fn(), getPlan: vi.fn(), getPlanExplanation: vi.fn(),
    listActive: vi.fn(), listPending: vi.fn(),
  };
});
vi.mock('../api/materials', () => ({ getMaterial: vi.fn(), updateMaterialAvailability: vi.fn() }));

import { generatePlan, getPlan, getPlanExplanation, listActive, listPending } from '../api/plans';
import { getMaterial, updateMaterialAvailability } from '../api/materials';
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
      // 与线上真实载荷同形：数量是长尾零字符串，单位单独一列
      unblock_suggestion: {
        material_id: 'MAT-STEEL-01',
        shortfall_quantity: '40.00000000000000000000',
        unit: 'kg',
        needed_before: '2026-03-04T17:00:00',
      },
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

beforeEach(() => {
  vi.mocked(listActive).mockResolvedValue([]);
  vi.mocked(listPending).mockResolvedValue([]);
  vi.mocked(getPlanExplanation).mockResolvedValue({
    plan_id: 'PLAN-abc', llm_mode: 'STUB', narrative: 'Offline fixture', numeric_check: 'FALLBACK',
    fallback_reason: 'STUB', decision_evidence: [],
    counterfactual: { kind: 'NO_TRADEOFF', reason: 'Initial plan', pivotal_job_id: null,
      component: null, current_value: null, counterfactual_value: null, selection_basis: null },
    assumptions: [], confidence: { level: 'MEDIUM', basis: 'Imported data snapshot' },
  });
  vi.mocked(getMaterial).mockResolvedValue({
    material_id: 'MAT-STEEL-01', name: 'Steel', unit: 'kg',
    quantity_available: '380', reserved_quantity: '20', input_snapshot_version: 1,
  });
  vi.mocked(updateMaterialAvailability).mockResolvedValue({
    material_id: 'MAT-STEEL-01', name: 'Steel', unit: 'kg',
    quantity_available: '1000', reserved_quantity: '20', input_snapshot_version: 2,
  });
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
  it('切回页面时从后端载入已审批计划，不显示空态', async () => {
    vi.mocked(listActive).mockResolvedValue([{
      plan_id: 'PLAN-abc', production_date: PLAN.production_date, status: 'ACTIVE',
      plan_version: 1, origin: 'PLAN_GENERATION', feasibility: PLAN.feasibility,
      input_snapshot_version: 1, generated_by_trace_id: null,
    }]);
    vi.mocked(getPlan).mockResolvedValue({ ...PLAN, status: 'ACTIVE' });
    render(<Schedule />);
    expect(await screen.findByRole('img', { name: 'Schedule Gantt chart' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Plan analysis' })).toBeInTheDocument();
    expect(screen.getByText(/2 scheduled jobs, 1 unschedulable jobs/)).toBeInTheDocument();
    expect(screen.getByText(/Status: ACTIVE/)).toBeInTheDocument();
    expect(screen.queryByText(/No demo plan is loaded automatically/)).not.toBeInTheDocument();
  });

  it('同时有生效计划和新待审计划时展示新提案，而非旧甘特图', async () => {
    vi.mocked(listActive).mockResolvedValue([{
      plan_id: 'PLAN-old', production_date: PLAN.production_date, status: 'ACTIVE',
      plan_version: 1, origin: 'RISK_MITIGATION', feasibility: PLAN.feasibility,
      input_snapshot_version: 1, generated_by_trace_id: null,
    }]);
    vi.mocked(listPending).mockResolvedValue([{
      plan_id: PLAN.plan_id, production_date: PLAN.production_date, status: 'PENDING_APPROVAL',
      plan_version: 1, origin: 'PLAN_GENERATION', feasibility: PLAN.feasibility,
      input_snapshot_version: 1, generated_by_trace_id: PLAN.generated_by_trace_id,
    }]);
    vi.mocked(getPlan).mockResolvedValue(PLAN);

    render(<Schedule />);

    expect(await screen.findByText(/Plan PLAN-abc is awaiting approval/)).toBeInTheDocument();
    expect(getPlan).toHaveBeenCalledWith('PLAN-abc');
    expect(getPlan).not.toHaveBeenCalledWith('PLAN-old');
    expect(screen.getByText(/Status: PENDING_APPROVAL/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Schedule Gantt chart' })).toBeInTheDocument();
  });

  it('只把数值校验通过的 LIVE 解释标为模型分析', async () => {
    vi.mocked(generatePlan).mockResolvedValue(PLAN);
    vi.mocked(getPlanExplanation).mockResolvedValue({
      plan_id: 'PLAN-abc', llm_mode: 'LIVE', narrative: 'Validated model conclusion', numeric_check: 'PASS',
      fallback_reason: null, decision_evidence: [],
      counterfactual: { kind: 'NO_TRADEOFF', reason: 'Initial plan', pivotal_job_id: null,
        component: null, current_value: null, counterfactual_value: null, selection_basis: null },
      assumptions: [], confidence: { level: 'MEDIUM', basis: 'Imported data snapshot' },
    });
    render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));
    expect(await screen.findByText('Validated model conclusion')).toBeInTheDocument();
    expect(screen.getByText(/LIVE model analysis · numeric check passed/)).toBeInTheDocument();
  });

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
    expect(screen.getByText(/Conclusion: this plan improves or matches FCFS/)).toBeInTheDocument();
    // 不可排产抽屉列出作业与量化解锁条件（R8.6）
    expect(screen.getByRole('heading', { name: /Unschedulable jobs/ })).toBeInTheDocument();
    expect(screen.getByText(/MATERIAL_INSUFFICIENT/)).toBeInTheDocument();
    expect(screen.getByText(/Shortfall: 40 kg/)).toBeInTheDocument();
    expect(screen.getByText(/Material: MAT-STEEL-01/)).toBeInTheDocument();
    expect(screen.getByText(/Needed before: /)).toBeInTheDocument();
  });

  it('基线指标相同时明确说明没有实测改进', async () => {
    vi.mocked(generatePlan).mockResolvedValue({
      ...PLAN,
      baseline_comparison: {
        ...PLAN.baseline_comparison!, baseline_on_time_rate: 0.9,
        baseline_total_tardiness_minutes: 47,
      },
    });
    render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));
    expect(await screen.findByText(/no measured improvement over FCFS/)).toBeInTheDocument();
  });

  it('缺料项可修改真实库存，但明确旧计划不会被直接改写', async () => {
    vi.mocked(generatePlan).mockResolvedValue(PLAN);
    render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));
    const quantity = await screen.findByRole('spinbutton', { name: /New total available quantity/ });
    fireEvent.change(quantity, { target: { value: '1000' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Reason for inventory correction' }), {
      target: { value: 'Inventory recount' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save inventory correction' }));
    expect(await screen.findByText(/This old plan still shows its original unschedulable job/)).toBeInTheDocument();
    expect(updateMaterialAvailability).toHaveBeenCalledWith('MAT-STEEL-01', 1000, 'Inventory recount');
    expect(screen.getByText(/Unschedulable jobs \(1\)/)).toBeInTheDocument();
  });

  it('生成失败时显示错误而不是空白', async () => {
    vi.mocked(generatePlan).mockRejectedValue(new Error('boom'));
    render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('已有待审批计划时显示现有甘特和审批入口，不重复创建', async () => {
    vi.mocked(generatePlan).mockRejectedValue(new ApiError(409, 'PENDING_PLAN_EXISTS', 'already pending', {
      existing_plan_id: 'PLAN-abc',
    }));
    vi.mocked(getPlan).mockResolvedValue(PLAN);
    render(<Schedule />);
    fireEvent.click(screen.getByRole('button', { name: 'Generate today’s plan' }));

    expect(await screen.findByRole('img', { name: 'Schedule Gantt chart' })).toBeInTheDocument();
    expect(getPlan).toHaveBeenCalledWith('PLAN-abc');
    expect(screen.getByRole('link', { name: 'approve or reject it' })).toHaveAttribute(
      'href', '/approval?plan_id=PLAN-abc',
    );
    expect(screen.getByRole('button', { name: 'Plan awaiting approval' })).toBeDisabled();
    expect(generatePlan).toHaveBeenCalledTimes(1);
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
