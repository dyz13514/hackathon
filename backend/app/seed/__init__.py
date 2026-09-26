"""演示数据 seed、表格样例与一键重置（任务 1.6，R28）。

三个模块，职责不重叠：

- `dataset.py`——**纯数据**。演示数据集长什么样，不 import ORM、不读时钟。
- `loader.py`——把纯数据写进库，以及 `reset_demo_data()`（`POST /api/demo/reset` 的实现）。
- `fixtures.py`——版本控制在案的脏 / 恶意表格样例及其标注（`EVAL-013` / `EVAL-202`）。

仅在需要演示数据时以模块方式显式运行：`python -m app.seed --demo`。

本包对外的面就是下面 `__all__` 里那几个名字。`app/api/admin.py` 只用
`reset_demo_data` 与 `DemoResetReport`；评估用例只用 `fixtures` 里的常量。
"""

from app.seed.dataset import DEMO_ANCHOR, HORIZON_DAYS, SEED_SOURCE
from app.seed.loader import (
    DemoResetReport,
    build_rows,
    clear_business_tables,
    demo_data_present,
    load_demo_data,
    reset_demo_data,
)

__all__ = [
    "DEMO_ANCHOR",
    "HORIZON_DAYS",
    "SEED_SOURCE",
    "DemoResetReport",
    "build_rows",
    "clear_business_tables",
    "demo_data_present",
    "load_demo_data",
    "reset_demo_data",
]
