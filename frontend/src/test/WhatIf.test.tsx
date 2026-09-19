import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AdoptResult, ScenarioResult } from '../api/scenarios';

// mock 隔离网络：本套测试守的是「表单 → 推演 → 对比 → 采纳」的视图行为，不测后端。
vi.mock('../api/scenarios', async () => {
  const actual = await vi.importActual<typeof import('../api/scenarios')>('../api/scenarios');
  return { ...actual, runScenario: vi.fn(), adoptScenario: vi.fn() };
});

import { adoptScenario, runScenario } from '../api/scenarios';
import { WhatIf } from '../routes/WhatIf';

const RESULT: ScenarioResult = {
  scenario_id: 'SCN-abc',
  feasibility: 'PARTIAL',
  late_order_count: 3,
  active_late_order_count: 1,
  late_order_count_delta: 2,
  total_tardiness_minutes: 500,
  active_total_tardiness_minutes: 200,
  total_tardiness_delta_minutes: 300,
  total_score: 1200,
  active_total_score: 900,
  new_unschedulable_jobs: ['ORD-9-OP1'],
  delayed_order_ids: ['ORD-9'],
};

const ADOPTED: AdoptResult = {
  scenario_id: 'SCN-abc',
  plan_id: 'PLAN-new',
  status: 'PENDING_APPROVAL',
};

afterEach(() => {
  vi.clearAllMocks();
});

describe('WhatIf 视图', () => {
  it('运行推演后渲染与 ACTIVE 的对比（R16.8）', async () => {
    vi.mocked(runScenario).mockResolvedValue(RESULT);
    render(<WhatIf />);

    fireEvent.click(screen.getByRole('button', { name: /运行推演/ }));
    await waitFor(() => expect(runScenario).toHaveBeenCalledTimes(1));

    expect(await screen.findByText(/推演结果/)).toBeInTheDocument();
    expect(screen.getByText(/PARTIAL/)).toBeInTheDocument();
    // 迟交 delta = +2（变差）
    expect(screen.getByText(/\+2（变差）/)).toBeInTheDocument();
    expect(screen.getByText(/ORD-9-OP1/)).toBeInTheDocument();
  });

  it('推演后可采纳，采纳成功显示新提案（R16.9）', async () => {
    vi.mocked(runScenario).mockResolvedValue(RESULT);
    vi.mocked(adoptScenario).mockResolvedValue(ADOPTED);
    render(<WhatIf />);

    fireEvent.click(screen.getByRole('button', { name: /运行推演/ }));
    await screen.findByText(/推演结果/);

    fireEvent.click(screen.getByRole('button', { name: /以此场景生成正式提案/ }));
    await waitFor(() => expect(adoptScenario).toHaveBeenCalledWith('SCN-abc'));
    expect(await screen.findByText(/已生成提案 PLAN-new/)).toBeInTheDocument();
  });

  it('切换变更类型渲染对应字段', async () => {
    vi.mocked(runScenario).mockResolvedValue(RESULT);
    render(<WhatIf />);

    // 默认是机器不可用 → 有「机器 ID」字段。
    expect(screen.getByLabelText('机器 ID')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('变更类型'), {
      target: { value: 'CHANGE_MATERIAL_AVAILABILITY' },
    });
    expect(screen.getByLabelText('物料 ID')).toBeInTheDocument();
    expect(screen.getByLabelText('可用量')).toBeInTheDocument();
  });

  it('后端不可用时显示错误而不是空白', async () => {
    vi.mocked(runScenario).mockRejectedValue(new Error('boom'));
    render(<WhatIf />);
    fireEvent.click(screen.getByRole('button', { name: /运行推演/ }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    const { container } = render(<WhatIf />);
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
