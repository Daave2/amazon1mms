import os
import sys
import types
import re
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("LOGIN_URL", "https://example.com/signin")
os.environ.setdefault("LOGIN_EMAIL", "tester@example.com")
os.environ.setdefault("LOGIN_PASSWORD", "password")
os.environ.setdefault("OTP_SECRET_KEY", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("TARGET_URL", "https://example.com/dashboard")
os.environ.setdefault("FORM_POST_URL", "https://example.com/form")

config_stub = types.ModuleType("core.config")
config_stub.DEBUG_MODE = False
config_stub.LOCAL_TIMEZONE = ZoneInfo("Europe/London")
config_stub.LOGIN_URL = os.environ["LOGIN_URL"]
config_stub.LOGIN_EMAIL = os.environ["LOGIN_EMAIL"]
config_stub.LOGIN_PASSWORD = os.environ["LOGIN_PASSWORD"]
config_stub.OTP_SECRET_KEY = os.environ["OTP_SECRET_KEY"]
config_stub.BASE_DASHBOARD_URL = os.environ["TARGET_URL"]
config_stub.CHAT_WEBHOOK_URL = ""
config_stub.CHAT_BATCH_SIZE = 100
config_stub.FORM_POST_URL = os.environ["FORM_POST_URL"]
config_stub.INITIAL_CONCURRENCY = 2
config_stub.NUM_FORM_SUBMITTERS = 2
config_stub.AUTO_ENABLED = True
config_stub.AUTO_MIN_CONCURRENCY = 1
config_stub.AUTO_MAX_CONCURRENCY = 5
config_stub.CPU_UPPER_THRESHOLD = 90
config_stub.CPU_LOWER_THRESHOLD = 65
config_stub.MEM_UPPER_THRESHOLD = 90
config_stub.CHECK_INTERVAL = 5
config_stub.COOLDOWN_SECONDS = 15
config_stub.PAGE_TIMEOUT = 30_000
config_stub.WAIT_TIMEOUT = 15_000
config_stub.ACTION_TIMEOUT = 15_000
config_stub.WORKER_RETRY_COUNT = 1
config_stub.STORE_PREFIX_RE = re.compile(r"^morrisons?\s*-?\s*|\s*-?\s*morrisons?$", re.I)
config_stub.SPECIAL_NAME_MAPPINGS = {
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
config_stub.EMOJI_GREEN_CHECK = "OK"
config_stub.EMOJI_RED_CROSS = "X"
config_stub.UPH_THRESHOLD = 80
config_stub.LATES_THRESHOLD = 3.0
config_stub.INF_THRESHOLD = 2.0
config_stub.FIELD_MAP = {
    "date": "entry.date",
    "store": "entry.store",
    "orders": "entry.orders",
    "units": "entry.units",
    "fulfilled": "entry.fulfilled",
    "uph": "entry.uph",
    "inf": "entry.inf",
    "found": "entry.found",
    "cancelled": "entry.cancelled",
    "lates": "entry.lates",
    "field_11": "entry.extra",
    "time_available": "entry.time_available",
}
config_stub.OUTPUT_DIR = "output"
config_stub.LOG_FILE = "output/submissions.log"
config_stub.JSON_LOG_FILE = "output/submissions.jsonl"
config_stub.STORAGE_STATE = "state.json"
config_stub.DISCOVERY_CACHE_FILE = "output/discovery_cache.json"
config_stub.RESOURCE_BLOCKLIST = []

sys.modules.setdefault("core.config", config_stub)
