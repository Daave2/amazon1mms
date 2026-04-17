from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

STORE_PREFIX_RE = re.compile(r"^morrisons?\s*-?\s*|\s*-?\s*morrisons?$", re.I)
SPECIAL_NAME_MAPPINGS = {
    "analby": "anlaby",
    "baglan moor": "baglan",
    "bradford": "thornbury",
    "cardiff tygals": "cardiff tyglass",
    "connahs quay": "connahs quays",
    "oxford": "carterton",
    "thornton cleveleys": "thornton-cleveleys",
    "auckland": "bishop auckland",
    "preston riversway": "preston",
    "harrow trident point": "harrow",
    "stevenson": "stevenston",
    "weston super mare": "weston-super-mare",
}

FORM_STORE_NAME_MAPPINGS = {
    "Morrisons Aberdeen": "Morrisons - Aberdeen",
    "Acton": "Morrisons - Acton",
    "Morrisons Boroughbridge": "Morrisons - Boroughbridge",
    "Morrisons Basingstoke": "Morrisons - Basingstoke",
    "Morrisons Auckland": "Morrisons - Bishop Auckland",
    "Morrisons Baglan Moor": "Morrisons - Baglan Moor",
    "Morrisons Becontree Heath": "Morrisons - Becontree Heath",
    "Morrisons Analby": "Morrisons - Hull",
    "Belle Vale Morrisons": "Morrisons - Belle Vale",
    "Morrisons Anniesland": "Morrisons - Anniesland",
    "Morrisons Banbury": "Morrisons - Banbury",
    "Morrisons Binley": "Morrisons - Binley",
    "Bradford": "Morrisons - Thornbury",
    "Morrisons Bristol": "Morrisons - Bristol",
    "Morrisons Bromsgrove": "Morrisons - Bromsgrove",
    "Byker Morrisons": "Morrisons - Byker",
    "Morrisons Bulwell": "Morrisons - Bulwell",
    "Morrisons Canning Town": "Morrisons - Canning Town",
    "Morrisons Cambourne": "Morrisons - Cambourne",
    "Morrisons Canterbury": "Morrisons - Canterbury",
    "Cardonald Morrisons": "Morrisons - Cardonald",
    "Morrisons Canvey Island": "Morrisons - Canvey Island",
    "Morrisons Cardiff Tygals": "Morrisons - Cardiff",
    "Catcliffe Morrisons": "Morrisons - Sheffield",
    "Morrisons Chingford": "Morrisons - Chingford",
    "Morrisons Cleethorpes": "Morrisons - Cleethorpes",
    "Morrisons Coalville": "Morrisons - Coalville",
    "Morrisons Chippenham": "Morrisons - Chippenham",
    "Morrisons Connahs Quay": "Morrisons - Connahs Quay",
    "Morrisons Corby": "Morrisons - Corby",
    "Croydon Morrisons": "Morrisons - Croydon",
    "Morrisons Derby": "Morrisons - Derby",
    "Morrisons Dundee": "Morrisons - Dundee",
    "Morrisons Eastbourne": "Morrisons - Eastbourne",
    "Morrisons Ebbw Vale": "Morrisons - Ebbw Vale",
    "Eccles Morrisons": "Morrisons - Eccles",
    "Glenrothes": "Morrisons - Glenrothes",
    "Morrisons Exeter": "Morrisons - Exeter",
    "Morrisons Gorleston": "Morrisons - Gorleston",
    "Morrisons Gloucester": "Morrisons - Gloucester",
    "Morrisons Gyle": "Morrisons - Gyle",
    "Morrisons Gravesend": "Morrisons - Gravesend",
    "Morrisons Halifax": "Morrisons - Halifax",
    "Morrisons Harrow - Trident Point": "Morrisons - Harrow",
    "Morrisons Ipswich": "Morrisons - Ipswich",
    "Morrisons High Wycombe": "Morrisons - High Wycombe",
    "Hunslet Morrisons": "Morrisons - Hunslet",
    "Jarrow Morrisons": "Morrisons - Jarrow",
    "Morrisons Kirkstall": "Morrisons - Kirkstall",
    "Morrisons Leicester": "Morrisons - Leicester",
    "Morrisons Lincoln": "Morrisons - Lincoln",
    "Morrisons Maidstone": "Morrisons - Maidstone",
    "Morrisons Milton Keynes": "Morrisons - Milton Keynes",
    "Morrisons Nelson": "Morrisons - Nelson",
    "Morrisons Middlesbrough": "Morrisons - Middlesbrough",
    "Morrisons Newark": "Morrisons - Newark",
    "Carterton Morrisons": "Morrisons - Oxford",
    "Morrisons Northampton": "Morrisons - Northampton",
    "Morrisons Newport": "Morrisons - Newport",
    "Morrisons Norwich": "Morrisons - Norwich",
    "Morrisons Oxted": "Morrisons - Oxted",
    "Peckham Morrisons": "Morrisons - Peckham",
    "Morrisons Peterborough": "Morrisons - Peterborough",
    "Morrisons Plymouth": "Morrisons - Plymouth",
    "Portsmouth Morrisons": "Morrisons - Portsmouth",
    "Morrisons Preston": "Morrisons - Preston",
    "Morrisons Reading": "Morrisons - Reading",
    "Reddish Morrisons": "Morrisons - Reddish",
    "Queensbury Morrisons": "Morrisons - Queensbury",
    "Sheldon Morrisons": "Morrisons - Sheldon",
    "Morrisons Redruth": "Morrisons - Redruth",
    "Morrisons Rhyl": "Morrisons - Rhyl",
    "St Helens Morrisons": "Morrisons - St. Helens",
    "Morrisons Staveley": "Morrisons - Staveley",
    "Morrisons Stevenson": "Morrisons - Stevenston",
    "Morrisons Stirling": "Morrisons - Stirling",
    "Stratford Morrisons": "Morrisons - Stratford",
    "Morrisons Stoke": "Morrisons - Stoke",
    "Morrisons Thornton Cleveleys": "Morrisons - Thornton-Cleveleys",
    "Morrisons Taunton": "Morrisons - Taunton",
    "Morrisons Totton": "Morrisons - Totton",
    "Morrisons Totnes": "Morrisons - Totnes",
    "Morrisons Swindon": "Morrisons - Swindon",
    "Morrisons Verwood": "Morrisons - Verwood",
    "Watford Morrisons": "Morrisons - Watford",
    "Walsall Morrisons": "Morrisons - Walsall",
    "Morrisons Witham": "Morrisons - Witham",
    "Woking Morrisons": "Morrisons - Woking",
    "Morrisons Winsford": "Morrisons - Winsford",
    "Morrisons Wellington": "Morrisons - Wellington",
    "Morrisons Welwyn": "Morrisons - Welwyn Garden City",
    "Morrisons Warminster": "Morrisons - Warminster",
    "Morrisons Welling": "Morrisons - Welling",
    "Weybridge Morrisons": "Morrisons - Weybridge",
    "Morrisons Worthing": "Morrisons - Worthing",
    "Morrisons Weston Super Mare": "Morrisons - Weston Super Mare",
    "Morrisons Wisbech": "Morrisons - Wisbech",
    "Morrisons Wrexham": "Morrisons - Wrexham",
    "Morrisons York": "Morrisons - York",
}

