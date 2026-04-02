from __future__ import annotations

import datetime
import logging
from logging.handlers import RotatingFileHandler

from core.config import Settings

app_logger = logging.getLogger("app")
app_logger.addHandler(logging.NullHandler())

_configured_signature: tuple[bool, str, str] | None = None


class LocalTimeFormatter(logging.Formatter):
    """Formatter that converts timestamps to the configured local timezone."""

    def __init__(self, timezone, fmt: str):
        super().__init__(fmt)
        self._timezone = timezone

    def converter(self, ts: float):
        dt = datetime.datetime.fromtimestamp(ts, self._timezone)
        return dt.timetuple()


def configure_logging(settings: Settings):
    global _configured_signature

    signature = (settings.debug_mode, settings.local_timezone_name, settings.app_log_file)
    if _configured_signature == signature and app_logger.handlers:
        return app_logger

    for handler in list(app_logger.handlers):
        app_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    app_logger.setLevel(logging.DEBUG if settings.debug_mode else logging.INFO)

    formatter = LocalTimeFormatter(settings.local_timezone, "%(asctime)s %(levelname)s %(message)s")

    app_file = RotatingFileHandler(settings.app_log_file, maxBytes=10**7, backupCount=5)
    app_file.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    app_logger.addHandler(app_file)
    app_logger.addHandler(console)
    _configured_signature = signature
    return app_logger
