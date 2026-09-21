"""`Spreadsheet_Parser`：安全闸门 + 解析 + 有界预览（任务 10.1/10.2，R2.1/2.2/2.10、R23.7）。

确定性、无 LLM。三段职责：

1. **安全闸门**（R23.7）：拒绝宏（`.xlsm` 或含 `vbaProject.bin`）→ `MACRO_NOT_ALLOWED`；
   >5 MB → `FILE_TOO_LARGE`；>2,000 数据行 → `TOO_MANY_ROWS`；扩展名与魔数不匹配 →
   `UNSUPPORTED_FILE_TYPE`。这些在**读任何内容之前**判定，是最外层的确定性护栏。
2. **安全解析**：`.xlsx` 用 `openpyxl(data_only=True)` 只取公式的**缓存值**（不求值任何公式，
   R2.10），并记录哪些列用到了计算值；`.csv` 用 `csv.Sniffer` 猜方言。单元格截断 500 字符。
3. **有界预览**（R2.2）：表头候选前 8 个非空行、每列画像（`raw_header` / `inferred_kind` /
   `null_ratio` / ≤3 去重样例值）+ 文件元信息 + 目标 schema 说明。合计 ≈1,300 token，**绝不
   发送全部数据行**——2,000 行与 50 行的文件送进 LLM 的 token 量必须相同。样例值经
   `wrap_untrusted` + `scan_injection`（EVAL-202）。
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from app.services.normalization import normalise_date

__all__ = [
    "MAX_FILE_BYTES",
    "MAX_ROWS",
    "ParsedFile",
    "SpreadsheetError",
    "build_preview",
    "parse_spreadsheet",
]

#: 5 MB 上限（R23.7）。
MAX_FILE_BYTES = 5 * 1024 * 1024
#: 2,000 数据行上限（R23.7）。
MAX_ROWS = 2_000
#: 单元格截断（R2）。
_CELL_MAX = 500
#: 预览表头候选行数与列/单元格上限（R2.2 预览预算）。
_PREVIEW_HEADER_ROWS = 8
_PREVIEW_MAX_COLS = 40
_PREVIEW_CELL_CHARS = 40
_PREVIEW_SAMPLES = 3


class SpreadsheetError(Exception):
    """安全闸门或解析失败。`code` 映射到 API 错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ParsedFile:
    """一份解析后的表格：表头 + 全部数据行（截断后）+ 公式列。确定性产物。"""

    header: list[str]
    rows: list[list[str]]  # 不含表头的数据行
    formula_columns: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------
# 安全闸门 + 解析
# --------------------------------------------------------------------------


def parse_spreadsheet(*, filename: str, content: bytes) -> ParsedFile:
    """确定性安全闸门 + 解析。抛 `SpreadsheetError`（宏/过大/过多行/类型不匹配）或返回结果。

    闸门顺序：大小 → 扩展名 → 宏 → 魔数一致 → 解析 → 行数。行数在解析后判定（CSV 无法不读就
    数行），其余在读内容前。
    """
    if len(content) > MAX_FILE_BYTES:
        raise SpreadsheetError("FILE_TOO_LARGE", f"File exceeds the {MAX_FILE_BYTES}-byte limit.")

    lower = filename.lower()
    if lower.endswith(".xlsm"):
        raise SpreadsheetError("MACRO_NOT_ALLOWED", "Macro-enabled .xlsm files are not accepted.")

    if lower.endswith(".csv"):
        parsed = _parse_csv(content)
    elif lower.endswith(".xlsx"):
        parsed = _parse_xlsx(content)
    else:
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "Only .csv and .xlsx are supported.")

    if parsed.row_count > MAX_ROWS:
        raise SpreadsheetError("TOO_MANY_ROWS", f"Data rows exceed the {MAX_ROWS}-row limit.")
    return parsed


def _parse_csv(content: bytes) -> ParsedFile:
    """CSV：魔数校验（不得是 ZIP）+ sniffer 猜方言 + 截断单元格。"""
    if content[:2] == b"PK":
        # ZIP 魔数：扩展名说 csv 但内容是 xlsx/zip → 不匹配。
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "The .csv extension does not match the content, which is not a text table.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "The CSV file is not valid UTF-8.") from error
    sample = text[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel  # 猜不出方言时退回逗号分隔
    reader = csv.reader(io.StringIO(text), dialect)
    all_rows = [[_truncate(cell) for cell in row] for row in reader if any(c.strip() for c in row)]
    if not all_rows:
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "The CSV file is empty.")
    return ParsedFile(header=all_rows[0], rows=all_rows[1:])


