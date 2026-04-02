import csv

from core.state import CacheManager


def test_update_csv_with_cache_only_backfills_blank_merchant_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with open("urls.csv", "w", newline="", encoding="utf-8") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["merchant_id", "new_id", "store_name", "marketplace_id"])
        writer.writerow(["A1", "", "Belle Vale Morrisons", "", ""])
        writer.writerow(["", "", "Carterton Morrisons", "", ""])

    cache = CacheManager()
    cache.merchant_id_cache["Belle Vale Morrisons"] = "SHOULD_NOT_REPLACE"
    cache.merchant_id_cache["Carterton Morrisons"] = "DISCOVERED"

    cache.update_csv_with_cache()

    with open("urls.csv", newline="", encoding="utf-8") as file_handle:
        rows = list(csv.reader(file_handle))

    assert rows[1][0] == "A1"
    assert rows[2][0] == "DISCOVERED"
