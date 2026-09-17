"""版本控制在案的表格样例及其标注（任务 1.6，R28.5 / R28.6）。

样例文件本体在 `app/seed/samples/`，那个目录的 `README.md` 说明了改动纪律。本模块只做
一件事：给它们一组**有名字的入口**，使 `EVAL-013` / `EVAL-202` 引用样例时不必各自拼路径。

## 为什么返回路径而不是解析后的内容

摄取路径（任务 10.x）的输入是**文件**：`read_uploaded_file_preview` 要处理编码、表头缺失、
公式列。如果本模块先把 CSV 解析成 `list[dict]` 再交出去，评估用例就跳过了被测代码里最
容易出错的那一段，转而验证本模块的解析器。因此样例一律以路径交付，只有标注（本来就是
JSON）才解析成 dict。

标注解析后是 `Mapping[str, Any]` 而不是一组 Pydantic 模型：标注 schema 的权威定义属于
任务 10.2 的 `ColumnMappingProposal`，在那之前先造一份平行模型，两份会漂移。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

#: 样例目录。`app/seed/samples/`，随包一起分发（`pyproject.toml` 的 package-data）。
SAMPLES_DIR: Final[Path] = Path(__file__).resolve().parent / "samples"

#: 「脏」表格样例（R28.5）：混合日期格式、多余列、缺失表头、前后空格、2 处歧义列。
DIRTY_ORDERS_CSV: Final[Path] = SAMPLES_DIR / "dirty_orders.csv"

#: 上一份样例的**列映射标注**，即 `EVAL-013` 的预期值。
DIRTY_ORDERS_MAPPING: Final[Path] = SAMPLES_DIR / "dirty_orders.mapping.json"

#: 「恶意」表格样例（R28.6）：单元格内含提示注入文本。
MALICIOUS_ORDERS_CSV: Final[Path] = SAMPLES_DIR / "malicious_orders.csv"

#: 上一份样例的预期检测结果与**导入后必须成立的不变量**，即 `EVAL-202` 的预期值。
MALICIOUS_ORDERS_EXPECTED: Final[Path] = SAMPLES_DIR / "malicious_orders.expected.json"

#: 全部样例文件（含标注）。`test_seed_samples.py` 逐个断言存在且非空。
SAMPLE_FILES: Final[tuple[Path, ...]] = (
    DIRTY_ORDERS_CSV,
    DIRTY_ORDERS_MAPPING,
    MALICIOUS_ORDERS_CSV,
    MALICIOUS_ORDERS_EXPECTED,
)


def _load_json(path: Path) -> Mapping[str, Any]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 的顶层必须是 JSON 对象")
    return payload


def dirty_orders_mapping() -> Mapping[str, Any]:
    """`dirty_orders.csv` 的列映射标注。"""
    return _load_json(DIRTY_ORDERS_MAPPING)


def malicious_orders_expectations() -> Mapping[str, Any]:
    """`malicious_orders.csv` 的预期检测结果与导入后不变量。"""
    return _load_json(MALICIOUS_ORDERS_EXPECTED)
