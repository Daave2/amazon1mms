import csv

from core.store_loader import load_stores_from_csv, parse_store_row


def test_load_stores_from_csv_handles_legacy_rows_and_skips_blank_store_names(tmp_path):
    csv_path = tmp_path / "urls.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["merchant_id", "new_id", "store_name", "marketplace_id"])
        writer.writerow(["A1", "", "Belle Vale Morrisons", "", ""])
        writer.writerow(["", "", "", "", ""])
        writer.writerow(["", "", "Carterton Morrisons", "", ""])

    skipped_rows: list[int] = []
    stores = load_stores_from_csv(str(csv_path), on_skip=lambda row_number, _row: skipped_rows.append(row_number))

    assert skipped_rows == [3]
    assert stores == [
        {
            "store_name": "Belle Vale Morrisons",
            "dropdown_name": "Belle Vale",
            "merchant_id": "A1",
            "marketplace_id": "",
        },
        {
            "store_name": "Carterton Morrisons",
            "dropdown_name": "Carterton",
            "merchant_id": "",
            "marketplace_id": "",
        },
    ]


def test_parse_store_row_requires_store_name():
    assert parse_store_row(["A1", "", "", "", ""]) is None
