/**
 * 风险面板视图（design.md Components §6 `/risks` 行，任务 8.5/8.6，R14）。
 *
 * 按 severity 分组的卡片（R14.6），每条含：度量值、阈值、`last_seen_at`、叙述与
 * `narrative_source` 徽章（`TEMPLATE`/`LLM` 可区分，R14.11），以及受影响订单。CRITICAL 项额外
 * 显示「查看缓解提案」入口（R14.7）——INFO/WARNING 仅入面板、无缓解入口（R14.6）。
 *
 * **缓解提案是走审批流的 `PENDING_APPROVAL` 计划**（`origin = RISK_MITIGATION`，design.md
 * 1618：CRITICAL 的缓解提案同样受 L5 约束、不会自动生效）。因此该入口指向**既有的审批视图**
 * 并带上该计划的 `plan_id`（`/approval?plan_id=…`），由它选中并处置这一份提案——design.md §6
 * 的视图清单里没有「计划详情页」，不需要为此新建一个视图。
 *
 * 数据来自 `GET /api/risks`（只读）；「重新扫描」按钮触发 `POST /api/risks/scan`（确定性、无
 * LLM）。加载态、错误态、空态都显式呈现。
 *
 * 可访问性（R27.9）：区块用 `<section aria-labelledby>`；severity 除颜色外带文字标签；来源
 * 徽章带文字；扫描按钮可键盘到达并有 `aria-label`。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { getRisks, type RiskFinding, type RiskList, scanRisks } from '../api/risks';

const SEVERITY_LABEL: Record<string, string> = {
  CRITICAL: 'Critical',
  WARNING: 'Warning',
  INFO: 'Info',
};

const RISK_TYPE_LABEL: Record<string, string> = {
  MATERIAL_RUNOUT_FORECAST: 'Material runout forecast',
  ZERO_SLACK_ORDER: 'Zero-slack order',
  BOTTLENECK_RESOURCE: 'Bottleneck resource',
  OVERCOMMITTED_SHIFT: 'Overcommitted shift',
  SINGLE_POINT_OF_FAILURE_MACHINE: 'Single point of failure machine',
};

const SEVERITY_ORDER = ['CRITICAL', 'WARNING', 'INFO'] as const;

function formatTimestamp(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

function RiskCard({ finding }: { finding: RiskFinding }) {
  return (
    <li className={`risk-card risk-${finding.severity}`} data-severity={finding.severity}>
      <p className="risk-head">
        <span className={`badge severity-${finding.severity}`} aria-label={`Severity: ${SEVERITY_LABEL[finding.severity] ?? finding.severity}`}>
          {SEVERITY_LABEL[finding.severity] ?? finding.severity}
        </span>{' '}
        <strong>{RISK_TYPE_LABEL[finding.risk_type] ?? finding.risk_type}</strong> ·{' '}
        {finding.entity_type} {finding.entity_id}
      </p>
      <p className="risk-metric">
        Metric {finding.metric_value} · threshold {finding.threshold_value}
      </p>
      {finding.narrative && (
        <p className="risk-narrative">
          {finding.narrative}
          {finding.narrative_source && (
            <span
              className={`badge source-${finding.narrative_source}`}
              aria-label={`Narrative source: ${finding.narrative_source}`}
            >
              {finding.narrative_source}
            </span>
          )}
        </p>
      )}
      {finding.affected_order_ids.length > 0 && (
        <p className="risk-affected">Affected orders: {finding.affected_order_ids.join(', ')}</p>
      )}
      <p className="risk-meta">Last seen: {formatTimestamp(finding.last_seen_at)}</p>
      {finding.severity === 'CRITICAL' && finding.mitigation_plan_id && (
        <p className="risk-mitigation">
          <a href={`/approval?plan_id=${encodeURIComponent(finding.mitigation_plan_id)}`}>
            View mitigation proposal
          </a>
        </p>
      )}
    </li>
  );
}

export function Risks() {
  const [data, setData] = useState<RiskList | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getRisks());
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `Risks unavailable (${err.code}): ${err.message}`
          : 'Risks unavailable: backend service unavailable.';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  const onScan = useCallback(async () => {
    setScanning(true);
    setError(null);
    try {
      await scanRisks();
      setData(await getRisks());
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `Scan failed (${err.code}): ${err.message}`
          : 'Scan failed: network or service unavailable.';
      setError(message);
    } finally {
      setScanning(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-labelledby="risks-heading" className="risks">
      <div className="risks-header">
        <h2 id="risks-heading">Risk radar</h2>
        <button
          type="button"
          onClick={() => void onScan()}
          disabled={scanning || loading}
          aria-busy={scanning}
          aria-label="Rescan risks"
        >
          {scanning ? 'Scanning…' : 'Rescan'}
        </button>
      </div>

      {error && (
        <p role="alert" className="risks-error">
          {error}
        </p>
      )}

      {data && !loading && (
        <div className="risks-body">
          <ul className="risks-summary" aria-label="Risk counts">
            <li>Critical: <strong>{data.critical_count}</strong></li>
            <li>Warning: <strong>{data.warning_count}</strong></li>
            <li>Info: <strong>{data.info_count}</strong></li>
          </ul>

          {data.findings.length === 0 ? (
            <p className="risks-empty">No risk findings. Click “Rescan” to run the deterministic risk scan.</p>
          ) : (
            SEVERITY_ORDER.map((severity) => {
              const group = data.findings.filter((f) => f.severity === severity);
              if (group.length === 0) {
                return null;
              }
              return (
                <section
                  key={severity}
                  aria-labelledby={`risk-group-${severity}`}
                  className="risk-group"
                >
                  <h3 id={`risk-group-${severity}`}>
                    {SEVERITY_LABEL[severity]} ({group.length})
                  </h3>
                  <ul className="risk-list">
                    {group.map((finding) => (
                      <RiskCard key={finding.finding_id} finding={finding} />
                    ))}
                  </ul>
                </section>
              );
            })
          )}
        </div>
      )}
    </section>
  );
}