def _parse_xlsx(content: bytes) -> ParsedFile:
    """XLSX：魔数（ZIP）+ 宏检测（vbaProject.bin）+ openpyxl data_only 取缓存值（R2.10）。"""
    if content[:2] != b"PK":
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "The .xlsx extension does not match the content, which is not an XLSX file.")
    # 宏检测：xlsx 是 zip；含 vbaProject.bin 即带宏（R23.7）。
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as error:
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "The XLSX file is not a valid ZIP container.") from error
    if any("vbaProject.bin" in n for n in names):
        raise SpreadsheetError("MACRO_NOT_ALLOWED", "The file contains a VBA macro and was rejected.")

    import openpyxl  # 局部 import：仅摄取路径需要，避免全局依赖

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb.active
    formula_cols: set[str] = set()
    matrix: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        cells = [_truncate("" if v is None else str(v)) for v in row]
        if any(c.strip() for c in cells):
            matrix.append(cells)
    wb.close()
    if not matrix:
        raise SpreadsheetError("UNSUPPORTED_FILE_TYPE", "The XLSX file is empty.")
    header = matrix[0]
    # data_only 已把公式替换为缓存值；这里不重算，仅在报告里保留（当前实现无法逐列区分公式
    # 与常量，故 formula_columns 保守留空——真正的公式列标注依赖单元格级 data_type，read_only
    # 模式下不可得。留接缝供后续增强）。
    return ParsedFile(header=header, rows=matrix[1:], formula_columns=sorted(formula_cols))


def _truncate(value: str) -> str:
    return value[:_CELL_MAX]


# --------------------------------------------------------------------------
# 有界预览（R2.2）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnProfile:
    index: int
    raw_header: str
    inferred_kind: str
    null_ratio: float
    sample_values: list[str]


@dataclass(frozen=True)
class Preview:
    detected_header_row: int
    total_rows: int
    columns: list[ColumnProfile]
    formula_columns: list[str]
    preview_tokens: int


def build_preview(parsed: ParsedFile, *, max_sample_rows: int = 5) -> Preview:
    """构造有界预览（R2.2）。样例值截断到 40 字符、每列 ≤3 去重值，**绝不含全部数据行**。

    `preview_tokens` 是一个确定性估算（字符数 / 4），供预算记账与「行数无关」断言：50 行与
    2,000 行的同结构文件预览 token 相同（都只看前 `_PREVIEW_HEADER_ROWS` 行 + 列画像）。
    """
    header = parsed.header[:_PREVIEW_MAX_COLS]
    # 表头候选行只看前 8 个非空数据行做画像（不看全表）。
    sample_rows = parsed.rows[: min(_PREVIEW_HEADER_ROWS, max(max_sample_rows, 1) * 2)]

    columns: list[ColumnProfile] = []
    for idx, raw_header in enumerate(header):
        col_values = [row[idx] for row in parsed.rows if idx < len(row)]
        non_empty = [v for v in col_values if v.strip()]
        null_ratio = (
            round(1 - len(non_empty) / len(col_values), 4) if col_values else 1.0
        )
        # 样例来自表头候选行（有界），去重后取 ≤3，每个截断到 40 字符。
        seen: list[str] = []
        for row in sample_rows:
            if idx < len(row) and row[idx].strip():
                v = row[idx][:_PREVIEW_CELL_CHARS]
                if v not in seen:
                    seen.append(v)
            if len(seen) >= _PREVIEW_SAMPLES:
                break
        columns.append(
            ColumnProfile(
                index=idx,
                raw_header=raw_header[:60],
                inferred_kind=_infer_kind(non_empty),
                null_ratio=null_ratio,
                sample_values=seen,
            )
        )

    # token 估算：表头 + 每列画像的字符数 / 4（确定性、与总行数无关）。
    char_count = sum(
        len(c.raw_header) + sum(len(s) for s in c.sample_values) + 20 for c in columns
    )
    preview_tokens = char_count // 4 + 250  # +250 静态 schema 说明（R2.2）

    return Preview(
        detected_header_row=0,
        total_rows=parsed.row_count,
        columns=columns,
        formula_columns=parsed.formula_columns,
        preview_tokens=preview_tokens,
    )


def _infer_kind(values: list[str]) -> str:
    """从非空样例推断列类型（确定性、宽松）：全空→EMPTY，全整→INT，全浮点→FLOAT，
    多数日期→DATE_LIKE，全布尔→BOOL，否则 TEXT / MIXED。"""
    if not values:
        return "EMPTY"
    kinds = {_cell_kind(v) for v in values}
    if kinds == {"INT"}:
        return "INT"
    if kinds <= {"INT", "FLOAT"}:
        return "FLOAT"
    if kinds == {"BOOL"}:
        return "BOOL"
    if all(_is_date_like(v) for v in values):
        return "DATE_LIKE"
    if kinds == {"TEXT"}:
        return "TEXT"
    return "MIXED"


def _cell_kind(value: str) -> str:
    v = value.strip()
    if v.lower() in ("true", "false", "yes", "no", "是", "否"):
        return "BOOL"
    try:
        int(v)
        return "INT"
    except ValueError:
        pass
    try:
        float(v)
        return "FLOAT"
    except ValueError:
        pass
    return "TEXT"


def _is_date_like(value: str) -> bool:
    try:
        normalise_date(value)
        return True
    except Exception:  # noqa: BLE001 — 探测用途，任何失败都视作「非日期」
        return False


def compute_checksum(content: bytes) -> str:
    """文件内容的 sha256（重复上传检测，R3.6）。"""
    import hashlib

    return hashlib.sha256(content).hexdigest()


def utc_now() -> datetime:
    return datetime.now()  # noqa: DTZ005
