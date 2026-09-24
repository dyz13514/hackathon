import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PreferenceList, PreferenceRule } from '../api/preferences';

// mock 隔离网络：本套测试守的是「视图拿到规则数据后画对了什么、点了按钮调对了哪个 API」。
vi.mock('../api/preferences', async () => {
  const actual = await vi.importActual<typeof import('../api/preferences')>('../api/preferences');
  return {
    ...actual,
    listPreferences: vi.fn(),
    createPreference: vi.fn(),
    enablePreference: vi.fn(),
    disablePreference: vi.fn(),
    deletePreference: vi.fn(),
    updatePreference: vi.fn(),
    getAffectedJobs: vi.fn(),
    distilPreferences: vi.fn(),
  };
});

import {
  createPreference,
  disablePreference,
  distilPreferences,
  enablePreference,
  listPreferences,
} from '../api/preferences';
import type { DistilResult } from '../api/preferences';
import { Preferences } from '../routes/Preferences';

const RULE_LOW_EVIDENCE: PreferenceRule = {
  rule_id: 'PR-001',
  human_text: 'Don’t schedule ORD-007 on CNC-03',
  structured_form: {
    kind: 'AVOID_MACHINE_FOR_ORDER',
    order_id: 'ORD-007',
    machine_id: 'CNC-03',
    weight_delta: 2,
  },
  kind: 'AVOID_MACHINE_FOR_ORDER',
  enabled: false,
  low_evidence: true,
  created_at: '2026-03-02T08:15:00',
  updated_at: '2026-03-02T08:15:00',
  created_by: 'PLANNER',
  source_decision_ids: ['DEC-1'],
};

const RULE_ENABLED: PreferenceRule = {
  rule_id: 'PR-002',
  human_text: 'Prefer W-01 for welding skill',
  structured_form: { kind: 'PREFER_WORKER_FOR_SKILL', skill: 'welding', worker_id: 'W-01', weight_delta: 1 },
  kind: 'PREFER_WORKER_FOR_SKILL',
  enabled: true,
  low_evidence: false,
  created_at: '2026-03-02T09:00:00',
  updated_at: '2026-03-02T09:00:00',
  created_by: 'PLANNER',
  source_decision_ids: ['DEC-1', 'DEC-2'],
};

