import csv
import re
from typing import Callable

STORE_PREFIX_RE = re.compile(r"^morrisons?\s*-?\s*|\s*-?\s*morrisons?$", re.I)


def build_dropdown_name(store_name: str) -> str:
    dropdown_name = STORE_PREFIX_RE.sub("", store_name).strip()
    return re.sub(r"(?i)\s*Morrisons?$", "", dropdown_name).strip()


def parse_store_row(row: list[str]) -> dict[str, str] | None:
    if not row:
        return None

    store_name = row[2].strip() if len(row) > 2 else ""
    if not store_name:
        return None

    return {
        "store_name": store_name,
        "dropdown_name": build_dropdown_name(store_name),
        "merchant_id": row[0].strip() if len(row) > 0 else "",
        "marketplace_id": row[3].strip() if len(row) > 3 else "",
    }


def load_stores_from_csv(
    csv_path: str = "urls.csv",
    on_skip: Callable[[int, list[str]], None] | None = None,
) -> list[dict[str, str]]:
    urls_data: list[dict[str, str]] = []

    with open(csv_path, newline="", encoding="utf-8") as file_handle:
        reader = csv.reader(file_handle)
        next(reader, None)

        for row_number, row in enumerate(reader, start=2):
            parsed_row = parse_store_row(row)
            if parsed_row is None:
                if on_skip:
                    on_skip(row_number, row)
                continue
            urls_data.append(parsed_row)

    return urls_data
