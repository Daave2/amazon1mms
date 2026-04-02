from dataclasses import dataclass
from typing import Mapping


@dataclass(slots=True)
class WorkItem:
    store_name: str
    dropdown_name: str
    merchant_id: str
    marketplace_id: str
    force_ui: bool = False

    @classmethod
    def from_store_info(
        cls,
        store_info: Mapping[str, str],
        merchant_id: str | None = None,
        force_ui: bool = False,
    ) -> "WorkItem":
        store_name = store_info["store_name"]
        return cls(
            store_name=store_name,
            dropdown_name=store_info.get("dropdown_name", store_name),
            merchant_id=(merchant_id if merchant_id is not None else store_info.get("merchant_id", "")).strip(),
            marketplace_id=store_info.get("marketplace_id", "").strip(),
            force_ui=force_ui,
        )
