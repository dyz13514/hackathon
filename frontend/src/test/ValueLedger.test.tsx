import { render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ValueLedger as ValueLedgerData } from '../api/valueLedger';

// mock 隔离网络：本套测试守的是「视图拿到台账数据后画对了什么」，不测后端。
vi.mock('../api/valueLedger', async () => {
  const actual = await vi.importActual<typeof import('../api/valueLedger')>('../api/valueLedger');
  return { ...actual, getValueLedger: vi.fn() };
});

import { getValueLedger } from '../api/valueLedger';
import { ValueLedger } from '../routes/ValueLedger';

const LEDGER: ValueLedgerData = {
  auto_handled_count: 3,
  escalated_count: 1,
  total_decisions: 4,
  auto_handled_ratio: 0.75,
  decisions: [
    {
      assessment_id: 'IA-001',
      candidate_plan_id: 'PLAN-rev1',
      impact_class: 'IMPACT_MODERATE',
      autonomy_level: 'L3',
      execution_path: 'PROPOSED',
      decisive_predicates: ['changed_job_count=1 <= 2', 'churn_ratio=0.10 <= 0.20'],
    },
    {
      assessment_id: 'IA-002',
      candidate_plan_id: 'PLAN-rev2',
      impact_class: 'IMPACT_MAJOR',
      autonomy_level: 'L5',
      execution_path: 'ESCALATED',
      decisive_predicates: ['touches_high_priority=true'],
    },
  ],
  active_plan_id: 'PLAN-active',
  on_time_rate: 0.9,
  baseline_on_time_rate: 0.6,
  total_tardiness_minutes: 47,
  baseline_total_tardiness_minutes: 210,
  metrics: {
    plan_id: 'PLAN-active',
    measured_at: '2026-03-02T08:00:00',
    plan_generation_seconds: null,
    disruption_response_seconds: null,
    on_time_rate: 0.9,
    total_tardiness_minutes: 47,
    churn_ratio: null,
    manual_steps_eliminated: 5,
    auto_handled_count: 3,
    escalated_count: 1,
    llm_tokens_used: 1234,
    estimated_usd_cost: 0.0123,
    real_run_count: 2,
    project_real_run_cap: 150,
    real_run_remaining: 148,
    baseline_plan_generation_seconds: null,
    baseline_disruption_response_seconds: null,
    baseline_on_time_rate: 0.6,
    baseline_total_tardiness_minutes: 210,
    projected_hero_demo_usd: null,
    projected_build_total_usd: null,
    labels: {
      manual_steps_eliminated: 'MEASURED',
    },
  },
  manual_steps: [
    { action: 'spreadsheet_import', label: 'Spreadsheet import', rule: 'Counted as 1 step per import batch', count: 1 },
    { action: 'plan_generation', label: 'Plan generation', rule: 'Counted as 1 step per successful generation', count: 1 },
  ],
  kpis: [
    {
      kpi_id: 'K-03',
      metric_name: 'On-time delivery rate',
      current_value: '0.9',
      baseline_value: '0.6',
      delta: '',
      target_value: '≤ 60',
      label: 'MEASURED',
      measured_at: '2026-03-02T08:00:00',
    },
  ],
};

const EMPTY_LEDGER: ValueLedgerData = {
  auto_handled_count: 0,
  escalated_count: 0,
  total_decisions: 0,
  auto_handled_ratio: 0,
  decisions: [],
  active_plan_id: null,
  on_time_rate: null,
  baseline_on_time_rate: null,
  total_tardiness_minutes: null,
  baseline_total_tardiness_minutes: null,
  metrics: {
    plan_id: null,
    measured_at: '2026-03-02T08:00:00',
    plan_generation_seconds: null,
    disruption_response_seconds: null,
    on_time_rate: null,
    total_tardiness_minutes: null,
    churn_ratio: null,
    manual_steps_eliminated: 0,
    auto_handled_count: 0,
    escalated_count: 0,
    llm_tokens_used: 0,
    estimated_usd_cost: 0,
    real_run_count: 0,
    project_real_run_cap: 150,
    real_run_remaining: 150,
    baseline_plan_generation_seconds: null,
    baseline_disruption_response_seconds: null,
    baseline_on_time_rate: null,
    baseline_total_tardiness_minutes: null,
    projected_hero_demo_usd: null,
    projected_build_total_usd: null,
    labels: {},
  },
  manual_steps: [],
  kpis: [],
};

