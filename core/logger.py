import datetime
import logging
from logging.handlers import RotatingFileHandler

from core.config import DEBUG_MODE, LOCAL_TIMEZONE


class LocalTimeFormatter(logging.Formatter):
    """Formatter that converts timestamps to ``LOCAL_TIMEZONE``."""

    def converter(self, ts: float):
        dt = datetime.datetime.fromtimestamp(ts, LOCAL_TIMEZONE)
        return dt.timetuple()


def setup_logging():
    """Configure application logging to file and console."""
    app_logger = logging.getLogger("app")
    # Prevent adding multiple handlers if called multiple times
    if not app_logger.handlers:
        app_logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
        app_file = RotatingFileHandler("app.log", maxBytes=10**7, backupCount=5)
        fmt = LocalTimeFormatter("%(asctime)s %(levelname)s %(message)s")
        app_file.setFormatter(fmt)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        app_logger.addHandler(app_file)
        app_logger.addHandler(console)
    return app_logger


# Singleton logger instance
app_logger = setup_logging()
