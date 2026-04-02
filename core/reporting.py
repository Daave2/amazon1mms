from collections import Counter
from typing import Iterable, Mapping

FAILURE_CATEGORY_LABELS = {
    "api_fast_path": "API Fast Path",
    "ui_fallback": "UI Fallback",
    "submission": "Submission",
    "cleanup": "Cleanup",
    "worker": "Worker",
    "general": "General",
}


def summarize_failure_events(
    failure_events: Iterable[Mapping[str, object]],
    recent_limit: int = 5,
) -> dict[str, object]:
    category_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    recent_failures: list[str] = []

    for event in failure_events:
        message = str(event.get("message", "Unknown issue"))
        category = str(event.get("category", "general"))
        category_counts[category] += 1
        reason_counts[_extract_failure_reason(message)] += 1
        recent_failures.append(message)

    return {
        "category_counts": dict(category_counts),
        "reason_counts": dict(reason_counts),
        "recent_failures": recent_failures[:recent_limit],
        "overflow_count": max(len(recent_failures) - recent_limit, 0),
    }


def _extract_failure_reason(message: str) -> str:
    if "(" in message and ")" in message:
        return message[message.rfind("(") + 1 : message.rfind(")")]
    return message
