"""日期与单位归一化的表驱动单元测试（任务 10.3，R2.4 / R2.5，承接原属性 34）。**非可选**。

表驱动在这里比随机生成更可控：4 种日期形式 × 边界年份 + Excel 序列号 + 各支持单位 ×
`conversion_factor`。全部纯内核，不触 LLM、不触库。
"""

from __future__ import annotations

import pytest

from app.services.normalization import (
    UNIT_FACTORS,
    NormalisationError,
    normalise_date,
    normalise_unit,
)

# --------------------------------------------------------------------------
# 日期：4 种形式 + 边界年份 + Excel 序列号
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_iso", "expected_pattern"),
    [
        # ISO 直通
        ("2026-03-02", "2026-03-02", "ISO_8601"),
        # DD/MM/YYYY（欧洲式，日在前）
        ("02/03/2026", "2026-03-02", "DD/MM/YYYY"),
        ("31/12/2025", "2025-12-31", "DD/MM/YYYY"),
        # MM-DD-YY（两位年份，世纪按固定 pivot=69 切分）
        ("03-02-26", "2026-03-02", "MM-DD-YY"),  # 26 <= 69 → 2026
        ("03-02-69", "2069-03-02", "MM-DD-YY"),  # 边界：69 → 2069
        ("03-02-70", "1970-03-02", "MM-DD-YY"),  # 边界：70 → 1970
        ("12-31-99", "1999-12-31", "MM-DD-YY"),
        # YYYY年M月D日
        ("2026年3月2日", "2026-03-02", "YYYY年M月D日"),
        ("2025年12月31日", "2025-12-31", "YYYY年M月D日"),
        # Excel 序列号（1 = 1900-01-01，含 1900 闰年 bug 的纪元 1899-12-30）
        ("1", "1900-01-01", "EXCEL_SERIAL"),
        ("46082", "2026-03-02", "EXCEL_SERIAL"),
    ],
)
def test_normalise_date_table(raw: str, expected_iso: str, expected_pattern: str) -> None:
    result = normalise_date(raw)
    assert result.iso == expected_iso
    assert result.detected_pattern == expected_pattern
    assert result.sample_before == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a date",
        "32/01/2026",  # 32 日非法（DD/MM）
        "02-30-26",  # 2 月 30 日非法
        "2026年13月1日",  # 13 月非法
        "0",  # Excel 序列号下界外
    ],
)
def test_normalise_date_rejects_invalid(raw: str) -> None:
    with pytest.raises(NormalisationError):
        normalise_date(raw)


# --------------------------------------------------------------------------
# 单位：各支持单位 × conversion_factor（R2.5 显式给出）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_factor"),
    [
        ("pcs", 1.0),
        ("units", 1.0),
        ("件", 1.0),
        ("箱", 12.0),  # 一箱 = 12 件
    ],
)
def test_normalise_unit_table(raw: str, expected_factor: float) -> None:
    result = normalise_unit(raw)
    assert result.conversion_factor == expected_factor
    assert result.base_unit == "pcs"
    assert result.detected_unit == raw


def test_all_supported_units_covered() -> None:
    """UNIT_FACTORS 的每个单位都能归一（防止表与实现漂移）。"""
    for unit in UNIT_FACTORS:
        assert normalise_unit(unit).conversion_factor == UNIT_FACTORS[unit]


def test_normalise_unit_rejects_unknown() -> None:
    with pytest.raises(NormalisationError):
        normalise_unit("dozen")  # 不静默当 1（K-07）