EMOJI_GREEN_CHECK = "\u2705"
EMOJI_RED_CROSS = "\u274c"
UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

DEFAULT_FIELD_MAP = {
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

DEFAULT_RESOURCE_BLOCKLIST = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "adservice.google.com",
    "facebook.net",
    "fbcdn.net",
    "analytics.tiktok.com",
)


@dataclass
class Settings:
    debug_mode: bool = False
    local_timezone_name: str = "Europe/London"

    login_url: str = ""
    login_email: str = ""
    login_password: str = ""
    otp_secret_key: str = ""
    target_url: str = "https://sellercentral.amazon.co.uk/snowdash/ref=xx_shopdash_dnav_xx"

    chat_webhook_url: str = ""
    chat_batch_size: int = 100
    form_post_url: str = ""

    initial_concurrency: int = 30
    num_form_submitters: int = 2
    dropdown_refresh_max_age_days: int = 7
    force_dropdown_discovery: bool = False

    auto_enabled: bool = True
    auto_min_concurrency: int = 1
    auto_max_concurrency: int = 40
    cpu_upper_threshold: int = 90
    cpu_lower_threshold: int = 65
    mem_upper_threshold: int = 90
    check_interval_seconds: int = 5
    cooldown_seconds: int = 15

    page_timeout_ms: int = 30000
    wait_timeout_ms: int = 15000
    action_timeout_ms: int = 15000
    worker_retry_count: int = 3
    fast_path_max_concurrency: int = 12
    fast_path_warmup_requests: int = 4
    fast_path_warmup_delay_ms: int = 150
    fast_path_retry_count: int = 3
    fast_path_retry_base_delay_ms: int = 1500
    max_login_attempts: int = 3

    output_dir: str = "output"
    app_log_file: str = "app.log"
    storage_state_path: str = "state.json"

    field_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FIELD_MAP))
    resource_blocklist: tuple[str, ...] = DEFAULT_RESOURCE_BLOCKLIST
    special_name_mappings: dict[str, str] = field(default_factory=lambda: dict(SPECIAL_NAME_MAPPINGS))
    form_store_name_mappings: dict[str, str] = field(default_factory=lambda: dict(FORM_STORE_NAME_MAPPINGS))

    uph_threshold: float = UPH_THRESHOLD
    lates_threshold: float = LATES_THRESHOLD
    inf_threshold: float = INF_THRESHOLD

    def output_path(self, *parts: str) -> str:
        return str(Path(self.output_dir).joinpath(*parts))

    @property
    def local_timezone(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone_name)

    @property
    def base_dashboard_url(self) -> str:
        return self.target_url

    @property
    def log_file(self) -> str:
        return self.output_path("submissions.log")

    @property
    def json_log_file(self) -> str:
        return self.output_path("submissions.jsonl")

    @property
    def discovery_cache_file(self) -> str:
        return self.output_path("discovery_cache.json")

    @property
    def submission_events_file(self) -> str:
        return self.output_path("submission_events.jsonl")

    @property
    def run_summary_file(self) -> str:
        return self.output_path("run_summary.json")

    @property
    def failure_events_file(self) -> str:
        return self.output_path("failure_events.json")


