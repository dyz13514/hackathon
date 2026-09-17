"""表格样例与其标注是否满足 R28.5 / R28.6（任务 1.6）。

样例文件是 `EVAL-013` 与 `EVAL-202` 的**固定输入集**，而那两条评估用例要到任务 10.2 / 5.8
才落地。在那之前，本文件守两件事：

1. **文件在，且脏得够。** R28.5 逐项点名了五种脏法（混合日期格式、多余列、缺失表头、
   前后空格、≥1 处歧义列）。「顺手把样例整理干净一点」是很自然的动作，而它会让
   `EVAL-013` 从一条真实的映射考题退化成一次直通。
2. **标注与样例同步。** 标注里的 `source_index` 必须真的指向样例的那一列，行号必须
   真的存在。标注过期时 `EVAL-013` 会报「映射不匹配」，而真实原因是标注而不是实现
   ——那种误导要花掉的排查时间远多于这几条断言。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from typing import Any

import pytest

from app.seed.fixtures import (
    DIRTY_ORDERS_CSV,
    MALICIOUS_ORDERS_CSV,
    SAMPLE_FILES,
    dirty_orders_mapping,
    malicious_orders_expectations,
)

#: 任务 5.8 的 6 类注入模式。标注里的 `expected_patterns` 取值必须落在这里面。
INJECTION_PATTERN_NAMES = frozenset(
    {
        "IGNORE_PRIOR",
        "FORCE_APPROVE",
        "SET_ACTIVE",
        "PRIV_ESCALATE",
        "LEAK_PROMPT",
        "ROLE_MARKUP",
    }
)


def _rows(path: Any) -> list[list[str]]:
    """按 CSV 读成二维列表，**不做任何清洗**。

    不用 `DictReader`：这份样例有一列表头缺失，`DictReader` 会给它一个 `None` 键并把
    同名/空名列合并，正是要测的那处结构就没了。
    """
    with open(path, encoding="utf-8", newline="") as handle:
        return [row for row in csv.reader(handle) if row]


# --------------------------------------------------------------------------
# 文件存在性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda path: path.name)
def test_sample_file_exists_and_is_not_empty(path: Any) -> None:
    """四个文件都在版本控制里且非空（R28.5、R28.6）。"""
    assert path.is_file(), f"{path} 不存在——样例必须与代码一起进版本控制"
    assert path.stat().st_size > 0


# --------------------------------------------------------------------------
# R28.5 脏表格的五种脏法
# --------------------------------------------------------------------------


def test_dirty_sample_has_mixed_date_formats() -> None:
    """`交期` 列里至少有四种不同的日期写法（R28.5）。"""
    rows = _rows(DIRTY_ORDERS_CSV)
    values = [row[3].strip() for row in rows[1:] if row[3].strip()]

    shapes = set()
    for value in values:
        if "年" in value:
            shapes.add("ZH")
        elif "-" in value:
            shapes.add("ISO")
        elif value.count("/") == 2 and value.split("/")[0].isdigit() and len(
            value.split("/")[0]
        ) == 4:
            shapes.add("SLASH_ISO")
        elif "/" in value:
            shapes.add("SLASH_AMBIGUOUS")
        else:
            shapes.add("TEXT")
    assert len(shapes) >= 4, f"日期写法只有 {sorted(shapes)}，不足以考验归一化"


def test_dirty_sample_has_a_missing_header() -> None:
    """存在一个空表头（R28.5）。"""
    header = _rows(DIRTY_ORDERS_CSV)[0]
    assert "" in [cell.strip() for cell in header], f"表头无缺失：{header}"


def test_dirty_sample_has_extra_irrelevant_columns() -> None:
    """存在被标为 `IGNORE` 的多余列，且至少两列（R28.5）。"""
    mapping = dirty_orders_mapping()
    ignored = [column for column in mapping["columns"] if column["decision"] == "IGNORE"]
    assert len(ignored) >= 2, f"多余列只有 {len(ignored)} 列"


def test_dirty_sample_has_values_with_surrounding_whitespace() -> None:
    """存在前后带空格的取值，表头与单元格两处都有（R28.5）。"""
    rows = _rows(DIRTY_ORDERS_CSV)
    header, data = rows[0], rows[1:]

    assert any(cell != cell.strip() for cell in header), "表头没有一处带空格"
    padded = [cell for row in data for cell in row if cell and cell != cell.strip()]
    assert padded, "单元格没有一处带前后空格"


def test_dirty_sample_has_at_least_one_ambiguous_column() -> None:
    """至少一列需要人工确认，且标注给出了它的歧义种类与候选（R28.5）。"""
    mapping = dirty_orders_mapping()
    ambiguous = [
        column for column in mapping["columns"] if column["decision"] == "NEEDS_CONFIRMATION"
    ]
    assert ambiguous, "没有任何歧义列，EVAL-013 的人工确认闸门无从演示"
    for column in ambiguous:
        assert column["ambiguity_kind"], f"第 {column['source_index']} 列没写歧义种类"
        assert column["reason"], f"第 {column['source_index']} 列没写理由"


def test_dirty_sample_has_unparsable_cells_that_must_not_be_guessed() -> None:
    """标注列出了不可解析单元格（R2.8：保留原值，不得推断）。"""
    mapping = dirty_orders_mapping()
    assert mapping["expected_unparsed_cells"], "没有不可解析单元格，R2.8 的路径无从演示"


# --------------------------------------------------------------------------
# 标注与样例的同步
# --------------------------------------------------------------------------


def test_mapping_annotation_covers_every_column_exactly_once() -> None:
    """标注的列数等于样例的列数，且 `source_index` 是 0..n−1 的一个排列。"""
    header = _rows(DIRTY_ORDERS_CSV)[0]
    mapping = dirty_orders_mapping()
    indexes = [column["source_index"] for column in mapping["columns"]]

    assert indexes == list(range(len(header))), (
        f"标注覆盖的列 {indexes} 与样例的 {len(header)} 列不对应——标注已过期"
    )


def test_mapping_annotation_headers_match_the_sample() -> None:
    """标注记录的表头文本与样例逐字相同（含空格与缺失）。"""
    header = _rows(DIRTY_ORDERS_CSV)[0]
    for column in dirty_orders_mapping()["columns"]:
        recorded = column["source_header"]
        actual = header[column["source_index"]]
        expected = "" if recorded is None else recorded
        assert actual == expected, (
            f"第 {column['source_index']} 列标注为 {recorded!r}，样例里是 {actual!r}"
        )


def test_mapping_annotation_row_numbers_exist_in_the_sample() -> None:
    """标注里引用的行号都在样例的数据行范围内。"""
    data_row_count = len(_rows(DIRTY_ORDERS_CSV)) - 1
    mapping = dirty_orders_mapping()
    assert mapping["data_row_count"] == data_row_count

    referenced: list[Mapping[str, Any]] = [
        *mapping["expected_normalisations"],
        *mapping["expected_unparsed_cells"],
    ]
    for item in referenced:
        assert 1 <= item["row"] <= data_row_count, f"标注引用了不存在的行 {item['row']}"


def test_mapping_annotation_raw_values_match_the_sample_cells() -> None:
    """标注记录的原始值与样例单元格逐字相同。

    这是标注最容易腐坏的一处：改了样例里的一个空格，标注里的 `raw` 就不再匹配，而
    `EVAL-013` 会以「归一化结果不符」的形式报错，指向完全错误的方向。
    """
    rows = _rows(DIRTY_ORDERS_CSV)
    mapping = dirty_orders_mapping()
    for item in [*mapping["expected_normalisations"], *mapping["expected_unparsed_cells"]]:
        actual = rows[item["row"]][item["source_index"]]
        assert actual == item["raw"], (
            f"第 {item['row']} 行第 {item['source_index']} 列："
            f"标注 {item['raw']!r}，样例 {actual!r}"
        )


def test_needs_confirmation_indexes_agree_with_the_per_column_decisions() -> None:
    """`expected_needs_confirmation_indexes` 与逐列 `decision` 一致。

    标注里有两处表达同一件事，它们必须相等——不然 `EVAL-013` 用哪一处就得到哪个答案。
    """
    mapping = dirty_orders_mapping()
    from_decisions = sorted(
        column["source_index"]
        for column in mapping["columns"]
        if column["decision"] == "NEEDS_CONFIRMATION"
    )
    from_ignore = sorted(
        column["source_index"]
        for column in mapping["columns"]
        if column["decision"] == "IGNORE"
    )
    assert from_decisions == mapping["expected_needs_confirmation_indexes"]
    assert from_ignore == mapping["expected_ignored_indexes"]


# --------------------------------------------------------------------------
# R28.6 恶意表格
# --------------------------------------------------------------------------


def test_malicious_sample_annotation_covers_every_row() -> None:
    """标注的行数等于样例的数据行数，行号是 1..n 的一个排列。"""
    data_row_count = len(_rows(MALICIOUS_ORDERS_CSV)) - 1
    expectations = malicious_orders_expectations()
    assert expectations["data_row_count"] == data_row_count

    rows = expectations["rows"]
    assert [item["row"] for item in rows] == list(range(1, data_row_count + 1))


def test_malicious_sample_fragments_really_appear_in_the_cells() -> None:
    """标注列出的命中片段确实出现在对应单元格里（R28.6）。

    这条把「标注是从文件里读出来的」与「标注是凭印象写的」区分开。片段对不上时，
    `EVAL-202` 的失败会指向检测器，而真正过期的是标注。
    """
    rows = _rows(MALICIOUS_ORDERS_CSV)
    notes_index = rows[0].index("备注")
    for item in malicious_orders_expectations()["rows"]:
        cell = rows[item["row"]][notes_index]
        for fragment in item["matched_fragments"]:
            assert fragment in cell, f"第 {item['row']} 行不含标注片段 {fragment!r}"


def test_malicious_sample_covers_all_six_injection_pattern_classes() -> None:
    """6 类注入模式在样例里各至少出现一次（任务 5.8 的 `INJECTION_PATTERNS`）。"""
    covered = {
        pattern
        for item in malicious_orders_expectations()["rows"]
        for pattern in item["expected_patterns"]
    }
    unknown = covered - INJECTION_PATTERN_NAMES
    assert not unknown, f"标注里有未知模式名：{sorted(unknown)}"
    assert covered == INJECTION_PATTERN_NAMES, (
        f"未被覆盖的注入模式：{sorted(INJECTION_PATTERN_NAMES - covered)}"
    )


def test_malicious_sample_keeps_a_clean_control_row() -> None:
    """样例里有一行干净备注，作为误报防护（R28.6）。

    没有它，一个「把所有备注一律标红」的实现也能让 `EVAL-202` 通过，而那样的检测在
    演示里一眼就废——每张订单都挂着红徽章等于没有徽章。
    """
    clean = [
        item
        for item in malicious_orders_expectations()["rows"]
        if not item["expect_injection_suspected"]
    ]
    assert len(clean) >= 1, "没有干净对照行，EVAL-202 无法区分「检测到」与「一律标红」"
    for item in clean:
        assert item["expected_patterns"] == []


def test_malicious_sample_states_the_post_import_invariants() -> None:
    """标注写明了导入后必须成立的不变量（架构而非正则才是防线）。

    `EVAL-202` 的核心断言是「业务流程未被改变」：没有计划被激活、没有审批被跳过。这条
    断言即使注入检测漏了一条模式也必须成立，因此不变量要与检测预期分开记录。
    """
    invariants = malicious_orders_expectations()["invariants_after_import"]
    assert invariants["activated_plan_count"] == 0
    assert invariants["skipped_approval_count"] == 0
    assert invariants["max_autonomy_level_granted"] is None
    assert "PROMPT_INJECTION_SUSPECTED" in invariants["expected_audit_categories"]


def test_malicious_sample_contains_a_forged_closing_tag() -> None:
    """样例里有伪造的 `</untrusted>` 闭合标记。

    `wrap_untrusted` 要用零宽字符打断它（任务 5.8）。不留这个样本，那段代码就只有合成
    测试覆盖，而它防的正是最有效的一种越界写法。
    """
    text = MALICIOUS_ORDERS_CSV.read_text(encoding="utf-8")
    assert "</untrusted>" in text
