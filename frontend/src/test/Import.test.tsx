import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { CommitResult, ProposalResponse, UploadResult } from '../api/imports';

vi.mock('../api/imports', async () => {
  const actual = await vi.importActual<typeof import('../api/imports')>('../api/imports');
  return {
    ...actual,
    uploadImport: vi.fn(),
    getProposal: vi.fn(),
    confirmImport: vi.fn(),
    listImports: vi.fn(),
    revertImport: vi.fn(),
  };
});

import {
  confirmImport,
  getProposal,
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
        source_column: '名称',
        confidence: 0.5,
        sample_values: ['钢板'],
        status: 'NEEDS_CONFIRMATION',
      },
    ],
    missing_required_fields: [{ target_field: 'quantity_available', reason: '未找到源列' }],
    normalisations: [
      {
        source_column: 'unit',
        kind: 'UNIT_CONVERSION',
        detected_pattern: 'UNIT',
        conversion_factor: 12,
        sample_before: ['箱'],
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
});

describe('Import 视图', () => {
  it('上传后渲染列映射、缺失必填与归一化对照（R2）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    render(<Import />);

    fireEvent.change(screen.getByLabelText('选择要导入的表格文件'), {
      target: { files: [fileOf('materials.csv')] },
    });

    expect(await screen.findByRole('heading', { name: /列映射/ })).toBeInTheDocument();
    expect(screen.getAllByText('material_id').length).toBeGreaterThanOrEqual(1);
    // 待确认状态可见（R2.7）
    expect(screen.getAllByText('待确认').length).toBeGreaterThanOrEqual(1);
    // 缺失必填（R2）
    expect(screen.getByRole('heading', { name: /缺失的必填字段/ })).toBeInTheDocument();
    // 归一化换算系数（R2.5）
    expect(screen.getByText(/换算系数 12/)).toBeInTheDocument();
  });

  it('确认并导入触发 confirm 并显示批次（R2.9/R3.2）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockResolvedValue(UPLOAD);
    vi.mocked(getProposal).mockResolvedValue(PROPOSAL);
    const commit: CommitResult = { batch_id: 'BATCH-9', entity_type: 'MATERIAL', imported_row_count: 2 };
    vi.mocked(confirmImport).mockResolvedValue(commit);
    render(<Import />);
    fireEvent.change(screen.getByLabelText('选择要导入的表格文件'), {
      target: { files: [fileOf('m.csv')] },
    });
    await screen.findByRole('heading', { name: /列映射/ });

    fireEvent.click(screen.getByRole('button', { name: /确认映射并落库/ }));
    await waitFor(() => expect(confirmImport).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/已导入为批次 BATCH-9/)).toBeInTheDocument();
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
    fireEvent.change(screen.getByLabelText('选择要导入的表格文件'), {
      target: { files: [fileOf('dup.csv')] },
    });
    expect(await screen.findByText(/此前已导入（批次 BATCH-old/)).toBeInTheDocument();
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
    render(<Import />);
    await screen.findByRole('heading', { name: /导入批次/ });

    fireEvent.click(await screen.findByRole('button', { name: /回滚批次 BATCH-1/ }));
    await waitFor(() => expect(revertImport).toHaveBeenCalledWith('BATCH-1'));
  });

  it('上传失败显示错误而不是空白', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    vi.mocked(uploadImport).mockRejectedValue(new Error('boom'));
    render(<Import />);
    fireEvent.change(screen.getByLabelText('选择要导入的表格文件'), {
      target: { files: [fileOf('bad.csv')] },
    });
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无批次时显示空态', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    render(<Import />);
    expect(await screen.findByText(/暂无导入批次/)).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(listImports).mockResolvedValue({ batches: [] });
    const { container } = render(<Import />);
    await screen.findByRole('heading', { name: /导入批次/ });
    const results = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
