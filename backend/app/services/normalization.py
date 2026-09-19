"""日期与单位归一化（任务 10.3，R2.4 / R2.5，design.md Components §2.4）。

确定性、纯函数、无 LLM、无 I/O。摄取路径的两处归一：

1. **日期** —— 把 `DD/MM/YYYY`、`MM-DD-YY`、`YYYY年M月D日`、Excel 序列号归一到 ISO 8601。
   `MM-DD-YY` 的两位年份按固定规则解释世纪（见 `_expand_two_digit_year`），使同一输入必得同一
   世纪（R5.7 同一纪律）。
2. **单位** —— 把 `pcs` / `units` / `件` / `箱` 归一到基准单位「件（pcs）」，并**显式给出**
   `conversion_factor`（R2.5）：`箱` 一箱 = 12 件，其余为 1。

两个函数都返回 `(归一值, 说明)` 或抛 `NormalisationError`——绝不静默猜测（K-07）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = [
    "UNIT_FACTORS",
    "DateNormalisation",
    "NormalisationError",
    "UnitNormalisation",
    "normalise_date",
    "normalise_unit",
]


class NormalisationError(ValueError):
    """一个值无法按任何支持的形式归一（R2）。摄取路径把它计入 `normalisation_failure_count`。"""


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------

#: 两位年份的世纪切分：`<= PIVOT` → 20xx，否则 19xx（固定规则，确定性）。
_TWO_DIGIT_YEAR_PIVOT = 69

#: Excel 1900 日期系统的纪元：序列号 1 = 1900-01-01，故 `_EXCEL_EPOCH + serial` 用 12-31。
_EXCEL_EPOCH = date(1899, 12, 31)

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SLASH_DMY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DASH_MDY2_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{2})$")
_CJK_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")


@dataclass(frozen=True)
class DateNormalisation:
    """一次日期归一的结果：ISO 值 + 识别到的形式 + 原始样例。"""

    iso: str
    detected_pattern: str
    sample_before: str


def _expand_two_digit_year(yy: int) -> int:
    """两位年份 → 四位。`<= 69` → 20xx，否则 19xx（固定切分，见 `_TWO_DIGIT_YEAR_PIVOT`）。"""
    return 2000 + yy if yy <= _TWO_DIGIT_YEAR_PIVOT else 1900 + yy


def normalise_date(raw: str) -> DateNormalisation:
    """把一个日期字符串归一到 ISO 8601（`YYYY-MM-DD`）。四种形式 + Excel 序列号。

    识别顺序固定，形式互不歧义（分隔符与结构不同）：ISO → `DD/MM/YYYY` → `MM-DD-YY` →
    `YYYY年M月D日` → 纯数字（Excel 序列号）。无法识别抛 `NormalisationError`。
    """
    text = raw.strip()
    if not text:
        raise NormalisationError("空日期值无法归一。")

    if m := _ISO_RE.match(text):
        y, mo, d = (int(g) for g in m.groups())
        return DateNormalisation(_iso(y, mo, d), "ISO_8601", raw)

    if m := _SLASH_DMY_RE.match(text):
        d, mo, y = (int(g) for g in m.groups())
        return DateNormalisation(_iso(y, mo, d), "DD/MM/YYYY", raw)

    if m := _DASH_MDY2_RE.match(text):
        mo, d, yy = (int(g) for g in m.groups())
        return DateNormalisation(_iso(_expand_two_digit_year(yy), mo, d), "MM-DD-YY", raw)

    if m := _CJK_RE.match(text):
        y, mo, d = (int(g) for g in m.groups())
        return DateNormalisation(_iso(y, mo, d), "YYYY年M月D日", raw)

    if text.isdigit():
        serial = int(text)
        # Excel 序列号：1..2958465（1900-01-01 .. 9999-12-31）。范围外视为不可归一。
        if 1 <= serial <= 2_958_465:
            resolved = _EXCEL_EPOCH + timedelta(days=serial)
            return DateNormalisation(resolved.isoformat(), "EXCEL_SERIAL", raw)

    raise NormalisationError(f"无法识别的日期形式：{raw!r}")


def _iso(year: int, month: int, day: int) -> str:
    """校验并格式化为 ISO；非法日期（如 13 月、2 月 30 日）抛 `NormalisationError`。"""
    try:
        return date(year, month, day).isoformat()
    except ValueError as error:
        raise NormalisationError(f"非法日期 {year}-{month}-{day}：{error}") from error


# --------------------------------------------------------------------------
# 单位
# --------------------------------------------------------------------------

#: 支持的单位 → 到基准单位「件」的换算系数（R2.5）。基准单位系数为 1。
UNIT_FACTORS: dict[str, float] = {
    "pcs": 1.0,
    "units": 1.0,
    "件": 1.0,
    "箱": 12.0,  # 一箱 = 12 件
}


@dataclass(frozen=True)
class UnitNormalisation:
    """一次单位归一的结果：基准单位 + 显式换算系数（R2.5）。"""

    base_unit: str
    conversion_factor: float
    detected_unit: str


def normalise_unit(raw: str) -> UnitNormalisation:
    """把一个单位字符串归一到基准单位「件（pcs）」，显式给出 `conversion_factor`（R2.5）。

    不认识的单位抛 `NormalisationError`——绝不静默当作 1。基准单位统一记为 `pcs`。
    """
    text = raw.strip()
    factor = UNIT_FACTORS.get(text)
    if factor is None:
        raise NormalisationError(f"不支持的单位：{raw!r}")
    return UnitNormalisation(base_unit="pcs", conversion_factor=factor, detected_unit=text)
