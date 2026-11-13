"""Logging configuration for the Netpulse daemon."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_file: str, level: str = "INFO", console: bool = None):
    """Attach a rotating file handler and, for interactive runs, a console one.

    Never raises: a daemon that cannot open its log file still runs, with a
    single warning on stderr, rather than dying at startup.
    """
    logger = logging.getLogger("netpulse")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if any(getattr(handler, "_netpulse", False) for handler in logger.handlers):
        return logger  # already configured (tests, repeat main() calls)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=2,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            fh._netpulse = True
            logger.addHandler(fh)
        except OSError as e:
            sys.stderr.write(f"[!] Cannot open log file {log_file}: {e}\n")

    if console is None:
        console = sys.stderr.isatty()
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh._netpulse = True
        logger.addHandler(sh)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger
