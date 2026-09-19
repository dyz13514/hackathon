/**
 * 电子表格摄取视图（design.md §6 `/import` 行，任务 10.5，R2/R3）。
 *
 * 工作流：上传区 → 列映射表（逐项确认/改选/不导入）→ 归一化前后样例对照 → 未解析单元格
 * 处置 → 重复文件提示 → 提交。落库经 `/api/imports/confirm`（`AcceptedMapping` 闸门，K-07）。
 * 批次列表支持整批回滚（R3.4）。
 *
 * P0 只有结构化流程；自然语言不在此。列映射提议来自 Ingestion_Agent（后端 STUB/REPLAY，无
 * LIVE），前端只呈现与确认，不做任何映射推断。
 *
 * 可访问性（R27.9）：区块 `aria-labelledby`；控件有 `<label>`；置信/状态除颜色外带文字；
 * 加载/错误/空态显式呈现。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import {
  type BatchSummary,
  type FieldMapping,
  type ProposalResponse,
  confirmImport,
  getProposal,
  listImports,
  revertImport,
  uploadImport,
} from '../api/imports';

const STATUS_LABEL: Record<FieldMapping['status'], string> = {
  AUTO_ACCEPTED: '已接受',
  NEEDS_CONFIRMATION: '待确认',
  NOT_IMPORTED: '不导入',
};

export function Import() {
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [duplicateOf, setDuplicateOf] = useState<string | null>(null);
  const [lastImportedAt, setLastImportedAt] = useState<string | null>(null);
  const [proposal, setProposal] = useState<ProposalResponse | null>(null);
  const [fields, setFields] = useState<FieldMapping[]>([]);
  const [unparsedResolved, setUnparsedResolved] = useState(false);
  const [committed, setCommitted] = useState<string | null>(null);
  const [batches, setBatches] = useState<readonly BatchSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshBatches = useCallback(async () => {
    try {
      setBatches((await listImports()).batches);
    } catch {
      /* 批次列表失败不阻断主流程 */
    }
  }, []);

  useEffect(() => {
    void refreshBatches();
  }, [refreshBatches]);

  const onUpload = useCallback(
    async (file: File) => {
      setLoading(true);
      setError(null);
      setProposal(null);
      setCommitted(null);
      try {
        const up = await uploadImport(file);
        setUploadId(up.upload_id);
        setDuplicateOf(up.duplicate_of);
        setLastImportedAt(up.last_imported_at);
        const prop = await getProposal(up.upload_id);
        setProposal(prop);
        setFields(prop.proposal.field_mappings.map((f) => ({ ...f })));
      } catch (err) {
        setError(
          err instanceof ApiError
            ? `上传失败（${err.code}）：${err.message}`
            : '上传失败：后端服务不可用。',
        );
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const setFieldStatus = (target: string, status: FieldMapping['status']) =>
    setFields((prev) => prev.map((f) => (f.target_field === target ? { ...f, status } : f)));

  const onConfirm = useCallback(async () => {
    if (!uploadId || !proposal) return;
    setLoading(true);
    setError(null);
    try {
      const result = await confirmImport(uploadId, {
        entity_type: proposal.proposal.entity_type,
        field_mappings: fields,
        unparsed_cells_resolved: unparsedResolved,
      });
      setCommitted(result.batch_id);
      await refreshBatches();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `提交失败（${err.code}）：${err.message}`
          : '提交失败：后端服务不可用。',
      );
    } finally {
      setLoading(false);
    }
  }, [uploadId, proposal, fields, unparsedResolved, refreshBatches]);

  const onRevert = useCallback(
    async (batchId: string) => {
      setError(null);
      try {
        await revertImport(batchId);
        await refreshBatches();
      } catch (err) {
        setError(
          err instanceof ApiError
            ? `回滚失败（${err.code}）：${err.message}`
            : '回滚失败：后端服务不可用。',
        );
      }
    },
    [refreshBatches],
  );

  return (
    <section aria-labelledby="import-heading" className="import">
      <h2 id="import-heading">表格导入</h2>

      <section aria-labelledby="import-upload-heading" className="import-upload">
        <h3 id="import-upload-heading">上传</h3>
        <label htmlFor="import-file">选择 .csv / .xlsx 文件</label>
        <input
          id="import-file"
          type="file"
          accept=".csv,.xlsx"
          aria-label="选择要导入的表格文件"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void onUpload(f);
          }}
        />
        {loading && <p role="status">处理中…</p>}
      </section>

      {error && (
        <p role="alert" className="import-error">
          <span aria-hidden="true">⚠ </span>
          {error}
        </p>
      )}

      {duplicateOf && (
        <p role="status" className="import-duplicate">
          该文件此前已导入（批次 {duplicateOf}
          {lastImportedAt ? `，上次导入于 ${lastImportedAt}` : ''}）。可选择「跳过」或作为新批次重新导入。
        </p>
      )}

      {proposal && !committed && (
        <>
          <section aria-labelledby="import-mapping-heading" className="import-mapping">
            <h3 id="import-mapping-heading">
              列映射（实体：{proposal.proposal.entity_type}
              {proposal.from_agent ? '，来自 Ingestion_Agent' : '，来自确定性提议'}）
            </h3>
            <table>
              <thead>
                <tr>
                  <th>目标字段</th>
                  <th>源列</th>
                  <th>置信</th>
                  <th>样例</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {fields.map((f) => (
                  <tr key={f.target_field} data-status={f.status}>
                    <td>{f.target_field}</td>
                    <td>{f.source_column ?? '—'}</td>
                    <td>{(f.confidence * 100).toFixed(0)}%</td>
                    <td>{f.sample_values.join('、') || '—'}</td>
                    <td>
                      <span className={`badge status-${f.status}`}>{STATUS_LABEL[f.status]}</span>
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => setFieldStatus(f.target_field, 'AUTO_ACCEPTED')}
                        aria-label={`确认字段 ${f.target_field}`}
                      >
                        确认
                      </button>{' '}
                      <button
                        type="button"
                        onClick={() => setFieldStatus(f.target_field, 'NOT_IMPORTED')}
                        aria-label={`标记字段 ${f.target_field} 不导入`}
                      >
                        不导入
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {proposal.proposal.missing_required_fields.length > 0 && (
            <section aria-labelledby="import-missing-heading" className="import-missing">
              <h3 id="import-missing-heading">缺失的必填字段</h3>
              <ul>
                {proposal.proposal.missing_required_fields.map((m) => (
                  <li key={m.target_field}>
                    {m.target_field}：{m.reason}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {proposal.proposal.normalisations.length > 0 && (
            <section aria-labelledby="import-norm-heading" className="import-normalisation">
              <h3 id="import-norm-heading">归一化前后对照</h3>
              <ul>
                {proposal.proposal.normalisations.map((n) => (
                  <li key={n.source_column}>
                    {n.source_column}（{n.kind}
                    {n.conversion_factor != null ? `，换算系数 ${n.conversion_factor}` : ''}）：
                    {n.sample_before.join('、')} → {n.sample_after.join('、')}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <label className="import-unparsed">
            <input
              type="checkbox"
              checked={unparsedResolved}
              onChange={(e) => setUnparsedResolved(e.target.checked)}
            />
            我已处置全部未解析单元格 / 冲突项
          </label>

          <button
            type="button"
            onClick={() => void onConfirm()}
            disabled={loading}
            aria-label="确认映射并落库"
          >
            确认并导入
          </button>
        </>
      )}

      {committed && (
        <p role="status" className="import-committed">
          已导入为批次 {committed}。
        </p>
      )}

      <section aria-labelledby="import-batches-heading" className="import-batches">
        <h3 id="import-batches-heading">导入批次</h3>
        {batches.length === 0 ? (
          <p className="import-empty">暂无导入批次。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>文件</th>
                <th>实体</th>
                <th>行数</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.batch_id}>
                  <td>{b.batch_id}</td>
                  <td>{b.file_name}</td>
                  <td>{b.entity_type}</td>
                  <td>{b.row_count}</td>
                  <td>{b.status}</td>
                  <td>
                    {b.status !== 'REVERTED' && (
                      <button
                        type="button"
                        onClick={() => void onRevert(b.batch_id)}
                        aria-label={`回滚批次 ${b.batch_id}`}
                      >
                        回滚
                      </button>
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