afterEach(() => {
  vi.clearAllMocks();
});

describe('ValueLedger 视图', () => {
  it('渲染自主 vs 上报计数与占比（K-14）', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);

    const ratioSection = (await screen.findByRole('heading', { name: /Auto-handled vs\. escalated/ }))
      .closest('section') as HTMLElement;
    expect(within(ratioSection).getByText('3')).toBeInTheDocument(); // auto_handled
    expect(within(ratioSection).getByText('1')).toBeInTheDocument(); // escalated
    expect(within(ratioSection).getByText('75.00%')).toBeInTheDocument(); // ratio
  });

  it('逐行列出每次判定的决定性判据（R13.12），含执行路径中文标签', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);
    await screen.findByRole('heading', { name: /Decisive predicates per decision/ });

    // 执行路径标签除颜色外带文字
    expect(screen.getByText('Proposed')).toBeInTheDocument();
    expect(screen.getByText('Escalated')).toBeInTheDocument();
    // 判据逐条可见
    expect(screen.getByText('changed_job_count=1 <= 2')).toBeInTheDocument();
    expect(screen.getByText('touches_high_priority=true')).toBeInTheDocument();
    // 影响等级与自主等级可见
    expect(screen.getByText('IMPACT_MAJOR')).toBeInTheDocument();
    expect(screen.getByText('L5')).toBeInTheDocument();
  });

  it('无裁决时显示诚实空态而不是空白', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(EMPTY_LEDGER);
    render(<ValueLedger />);
    expect(await screen.findByText(/No impact-classification decisions yet/)).toBeInTheDocument();
  });

  it('KPI 表只显示可计算项，不再显示固定演示预测', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);
    await screen.findByRole('heading', { name: /KPIs with recorded values/ });
    // KPI 行可见
    expect(screen.getByRole('rowheader', { name: 'K-03' })).toBeInTheDocument();
    expect(screen.queryByRole('rowheader', { name: 'K-17' })).not.toBeInTheDocument();
    // 标签用文字（不仅颜色）
    expect(screen.getAllByText(/Measured/).length).toBeGreaterThan(0);
  });

  it('展示记录的用量与真实运行配额剩余', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);
    await screen.findByRole('heading', { name: /Recorded LLM usage and cost estimate/ });
    expect(screen.getByText('1234')).toBeInTheDocument(); // 累计 token
    // 配额剩余（148）可见
    const quota = screen.getByText(/Real-run quota/);
    expect(quota.textContent).toMatch(/148/);
  });

  it('展示 manual_steps 口径表（R19.5）', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);
    await screen.findByRole('heading', { name: /Manual steps eliminated/ });
    expect(screen.getByRole('rowheader', { name: 'Spreadsheet import' })).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'Plan generation' })).toBeInTheDocument();
  });

  it('提供 CSV 导出链接（R19.8）', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);
    const link = await screen.findByRole('link', { name: 'Export value ledger as CSV' });
    expect(link).toHaveAttribute('href', '/api/value-ledger/export.csv');
  });

  it('后端不可用时显示错误而不是空白', async () => {
    vi.mocked(getValueLedger).mockRejectedValue(new Error('boom'));
    render(<ValueLedger />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    const { container } = render(<ValueLedger />);
    await screen.findByRole('heading', { name: /Decisive predicates per decision/ });

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
