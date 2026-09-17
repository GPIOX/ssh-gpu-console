"""Small structured-logging boundary for the local control plane."""

from __future__ import annotations

import logging


def configure_logging(level: int = logging.INFO) -> None:
    """Configure stdout/stderr logging once; no application log files."""

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
