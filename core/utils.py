from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core.config import EMOJI_GREEN_CHECK, EMOJI_RED_CROSS, STORE_PREFIX_RE, Settings
from core.logger import app_logger

BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}


def normalize_name(name: str) -> str:
    normalized = name.lower().replace("morrisons", "")
    normalized = re.sub(r"[-_\.]", " ", normalized)
    return normalized.strip()


def sanitize_store_name(name: str) -> str:
    return STORE_PREFIX_RE.sub("", name).strip()


def format_metric_with_emoji(
    value_str: str,
    threshold: float,
    is_uph: bool = False,
    pass_emoji: str = EMOJI_GREEN_CHECK,
    fail_emoji: str = EMOJI_RED_CROSS,
) -> str:
    try:
        numeric_value = float(re.sub(r"[^\d.]", "", value_str))
        is_good = numeric_value >= threshold if is_uph else numeric_value <= threshold
        return f"{pass_emoji if is_good else fail_emoji} {value_str}"
    except (TypeError, ValueError):
        return value_str


def ensure_directory(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: str, content: str, encoding: str = "utf-8"):
    directory = Path(path).parent
    ensure_directory(str(directory))
    handle = None
    tmp_path = None
    try:
        handle = tempfile.NamedTemporaryFile("w", encoding=encoding, dir=directory, delete=False)
        tmp_path = handle.name
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(tmp_path, path)
    finally:
        if handle is not None:
            handle.close()
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_json(path: str, payload: object, *, indent: int = 2):
    atomic_write_text(path, json.dumps(payload, indent=indent), encoding="utf-8")


async def save_screenshot(page, prefix: str, settings: Settings):
    if not page or page.is_closed():
        app_logger.warning(f"Cannot save screenshot '{prefix}': page is closed or unavailable.")
        return

    try:
        safe_prefix = re.sub(r'[\\/*?:"<>|]', "_", prefix)
        timestamp = datetime.now(settings.local_timezone).strftime("%Y%m%d_%H%M%S")
        path = settings.output_path(f"{safe_prefix}_{timestamp}.png")
        ensure_directory(settings.output_dir)
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved for debugging: {path}")
    except Exception as exc:
        app_logger.error(f"Failed to save screenshot with prefix '{prefix}': {exc}")


async def optimize_browser_context(context, settings: Settings):
    route_method = getattr(context, "route", None)
    if not callable(route_method):
        return

    async def route_handler(route):
        request = route.request
        hostname = urlparse(request.url).netloc.lower()
        if request.resource_type in BLOCKED_RESOURCE_TYPES or any(
            domain in hostname for domain in settings.resource_blocklist
        ):
            await route.abort()
            return
        await route.continue_()

    await route_method("**/*", route_handler)


async def safe_close(resource, label: str, failure_recorder=None):
    if resource is None:
        return

    try:
        is_closed = getattr(resource, "is_closed", None)
        if callable(is_closed):
            try:
                if is_closed():
                    return
            except Exception:
                pass

        is_connected = getattr(resource, "is_connected", None)
        if callable(is_connected):
            try:
                if not is_connected():
                    return
            except Exception:
                pass

        await resource.close()
    except Exception as exc:
        app_logger.warning(f"Failed to close {label}: {exc}")
        if failure_recorder:
            await failure_recorder(
                f"{label} (Cleanup failure)",
                asyncio.get_running_loop().time(),
                category="cleanup",
            )
