"""
Probe the dashboard to identify which post-refresh JSON response drives the rendered
"Late Picks" value on the page.

Usage:
    python3 scripts/debug/late_picks_probe.py --store "Morrisons - Anniesland" --headful
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import load_settings  # noqa: E402
from core.utils import ensure_directory, optimize_browser_context, safe_close  # noqa: E402
from services.auth_service import check_if_login_needed  # noqa: E402
from services.metrics_service import select_store_from_dropdown  # noqa: E402

JSON_RESOURCE_TYPES = {"fetch", "xhr"}
DEFAULT_MAX_WAIT_SECONDS = 15.0
DEFAULT_QUIET_SECONDS = 2.5
DEFAULT_POLL_INTERVAL_MS = 100
DEFAULT_ATTRIBUTION_GAP_MS = 2500

LATE_PROBE_SCRIPT = r"""
(() => {
  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function ownText(element) {
    if (!element) {
      return "";
    }
    const parts = [];
    for (const node of element.childNodes || []) {
      if (node.nodeType === Node.TEXT_NODE) {
        const text = normalizeText(node.textContent || "");
        if (text) {
          parts.push(text);
        }
      }
    }
    return normalizeText(parts.join(" "));
  }

  function isVisible(element) {
    if (!element || !(element instanceof Element)) {
      return false;
    }
    const style = window.getComputedStyle(element);
    if (!style || style.visibility === "hidden" || style.display === "none") {
      return false;
    }
    if (Number(style.opacity || "1") === 0) {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function depth(element) {
    let count = 0;
    let cursor = element;
    while (cursor && cursor.parentElement) {
      count += 1;
      cursor = cursor.parentElement;
    }
    return count;
  }

  function cssPath(element) {
    if (!element || !(element instanceof Element)) {
      return "";
    }
    const parts = [];
    let cursor = element;
    while (cursor && cursor.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
      let selector = cursor.tagName.toLowerCase();
      if (cursor.id) {
        selector += "#" + cursor.id;
        parts.unshift(selector);
        break;
      }
      let sibling = cursor;
      let position = 1;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === cursor.tagName) {
          position += 1;
        }
      }
      selector += `:nth-of-type(${position})`;
      parts.unshift(selector);
      cursor = cursor.parentElement;
    }
    return parts.join(" > ");
  }

  function extractPercent(text) {
    const match = normalizeText(text).match(/(\d+(?:\.\d+)?)\s*%/);
    return match ? `${match[1]}%` : null;
  }

  function domDistance(first, second) {
    if (!first || !second) {
      return Number.MAX_SAFE_INTEGER;
    }
    const firstAncestors = [];
    let cursor = first;
    while (cursor) {
      firstAncestors.push(cursor);
      cursor = cursor.parentElement;
    }
    const secondAncestors = [];
    cursor = second;
    while (cursor) {
      secondAncestors.push(cursor);
      cursor = cursor.parentElement;
    }
    for (let firstIndex = 0; firstIndex < firstAncestors.length; firstIndex += 1) {
      const sharedIndex = secondAncestors.indexOf(firstAncestors[firstIndex]);
      if (sharedIndex >= 0) {
        return firstIndex + sharedIndex;
      }
    }
    return Number.MAX_SAFE_INTEGER;
  }

  function collectFallbackPercents() {
    const seen = new Set();
    const results = [];
    for (const element of document.body.querySelectorAll("*")) {
      if (!isVisible(element)) {
        continue;
      }
      const text = normalizeText(ownText(element) || element.textContent || "");
      if (!text || text.length > 40) {
        continue;
      }
      const valueText = extractPercent(text);
      if (!valueText || seen.has(`${valueText}|${text}`)) {
        continue;
      }
      seen.add(`${valueText}|${text}`);
      results.push({
        valueText,
        text,
        path: cssPath(element),
      });
      if (results.length >= 12) {
        break;
      }
    }
    return results;
  }

  function findLabelCandidates() {
    const candidates = [];
    for (const element of document.body.querySelectorAll("*")) {
      if (!isVisible(element)) {
        continue;
      }
      const own = normalizeText(ownText(element));
      const aria = normalizeText(element.getAttribute("aria-label") || element.getAttribute("title") || "");
      const full = normalizeText(element.textContent || "");
      let score = null;
      let text = "";
      if (/late\s*picks?/i.test(own) && own.length <= 80) {
        score = 0;
        text = own;
      } else if (/late\s*picks?/i.test(aria) && aria.length <= 80) {
        score = 1;
        text = aria;
      } else if (/late\s*picks?/i.test(full) && full.length <= 80) {
        score = 2;
        text = full;
      }
      if (score === null) {
        continue;
      }
      candidates.push({
        element,
        text,
        score,
        depth: depth(element),
      });
    }

    candidates.sort((left, right) => {
      return (
        left.score - right.score ||
        left.text.length - right.text.length ||
        right.depth - left.depth
      );
    });
    return candidates;
  }

  function findValueSnapshot() {
    const searchedAtMs = Date.now();
    const fallbackPercents = collectFallbackPercents();
    const labelCandidates = findLabelCandidates();
    if (!labelCandidates.length) {
      return {
        found: false,
        searchedAtMs,
        reason: "late_label_not_found",
        fallbackPercents,
      };
    }

    const label = labelCandidates[0];
    const labelText = normalizeText(label.text);
    const labelFullText = normalizeText(label.element.textContent || labelText);
    const inlineMatch = labelFullText.match(/late\s*picks?.*?(\d+(?:\.\d+)?\s*%)/i)
      || labelFullText.match(/(\d+(?:\.\d+)?\s*%).*late\s*picks?/i);
    if (inlineMatch) {
      return {
        found: true,
        searchedAtMs,
        labelText,
        valueText: normalizeText(inlineMatch[1]).replace(/\s+/g, ""),
        labelPath: cssPath(label.element),
        valuePath: cssPath(label.element),
        containerPath: cssPath(label.element.parentElement || label.element),
        containerText: normalizeText((label.element.parentElement || label.element).textContent || "").slice(0, 600),
        fallbackPercents,
      };
    }

    let container = label.element;
    for (let level = 0; container && level < 5; level += 1, container = container.parentElement) {
      const candidates = [];
      for (const element of [container, ...container.querySelectorAll("*")]) {
        if (!isVisible(element) || element === label.element) {
          continue;
        }
        const text = normalizeText(ownText(element) || element.textContent || "");
        if (!text || text.length > 48) {
          continue;
        }
        const valueText = extractPercent(text);
        if (!valueText) {
          continue;
        }
        candidates.push({
          element,
          text,
          valueText,
          distance: domDistance(label.element, element),
        });
      }

      candidates.sort((left, right) => {
        return left.distance - right.distance || left.text.length - right.text.length;
      });

      if (candidates.length) {
        const best = candidates[0];
        return {
          found: true,
          searchedAtMs,
          labelText,
          valueText: best.valueText,
          labelPath: cssPath(label.element),
          valuePath: cssPath(best.element),
          containerPath: cssPath(container),
          containerText: normalizeText(container.textContent || "").slice(0, 600),
          fallbackPercents,
        };
      }
    }

    return {
      found: false,
      searchedAtMs,
      reason: "late_value_not_found",
      labelText,
      labelPath: cssPath(label.element),
      containerText: labelFullText.slice(0, 600),
      fallbackPercents,
    };
  }

  function install(intervalMs) {
    if (window.__latePicksProbe && window.__latePicksProbe.intervalId) {
      return true;
    }

    const state = {
      events: [],
      lastSignature: null,
      intervalId: null,
    };

    function recordSnapshot() {
      const snapshot = findValueSnapshot();
      const signature = [
        snapshot.valueText || "",
        snapshot.valuePath || "",
        snapshot.containerPath || "",
      ].join("|");
      if (!state.events.length || signature !== state.lastSignature) {
        state.events.push({
          timestampMs: Date.now(),
          valueText: snapshot.valueText || null,
          signature,
          snapshot,
        });
        state.lastSignature = signature;
      }
    }

    recordSnapshot();
    state.intervalId = window.setInterval(recordSnapshot, intervalMs || 100);

    window.__latePicksProbe = state;
    window.__latePicksProbeSnapshot = findValueSnapshot;
    window.__latePicksProbeGetState = function() {
      return {
        events: state.events.slice(),
        current: findValueSnapshot(),
      };
    };
    window.__latePicksProbeStop = function() {
      if (state.intervalId) {
        window.clearInterval(state.intervalId);
        state.intervalId = null;
      }
      return true;
    };
    return true;
  }

  window.__latePicksProbeInstall = install;
})();
"""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "late_probe"


def _normalize_percentage_string(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    return f"{float(match.group(1)):.1f}%"


def _find_late_related_entries(payload: Any, max_results: int = 12) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def walk(node: Any, path: str = "", depth: int = 0):
        if len(results) >= max_results or depth > 6:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                next_path = f"{path}.{key}" if path else str(key)
                if "late" in str(key).lower():
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        results.append({"path": next_path, "value": value})
                    else:
                        results.append({"path": next_path, "value_type": type(value).__name__})
                    if len(results) >= max_results:
                        return
                walk(value, next_path, depth + 1)
        elif isinstance(node, list):
            for index, item in enumerate(node[:25]):
                walk(item, f"{path}[{index}]", depth + 1)
                if len(results) >= max_results:
                    return

    walk(payload)
    return results


def _summarize_payload(payload: Any) -> dict[str, Any]:
    summary: dict[str, Any]
    if isinstance(payload, list):
        summary = {"kind": "list", "length": len(payload)}
        if payload and isinstance(payload[0], dict):
            summary["first_keys"] = sorted(payload[0].keys())[:20]
            type_counts = Counter(
                str(item.get("type"))
                for item in payload
                if isinstance(item, dict) and item.get("type") is not None
            )
            if type_counts:
                summary["type_counts"] = dict(type_counts)
            merchant_ids = sorted(
                {
                    str(item.get("merchantId"))
                    for item in payload
                    if isinstance(item, dict) and item.get("merchantId")
                }
            )
            if merchant_ids:
                summary["merchant_ids"] = merchant_ids[:10]
    elif isinstance(payload, dict):
        summary = {"kind": "dict", "keys": sorted(payload.keys())[:30]}
    else:
        summary = {"kind": type(payload).__name__}

    late_related = _find_late_related_entries(payload)
    if late_related:
        summary["late_related"] = late_related

    return summary


def _attribute_render_events(
    render_events: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    *,
    max_gap_ms: int = DEFAULT_ATTRIBUTION_GAP_MS,
) -> list[dict[str, Any]]:
    sorted_responses = sorted(
        (response for response in responses if response.get("finishedAtMs") is not None),
        key=lambda item: item["finishedAtMs"],
    )
    annotated: list[dict[str, Any]] = []
    previous_signature: str | None = None
    previous_value: str | None = None

    for event in sorted(render_events, key=lambda item: item.get("timestampMs", 0)):
        timestamp_ms = int(event.get("timestampMs") or 0)
        signature = str(event.get("signature") or "")
        normalized_value = _normalize_percentage_string(event.get("valueText"))
        signature_changed = signature != previous_signature
        value_changed = normalized_value != previous_value

        matched_response = None
        matched_gap_ms = None
        if signature_changed:
            candidates = [
                response
                for response in sorted_responses
                if 0 <= timestamp_ms - response["finishedAtMs"] <= max_gap_ms
            ]
            if candidates:
                matched_response = candidates[-1]
                matched_gap_ms = timestamp_ms - matched_response["finishedAtMs"]

        annotated_event = dict(event)
        annotated_event["normalizedValue"] = normalized_value
        annotated_event["signatureChanged"] = signature_changed
        annotated_event["valueChanged"] = value_changed
        if matched_response is not None:
            annotated_event["matchedResponseId"] = matched_response["id"]
            annotated_event["matchedResponseGapMs"] = matched_gap_ms

        annotated.append(annotated_event)
        previous_signature = signature
        previous_value = normalized_value

    return annotated


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="Configured store name to select in the dashboard.")
    parser.add_argument(
        "--dropdown-name",
        default="",
        help="Optional explicit dropdown name if it differs from the configured store name.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Launch Chromium with a visible window for easier inspection.",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=float,
        default=DEFAULT_MAX_WAIT_SECONDS,
        help="Maximum time to observe post-refresh traffic before stopping.",
    )
    parser.add_argument(
        "--quiet-seconds",
        type=float,
        default=DEFAULT_QUIET_SECONDS,
        help="Stop once there have been no new JSON responses for this many seconds.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=DEFAULT_POLL_INTERVAL_MS,
        help="Polling cadence for the rendered Late Picks value.",
    )
    return parser.parse_args()


async def _handle_response(
    response,
    run_dir: Path,
    response_id: int,
    response_records: list[dict[str, Any]],
    tracker: dict[str, Any],
    lock: asyncio.Lock,
):
    request = response.request
    if request.resource_type not in JSON_RESOURCE_TYPES:
        return

    started_at_ms = int(time.time() * 1000)
    headers = await response.all_headers()
    content_type = headers.get("content-type", "")
    try:
        payload = await response.json()
    except Exception:
        return
    finished_at_ms = int(time.time() * 1000)

    body_path = run_dir / "responses" / f"response_{response_id:03d}.json"
    body_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    record = {
        "id": response_id,
        "url": response.url,
        "method": request.method,
        "resourceType": request.resource_type,
        "status": response.status,
        "contentType": content_type,
        "startedAtMs": started_at_ms,
        "finishedAtMs": finished_at_ms,
        "bodyPath": str(body_path),
        "payloadSummary": _summarize_payload(payload),
    }

    async with lock:
        response_records.append(record)
        tracker["response_count"] += 1
        tracker["last_response_monotonic"] = time.monotonic()


async def _wait_for_quiet_period(tracker: dict[str, Any], max_wait_seconds: float, quiet_seconds: float):
    start = time.monotonic()
    while time.monotonic() - start < max_wait_seconds:
        await asyncio.sleep(0.25)
        if tracker["response_count"] == 0:
            continue
        last_seen = tracker["last_response_monotonic"]
        if last_seen is not None and time.monotonic() - last_seen >= quiet_seconds:
            return


async def _capture_container_html(page, container_path: str | None) -> str | None:
    if not container_path:
        return None
    return await page.evaluate(
        """(path) => {
            const element = document.querySelector(path);
            return element ? element.outerHTML : null;
        }""",
        container_path,
    )


async def _write_debug_artifacts(page, run_dir: Path, prefix: str):
    screenshot_path = run_dir / f"{prefix}.png"
    html_path = run_dir / f"{prefix}.html"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        pass
    try:
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        pass


async def run_probe(args: argparse.Namespace):
    settings = load_settings()
    storage_state_path = Path(settings.storage_state_path)
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Storage state file not found at '{storage_state_path}'. Run the normal scraper login flow first."
        )

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / settings.output_dir / "debug" / f"late_probe_{_slugify(args.store)}_{timestamp}"
    ensure_directory(str(run_dir / "responses"))

    browser = None
    context = None
    page = None
    response_tasks: set[asyncio.Task] = set()
    response_records: list[dict[str, Any]] = []
    tracker = {"response_count": 0, "last_response_monotonic": None}
    response_lock = asyncio.Lock()
    response_counter = 0

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not args.headful)
            context = await browser.new_context(storage_state=str(storage_state_path))
            await optimize_browser_context(context, settings)
            page = await context.new_page()

            await page.goto(settings.base_dashboard_url, timeout=settings.page_timeout_ms, wait_until="domcontentloaded")
            if await check_if_login_needed(page, settings.base_dashboard_url, settings):
                await _write_debug_artifacts(page, run_dir, "login_required")
                raise RuntimeError(
                    "The saved session is no longer valid. Re-run the normal scraper login flow so state.json is refreshed, "
                    "then retry the probe. Debug artifacts were saved in the probe output folder."
                )

            try:
                await select_store_from_dropdown(
                    page,
                    args.dropdown_name or args.store,
                    args.store,
                    settings,
                )
            except Exception as exc:
                await _write_debug_artifacts(page, run_dir, "dropdown_failure")
                raise RuntimeError(
                    f"Failed to open or use the store dropdown for '{args.store}'. "
                    f"Saved screenshot and HTML to {run_dir} for inspection. Original error: {exc}"
                ) from exc

            await page.evaluate(LATE_PROBE_SCRIPT)
            await page.evaluate(
                "(pollIntervalMs) => window.__latePicksProbeInstall(pollIntervalMs)",
                args.poll_interval_ms,
            )
            initial_snapshot = await page.evaluate("window.__latePicksProbeSnapshot()")

            def on_response(response):
                nonlocal response_counter
                response_counter += 1
                task = asyncio.create_task(
                    _handle_response(response, run_dir, response_counter, response_records, tracker, response_lock)
                )
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", on_response)

            refresh_button = page.get_by_role("button", name="Refresh")
            await refresh_button.click()
            refresh_clicked_at_ms = int(time.time() * 1000)

            await _wait_for_quiet_period(tracker, args.max_wait_seconds, args.quiet_seconds)
            if response_tasks:
                await asyncio.gather(*response_tasks)

            probe_state = await page.evaluate("window.__latePicksProbeGetState()")
            await page.evaluate("window.__latePicksProbeStop()")

            current_snapshot = probe_state.get("current") if isinstance(probe_state, dict) else None
            container_html = await _capture_container_html(
                page,
                current_snapshot.get("containerPath") if isinstance(current_snapshot, dict) else None,
            )
            if container_html:
                (run_dir / "late_container.html").write_text(container_html, encoding="utf-8")

            screenshot_path = run_dir / "dashboard.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)

    finally:
        await safe_close(page, "Late Picks probe page")
        await safe_close(context, "Late Picks probe context")
        await safe_close(browser, "Late Picks probe browser")

    raw_events = []
    if isinstance(probe_state, dict):
        raw_events = [event for event in probe_state.get("events", []) if event.get("timestampMs", 0) >= refresh_clicked_at_ms]

    attributed_events = _attribute_render_events(raw_events, response_records)
    current_snapshot = probe_state.get("current") if isinstance(probe_state, dict) else None
    matching_response_ids = sorted(
        {
            event["matchedResponseId"]
            for event in attributed_events
            if event.get("matchedResponseId") is not None and event.get("signatureChanged")
        }
    )

    summary = {
        "store": args.store,
        "dropdownName": args.dropdown_name or args.store,
        "runDirectory": str(run_dir),
        "refreshClickedAtMs": refresh_clicked_at_ms,
        "initialSnapshot": initial_snapshot,
        "finalSnapshot": current_snapshot,
        "responseCount": len(response_records),
        "responseRecords": response_records,
        "renderEvents": attributed_events,
        "likelyResponseIds": matching_response_ids,
        "screenshotPath": str(screenshot_path),
        "containerHtmlPath": str(run_dir / "late_container.html") if container_html else None,
    }

    (run_dir / "probe_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    initial_value = _normalize_percentage_string((initial_snapshot or {}).get("valueText"))
    final_value = _normalize_percentage_string((current_snapshot or {}).get("valueText"))

    print(f"Store: {args.store}")
    print(f"Output: {run_dir}")
    print(f"Initial Late Picks: {initial_value or 'not found'}")
    print(f"Final Late Picks: {final_value or 'not found'}")
    print(f"Captured JSON responses: {len(response_records)}")
    print(f"Likely response ids tied to DOM changes: {matching_response_ids or 'none'}")
    for response_id in matching_response_ids:
        matching = next((record for record in response_records if record["id"] == response_id), None)
        if matching:
            print(f"  - #{response_id}: {matching['url']}")


def main():
    args = _parse_args()
    asyncio.run(run_probe(args))


if __name__ == "__main__":
    main()
