import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CommitResult, ProposalResponse, UploadResult } from '../api/imports';
import { ApiError } from '../api/client';

vi.mock('../api/imports', async () => {
  const actual = await vi.importActual<typeof import('../api/imports')>('../api/imports');
  return {
    ...actual,
    uploadImport: vi.fn(),
    getProposal: vi.fn(),
    validateMapping: vi.fn(),
    confirmImport: vi.fn(),
    listImports: vi.fn(),
    revertImport: vi.fn(),
  };
});

import {
  confirmImport,
  getProposal,
  validateMapping,
  listImports,
  revertImport,
  uploadImport,
} from '../api/imports';
import { Import } from '../routes/Import';

const UPLOAD: UploadResult = {
  upload_id: 'UP-1',
  file_name: 'materials.csv',
  total_rows: 2,
  duplicate_of: null,
  last_imported_at: null,
};

const PROPOSAL: ProposalResponse = {
  upload_id: 'UP-1',
  agent_outcome: 'OK',
  from_agent: true,
  proposal: {
    entity_type: 'MATERIAL',
    entity_type_confidence: 0.9,
    field_mappings: [
      {
        target_field: 'material_id',
        source_column: 'material_id',
        confidence: 0.95,
        sample_values: ['MAT-1'],
        status: 'AUTO_ACCEPTED',
      },
      {
        target_field: 'name',
        source_column: 'Name',
        confidence: 0.5,
        sample_values: ['Steel plate'],
        status: 'NEEDS_CONFIRMATION',
      },
    ],
    missing_required_fields: [{ target_field: 'quantity_available', reason: 'No source column found' }],
    normalisations: [
      {
        source_column: 'unit',
        kind: 'UNIT_CONVERSION',
        detected_pattern: 'UNIT',
        conversion_factor: 12,
        sample_before: ['box'],
        sample_after: ['12'],
      },
    ],
  },
  preview: {
    detected_header_row: 0,
    total_rows: 2,
    preview_tokens: 300,
    columns: [],
    formula_columns: [],
  },
};

function fileOf(name: string): File {
  return new File(['a,b\n1,2'], name, { type: 'text/csv' });
}

afterEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

const VALIDATION = {
  upload_id: 'UP-1', parsed_row_count: 2, unparsed_cells: [], type_error_count: 0, normalisation_failure_count: 0,
};

// Each upload now runs the deterministic validation endpoint before showing confirmation.
beforeEach(() => vi.mocked(validateMapping).mockResolvedValue(VALIDATION));

