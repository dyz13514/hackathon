/**
 * 风险面板视图（design.md Components §6 `/risks` 行，任务 8.5/8.6，R14）。
 *
 * 按 severity 分组的卡片（R14.6），每条含：度量值、阈值、`last_seen_at`、叙述与
 * `narrative_source` 徽章（`TEMPLATE`/`LLM` 可区分，R14.11），以及受影响订单。CRITICAL 项额外
 * 显示「查看缓解提案」入口（R14.7，链到该风险的 `mitigation_plan_id`）——INFO/WARNING 仅入
 * 面板、无缓解入口（R14.6）。
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
  CRITICAL: '严重',
  WARNING: '警告',
  INFO: '提示',
};

const RISK_TYPE_LABEL: Record<string, string> = {
  MATERIAL_RUNOUT_FORECAST: '物料将耗尽',
  ZERO_SLACK_ORDER: '订单零余量',
  BOTTLENECK_RESOURCE: '瓶颈资源',
  OVERCOMMITTED_SHIFT: '班次超配',
  SINGLE_POINT_OF_FAILURE_MACHINE: '单点故障机器',
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
        <span className={`badge severity-${finding.severity}`} aria-label={`严重度：${SEVERITY_LABEL[finding.severity] ?? finding.severity}`}>
          {SEVERITY_LABEL[finding.severity] ?? finding.severity}
        </span>{' '}
        <strong>{RISK_TYPE_LABEL[finding.risk_type] ?? finding.risk_type}</strong> ·{' '}
        {finding.entity_type} {finding.entity_id}
      </p>
      <p className="risk-metric">
        度量 {finding.metric_value} · 阈值 {finding.threshold_value}
      </p>
      {finding.narrative && (
        <p className="risk-narrative">
          {finding.narrative}
          {finding.narrative_source && (
            <span
              className={`badge source-${finding.narrative_source}`}
              aria-label={`叙述来源：${finding.narrative_source}`}
            >
              {finding.narrative_source}
            </span>
          )}
        </p>
      )}
      {finding.affected_order_ids.length > 0 && (
        <p className="risk-affected">受影响订单：{finding.affected_order_ids.join('、')}</p>
      )}
      <p className="risk-meta">最近出现：{formatTimestamp(finding.last_seen_at)}</p>
      {finding.severity === 'CRITICAL' && finding.mitigation_plan_id && (
        <p className="risk-mitigation">
          <a href={`/plans/${finding.mitigation_plan_id}`}>查看缓解提案</a>
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
          ? `风险面板不可用（${err.code}）：${err.message}`
          : '风险面板不可用：后端服务不可用。';
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
          ? `扫描失败（${err.code}）：${err.message}`
          : '扫描失败：网络或服务不可用。';
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
        <h2 id="risks-heading">风险雷达</h2>
        <button
          type="button"
          onClick={() => void onScan()}
          disabled={scanning || loading}
          aria-busy={scanning}
          aria-label="重新扫描风险"
        >
          {scanning ? '扫描中…' : '重新扫描'}
        </button>
      </div>

      {error && (
        <p role="alert" className="risks-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {data && !loading && (
        <div className="risks-body">
          <ul className="risks-summary" aria-label="风险计数">
            <li>严重：<strong>{data.critical_count}</strong></li>
            <li>警告：<strong>{data.warning_count}</strong></li>
            <li>提示：<strong>{data.info_count}</strong></li>
          </ul>

          {data.findings.length === 0 ? (
            <p className="risks-empty">当前无风险发现。点击「重新扫描」运行确定性风险扫描。</p>
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
                    {SEVERITY_LABEL[severity]}（{group.length}）
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
