from services.metrics_service import (
    _build_search_terms,
    _parse_available_store_options,
    _selection_matches_target,
    resolve_dropdown_name,
)


def test_build_search_terms_includes_normalized_and_original_variants():
    terms = _build_search_terms("weston-super-mare", "Morrisons Weston Super Mare")

    assert "weston-super-mare" in terms
    assert "weston super mare" in terms
    assert "Morrisons Weston Super Mare" in terms


def test_selection_matches_target_tolerates_formatting_variants():
    assert _selection_matches_target("Morrisons Weston Super Mare", "weston-super-mare", "Morrisons Weston Super Mare")
    assert _selection_matches_target("Wellington Gardens", "wellington gardens", "Wellington Gardens")
    assert _selection_matches_target("Oxford", "carterton", "Carterton Morrisons")
    assert _selection_matches_target("Bradford", "Thornbury", "Thornbury Morrisons")
    assert not _selection_matches_target("Morrisons Aberdeen", "basingstoke", "Morrisons Basingstoke")
    assert not _selection_matches_target("Wellington", "Welling", "Morrisons Welling")


def test_resolve_dropdown_name_applies_special_store_mappings():
    assert resolve_dropdown_name("Morrisons Cardiff Tygals") == "cardiff tyglass"
    assert resolve_dropdown_name("Morrisons Auckland") == "bishop auckland"
    assert resolve_dropdown_name("Oxford") == "carterton"
    assert resolve_dropdown_name("Bradford") == "thornbury"
    assert resolve_dropdown_name("Morrisons Weston Super Mare") == "weston-super-mare"


def test_parse_available_store_options_extracts_merchant_ids_and_deduplicates():
    parsed = _parse_available_store_options(
        [
            ("store-selector-option-A1KDGRVT6JAV6B", "Belle Vale"),
            ("store-selector-option-A1KDGRVT6JAV6B", "Belle Vale"),
            ("store-selector-option-A3W2L835GZRAX2", "Cardiff Tyglass"),
            ("store-selector-option-A2UNKNOWN", "Oxford"),
            ("store-selector-option-A2BRADFORD", "Bradford"),
            (None, ""),
        ]
    )

    assert parsed == [
        {
            "store_name": "Belle Vale",
            "normalized_name": "belle vale",
            "merchant_id": "A1KDGRVT6JAV6B",
        },
        {
            "store_name": "Cardiff Tyglass",
            "normalized_name": "cardiff tyglass",
            "merchant_id": "A3W2L835GZRAX2",
        },
        {
            "store_name": "Oxford",
            "normalized_name": "carterton",
            "merchant_id": "A2UNKNOWN",
        },
        {
            "store_name": "Bradford",
            "normalized_name": "thornbury",
            "merchant_id": "A2BRADFORD",
        },
    ]
