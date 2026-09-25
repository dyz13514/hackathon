/**
 * 电子表格摄取视图（design.md §6 `/import` 行，任务 10.5，R2/R3）。
 *
 * 工作流：上传区 → 列映射表（逐项确认/改选/不导入）→ 归一化前后样例对照 → 未解析单元格
 * 处置 → 重复文件提示 → 提交。落库经 `/api/imports/confirm`（`AcceptedMapping` 闸门，K-07）。
 * 批次列表支持整批回滚（R3.4）。
 *
 * P0 只有结构化流程；自然语言不在此。列映射提议来自后端 Ingestion_Agent，
 * 前端只呈现与确认，不做任何映射推断。LIVE 模型失败时显示错误，不冒充成功映射。
 *
 * 可访问性（R27.9）：区块 `aria-labelledby`；控件有 `<label>`；置信/状态除颜色外带文字；
 * 加载/错误/空态显式呈现。
 */

import { useCallback, useEffect, useState } from 'react';

import { ApiError, LoginCancelledError } from '../api/client';
import {
  type BatchSummary,
  type FieldMapping,
  type ProposalResponse,
  type ValidateResult,
  confirmImport,
  getProposal,
  listImports,
  revertImport,
  uploadImport,
  validateMapping,
} from '../api/imports';

const STATUS_LABEL: Record<FieldMapping['status'], string> = {
  AUTO_ACCEPTED: 'Accepted',
  NEEDS_CONFIRMATION: 'Needs confirmation',
  NOT_IMPORTED: 'Not imported',
};

