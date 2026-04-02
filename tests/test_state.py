import csv

import core.state as state_module
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


async def _save_cache(cache: CacheManager):
    await cache.save()


def test_cache_manager_persists_live_dropdown_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "DISCOVERY_CACHE_FILE", str(tmp_path / "discovery_cache.json"))

    cache = CacheManager()
    cache.merchant_id_cache["Belle Vale Morrisons"] = "MID-1"
    cache.live_dropdown_store_names = ["Belle Vale", "Welling"]

    import asyncio

    asyncio.run(_save_cache(cache))

    loaded_cache = CacheManager()
    loaded_cache.load()

    assert loaded_cache.merchant_id_cache == {"Belle Vale Morrisons": "MID-1"}
    assert loaded_cache.live_dropdown_store_names == ["Belle Vale", "Welling"]