function listWith(items: PreferenceRule[], enabledCount = 0): PreferenceList {
  return { items, total: items.length, enabled_count: enabledCount, max_enabled: 20 };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('Preferences 视图', () => {
  it('列出规则并展示 human_text、来源决策、启用状态', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE, RULE_ENABLED], 1));
    render(<Preferences />);

    expect(await screen.findByText('Don’t schedule ORD-007 on CNC-03')).toBeInTheDocument();
    expect(screen.getByText('Prefer W-01 for welding skill')).toBeInTheDocument();
    // 来源决策链接（两条规则都引用了 DEC-1，因此用 getAllByText）
    expect(screen.getAllByText('DEC-1').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('DEC-2')).toBeInTheDocument();
    // 启用状态用文字（不仅颜色）
    expect(screen.getByText('Enabled')).toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();
  });

  it('LOW_EVIDENCE 用图标 + 文字提示，不仅靠颜色（R18.10、R27.9）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE]));
    render(<Preferences />);
    expect(await screen.findByText(/Low evidence/)).toBeInTheDocument();
  });

  it('展示已启用 N / 20 上限提示（R18.11）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_ENABLED], 20));
    render(<Preferences />);
    const status = await screen.findByText(/20 \/ 20 enabled/);
    expect(status).toBeInTheDocument();
    expect(status.textContent).toMatch(/limit reached/);
  });

  it('创建表单没有「启用」勾选框——创建即未启用（R18.4）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([]));
    render(<Preferences />);
    await screen.findByRole('heading', { name: 'New rule' });
    // 表单里不应出现任何 name/label 含「启用」的勾选控件
    expect(screen.queryByLabelText(/Enable/)).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('提交创建表单调用 createPreference 且入参不含 enabled', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([]));
    vi.mocked(createPreference).mockResolvedValue(RULE_LOW_EVIDENCE);
    render(<Preferences />);
    await screen.findByRole('heading', { name: 'New rule' });

    fireEvent.change(screen.getByLabelText(/Rule description/), {
      target: { value: 'Avoid CNC-03 for ORD-007' },
    });
    fireEvent.change(screen.getByLabelText('Order ID'), { target: { value: 'ORD-007' } });
    fireEvent.change(screen.getByLabelText('Machine ID'), { target: { value: 'CNC-03' } });
    // jsdom 下点击 submit 按钮不总会触发带 required 字段的原生表单提交，
    // 直接 submit 表单本身（浏览器里点击按钮等价于此），断言真实的提交行为。
    const submitButton = screen.getByRole('button', { name: /Create rule/ });
    fireEvent.submit(submitButton.closest('form') as HTMLFormElement);

    await waitFor(() => expect(createPreference).toHaveBeenCalledTimes(1));
    const arg = vi.mocked(createPreference).mock.calls[0]?.[0];
    expect(arg).toBeDefined();
    expect(arg).not.toHaveProperty('enabled');
    expect(arg?.structured_form.kind).toBe('AVOID_MACHINE_FOR_ORDER');
  });

  // 回归（round-2）：Penalty weight 数字输入的 min/step 必须让其默认值合法，
  // 否则浏览器的原生约束校验会**静默拦截**整个表单提交（不发请求、不报错、
  // 不重置），导致「创建规则」在真实浏览器里点了没反应。历史缺陷：
  // min=0.0001 step=0.5 使默认值 1 非法（合法值为 0.0001/0.5001/1.0001…）。
  // jsdom 的 checkValidity 不校验 step，这里显式按 HTML5 规则手算，避免误判。
  it('Penalty weight 输入的默认值满足 min/step 约束（否则原生校验会拦截提交）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([]));
    render(<Preferences />);
    await screen.findByRole('heading', { name: 'New rule' });

    const weight = screen.getByLabelText(/Penalty weight/) as HTMLInputElement;
    const value = Number(weight.value); // 表单默认值
    const min = Number(weight.min);
    const max = weight.max === '' ? Infinity : Number(weight.max);
    const step = weight.step === 'any' ? null : Number(weight.step);

    expect(Number.isFinite(value)).toBe(true);
    expect(value).toBeGreaterThanOrEqual(min);
    expect(value).toBeLessThanOrEqual(max);
    if (step !== null) {
      // HTML5：value 必须能表示为 min + n*step（容忍浮点误差）
      const n = (value - min) / step;
      expect(Math.abs(n - Math.round(n))).toBeLessThan(1e-9);
    }
    // 后端约束是 gt=0 且 le=10：min 必须 > 0，max 不得超过 10
    expect(min).toBeGreaterThan(0);
    expect(max).toBeLessThanOrEqual(10);
  });

  // 回归（round-2）：点击 Create 按钮（而非直接 submit 表单）在字段合法时应发起
  // 创建请求。用真实按钮点击验证提交链路不被约束校验拦截。
  it('填写合法字段后点击 Create 触发 createPreference', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([]));
    vi.mocked(createPreference).mockResolvedValue(RULE_LOW_EVIDENCE);
    render(<Preferences />);
    await screen.findByRole('heading', { name: 'New rule' });

    fireEvent.change(screen.getByLabelText(/Rule description/), {
      target: { value: 'Avoid CNC-01 for ORD-001' },
    });
    fireEvent.change(screen.getByLabelText('Order ID'), { target: { value: 'ORD-001' } });
    fireEvent.change(screen.getByLabelText('Machine ID'), { target: { value: 'CNC-01' } });

    fireEvent.click(screen.getByRole('button', { name: /Create rule/ }));

    await waitFor(() => expect(createPreference).toHaveBeenCalledTimes(1));
    // 成功后展示已创建提示，并清空描述输入（表单重置）
    expect(await screen.findByText(/Rule created/)).toBeInTheDocument();
    expect((screen.getByLabelText(/Rule description/) as HTMLInputElement).value).toBe('');
  });

  it('点「启用」调用 enablePreference（独立动作）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE]));
    vi.mocked(enablePreference).mockResolvedValue({ ...RULE_LOW_EVIDENCE, enabled: true });
    render(<Preferences />);
    await screen.findByText('Don’t schedule ORD-007 on CNC-03');

    fireEvent.click(screen.getByRole('button', { name: /Enable rule PR-001/ }));
    await waitFor(() => expect(enablePreference).toHaveBeenCalledWith('PR-001'));
  });

  it('点已启用规则的「停用」调用 disablePreference', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_ENABLED], 1));
    vi.mocked(disablePreference).mockResolvedValue({ ...RULE_ENABLED, enabled: false });
    render(<Preferences />);
    await screen.findByText('Prefer W-01 for welding skill');

    fireEvent.click(screen.getByRole('button', { name: /Disable rule PR-002/ }));
    await waitFor(() => expect(disablePreference).toHaveBeenCalledWith('PR-002'));
  });

  it('达上限时未启用规则的「启用」按钮被禁用', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE], 20));
    render(<Preferences />);
    await screen.findByText('Don’t schedule ORD-007 on CNC-03');
    expect(screen.getByRole('button', { name: /Enable rule PR-001/ })).toBeDisabled();
  });

  it('后端不可用时显示错误而不是空白', async () => {
    vi.mocked(listPreferences).mockRejectedValue(new Error('boom'));
    render(<Preferences />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE, RULE_ENABLED], 1));
    const { container } = render(<Preferences />);
    await screen.findByText('Don’t schedule ORD-007 on CNC-03');

    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });

  // ---- 任务 13.3 从历史决策蒸馏（P1）----

  it('「从历史决策蒸馏」调用 distil 并在确认区展示候选（均未启用）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([], 0));
    const distilled: DistilResult = {
      outcome: 'DISTILLED',
      injection_suspected: false,
      considered_decision_ids: ['DEC-1', 'DEC-2'],
      candidates: [
        {
          rule_id: 'PR-cand',
          human_text: 'Avoid CNC-03 for ORD-007',
          structured_form: {
            kind: 'AVOID_MACHINE_FOR_ORDER',
            order_id: 'ORD-007',
            machine_id: 'CNC-03',
            weight_delta: 1,
          },
          source_decision_ids: ['DEC-1', 'DEC-2'],
          enabled: false,
          low_evidence: false,
        },
      ],
    };
    vi.mocked(distilPreferences).mockResolvedValue(distilled);
    render(<Preferences />);
    await screen.findByRole('heading', { name: /Preferences/ });

    fireEvent.click(screen.getByRole('button', { name: /Distil candidate preference rules/ }));
    await waitFor(() => expect(distilPreferences).toHaveBeenCalledTimes(1));

    const group = await screen.findByRole('group', { name: 'Distilled candidates confirmation' });
    expect(group).toBeInTheDocument();
    // 候选展示为「未启用」——蒸馏不启用任何规则。
    expect(within(group).getByText('Disabled')).toBeInTheDocument();
    expect(within(group).getByText('Avoid CNC-03 for ORD-007')).toBeInTheDocument();
  });

  it('蒸馏检测到注入时在确认区显著提示', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([], 0));
    vi.mocked(distilPreferences).mockResolvedValue({
      outcome: 'DISTILLED',
      injection_suspected: true,
      considered_decision_ids: ['DEC-1'],
      candidates: [
        {
          rule_id: 'PR-cand',
          human_text: 'x',
          structured_form: {
            kind: 'AVOID_MACHINE_FOR_ORDER',
            order_id: 'ORD-1',
            machine_id: 'M-1',
            weight_delta: 1,
          },
          source_decision_ids: ['DEC-1'],
          enabled: false,
          low_evidence: true,
        },
      ],
    });
    render(<Preferences />);
    await screen.findByRole('heading', { name: /Preferences/ });
    fireEvent.click(screen.getByRole('button', { name: /Distil candidate preference rules/ }));
    expect(await screen.findByText(/suspected prompt injection was detected/)).toBeInTheDocument();
  });

  it('降级模式蒸馏返回 LLM_UNAVAILABLE 时提示改用手写', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([], 0));
    vi.mocked(distilPreferences).mockResolvedValue({
      outcome: 'LLM_UNAVAILABLE',
      injection_suspected: false,
      considered_decision_ids: [],
      candidates: [],
    });
    render(<Preferences />);
    await screen.findByRole('heading', { name: /Preferences/ });
    fireEvent.click(screen.getByRole('button', { name: /Distil candidate preference rules/ }));
    expect(await screen.findByText(/degraded mode/)).toBeInTheDocument();
  });
});
