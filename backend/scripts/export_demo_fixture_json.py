"""Emit the test/demo fixture as JSON for the user-editable workbook builder.

This is an authoring aid only. The running application never calls this file.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect

from app.seed.loader import build_rows


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor-date", type=date.fromisoformat, required=True)
    arguments = parser.parse_args()
    anchor = datetime.combine(arguments.anchor_date, datetime.min.time()).replace(hour=8)
    rows: dict[str, list[dict[str, object]]] = {}
    for entity in build_rows(anchor):
        name = type(entity).__name__
        columns = inspect(type(entity)).column_attrs
        rows.setdefault(name, []).append(
            {column.key: _json_value(getattr(entity, column.key)) for column in columns}
        )
    print(json.dumps({"anchor_date": arguments.anchor_date.isoformat(), "rows": rows}))


if __name__ == "__main__":
    main()
