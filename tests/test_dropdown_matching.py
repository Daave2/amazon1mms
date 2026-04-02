from services.metrics_service import _build_search_terms, _selection_matches_target


def test_build_search_terms_includes_normalized_and_original_variants():
    terms = _build_search_terms("weston-super-mare", "Morrisons Weston Super Mare")

    assert "weston-super-mare" in terms
    assert "weston super mare" in terms
    assert "Morrisons Weston Super Mare" in terms


def test_selection_matches_target_tolerates_formatting_variants():
    assert _selection_matches_target("Morrisons Weston Super Mare", "weston-super-mare", "Morrisons Weston Super Mare")
    assert _selection_matches_target("Wellington Gardens", "wellington gardens", "Wellington Gardens")
    assert not _selection_matches_target("Morrisons Aberdeen", "basingstoke", "Morrisons Basingstoke")
