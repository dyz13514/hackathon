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
  distilPreferences,
  enablePreference,
  getAffectedJobs,
  listPreferences,
  updatePreference,
  type DistilledCandidate,
  type PreferenceForm,
  type PreferenceKind,
  type PreferenceRule,
  type SoftWeightKey,
} from '../api/preferences';

const KIND_LABEL: Record<PreferenceKind, string> = {
  AVOID_MACHINE_FOR_ORDER: 'Avoid machine for order',
  AVOID_MACHINE_FOR_PRODUCT: 'Avoid machine for product',
  PREFER_WORKER_FOR_SKILL: 'Prefer worker for skill',
  ADJUST_OBJECTIVE_WEIGHT: 'Adjust objective weight',
};

const SOFT_WEIGHT_LABEL: Record<SoftWeightKey, string> = {
  late_order_count: 'Late order count',
  total_tardiness_minutes: 'Total tardiness minutes',
  urgent_order_lateness: 'Urgent order lateness',
  churn_ratio: 'Churn ratio',
  machine_utilisation: 'Machine utilisation',
  total_changeover_minutes: 'Total changeover minutes',
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
      return `Order ${form.order_id} avoids machine ${form.machine_id} (penalty ${form.weight_delta ?? 1})`;
    case 'AVOID_MACHINE_FOR_PRODUCT':
      return `Product ${form.product_id} avoids machine ${form.machine_id} (penalty ${form.weight_delta ?? 1})`;
    case 'PREFER_WORKER_FOR_SKILL':
      return `Skill ${form.skill} prefers worker ${form.worker_id} (penalty ${form.weight_delta ?? 1})`;
    case 'ADJUST_OBJECTIVE_WEIGHT':
      return `Objective “${SOFT_WEIGHT_LABEL[form.component]}” weight ×${form.multiplier}`;
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

  // ---- 任务 13.3 从历史决策蒸馏（P1）----
  const [distilling, setDistilling] = useState(false);
  const [candidates, setCandidates] = useState<DistilledCandidate[] | null>(null);
  const [distilInjection, setDistilInjection] = useState(false);

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
          ? `Preferences unavailable (${err.code}): ${err.message}`
          : 'Preferences unavailable: backend service unavailable.',
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
        setNotice('Rule created (disabled). Click “Enable” in the list to make it take effect.');
        setHumanText('');
        setSourceIds('');
        setFields(EMPTY_FIELDS);
        await load();
      } catch (err) {
        setError(
          err instanceof ApiError
            ? `Create failed (${err.code}): ${err.message}`
            : 'Create failed: backend service unavailable.',
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
          setError(`Enable limit of ${maxEnabled} reached; disable an existing rule before enabling another.`);
        } else {
          setError(
            err instanceof ApiError ? `Operation failed (${err.code}): ${err.message}` : 'Operation failed.',
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
          err instanceof ApiError ? `Delete failed (${err.code}): ${err.message}` : 'Delete failed.',
        );
      }
    },
    [load],
  );

  const handleEditText = useCallback(
    async (ruleId: string, currentText: string) => {
      const next = window.prompt('Edit rule description (human_text)', currentText);
      if (next == null || next.trim() === '' || next.trim() === currentText) {
        return;
      }
      setError(null);
      try {
        await updatePreference(ruleId, { human_text: next.trim() });
        await load();
      } catch (err) {
        setError(
          err instanceof ApiError ? `Edit failed (${err.code}): ${err.message}` : 'Edit failed.',
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

  // 从历史决策蒸馏候选：候选已落库但一律 enabled=false，出现在下方规则列表里（未启用），
  // 并在确认区列出，供逐条「启用」。蒸馏不启用任何规则。
  const handleDistil = useCallback(async () => {
    setDistilling(true);
    setError(null);
    setNotice(null);
    setCandidates(null);
    try {
      const result = await distilPreferences();
      setDistilInjection(result.injection_suspected);
      if (result.outcome === 'NO_EVIDENCE') {
        setNotice('No historical decisions available to distil yet (rejection/modification decisions with reasons are needed first).');
        setCandidates([]);
      } else if (result.outcome === 'LLM_UNAVAILABLE') {
        setNotice('The LLM is in degraded mode, so distilling from historical decisions is unavailable. You can keep writing rules manually.');
        setCandidates([]);
      } else {
        setCandidates([...result.candidates]);
        setNotice(
          result.candidates.length > 0
            ? `Distilled ${result.candidates.length} candidate rule(s) (all disabled). Please confirm and enable each one.`
            : 'Could not distil any usable candidate rules from historical decisions.',
        );
      }
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Distillation failed (${err.code}): ${err.message}`
          : 'Distillation failed: backend service unavailable.',
      );
    } finally {
      setDistilling(false);
    }
  }, [load]);

  const limitNotice = useMemo(() => {
    if (atLimit) {
      return `${enabledCount} / ${maxEnabled} enabled (limit reached). To enable a new rule, disable an existing one first.`;
    }
    return `${enabledCount} / ${maxEnabled} enabled.`;
  }, [atLimit, enabledCount, maxEnabled]);

  return (
    <section aria-labelledby="preferences-heading" className="preferences">
      <div className="preferences-header">
        <h2 id="preferences-heading">Preferences</h2>
        <button
          type="button"
          onClick={() => void handleDistil()}
          disabled={distilling}
          aria-busy={distilling}
          aria-label="Distil candidate preference rules from historical decisions"
        >
          {distilling ? 'Distilling…' : 'Distil from history'}
        </button>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          aria-busy={loading}
          aria-label="Refresh preference rule list"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      <p role="status" className={atLimit ? 'preferences-limit is-at-limit' : 'preferences-limit'}>
        {limitNotice}
      </p>

      {error && (
        <p role="alert" className="preferences-error">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="preferences-notice">
          {notice}
        </p>
      )}

      {/* --- 任务 13.3 蒸馏候选确认区（候选均已落库但未启用，逐条确认后启用） --- */}
      {candidates !== null && candidates.length > 0 && (
        <section
          aria-label="Distilled candidates confirmation"
          className="preferences-candidates"
          role="group"
        >
          <h3>Distilled candidates (confirm and enable each one)</h3>
          {distilInjection && (
            <p role="alert" className="preferences-injection-warning">
              A suspected prompt injection was detected in some source-decision reasons; the system treated it
              as data and recorded it for audit. All candidates remain disabled.
            </p>
          )}
          <ul className="distil-candidate-list">
            {candidates.map((c) => (
              <li key={c.rule_id} data-rule-id={c.rule_id}>
                <span className="distil-candidate-text">{c.human_text}</span>
                <span className="distil-candidate-form">{describeForm(c.structured_form)}</span>
                <span className={c.enabled ? 'status status-on' : 'status status-off'}>
                  {c.enabled ? 'Enabled' : 'Disabled'}
                </span>
                {c.low_evidence && (
                  <span className="badge badge-low-evidence" title="Fewer than 2 source decisions">
                    Low evidence
                  </span>
                )}
                <span className="distil-candidate-sources">
                  Sources: {c.source_decision_ids.length > 0 ? c.source_decision_ids.join(', ') : '—'}
                </span>
              </li>
            ))}
          </ul>
          <p className="field-hint">
            Candidates have been added to the list below as disabled rules. After reviewing, click “Enable” in
            the list to make them take effect — distillation itself never enables any rule.
          </p>
        </section>
      )}

      {/* --- 新建规则表单（无「启用」勾选框：创建即未启用，R18.4） --- */}
      <section aria-labelledby="new-rule-heading" className="preferences-form">
        <h3 id="new-rule-heading">New rule</h3>
        <form onSubmit={(e) => void handleCreate(e)}>
          <div className="field">
            <label htmlFor="rule-human-text">Rule description (human_text)</label>
            <input
              id="rule-human-text"
              type="text"
              required
              maxLength={200}
              value={humanText}
              onChange={(e) => setHumanText(e.target.value)}
              placeholder="e.g. Don’t schedule ORD-007 on CNC-03 — that customer complained about the finish"
            />
          </div>

          <div className="field">
            <label htmlFor="rule-kind">Rule type</label>
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
                <label htmlFor="f-order-id">Order ID</label>
                <input
                  id="f-order-id"
                  type="text"
                  required
                  value={fields.order_id}
                  onChange={(e) => setFields({ ...fields, order_id: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="f-machine-id-o">Machine ID</label>
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
                <label htmlFor="f-product-id">Product ID</label>
                <input
                  id="f-product-id"
                  type="text"
                  required
                  value={fields.product_id}
                  onChange={(e) => setFields({ ...fields, product_id: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="f-machine-id-p">Machine ID</label>
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
                <label htmlFor="f-skill">Skill</label>
                <input
                  id="f-skill"
                  type="text"
                  required
                  value={fields.skill}
                  onChange={(e) => setFields({ ...fields, skill: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="f-worker-id">Worker ID</label>
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
              <label htmlFor="f-weight-delta">Penalty weight (0–10, penalties only)</label>
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
                <label htmlFor="f-component">Objective component</label>
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
                <label htmlFor="f-multiplier">Weight multiplier (0.5–2.0)</label>
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
            <label htmlFor="f-source-ids">Source decision IDs (comma-separated, optional)</label>
            <input
              id="f-source-ids"
              type="text"
              value={sourceIds}
              onChange={(e) => setSourceIds(e.target.value)}
              placeholder="DEC-xxxx, DEC-yyyy"
            />
            <p className="field-hint">Rules with fewer than 2 source decisions are marked “Low evidence” (R18.10).</p>
          </div>

          <button type="submit">Create rule (must be enabled explicitly)</button>
        </form>
      </section>

      {/* --- 规则列表 --- */}
      <section aria-labelledby="rule-list-heading" className="preferences-list">
        <h3 id="rule-list-heading">Rule list ({rules.length})</h3>
        {rules.length === 0 ? (
          <p>No preference rules yet. Create the first one with the form above.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">Description</th>
                <th scope="col">Type / parameters</th>
                <th scope="col">Source decisions</th>
                <th scope="col">Created</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <tr key={rule.rule_id} data-enabled={rule.enabled} data-rule-id={rule.rule_id}>
                  <th scope="row">
                    {rule.human_text}
                    {rule.low_evidence && (
                      <span className="badge badge-low-evidence" title="Fewer than 2 source decisions">
                        Low evidence
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
                      {rule.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </td>
                  <td className="rule-actions">
                    <button
                      type="button"
                      onClick={() => void handleEnable(rule)}
                      disabled={!rule.enabled && atLimit}
                      aria-label={rule.enabled ? `Disable rule ${rule.rule_id}` : `Enable rule ${rule.rule_id}`}
                    >
                      {rule.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleEditText(rule.rule_id, rule.human_text)}
                      aria-label={`Edit rule ${rule.rule_id}`}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDelete(rule.rule_id)}
                      aria-label={`Delete rule ${rule.rule_id}`}
                    >
                      Delete
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleAffected(rule.rule_id)}
                      aria-label={`Show which jobs rule ${rule.rule_id} affects`}
                    >
                      Affected jobs
                    </button>
                    {affected[rule.rule_id] !== undefined && (
                      <div className="affected-jobs" role="status">
                        {(affected[rule.rule_id] ?? []).length > 0 ? (
                          <>Affected jobs: {(affected[rule.rule_id] ?? []).join(', ')}</>
                        ) : (
                          <>No jobs in the current active plan are affected by this rule.</>
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
