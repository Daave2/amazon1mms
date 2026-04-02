import os
import re

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pytz import timezone


class Settings(BaseSettings):
    # App Settings
    debug_mode: bool = Field(default=False, alias="DEBUG")
    local_timezone: str = Field(default="Europe/London")

    # Auth & API Setup
    login_url: str = Field(...)
    login_email: str = Field(...)
    login_password: str = Field(...)
    otp_secret_key: str = Field(...)
    base_dashboard_url: str = Field(
        default="https://sellercentral.amazon.co.uk/snowdash/ref=xx_shopdash_dnav_xx", alias="TARGET_URL"
    )

    # Integrations
    chat_webhook_url: str = Field(default="")
    chat_batch_size: int = Field(default=100, alias="CHAT_BATCH_SIZE")
    form_post_url: str = Field(
        default="https://docs.google.com/forms/d/e/1FAIpQLSefktpkvAEYtT8pgYknAdWH_GmopNb-QLrmtTS-ijrBTc1hew/formResponse",
        alias="FORM_POST_URL",
    )

    # Concurrency
    initial_concurrency: int = Field(default=30)
    num_form_submitters: int = Field(default=2)

    # Auto Concurrency config
    auto_enabled: bool = Field(default=True)
    auto_min_concurrency: int = Field(default=1)
    auto_max_concurrency: int = Field(default=40)
    cpu_upper_threshold: int = Field(default=90)
    cpu_lower_threshold: int = Field(default=65)
    mem_upper_threshold: int = Field(default=90)
    check_interval_seconds: int = Field(default=5)
    cooldown_seconds: int = Field(default=15)

    # Limits & Retries
    page_timeout_ms: int = Field(default=30000)
    wait_timeout_ms: int = Field(default=15000)
    action_timeout_ms: int = Field(default=15000)
    worker_retry_count: int = Field(default=1)
    fast_path_max_concurrency: int = Field(default=12)
    fast_path_warmup_requests: int = Field(default=4)
    fast_path_warmup_delay_ms: int = Field(default=150)
    fast_path_retry_count: int = Field(default=3)
    fast_path_retry_base_delay_ms: int = Field(default=1500)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


def load_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:
        raise RuntimeError(
            f"Invalid runtime configuration: {exc}. Run 'python scripts/preflight.py' for a full validation report."
        ) from exc


settings = load_settings()

LOCAL_TIMEZONE = timezone(settings.local_timezone)
DEBUG_MODE = settings.debug_mode

LOGIN_URL = settings.login_url
LOGIN_EMAIL = settings.login_email
LOGIN_PASSWORD = settings.login_password
OTP_SECRET_KEY = settings.otp_secret_key
BASE_DASHBOARD_URL = settings.base_dashboard_url

CHAT_WEBHOOK_URL = settings.chat_webhook_url
CHAT_BATCH_SIZE = settings.chat_batch_size
FORM_POST_URL = settings.form_post_url

INITIAL_CONCURRENCY = settings.initial_concurrency
NUM_FORM_SUBMITTERS = settings.num_form_submitters

AUTO_ENABLED = settings.auto_enabled
AUTO_MIN_CONCURRENCY = settings.auto_min_concurrency
AUTO_MAX_CONCURRENCY = settings.auto_max_concurrency
CPU_UPPER_THRESHOLD = settings.cpu_upper_threshold
CPU_LOWER_THRESHOLD = settings.cpu_lower_threshold
MEM_UPPER_THRESHOLD = settings.mem_upper_threshold
CHECK_INTERVAL = settings.check_interval_seconds
COOLDOWN_SECONDS = settings.cooldown_seconds

PAGE_TIMEOUT = settings.page_timeout_ms
WAIT_TIMEOUT = settings.wait_timeout_ms
ACTION_TIMEOUT = settings.action_timeout_ms
WORKER_RETRY_COUNT = settings.worker_retry_count
FAST_PATH_MAX_CONCURRENCY = settings.fast_path_max_concurrency
FAST_PATH_WARMUP_REQUESTS = settings.fast_path_warmup_requests
FAST_PATH_WARMUP_DELAY_MS = settings.fast_path_warmup_delay_ms
FAST_PATH_RETRY_COUNT = settings.fast_path_retry_count
FAST_PATH_RETRY_BASE_DELAY_MS = settings.fast_path_retry_base_delay_ms

STORE_PREFIX_RE = re.compile(r"^morrisons?\s*-?\s*|\s*-?\s*morrisons?$", re.I)
SPECIAL_NAME_MAPPINGS = {
    "analby": "anlaby",
    "baglan moor": "baglan",
    "cardiff tygals": "cardiff tyglass",
    "connahs quay": "connahs quays",
    "thornton cleveleys": "thornton-cleveleys",
    "auckland": "bishop aukland",
    "preston riversway": "preston",
    "harrow trident point": "harrow",
    "stevenson": "stevenston",
    "weston super mare": "weston-super-mare",
}

# --- Constants for target-based emojis ---
EMOJI_GREEN_CHECK = "\u2705"  # ✅
EMOJI_RED_CROSS = "\u274c"  # ❌
UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

FIELD_MAP = {
    "date": "entry.1483325020",
    "store": "entry.117918617",
    "orders": "entry.128719511",
    "units": "entry.66444552",
    "fulfilled": "entry.2093280675",
    "uph": "entry.316694141",
    "inf": "entry.909185879",
    "found": "entry.637588300",
    "cancelled": "entry.1775576921",
    "lates": "entry.2130893076",
    "field_11": "entry.2071609599",
    "time_available": "entry.1823671734",
}

OUTPUT_DIR = "output"
LOG_FILE = os.path.join(OUTPUT_DIR, "submissions.log")
JSON_LOG_FILE = os.path.join(OUTPUT_DIR, "submissions.jsonl")
STORAGE_STATE = "state.json"
DISCOVERY_CACHE_FILE = os.path.join(OUTPUT_DIR, "discovery_cache.json")

RESOURCE_BLOCKLIST = [
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "adservice.google.com",
    "facebook.net",
    "fbcdn.net",
    "analytics.tiktok.com",
]

os.makedirs(OUTPUT_DIR, exist_ok=True)
