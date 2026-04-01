import re

from core.config import EMOJI_GREEN_CHECK, EMOJI_RED_CROSS, STORE_PREFIX_RE


def normalize_name(name: str) -> str:
    # 1. Lowercase and remove 'Morrisons'
    n = name.lower().replace("morrisons", "")
    # 2. Replace common separators with spaces
    n = re.sub(r"[-_\.]", " ", n)
    # 3. Trim extra whitespace
    return n.strip()


def sanitize_store_name(name: str) -> str:
    """Trim 'Morrisons' prefix or suffix from store names for chat display."""
    return STORE_PREFIX_RE.sub("", name).strip()


def format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    """Applies a pass/fail emoji to a metric string based on a threshold."""
    try:
        numeric_value = float(re.sub(r"[^\d.]", "", value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        emoji = EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS
        return f"{emoji} {value_str}"
    except (ValueError, TypeError):
        return value_str  # Return as is if not a number


async def save_screenshot(page, prefix: str):
    import os
    import re
    from datetime import datetime

    from core.config import LOCAL_TIMEZONE, OUTPUT_DIR
    from core.logger import app_logger

    if not page or page.is_closed():
        app_logger.warning(f"Cannot save screenshot '{prefix}': Page is closed or unavailable.")
        return
    try:
        safe_prefix = re.sub(r'[\\/*?:"<>|]', "_", prefix)
        timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(OUTPUT_DIR, f"{safe_prefix}_{timestamp}.png")
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved for debugging: {path}")
    except Exception as e:
        app_logger.error(f"Failed to save screenshot with prefix '{prefix}': {e}")
