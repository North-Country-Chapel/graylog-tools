import logging
import datetime
import os
from logging.handlers import RotatingFileHandler

try:
    from pygelf import GelfUdpHandler
    _PYGELF_AVAILABLE = True
except ImportError:
    _PYGELF_AVAILABLE = False

_GELF_HOST = os.getenv("GELF_HOST", "10.10.0.77")
_GELF_PORT = int(os.getenv("GELF_PORT", "12201"))


class _NotifyingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that sends a GELF message when an archive is created."""

    def __init__(self, *args, gelf_handler, script_name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._gelf_handler = gelf_handler
        self._script_name = script_name

    def doRollover(self):
        archive = self.rotation_filename(self.baseFilename + ".1")
        super().doRollover()
        if self._gelf_handler is None:
            return
        try:
            record = logging.LogRecord(
                name=self._script_name,
                level=logging.INFO,
                pathname=self.baseFilename,
                lineno=0,
                msg=f"Log rotated — archive created: {archive}",
                args=(),
                exc_info=None,
            )
            record.__dict__["script"] = self._script_name
            record.__dict__["archive"] = archive
            self._gelf_handler.emit(record)
        except Exception:
            pass


def get_logger(script_name: str, caller_file: str = None, log_file: str = None, file_level=logging.INFO) -> logging.Logger:
    """
    Build and return a logger with a dated file handler and a Graylog GELF handler.

    Args:
        script_name:  Identifier used as the logger name and included in GELF messages.
        caller_file:  Pass __file__ from the calling script so logs are written next to it.
        log_file:     Override the full log file path. If omitted, defaults to
                      <caller's directory>/<script_name>_YYYY-MM-DD.log
        file_level:   Level for the file handler (default INFO). Logger itself is always DEBUG
                      so GELF receives everything.

    Usage:
        from logger import get_logger
        logger = get_logger("my_script", __file__)
        logger.info("Starting", extra={"script": "my_script"})

    Environment variables:
        GELF_HOST  — Graylog host (default: 10.10.0.77)
        GELF_PORT  — Graylog UDP port (default: 12201)
    """
    today = str(datetime.date.today())

    if log_file is None:
        base_dir = os.path.dirname(os.path.abspath(caller_file)) if caller_file else os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(base_dir, f"{script_name}.log")

    logger = logging.getLogger(script_name)

    # Avoid adding duplicate handlers if get_logger is called more than once
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # GELF handler — built first so it can be passed to the file handler for rotation notices
    gelf_handler = None
    if _PYGELF_AVAILABLE:
        try:
            gelf_handler = GelfUdpHandler(
                host=_GELF_HOST,
                port=_GELF_PORT,
                include_extra_fields=True,
            )
            logger.addHandler(gelf_handler)
        except Exception as e:
            logger.warning(f"GELF handler could not be created, logging to file only: {e}")

    # File handler — appends, rotates at 100 MB, keeps 1 backup, notifies Graylog on rotation
    file_handler = _NotifyingRotatingFileHandler(
        log_file, maxBytes=100 * 1024 * 1024, backupCount=1,
        gelf_handler=gelf_handler, script_name=script_name,
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if not _PYGELF_AVAILABLE:
        logger.warning("pygelf is not installed — logging to file only. Install with: pip install pygelf")

    return logger
