"""Logging: console plus a rotating file, with run_id stamped on every record."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s"


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str = "-"):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        return True


def setup_logging(logs_root: Path | None = None, level: str = "INFO",
                  run_id: str = "-") -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    flt = _RunIdFilter(run_id)
    fmt = logging.Formatter(_FMT, datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(flt)
    root.addHandler(console)

    if logs_root:
        logs_root.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            logs_root / "screener.log", maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(flt)
        root.addHandler(fh)

    # requests/urllib3 are chatty at INFO during retries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
