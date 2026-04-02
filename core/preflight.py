import csv
import json
import os
import tempfile
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from core.store_loader import parse_store_row

DEFAULTS = {
    "TARGET_URL": "https://sellercentral.amazon.co.uk/snowdash/ref=xx_shopdash_dnav_xx",
    "INITIAL_CONCURRENCY": 30,
    "NUM_FORM_SUBMITTERS": 2,
    "AUTO_ENABLED": True,
    "AUTO_MIN_CONCURRENCY": 1,
    "AUTO_MAX_CONCURRENCY": 40,
    "FAST_PATH_MAX_CONCURRENCY": 12,
}

REQUIRED_ENV_VARS = (
    "LOGIN_URL",
    "LOGIN_EMAIL",
    "LOGIN_PASSWORD",
    "OTP_SECRET_KEY",
    "FORM_POST_URL",
)

HTTPS_URL_VARS = (
    "LOGIN_URL",
    "TARGET_URL",
    "FORM_POST_URL",
    "CHAT_WEBHOOK_URL",
)


def run_preflight(
    env_file: str = ".env",
    csv_path: str = "urls.csv",
    output_dir: str = "output",
    state_path: str = "state.json",
    discovery_cache_path: str | None = None,
) -> dict[str, Any]:
    load_dotenv(env_file, override=False)

    if discovery_cache_path is None:
        discovery_cache_path = os.path.join(output_dir, "discovery_cache.json")

    errors: list[str] = []
    warnings: list[str] = []

    missing_required = [name for name in REQUIRED_ENV_VARS if not os.getenv(name, "").strip()]
    if missing_required:
        errors.append(f"Missing required environment variables: {', '.join(missing_required)}")

    validated_urls = {
        "login_url": _required_value("LOGIN_URL"),
        "target_url": os.getenv("TARGET_URL", DEFAULTS["TARGET_URL"]),
        "form_post_url": _required_value("FORM_POST_URL"),
        "chat_webhook_url": os.getenv("CHAT_WEBHOOK_URL", ""),
    }

    for env_name in HTTPS_URL_VARS:
        value = os.getenv(env_name, "").strip()
        if not value and env_name == "TARGET_URL":
            value = str(DEFAULTS["TARGET_URL"])
        if value:
            _validate_https_url(env_name, value, errors)

    concurrency = {
        "initial_concurrency": _get_int_env("INITIAL_CONCURRENCY", int(DEFAULTS["INITIAL_CONCURRENCY"]), errors),
        "num_form_submitters": _get_int_env("NUM_FORM_SUBMITTERS", int(DEFAULTS["NUM_FORM_SUBMITTERS"]), errors),
        "auto_enabled": _get_bool_env("AUTO_ENABLED", bool(DEFAULTS["AUTO_ENABLED"]), errors),
        "auto_min_concurrency": _get_int_env("AUTO_MIN_CONCURRENCY", int(DEFAULTS["AUTO_MIN_CONCURRENCY"]), errors),
        "auto_max_concurrency": _get_int_env("AUTO_MAX_CONCURRENCY", int(DEFAULTS["AUTO_MAX_CONCURRENCY"]), errors),
        "fast_path_max_concurrency": _get_int_env(
            "FAST_PATH_MAX_CONCURRENCY",
            int(DEFAULTS["FAST_PATH_MAX_CONCURRENCY"]),
            errors,
        ),
    }
    _validate_concurrency(concurrency, errors)

    store_file_details = _validate_store_file(csv_path, errors)
    output_dir_details = _validate_output_dir(output_dir, errors)

    state_present = os.path.exists(state_path)
    cache_present = os.path.exists(discovery_cache_path)
    if not state_present:
        warnings.append(f"Optional auth state artifact not found at {state_path}")
    if not cache_present:
        warnings.append(f"Optional discovery cache artifact not found at {discovery_cache_path}")

    status = "ok" if not errors else "error"
    return {
        "status": status,
        "checked_at": datetime.now().astimezone().isoformat(),
        "errors": errors,
        "warnings": warnings,
        "details": {
            "env_file": env_file,
            "required_env_vars_present": len(missing_required) == 0,
            "urls": validated_urls,
            "concurrency": concurrency,
            "store_file": store_file_details,
            "output_dir": output_dir_details,
            "artifacts": {
                "state_json_present": state_present,
                "discovery_cache_present": cache_present,
            },
        },
    }


def emit_preflight_report(result: dict[str, Any]) -> int:
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


def _required_value(env_name: str) -> str:
    return os.getenv(env_name, "").strip()


def _get_int_env(env_name: str, default: int, errors: list[str]) -> int:
    raw_value = os.getenv(env_name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        errors.append(f"{env_name} must be an integer, got {raw_value!r}")
        return default


def _get_bool_env(env_name: str, default: bool, errors: list[str]) -> bool:
    raw_value = os.getenv(env_name, "").strip()
    if not raw_value:
        return default

    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    errors.append(f"{env_name} must be a boolean value, got {raw_value!r}")
    return default


def _validate_https_url(env_name: str, value: str, errors: list[str]):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        errors.append(f"{env_name} must be a valid HTTPS URL")


def _validate_concurrency(concurrency: dict[str, Any], errors: list[str]):
    positive_fields = (
        "initial_concurrency",
        "num_form_submitters",
        "auto_min_concurrency",
        "auto_max_concurrency",
        "fast_path_max_concurrency",
    )
    for field_name in positive_fields:
        if concurrency[field_name] < 1:
            errors.append(f"{field_name} must be at least 1")

    if concurrency["auto_max_concurrency"] < concurrency["auto_min_concurrency"]:
        errors.append("AUTO_MAX_CONCURRENCY must be greater than or equal to AUTO_MIN_CONCURRENCY")

    if concurrency["auto_enabled"]:
        initial = concurrency["initial_concurrency"]
        if not concurrency["auto_min_concurrency"] <= initial <= concurrency["auto_max_concurrency"]:
            errors.append("INITIAL_CONCURRENCY must fall within the AUTO_MIN_CONCURRENCY/AUTO_MAX_CONCURRENCY range")


def _validate_store_file(csv_path: str, errors: list[str]) -> dict[str, Any]:
    if not os.path.exists(csv_path):
        errors.append(f"Store file not found: {csv_path}")
        return {"path": csv_path, "usable_rows": 0, "skipped_rows": 0}

    usable_rows = 0
    skipped_rows = 0

    with open(csv_path, newline="", encoding="utf-8") as file_handle:
        reader = csv.reader(file_handle)
        next(reader, None)

        for row in reader:
            if parse_store_row(row) is None:
                skipped_rows += 1
                continue
            usable_rows += 1

    if usable_rows == 0:
        errors.append(f"No usable store rows found in {csv_path}")

    return {
        "path": csv_path,
        "usable_rows": usable_rows,
        "skipped_rows": skipped_rows,
    }


def _validate_output_dir(output_dir: str, errors: list[str]) -> dict[str, Any]:
    temp_path = None
    try:
        os.makedirs(output_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_dir, prefix=".preflight_", delete=False) as temp_file:
            temp_path = temp_file.name
        writable = True
    except OSError as exc:
        errors.append(f"Output directory is not writable: {output_dir} ({exc})")
        writable = False
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

    return {
        "path": output_dir,
        "writable": writable,
    }
