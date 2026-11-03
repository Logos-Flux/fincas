"""Log formatting for the release harness server (no Django imports: settings load this).

Every line the harness server writes starts with a UTC ISO-8601 timestamp with
milliseconds, so ``scripts/harness/run.sh`` can slice the log by a step's
time window and fail the step on any 5xx or traceback inside it
(docs/release-testing.md, "server-error capture").
"""

from __future__ import annotations

import datetime
import logging


class IsoUtcFormatter(logging.Formatter):
    """``[2026-10-17T14:03:07.123Z] LEVEL logger message`` plus the traceback, if any."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API)
        when = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
        return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def harness_logging(path: str, base: dict) -> dict:
    """``base`` (the packaged LOGGING dict) plus a file handler writing to ``path``.

    Request lines (``django.server``, what runserver prints), 4xx/5xx
    (``django.request`` at WARNING) and every unhandled exception reach the
    file; the console keeps what it had.
    """
    config = {
        **base,
        "formatters": {**base.get("formatters", {}),
                       "harness": {"()": "config.harness_log.IsoUtcFormatter",
                                   "format": "[{asctime}] {levelname} {name} {message}", "style": "{"}},
        "handlers": {**base.get("handlers", {}),
                     "harness_file": {"class": "logging.FileHandler", "filename": path,
                                      "formatter": "harness", "encoding": "utf-8"}},
    }
    root = dict(config.get("root", {}))
    root["handlers"] = [*root.get("handlers", []), "harness_file"]
    config["root"] = root
    loggers = dict(config.get("loggers", {}))
    # 4xx are WARNING, 5xx ERROR in django.request; the packaged config keeps only ERROR.
    loggers["django.request"] = {"level": "WARNING", "propagate": True}
    # runserver's request lines: Django's default config gives this logger its
    # own console handler and no propagation, so add the file explicitly.
    loggers["django.server"] = {"handlers": ["console", "harness_file"], "level": "INFO", "propagate": False}
    config["loggers"] = loggers
    return config
