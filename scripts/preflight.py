import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.preflight import emit_preflight_report, run_preflight


def main() -> int:
    result = run_preflight()
    return emit_preflight_report(result)


if __name__ == "__main__":
    raise SystemExit(main())