def _load_env_file(env_file: str) -> dict[str, str]:
    path = Path(env_file)
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _get_env_value(env: dict[str, str], name: str, default: str = "") -> str:
    return os.environ.get(name, env.get(name, default))


def _parse_bool(value: str, default: bool) -> bool:
    cleaned = str(value).strip().lower()
    if not cleaned:
        return default
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected boolean value, got {value!r}")


def _parse_int(value: str, default: int) -> int:
    cleaned = str(value).strip()
    if not cleaned:
        return default
    return int(cleaned)


def _parse_float(value: str, default: float) -> float:
    cleaned = str(value).strip()
    if not cleaned:
        return default
    return float(cleaned)


def load_settings(env_file: str | None = None) -> Settings:
    env_values = _load_env_file(env_file or ".env")
    try:
        return Settings(
            debug_mode=_parse_bool(_get_env_value(env_values, "DEBUG"), False),
            local_timezone_name=_get_env_value(env_values, "LOCAL_TIMEZONE", "Europe/London"),
            login_url=_get_env_value(env_values, "LOGIN_URL"),
            login_email=_get_env_value(env_values, "LOGIN_EMAIL"),
            login_password=_get_env_value(env_values, "LOGIN_PASSWORD"),
            otp_secret_key=_get_env_value(env_values, "OTP_SECRET_KEY"),
            target_url=_get_env_value(
                env_values,
                "TARGET_URL",
                "https://sellercentral.amazon.co.uk/snowdash/ref=xx_shopdash_dnav_xx",
            ),
            chat_webhook_url=_get_env_value(env_values, "CHAT_WEBHOOK_URL"),
            chat_batch_size=_parse_int(_get_env_value(env_values, "CHAT_BATCH_SIZE"), 100),
            form_post_url=_get_env_value(env_values, "FORM_POST_URL"),
            initial_concurrency=_parse_int(_get_env_value(env_values, "INITIAL_CONCURRENCY"), 30),
            num_form_submitters=_parse_int(_get_env_value(env_values, "NUM_FORM_SUBMITTERS"), 2),
            dropdown_refresh_max_age_days=_parse_int(_get_env_value(env_values, "DROPDOWN_REFRESH_MAX_AGE_DAYS"), 7),
            force_dropdown_discovery=_parse_bool(_get_env_value(env_values, "FORCE_DROPDOWN_DISCOVERY"), False),
            auto_enabled=_parse_bool(_get_env_value(env_values, "AUTO_ENABLED"), True),
            auto_min_concurrency=_parse_int(_get_env_value(env_values, "AUTO_MIN_CONCURRENCY"), 1),
            auto_max_concurrency=_parse_int(_get_env_value(env_values, "AUTO_MAX_CONCURRENCY"), 40),
            cpu_upper_threshold=_parse_int(_get_env_value(env_values, "CPU_UPPER_THRESHOLD"), 90),
            cpu_lower_threshold=_parse_int(_get_env_value(env_values, "CPU_LOWER_THRESHOLD"), 65),
            mem_upper_threshold=_parse_int(_get_env_value(env_values, "MEM_UPPER_THRESHOLD"), 90),
            check_interval_seconds=_parse_int(_get_env_value(env_values, "CHECK_INTERVAL"), 5),
            cooldown_seconds=_parse_int(_get_env_value(env_values, "COOLDOWN_SECONDS"), 15),
            page_timeout_ms=_parse_int(_get_env_value(env_values, "PAGE_TIMEOUT"), 30000),
            wait_timeout_ms=_parse_int(_get_env_value(env_values, "WAIT_TIMEOUT"), 15000),
            action_timeout_ms=_parse_int(_get_env_value(env_values, "ACTION_TIMEOUT"), 15000),
            worker_retry_count=_parse_int(_get_env_value(env_values, "WORKER_RETRY_COUNT"), 3),
            fast_path_max_concurrency=_parse_int(_get_env_value(env_values, "FAST_PATH_MAX_CONCURRENCY"), 12),
            fast_path_warmup_requests=_parse_int(_get_env_value(env_values, "FAST_PATH_WARMUP_REQUESTS"), 4),
            fast_path_warmup_delay_ms=_parse_int(_get_env_value(env_values, "FAST_PATH_WARMUP_DELAY_MS"), 150),
            fast_path_retry_count=_parse_int(_get_env_value(env_values, "FAST_PATH_RETRY_COUNT"), 3),
            fast_path_retry_base_delay_ms=_parse_int(_get_env_value(env_values, "FAST_PATH_RETRY_BASE_DELAY_MS"), 1500),
            max_login_attempts=_parse_int(_get_env_value(env_values, "MAX_LOGIN_ATTEMPTS"), 3),
            output_dir=_get_env_value(env_values, "OUTPUT_DIR", "output"),
            app_log_file=_get_env_value(env_values, "APP_LOG_FILE", "app.log"),
            storage_state_path=_get_env_value(env_values, "STORAGE_STATE", "state.json"),
            uph_threshold=_parse_float(_get_env_value(env_values, "UPH_THRESHOLD"), UPH_THRESHOLD),
            lates_threshold=_parse_float(_get_env_value(env_values, "LATES_THRESHOLD"), LATES_THRESHOLD),
            inf_threshold=_parse_float(_get_env_value(env_values, "INF_THRESHOLD"), INF_THRESHOLD),
        )
    except Exception as exc:
        raise RuntimeError(
            f"Invalid runtime configuration: {exc}. Run 'python3 scripts/preflight.py' for a full validation report."
        ) from exc