describe('Import 视图', () => {
  it('上传后渲染列映射、缺失必填与归一化对照（R2）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    render(<Import />);

    fireEvent.change(screen.getByLabelText('Choose a spreadsheet file to import'), {
      target: { files: [fileOf('materials.csv')] },
    });

    expect(await screen.findByRole('heading', { name: /Column mapping/ })).toBeInTheDocument();
    expect(screen.getAllByText('material_id').length).toBeGreaterThanOrEqual(1);
    // 待确认状态可见（R2.7）
    expect(screen.getAllByText('Needs confirmation').length).toBeGreaterThanOrEqual(1);
    // 缺失必填（R2）
    expect(screen.getByRole('heading', { name: /Missing required fields/ })).toBeInTheDocument();
    // 归一化换算系数（R2.5）
    expect(screen.getByText(/conversion factor 12/)).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Validation' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/reviewed and resolved/)).not.toBeInTheDocument();
  });

  it('确认并导入触发 confirm 并显示批次（R2.9/R3.2）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    const commit: CommitResult = { batch_id: 'BATCH-9', entity_type: 'MATERIAL', imported_row_count: 2 };
    vi.mocked(confirmImport).mockResolvedValue(commit);
    render(<Import />);
    fireEvent.change(screen.getByLabelText('Choose a spreadsheet file to import'), {
      target: { files: [fileOf('m.csv')] },
    });
    await screen.findByRole('heading', { name: /Column mapping/ });

    fireEvent.click(screen.getByRole('button', { name: /Confirm mapping and import/ }));
    await waitFor(() => expect(confirmImport).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Imported as batch BATCH-9/)).toBeInTheDocument();
  });

  it('重复文件提示上次导入（R3.6）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue({
      ...UPLOAD,
      duplicate_of: 'BATCH-old',
      last_imported_at: '2026-03-01T00:00:00',
    });
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    render(<Import />);
    fireEvent.change(screen.getByLabelText('Choose a spreadsheet file to import'), {
      target: { files: [fileOf('dup.csv')] },
    });
    expect(await screen.findByText(/imported before \(batch BATCH-old/)).toBeInTheDocument();
  });

  it('批次列表支持回滚（R3.4）', async () => {
    vi.mocked(listImports).mockResolvedValue({
      batches: [
        {
          batch_id: 'BATCH-1',
          file_name: 'a.csv',
          entity_type: 'MATERIAL',
          row_count: 3,
          status: 'COMMITTED',
          imported_at: '2026-03-02T00:00:00',
        },
      ],
    });
    vi.mocked(revertImport).mockResolvedValue({ batch_id: 'BATCH-1', reverted_row_count: 3 });
    // 回滚现在需要确认（破坏性操作）：确认放行。
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<Import />);
    await screen.findByRole('heading', { name: /Import batches/ });

    fireEvent.click(await screen.findByRole('button', { name: /Revert batch BATCH-1/ }));
    await waitFor(() => expect(revertImport).toHaveBeenCalledWith('BATCH-1'));
  });

  // 回归（round-2）：回滚需确认，取消则不发请求（防误滚）。
  it('回滚在用户取消确认时不发请求', async () => {
    vi.mocked(listImports).mockResolvedValue({
      batches: [
        {
          batch_id: 'BATCH-1',
          file_name: 'a.csv',
          entity_type: 'MATERIAL',
          row_count: 3,
          status: 'COMMITTED',
          imported_at: '2026-03-02T00:00:00',
        },
      ],
    });
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<Import />);
    await screen.findByRole('heading', { name: /Import batches/ });

    fireEvent.click(await screen.findByRole('button', { name: /Revert batch BATCH-1/ }));
    expect(revertImport).not.toHaveBeenCalled();
  });

  // 回归（round-2）：选择文件后 input 会被清空，以便再次选择同名文件仍能触发上传。
  it('选择文件后清空 input 值，允许再次选择同名文件', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    render(<Import />);

    const input = screen.getByLabelText('Choose a spreadsheet file to import') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [fileOf('materials.csv')] } });

    await waitFor(() => expect(uploadImport).toHaveBeenCalledTimes(1));
    // change 处理后 input 值被清空，再选同名文件仍会触发 change
    expect(input.value).toBe('');
  });

  it('上传失败显示错误而不是空白', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockRejectedValue(new Error('boom'));
    render(<Import />);
    fireEvent.change(screen.getByLabelText('Choose a spreadsheet file to import'), {
      target: { files: [fileOf('bad.csv')] },
    });
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('LIVE 映射失败时明确显示映射阶段失败，不误报上传失败', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockRejectedValue(new ApiError(503, 'LLM_GENERATION_FAILED', 'gateway failed'));
    render(<Import />);
    fireEvent.change(screen.getByLabelText('Choose a spreadsheet file to import'), {
      target: { files: [fileOf('materials.csv')] },
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Mapping proposal failed (LLM_GENERATION_FAILED)');
  });

  it('存在未解析单元格时展示行号并阻止未经确认的落库', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    vi.mocked(validateMapping).mockResolvedValue({
      ...VALIDATION,
      parsed_row_count: 1,
      type_error_count: 1,
      unparsed_cells: [{ row_number: 3, column_name: '可用量', raw_value: 'oops', reason: '数值解析失败' }],
    });
    render(<Import />);
    fireEvent.change(screen.getByLabelText('Choose a spreadsheet file to import'), {
      target: { files: [fileOf('dirty.csv')] },
    });
    expect(await screen.findByText(/Row 3, 可用量: oops/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Confirm mapping and import/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Review the unparsed cells/);
    expect(confirmImport).not.toHaveBeenCalled();
  });

  it('无批次时显示空态', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    render(<Import />);
    expect(await screen.findByText(/No import batches yet/)).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    const { container } = render(<Import />);
    await screen.findByRole('heading', { name: /Import batches/ });
    const results = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