export function Import() {
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [duplicateOf, setDuplicateOf] = useState<string | null>(null);
  const [lastImportedAt, setLastImportedAt] = useState<string | null>(null);
  const [proposal, setProposal] = useState<ProposalResponse | null>(null);
  const [fields, setFields] = useState<FieldMapping[]>([]);
  const [validation, setValidation] = useState<ValidateResult | null>(null);
  const [unparsedResolved, setUnparsedResolved] = useState(false);
  const [committed, setCommitted] = useState<string | null>(null);
  const [batches, setBatches] = useState<readonly BatchSummary[]>([]);
  const [loading, setLoading] = useState(false);
  /** 正在回滚的批次 id，用于禁用该行按钮、防重复点击。 */
  const [reverting, setReverting] = useState<string | null>(null);
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
      setValidation(null);
      setUnparsedResolved(false);
      setCommitted(null);
      let stage = 'Upload';
      try {
        const up = await uploadImport(file);
        setUploadId(up.upload_id);
        setDuplicateOf(up.duplicate_of);
        setLastImportedAt(up.last_imported_at);
        stage = 'Mapping proposal';
        const prop = await getProposal(up.upload_id);
        setProposal(prop);
        setFields(prop.proposal.field_mappings.map((f) => ({ ...f })));
        stage = 'Mapping validation';
        setValidation(await validateMapping(up.upload_id, {
          field_mappings: prop.proposal.field_mappings,
        }));
      } catch (err) {
        if (err instanceof LoginCancelledError) return;
        setError(
          err instanceof ApiError
            ? `${stage} failed (${err.code}): ${err.message}`
            : `${stage} failed: backend service unavailable.`,
        );
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const setFieldStatus = (target: string, status: FieldMapping['status']) => {
    setValidation(null);
    setUnparsedResolved(false);
    setFields((prev) => prev.map((f) => (f.target_field === target ? { ...f, status } : f)));
  };

  const onConfirm = useCallback(async () => {
    if (!uploadId || !proposal) return;
    setLoading(true);
    setError(null);
    try {
      const checked = await validateMapping(uploadId, { field_mappings: fields });
      setValidation(checked);
      if (checked.unparsed_cells.length > 0 && !unparsedResolved) {
        setError('Review the unparsed cells below before confirming the import.');
        return;
      }
      const result = await confirmImport(uploadId, {
        entity_type: proposal.proposal.entity_type,
        field_mappings: fields,
        unparsed_cells_resolved: checked.unparsed_cells.length === 0 || unparsedResolved,
      });
      setCommitted(result.batch_id);
      await refreshBatches();
    } catch (err) {
      if (err instanceof LoginCancelledError) return;
      setError(
        err instanceof ApiError
          ? `Confirm failed (${err.code}): ${err.message}`
          : 'Confirm failed: backend service unavailable.',
      );
    } finally {
      setLoading(false);
    }
  }, [uploadId, proposal, fields, unparsedResolved, refreshBatches]);

  const onRevert = useCallback(
    async (batchId: string) => {
      if (reverting) return;
      // 回滚整批已导入数据是破坏性操作：先确认，取消则不发请求。
      const confirmed = window.confirm(
        `Revert import batch ${batchId}? This rolls back all rows imported in this batch and cannot be undone.`,
      );
      if (!confirmed) return;
      setError(null);
      setReverting(batchId);
      try {
        await revertImport(batchId);
        await refreshBatches();
      } catch (err) {
        setError(
          err instanceof ApiError
            ? `Revert failed (${err.code}): ${err.message}`
            : 'Revert failed: backend service unavailable.',
        );
      } finally {
        setReverting(null);
      }
    },
    [refreshBatches, reverting],
  );

  return (
    <section aria-labelledby="import-heading" className="import">
      <h2 id="import-heading">Spreadsheet import</h2>

      <section aria-labelledby="import-upload-heading" className="import-upload">
        <h3 id="import-upload-heading">Upload</h3>
        <label htmlFor="import-file">Choose a .csv / .xlsx file</label>
        <input
          id="import-file"
          type="file"
          accept=".csv,.xlsx"
          aria-label="Choose a spreadsheet file to import"
          onChange={(e) => {
            const f = e.target.files?.[0];
            // 立刻清空 input 的值：否则再次选择"同名同文件"时不会触发 change 事件，
            // 用户会看到"点了没反应"。清空后即使选同一个文件也能重新上传/重试。
            e.target.value = '';
            if (f) void onUpload(f);
          }}
        />
        {loading && <p role="status">Processing…</p>}
      </section>

      {error && (
        <p role="alert" className="import-error">
          {error}
        </p>
      )}

      {duplicateOf && (
        <p role="status" className="import-duplicate">
          This file was imported before (batch {duplicateOf}
          {lastImportedAt ? `, last imported ${lastImportedAt}` : ''}). You can skip it or re-import it as a new batch.
        </p>
      )}

      {proposal && !committed && (
        <>
          <section aria-labelledby="import-mapping-heading" className="import-mapping">
            <h3 id="import-mapping-heading">
              Column mapping (entity: {proposal.proposal.entity_type}
              {proposal.from_agent ? ', from Ingestion_Agent' : ', from deterministic proposal'})
            </h3>
            <table>
              <thead>
                <tr>
                  <th>Target field</th>
                  <th>Source column</th>
                  <th>Confidence</th>
                  <th>Samples</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {fields.map((f) => (
                  <tr key={f.target_field} data-status={f.status}>
                    <td>{f.target_field}</td>
                    <td>{f.source_column ?? '-'}</td>
                    <td>{(f.confidence * 100).toFixed(0)}%</td>
                    <td>{f.sample_values.join(', ') || '-'}</td>
                    <td>
                      <span className={`badge status-${f.status}`}>{STATUS_LABEL[f.status]}</span>
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => setFieldStatus(f.target_field, 'AUTO_ACCEPTED')}
                        aria-label={`Accept field ${f.target_field}`}
                      >
                        Accept
                      </button>{' '}
                      <button
                        type="button"
                        onClick={() => setFieldStatus(f.target_field, 'NOT_IMPORTED')}
                        aria-label={`Mark field ${f.target_field} as not imported`}
                      >
                        Don’t import
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {proposal.proposal.missing_required_fields.length > 0 && (
            <section aria-labelledby="import-missing-heading" className="import-missing">
              <h3 id="import-missing-heading">Missing required fields</h3>
              <ul>
                {proposal.proposal.missing_required_fields.map((m) => (
                  <li key={m.target_field}>
                    {m.target_field}: {m.reason}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {proposal.proposal.normalisations.length > 0 && (
            <section aria-labelledby="import-norm-heading" className="import-normalisation">
              <h3 id="import-norm-heading">Normalisation before / after</h3>
              <ul>
                {proposal.proposal.normalisations.map((n) => (
                  <li key={n.source_column}>
                    {n.source_column} ({n.kind}
                    {n.conversion_factor != null ? `, conversion factor ${n.conversion_factor}` : ''}):{' '}
                    {n.sample_before.join(', ')} -&gt; {n.sample_after.join(', ')}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {validation && (
            <section aria-labelledby="import-validation-heading" className="import-validation">
              <h3 id="import-validation-heading">Validation</h3>
              <p>{validation.parsed_row_count} rows parsed; {validation.type_error_count} type errors; {validation.normalisation_failure_count} normalisation failures.</p>
              {validation.unparsed_cells.length > 0 && (
                <>
                  <ul>
                    {validation.unparsed_cells.map((cell) => (
                      <li key={`${cell.row_number}-${cell.column_name}`}>
                        Row {cell.row_number}, {cell.column_name}: {cell.raw_value} — {cell.reason}
                      </li>
                    ))}
                  </ul>
                  <label className="import-unparsed">
                    <input
                      type="checkbox"
                      checked={unparsedResolved}
                      onChange={(e) => setUnparsedResolved(e.target.checked)}
                    />
                    I have reviewed and resolved the listed cells
                  </label>
                </>
              )}
            </section>
          )}

          <button
            type="button"
            onClick={() => void onConfirm()}
            disabled={loading}
            aria-label="Confirm mapping and import"
          >
            Confirm and import
          </button>
        </>
      )}

      {committed && (
        <p role="status" className="import-committed">
          Imported as batch {committed}.
        </p>
      )}

      <section aria-labelledby="import-batches-heading" className="import-batches">
        <h3 id="import-batches-heading">Import batches</h3>
        {batches.length === 0 ? (
          <p className="import-empty">No import batches yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Batch</th>
                <th>File</th>
                <th>Entity</th>
                <th>Rows</th>
                <th>Status</th>
                <th>Actions</th>
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
                        disabled={reverting !== null}
                        aria-busy={reverting === b.batch_id}
                        aria-label={`Revert batch ${b.batch_id}`}
                      >
                        {reverting === b.batch_id ? 'Reverting…' : 'Revert'}
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
