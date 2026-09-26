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
  type PackagePreview,
  type ProposalResponse,
  type ValidateResult,
  confirmImport,
  confirmPlanningPackage,
  getProposal,
  listImports,
  previewPlanningPackage,
  revertImport,
  uploadImport,
  validateMapping,
} from '../api/imports';

const STATUS_LABEL: Record<FieldMapping['status'], string> = {
  AUTO_ACCEPTED: 'Accepted',
  NEEDS_CONFIRMATION: 'Needs confirmation',
  NOT_IMPORTED: 'Not imported',
};

const IMPORT_DRAFT_KEY = 'planning-import-draft-v1';

interface ImportDraft {
  packagePreview: PackagePreview | null;
  packageCommitted: string | null;
  uploadId: string | null;
  duplicateOf: string | null;
  lastImportedAt: string | null;
  proposal: ProposalResponse | null;
  fields: FieldMapping[];
  validation: ValidateResult | null;
  committed: string | null;
}

function loadImportDraft(): Partial<ImportDraft> {
  try {
    return JSON.parse(window.sessionStorage.getItem(IMPORT_DRAFT_KEY) ?? '{}') as Partial<ImportDraft>;
  } catch {
    return {};
  }
}

export function Import() {
  const [restored] = useState(loadImportDraft);
  const [packagePreview, setPackagePreview] = useState<PackagePreview | null>(restored.packagePreview ?? null);
  const [packageLoading, setPackageLoading] = useState(false);
  const [packageError, setPackageError] = useState<string | null>(null);
  const [packageConflict, setPackageConflict] = useState(false);
  const [packageCommitted, setPackageCommitted] = useState<string | null>(restored.packageCommitted ?? null);
  const [uploadId, setUploadId] = useState<string | null>(restored.uploadId ?? null);
  const [duplicateOf, setDuplicateOf] = useState<string | null>(restored.duplicateOf ?? null);
  const [lastImportedAt, setLastImportedAt] = useState<string | null>(restored.lastImportedAt ?? null);
  const [proposal, setProposal] = useState<ProposalResponse | null>(restored.proposal ?? null);
  const [fields, setFields] = useState<FieldMapping[]>(restored.fields ?? []);
  const [validation, setValidation] = useState<ValidateResult | null>(restored.validation ?? null);
  const [committed, setCommitted] = useState<string | null>(restored.committed ?? null);
  const [batches, setBatches] = useState<readonly BatchSummary[]>([]);
  const [batchesError, setBatchesError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /** 正在回滚的批次 id，用于禁用该行按钮、防重复点击。 */
  const [reverting, setReverting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshBatches = useCallback(async () => {
    try {
      setBatches((await listImports()).batches);
      setBatchesError(null);
    } catch {
      setBatchesError('Could not load import batches. Refresh the page to retry.');
    }
  }, []);

  useEffect(() => {
    void refreshBatches();
  }, [refreshBatches]);

  useEffect(() => {
    const draft: ImportDraft = {
      packagePreview, packageCommitted, uploadId, duplicateOf, lastImportedAt,
      proposal, fields, validation, committed,
    };
    try {
      window.sessionStorage.setItem(IMPORT_DRAFT_KEY, JSON.stringify(draft));
    } catch {
      // The import still works when browser storage is disabled or full.
    }
  }, [
    packagePreview, packageCommitted, uploadId, duplicateOf, lastImportedAt,
    proposal, fields, validation, committed,
  ]);

  const onPackageUpload = useCallback(async (file: File) => {
    setPackageLoading(true);
    setPackageError(null);
    setPackageConflict(false);
    setPackageCommitted(null);
    setPackagePreview(null);
    try {
      setPackagePreview(await previewPlanningPackage(file));
    } catch (err) {
      if (err instanceof LoginCancelledError) return;
      setPackageError(err instanceof ApiError
        ? `Workbook validation failed (${err.code}): ${err.message}`
        : 'Workbook validation failed: backend service unavailable.');
    } finally {
      setPackageLoading(false);
    }
  }, []);

  const onPackageConfirm = useCallback(async () => {
    if (!packagePreview) return;
    setPackageLoading(true);
    setPackageError(null);
    setPackageConflict(false);
    try {
      const result = await confirmPlanningPackage(packagePreview.upload_id);
      setPackageCommitted(result.batch_id);
      setPackagePreview(null);
      await refreshBatches();
    } catch (err) {
      if (err instanceof LoginCancelledError) return;
      setPackageConflict(err instanceof ApiError && err.code === 'IMPORT_DATA_INVALID'
        && err.message.includes('planning workspace already contains data'));
      setPackageError(err instanceof ApiError
        ? `Workbook import failed (${err.code}): ${err.message}`
        : 'Workbook import failed: backend service unavailable.');
    } finally {
      setPackageLoading(false);
    }
  }, [packagePreview, refreshBatches]);

  const onUpload = useCallback(
    async (file: File) => {
      setLoading(true);
      setError(null);
      setProposal(null);
      setValidation(null);
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
        setFields([
          ...prop.proposal.field_mappings.map((f) => ({ ...f })),
          ...prop.proposal.missing_required_fields
            .filter((missing) => !prop.proposal.field_mappings.some((f) => f.target_field === missing.target_field))
            .map((missing) => ({
              target_field: missing.target_field,
              source_column: null,
              confidence: 0,
              sample_values: [],
              status: 'NEEDS_CONFIRMATION' as const,
            })),
        ]);
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
    setFields((prev) => prev.map((f) => (f.target_field === target ? { ...f, status } : f)));
  };

  const setFieldSource = (target: string, source: string) => {
    setValidation(null);
    const column = proposal?.preview.columns.find((item) => item.raw_header === source);
    setFields((prev) => prev.map((field) => field.target_field === target
      ? {
          ...field,
          source_column: source || null,
          confidence: source ? 1 : 0,
          sample_values: column?.sample_values ?? [],
          status: source ? 'AUTO_ACCEPTED' : 'NEEDS_CONFIRMATION',
        }
      : field));
  };

  const missingRequired = proposal?.proposal.missing_required_fields.filter((missing) =>
    !fields.some((field) =>
      field.target_field === missing.target_field && field.source_column && field.status === 'AUTO_ACCEPTED')) ?? [];
  const canImportEntity = proposal?.proposal.entity_type === 'ORDER' || proposal?.proposal.entity_type === 'MATERIAL';
  const sourceOptions = Array.from(new Set([
    ...(proposal?.preview.columns.map((column) => column.raw_header).filter(Boolean) ?? []),
    ...fields.map((field) => field.source_column).filter((source): source is string => Boolean(source)),
  ]));

  const onConfirm = useCallback(async () => {
    if (!uploadId || !proposal) return;
    setLoading(true);
    setError(null);
    try {
      if (missingRequired.length > 0 || fields.some((field) => field.status === 'NEEDS_CONFIRMATION')) {
        setError('Map and accept every required field before importing. Accepting a field alone does not save the batch.');
        return;
      }
      const checked = await validateMapping(uploadId, { field_mappings: fields });
      setValidation(checked);
      if (checked.unparsed_cells.length > 0) {
        setError('Some cells cannot be parsed. Correct the file and upload it again before importing.');
        return;
      }
      const result = await confirmImport(uploadId, {
        entity_type: proposal.proposal.entity_type,
        field_mappings: fields,
        unparsed_cells_resolved: true,
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
  }, [uploadId, proposal, fields, missingRequired.length, refreshBatches]);

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

      <section aria-labelledby="package-heading" className="import-upload">
        <h3 id="package-heading">Complete planning workbook</h3>
        <p>Start an empty workspace with products, routings, materials, machines, workers and orders in one workbook. Validation does not save data; confirm the preview to create a batch.</p>
        <p>Preview and mapping decisions stay in this browser tab when you switch pages.</p>
        <label htmlFor="package-file">Choose a complete .xlsx workbook</label>
        <input
          id="package-file"
          type="file"
          accept=".xlsx"
          aria-label="Choose a complete planning workbook"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = '';
            if (file) void onPackageUpload(file);
          }}
        />
        {packageLoading && <p role="status">Validating workbook…</p>}
        {packageError && <p role="alert" className="import-error">{packageError}</p>}
        {packageConflict && (
          <p className="import-error">
            Complete workbooks replace the whole planning dataset; they cannot be layered onto
            an active package. To correct only an order or material, use the daily update below
            or edit material inventory from the Schedule blocker. To replace the full package,
            resolve any pending proposal in <a href="/approval">Approval</a>, then deliberately
            revert the current PACKAGE row in <a href="#import-batches-heading">Import batches</a>
            {' '}and confirm this workbook again. Existing plans and traces remain historical.
          </p>
        )}
        {packagePreview && (
          <div className="import-validation">
            <p>{packagePreview.file_name}: {packagePreview.row_count} rows passed schema and reference checks. Earliest order due: {packagePreview.earliest_due_date ?? 'none'}.</p>
            <ul>
              {Object.entries(packagePreview.sheets).map(([name, count]) => (
                <li key={name}>{name}: {count} rows</li>
              ))}
            </ul>
            <button type="button" disabled={packageLoading} onClick={() => void onPackageConfirm()}>
              Confirm complete workbook import
            </button>
          </div>
        )}
        {packageCommitted && (
          <p role="status">Workbook imported as batch {packageCommitted}. <a href="/schedule">Open Schedule</a></p>
        )}
      </section>

      <section aria-labelledby="import-upload-heading" className="import-upload">
        <h3 id="import-upload-heading">Daily order or material update</h3>
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
            <p>Review each field, then select “Confirm and import” below to save a batch. “Accept” only confirms one field.</p>
            {!canImportEntity && (
              <p role="status">This entity can be previewed, but importing its complete scheduling data is not supported yet. No batch will be created.</p>
            )}
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
                    <td>
                      <select
                        aria-label={`Source column for ${f.target_field}`}
                        value={f.source_column ?? ''}
                        onChange={(event) => setFieldSource(f.target_field, event.target.value)}
                      >
                        <option value="">Choose a source column</option>
                        {sourceOptions.map((source) => <option key={source} value={source}>{source}</option>)}
                      </select>
                    </td>
                    <td>{(f.confidence * 100).toFixed(0)}%</td>
                    <td>{f.sample_values.join(', ') || '-'}</td>
                    <td>
                      <span className={`badge status-${f.status}`}>{STATUS_LABEL[f.status]}</span>
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => setFieldStatus(f.target_field, 'AUTO_ACCEPTED')}
                        disabled={!f.source_column}
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

          {missingRequired.length > 0 && (
            <section aria-labelledby="import-missing-heading" className="import-missing">
              <h3 id="import-missing-heading">Missing required fields</h3>
              <ul>
                {missingRequired.map((m) => (
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
                  <p>Correct these cells in the source file and upload it again.</p>
                </>
              )}
            </section>
          )}

          <button
            type="button"
            onClick={() => void onConfirm()}
            disabled={loading || !canImportEntity || missingRequired.length > 0 || fields.some((field) => field.status === 'NEEDS_CONFIRMATION') || Boolean(validation?.unparsed_cells.length)}
            aria-label="Confirm mapping and import"
          >
            Confirm and import
          </button>
        </>
      )}

      {committed && (
        <p role="status" className="import-committed">
          Imported as batch {committed}. {proposal?.proposal.entity_type === 'ORDER' && <a href="/schedule">Open Schedule to generate a plan</a>}
        </p>
      )}

      <section aria-labelledby="import-batches-heading" className="import-batches">
        <h3 id="import-batches-heading">Import batches</h3>
        {batchesError && <p role="alert">{batchesError}</p>}
        {batches.length === 0 && !batchesError ? (
          <p className="import-empty">No import batches yet.</p>
        ) : batches.length > 0 ? (
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
        ) : null}
      </section>
    </section>
  );
}
