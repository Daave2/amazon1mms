from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.debug.late_picks_probe import _attribute_render_events, _summarize_payload


def test_attribute_render_events_matches_latest_preceding_response():
    responses = [
        {"id": 1, "finishedAtMs": 1000, "url": "https://example.test/a"},
        {"id": 2, "finishedAtMs": 1800, "url": "https://example.test/b"},
    ]
    render_events = [
        {"timestampMs": 900, "valueText": "3.1%", "signature": "3.1%|old"},
        {"timestampMs": 2100, "valueText": "3.3%", "signature": "3.3%|new"},
    ]

    annotated = _attribute_render_events(render_events, responses, max_gap_ms=1000)

    assert annotated[0]["signatureChanged"] is True
    assert "matchedResponseId" not in annotated[0]
    assert annotated[1]["signatureChanged"] is True
    assert annotated[1]["matchedResponseId"] == 2
    assert annotated[1]["matchedResponseGapMs"] == 300


def test_summarize_payload_surfaces_late_related_entries():
    payload = [
        {
            "merchantId": "MID123",
            "type": "MASTER",
            "metrics": {"LatePicksRate": 4.2},
        }
    ]

    summary = _summarize_payload(payload)

    assert summary["kind"] == "list"
    assert summary["length"] == 1
    assert summary["merchant_ids"] == ["MID123"]
    assert summary["type_counts"] == {"MASTER": 1}
    assert any(entry["path"].endswith("LatePicksRate") for entry in summary["late_related"])
