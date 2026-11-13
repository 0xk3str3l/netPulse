"""Logging configuration for the Netpulse daemon."""

import logging


def setup_logging(log_file: str, level: str = "INFO"):
    logger = logging.getLogger("netpulse")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh = logging.FileHandler(log_file) if log_file else logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger
