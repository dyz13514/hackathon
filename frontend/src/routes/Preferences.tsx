/**
 * 偏好规则管理视图（design.md Components §6 `/preferences` 行，任务 11.1，R18）。
 *
 * P0 唯一的建规则入口：规划员手写 `human_text` + 四类 `structured_form` 之一的参数。本视图交付
 * tasks.md 11.1 第 5 点的全部界面元素：
 *   - 新建规则表单（4 类 structured_form，随 kind 切换字段）
 *   - 规则列表：human_text / 来源决策 / 创建时间 / 启用开关 / LOW_EVIDENCE 徽章
 *   - 编辑 / 停用 / 删除
 *   - 20 条上限提示（「已启用 N / 20」，接近或达上限时给出说明）
 *   - 每条规则的「影响了哪些作业」入口
 *
 * **显式启用**：创建表单没有「启用」勾选框，新建的规则一律未启用，需在列表里点「启用」这一独立
 * 动作。编辑面板也不含启用开关——启用/停用只经列表里的专门按钮。这与后端「创建/PATCH 都不接受
 * enabled」一致（R18.4）。
 *
 * 可访问性（R27.9）：表单控件均有 `<label>`；LOW_EVIDENCE 徽章除颜色外带图标 + 文字（不仅靠颜色）；
 * 启用状态用文字（「已启用」/「未启用」）而非仅颜色传达；错误用 `role="alert"`；上限提示用
 * `role="status"`。
 */

import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react';

import { ApiError } from '../api/client';
import {
  createPreference,
  deletePreference,
  disablePreference,
  enablePreference,
  getAffectedJobs,
  listPreferences,
  updatePreference,
  type PreferenceForm,
  type PreferenceKind,
  type PreferenceRule,
  type SoftWeightKey,
} from '../api/preferences';

const KIND_LABEL: Record<PreferenceKind, string> = {
  AVOID_MACHINE_FOR_ORDER: '订单避开机器',
  AVOID_MACHINE_FOR_PRODUCT: '产品避开机器',
  PREFER_WORKER_FOR_SKILL: '技能优先指派工人',
  ADJUST_OBJECTIVE_WEIGHT: '调整目标权重',
};

const SOFT_WEIGHT_LABEL: Record<SoftWeightKey, string> = {
  late_order_count: '迟交订单数',
  total_tardiness_minutes: '总拖期分钟',
  urgent_order_lateness: '加急订单迟交',
  churn_ratio: '扰动比率',
  machine_utilisation: '机器利用率',
  total_changeover_minutes: '总换型分钟',
};

const KINDS = Object.keys(KIND_LABEL) as PreferenceKind[];
const SOFT_WEIGHTS = Object.keys(SOFT_WEIGHT_LABEL) as SoftWeightKey[];

function kindLabel(kind: string): string {
  return KIND_LABEL[kind as PreferenceKind] ?? kind;
}

/** 把一条规则的 structured_form 渲染成一行可读文字，供列表展示。 */
function describeForm(form: PreferenceForm): string {
  switch (form.kind) {
    case 'AVOID_MACHINE_FOR_ORDER':
      return `订单 ${form.order_id} 避开机器 ${form.machine_id}（惩罚 ${form.weight_delta ?? 1}）`;
    case 'AVOID_MACHINE_FOR_PRODUCT':
      return `产品 ${form.product_id} 避开机器 ${form.machine_id}（惩罚 ${form.weight_delta ?? 1}）`;
    case 'PREFER_WORKER_FOR_SKILL':
      return `技能 ${form.skill} 优先指派工人 ${form.worker_id}（惩罚 ${form.weight_delta ?? 1}）`;
    case 'ADJUST_OBJECTIVE_WEIGHT':
      return `目标「${SOFT_WEIGHT_LABEL[form.component]}」权重 ×${form.multiplier}`;
  }
}

interface FormFields {
  kind: PreferenceKind;
  order_id: string;
  product_id: string;
  machine_id: string;
  skill: string;
  worker_id: string;
  weight_delta: string;
  component: SoftWeightKey;
  multiplier: string;
}

const EMPTY_FIELDS: FormFields = {
  kind: 'AVOID_MACHINE_FOR_ORDER',
  order_id: '',
  product_id: '',
  machine_id: '',
  skill: '',
  worker_id: '',
  weight_delta: '1',
  component: 'total_tardiness_minutes',
  multiplier: '1.0',
};

