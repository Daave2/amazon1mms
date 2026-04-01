import os
import re
import json
import logging
from pytz import timezone

# Config loading helper
def load_config():
    try:
        with open('config.json', 'r') as config_file:
            return json.load(config_file)
    except FileNotFoundError:
        # We will let the entrypoint handle graceful exits if config doesn't exist
        return {}
    except json.JSONDecodeError:
        return {}

config = load_config()

LOCAL_TIMEZONE = timezone('Europe/London')
DEBUG_MODE      = config.get('debug', False)
LOGIN_URL       = config.get('login_url', '')
LOGIN_EMAIL     = config.get('login_email', '')
LOGIN_PASSWORD  = config.get('login_password', '')
OTP_SECRET_KEY  = config.get('otp_secret_key', '')

BASE_DASHBOARD_URL = config.get('target_url', '')
CHAT_WEBHOOK_URL = config.get('chat_webhook_url')
CHAT_BATCH_SIZE  = config.get('chat_batch_size', 100)

STORE_PREFIX_RE  = re.compile(r"^morrisons?\s*-?\s*|\s*-?\s*morrisons?$", re.I)
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
EMOJI_GREEN_CHECK = "\u2705" # ✅
EMOJI_RED_CROSS = "\u274C"   # ❌
UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

FORM_POST_URL = "https://docs.google.com/forms/d/e/1FAIpQLSefktpkvAEYtT8pgYknAdWH_GmopNb-QLrmtTS-ijrBTc1hew/formResponse"
FIELD_MAP = {
    'date':           'entry.1483325020',
    'store':          'entry.117918617',
    'orders':         'entry.128719511',
    'units':          'entry.66444552',
    'fulfilled':      'entry.2093280675',
    'uph':            'entry.316694141',
    'inf':            'entry.909185879',
    'found':          'entry.637588300',
    'cancelled':      'entry.1775576921',
    'lates':          'entry.2130893076',
    'field_11':       'entry.2071609599',
    'time_available': 'entry.1823671734',
}

INITIAL_CONCURRENCY = config.get('initial_concurrency', 30)
NUM_FORM_SUBMITTERS = config.get('num_form_submitters', 2)

AUTO_CONF = config.get('auto_concurrency', {})
AUTO_ENABLED = AUTO_CONF.get('enabled', False)
AUTO_MIN_CONCURRENCY = AUTO_CONF.get('min_concurrency', config.get('min_concurrency', 1))
AUTO_MAX_CONCURRENCY = AUTO_CONF.get('max_concurrency', config.get('max_concurrency', INITIAL_CONCURRENCY))
CPU_UPPER_THRESHOLD = AUTO_CONF.get('cpu_upper_threshold', 90)
CPU_LOWER_THRESHOLD = AUTO_CONF.get('cpu_lower_threshold', 65)
MEM_UPPER_THRESHOLD = AUTO_CONF.get('mem_upper_threshold', 90)
CHECK_INTERVAL = AUTO_CONF.get('check_interval_seconds', 5)
COOLDOWN_SECONDS = AUTO_CONF.get('cooldown_seconds', 15)

OUTPUT_DIR      = 'output'
LOG_FILE        = os.path.join(OUTPUT_DIR, 'submissions.log')
JSON_LOG_FILE   = os.path.join(OUTPUT_DIR, 'submissions.jsonl')
STORAGE_STATE   = 'state.json'
DISCOVERY_CACHE_FILE = os.path.join(OUTPUT_DIR, 'discovery_cache.json')

PAGE_TIMEOUT    = config.get('page_timeout_ms', 30000)
WAIT_TIMEOUT    = config.get('element_wait_timeout_ms', 15000)
ACTION_TIMEOUT = int(PAGE_TIMEOUT / 2)
WORKER_RETRY_COUNT = 1

RESOURCE_BLOCKLIST = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "adservice.google.com", "facebook.net", "fbcdn.net", "analytics.tiktok.com",
]

# Create output dir upon initialization
os.makedirs(OUTPUT_DIR, exist_ok=True)
