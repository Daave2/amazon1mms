import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PYTEST_ARTIFACTS_DIR = PROJECT_ROOT / ".pytest_artifacts"
PYTEST_OUTPUT_DIR = PYTEST_ARTIFACTS_DIR / "output"
PYTEST_ARTIFACTS_DIR.mkdir(exist_ok=True)
PYTEST_OUTPUT_DIR.mkdir(exist_ok=True)

os.environ.setdefault("LOGIN_URL", "https://example.com/signin")
os.environ.setdefault("LOGIN_EMAIL", "tester@example.com")
os.environ.setdefault("LOGIN_PASSWORD", "password")
os.environ.setdefault("OTP_SECRET_KEY", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("TARGET_URL", "https://example.com/dashboard")
os.environ.setdefault("FORM_POST_URL", "https://example.com/form")
os.environ.setdefault("OUTPUT_DIR", str(PYTEST_OUTPUT_DIR))
os.environ.setdefault("APP_LOG_FILE", str(PYTEST_ARTIFACTS_DIR / "app.log"))
os.environ.setdefault("STORAGE_STATE", str(PYTEST_ARTIFACTS_DIR / "state.json"))