/** 从表单字段组装成后端要的 structured_form（只取当前 kind 相关字段）。 */
function buildForm(f: FormFields): PreferenceForm {
  switch (f.kind) {
    case 'AVOID_MACHINE_FOR_ORDER':
      return {
        kind: f.kind,
        order_id: f.order_id.trim(),
        machine_id: f.machine_id.trim(),
        weight_delta: Number(f.weight_delta),
      };
    case 'AVOID_MACHINE_FOR_PRODUCT':
      return {
        kind: f.kind,
        product_id: f.product_id.trim(),
        machine_id: f.machine_id.trim(),
        weight_delta: Number(f.weight_delta),
      };
    case 'PREFER_WORKER_FOR_SKILL':
      return {
        kind: f.kind,
        skill: f.skill.trim(),
        worker_id: f.worker_id.trim(),
        weight_delta: Number(f.weight_delta),
      };
    case 'ADJUST_OBJECTIVE_WEIGHT':
      return { kind: f.kind, component: f.component, multiplier: Number(f.multiplier) };
  }
}

export function Preferences() {
  const [rules, setRules] = useState<PreferenceRule[]>([]);
  const [enabledCount, setEnabledCount] = useState(0);
  const [maxEnabled, setMaxEnabled] = useState(20);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [fields, setFields] = useState<FormFields>(EMPTY_FIELDS);
  const [humanText, setHumanText] = useState('');
  const [sourceIds, setSourceIds] = useState('');
  const [affected, setAffected] = useState<Record<string, readonly string[]>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listPreferences();
      setRules([...list.items]);
      setEnabledCount(list.enabled_count);
      setMaxEnabled(list.max_enabled);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `偏好规则不可用（${err.code}）：${err.message}`
          : '偏好规则不可用：后端服务不可用。',
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const atLimit = enabledCount >= maxEnabled;

  const handleCreate = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setError(null);
      setNotice(null);
      try {
        const sources = sourceIds
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
        await createPreference({
          human_text: humanText.trim(),
          structured_form: buildForm(fields),
          source_decision_ids: sources,
        });
        setNotice('规则已创建（未启用）。请在列表中点「启用」使其生效。');
        setHumanText('');
        setSourceIds('');
        setFields(EMPTY_FIELDS);
        await load();
      } catch (err) {
        setError(
          err instanceof ApiError
            ? `创建失败（${err.code}）：${err.message}`
            : '创建失败：后端服务不可用。',
        );
      }
    },
    [fields, humanText, sourceIds, load],
  );

  const handleEnable = useCallback(
    async (rule: PreferenceRule) => {
      setError(null);
      setNotice(null);
      try {
        if (rule.enabled) {
          await disablePreference(rule.rule_id);
        } else {
          await enablePreference(rule.rule_id);
        }
        await load();
      } catch (err) {
        if (err instanceof ApiError && err.code === 'PREFERENCE_RULE_LIMIT_REACHED') {
          setError(`已达启用上限 ${maxEnabled} 条，请先停用一条既有规则再启用。`);
        } else {
          setError(
            err instanceof ApiError ? `操作失败（${err.code}）：${err.message}` : '操作失败。',
          );
        }
      }
    },
    [load, maxEnabled],
  );

  const handleDelete = useCallback(
    async (ruleId: string) => {
      setError(null);
      setNotice(null);
      try {
        await deletePreference(ruleId);
        await load();
      } catch (err) {
        setError(
          err instanceof ApiError ? `删除失败（${err.code}）：${err.message}` : '删除失败。',
        );
      }
    },
    [load],
  );

  const handleEditText = useCallback(
    async (ruleId: string, currentText: string) => {
      const next = window.prompt('编辑规则说明（human_text）', currentText);
      if (next == null || next.trim() === '' || next.trim() === currentText) {
        return;
      }
      setError(null);
      try {
        await updatePreference(ruleId, { human_text: next.trim() });
        await load();
      } catch (err) {
        setError(
          err instanceof ApiError ? `编辑失败（${err.code}）：${err.message}` : '编辑失败。',
        );
      }
    },
    [load],
  );

  const handleAffected = useCallback(async (ruleId: string) => {
    try {
      const result = await getAffectedJobs(ruleId);
      setAffected((prev) => ({ ...prev, [ruleId]: result.job_ids }));
    } catch {
      setAffected((prev) => ({ ...prev, [ruleId]: [] }));
    }
  }, []);

  const limitNotice = useMemo(() => {
    if (atLimit) {
      return `已启用 ${enabledCount} / ${maxEnabled}（已达上限）。要启用新规则，请先停用一条既有规则。`;
    }
    return `已启用 ${enabledCount} / ${maxEnabled}。`;
  }, [atLimit, enabledCount, maxEnabled]);

  return (
    <section aria-labelledby="preferences-heading" className="preferences">
      <div className="preferences-header">
        <h2 id="preferences-heading">偏好规则管理</h2>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          aria-busy={loading}
          aria-label="刷新偏好规则列表"
        >
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      <p role="status" className={atLimit ? 'preferences-limit is-at-limit' : 'preferences-limit'}>
        {atLimit && <span aria-hidden="true">⛔ </span>}
        {limitNotice}
      </p>

      {error && (
        <p role="alert" className="preferences-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="preferences-notice">
          {notice}
        </p>
      )}

      {/* --- 新建规则表单（无「启用」勾选框：创建即未启用，R18.4） --- */}
      <section aria-labelledby="new-rule-heading" className="preferences-form">
        <h3 id="new-rule-heading">新建规则</h3>
        <form onSubmit={(e) => void handleCreate(e)}>
          <div className="field">
            <label htmlFor="rule-human-text">规则说明（human_text）</label>
            <input
              id="rule-human-text"
              type="text"
              required
              maxLength={200}
              value={humanText}
              onChange={(e) => setHumanText(e.target.value)}
              placeholder="例如：ORD-007 不要排 CNC-03，那个客户投诉过表面处理"
            />
          </div>

          <div className="field">
            <label htmlFor="rule-kind">规则类型</label>
            <select
              id="rule-kind"
              value={fields.kind}
              onChange={(e) => setFields({ ...fields, kind: e.target.value as PreferenceKind })}
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABEL[k]}
                </option>
              ))}
            </select>
          </div>

          {fields.kind === 'AVOID_MACHINE_FOR_ORDER' && (
            <>
              <div className="field">
                <label htmlFor="f-order-id">订单 ID</label>
                <input
                  id="f-order-id"
                  type="text"
                  required
                  value={fields.order_id}
                  onChange={(e) => setFields({ ...fields, order_id: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="f-machine-id-o">机器 ID</label>
                <input
                  id="f-machine-id-o"
                  type="text"
                  required
                  value={fields.machine_id}
                  onChange={(e) => setFields({ ...fields, machine_id: e.target.value })}
                />
              </div>
            </>
          )}

          {fields.kind === 'AVOID_MACHINE_FOR_PRODUCT' && (
            <>
              <div className="field">
                <label htmlFor="f-product-id">产品 ID</label>
                <input
                  id="f-product-id"
                  type="text"
                  required
                  value={fields.product_id}
                  onChange={(e) => setFields({ ...fields, product_id: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="f-machine-id-p">机器 ID</label>
                <input
                  id="f-machine-id-p"
                  type="text"
                  required
                  value={fields.machine_id}
                  onChange={(e) => setFields({ ...fields, machine_id: e.target.value })}
                />
              </div>
            </>
          )}

          {fields.kind === 'PREFER_WORKER_FOR_SKILL' && (
            <>
              <div className="field">
                <label htmlFor="f-skill">技能</label>
                <input
                  id="f-skill"
                  type="text"
                  required
                  value={fields.skill}
                  onChange={(e) => setFields({ ...fields, skill: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="f-worker-id">工人 ID</label>
                <input
                  id="f-worker-id"
                  type="text"
                  required
                  value={fields.worker_id}
                  onChange={(e) => setFields({ ...fields, worker_id: e.target.value })}
                />
              </div>
            </>
          )}

          {fields.kind !== 'ADJUST_OBJECTIVE_WEIGHT' && (
            <div className="field">
              <label htmlFor="f-weight-delta">惩罚权重（0–10，只能加惩罚）</label>
              <input
                id="f-weight-delta"
                type="number"
                min={0.0001}
                max={10}
                step="0.5"
                value={fields.weight_delta}
                onChange={(e) => setFields({ ...fields, weight_delta: e.target.value })}
              />
            </div>
          )}

          {fields.kind === 'ADJUST_OBJECTIVE_WEIGHT' && (
            <>
              <div className="field">
                <label htmlFor="f-component">目标分量</label>
                <select
                  id="f-component"
                  value={fields.component}
                  onChange={(e) =>
                    setFields({ ...fields, component: e.target.value as SoftWeightKey })
                  }
                >
                  {SOFT_WEIGHTS.map((c) => (
                    <option key={c} value={c}>
                      {SOFT_WEIGHT_LABEL[c]}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="f-multiplier">权重倍数（0.5–2.0）</label>
                <input
                  id="f-multiplier"
                  type="number"
                  min={0.5}
                  max={2.0}
                  step="0.1"
                  value={fields.multiplier}
                  onChange={(e) => setFields({ ...fields, multiplier: e.target.value })}
                />
              </div>
            </>
          )}

          <div className="field">
            <label htmlFor="f-source-ids">来源决策 ID（逗号分隔，可留空）</label>
            <input
              id="f-source-ids"
              type="text"
              value={sourceIds}
              onChange={(e) => setSourceIds(e.target.value)}
              placeholder="DEC-xxxx, DEC-yyyy"
            />
            <p className="field-hint">少于 2 条来源决策的规则会被标记为「证据不足」（R18.10）。</p>
          </div>

          <button type="submit">创建规则（创建后需显式启用）</button>
        </form>
      </section>

      {/* --- 规则列表 --- */}
      <section aria-labelledby="rule-list-heading" className="preferences-list">
        <h3 id="rule-list-heading">规则列表（{rules.length}）</h3>
        {rules.length === 0 ? (
          <p>暂无偏好规则。用上方表单创建第一条。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">说明</th>
                <th scope="col">类型 / 参数</th>
                <th scope="col">来源决策</th>
                <th scope="col">创建时间</th>
                <th scope="col">状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <tr key={rule.rule_id} data-enabled={rule.enabled} data-rule-id={rule.rule_id}>
                  <th scope="row">
                    {rule.human_text}
                    {rule.low_evidence && (
                      <span className="badge badge-low-evidence" title="来源决策少于 2 条">
                        <span aria-hidden="true">⚠ </span>证据不足
                      </span>
                    )}
                  </th>
                  <td>
                    <span className="rule-kind">{kindLabel(rule.kind)}</span>
                    <br />
                    <span className="rule-form-desc">{describeForm(rule.structured_form)}</span>
                  </td>
                  <td>
                    {rule.source_decision_ids.length > 0 ? (
                      <ul className="source-list">
                        {rule.source_decision_ids.map((id) => (
                          <li key={id}>
                            <a href={`/traces?decision=${encodeURIComponent(id)}`}>{id}</a>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>{new Date(rule.created_at).toLocaleString()}</td>
                  <td>
                    <span className={rule.enabled ? 'status status-on' : 'status status-off'}>
                      {rule.enabled ? '已启用' : '未启用'}
                    </span>
                  </td>
                  <td className="rule-actions">
                    <button
                      type="button"
                      onClick={() => void handleEnable(rule)}
                      disabled={!rule.enabled && atLimit}
                      aria-label={rule.enabled ? `停用规则 ${rule.rule_id}` : `启用规则 ${rule.rule_id}`}
                    >
                      {rule.enabled ? '停用' : '启用'}
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleEditText(rule.rule_id, rule.human_text)}
                      aria-label={`编辑规则 ${rule.rule_id}`}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDelete(rule.rule_id)}
                      aria-label={`删除规则 ${rule.rule_id}`}
                    >
                      删除
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleAffected(rule.rule_id)}
                      aria-label={`查看规则 ${rule.rule_id} 影响了哪些作业`}
                    >
                      影响了哪些作业
                    </button>
                    {affected[rule.rule_id] && (
                      <div className="affected-jobs" role="status">
                        {affected[rule.rule_id].length > 0 ? (
                          <>受影响作业：{affected[rule.rule_id].join(', ')}</>
                        ) : (
                          <>当前生效计划中无受该规则影响的作业。</>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}
