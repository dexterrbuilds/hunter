"""Credential-safe logging utilities for Hunter."""

import logging
from atexit import register
from copy import copy
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue

from utils.redaction import sanitize_text

# Global dict to store loggers
_loggers: dict[str, logging.Logger] = {}
_queue_listener: QueueListener | None = None
_queue_handler: QueueHandler | None = None


class RedactingFormatter(logging.Formatter):
    """Format a complete log record, then remove sensitive values."""

    def format(self, record: logging.LogRecord) -> str:
        """Return a sanitized rendering, including exception text."""
        return sanitize_text(super().format(record))


class RedactingQueueHandler(QueueHandler):
    """Sanitize a prepared record before asynchronous handoff."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler.prepare formats exception/stack text synchronously. Keep
        # those objects for the listener thread and only render the cheap message
        # interpolation before handoff.
        prepared = copy(record)
        prepared.msg = sanitize_text(record.getMessage())
        prepared.args = None
        return prepared


def _formatter() -> RedactingFormatter:
    return RedactingFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def configure_safe_console_logging(level: int = logging.INFO) -> None:
    """Apply redaction to every root handler and ensure a console handler exists."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())
    formatter = _formatter()
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get or create a logger with the given name.

    Args:
        name: Logger name, typically __name__
        level: Logging level

    Returns:
        Configured logger
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(level)

    _loggers[name] = logger
    return logger


def setup_file_logging(filename: str = "hunter.log", level: int = logging.INFO) -> None:
    """Set up file logging for all loggers.

    Args:
        filename: Log file path
        level: Logging level for file handler
    """
    root_logger = logging.getLogger()

    # Check if file handler with same filename already exists
    for handler in root_logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and handler.baseFilename == filename
        ):
            return  # File handler already added

    file_handler = logging.FileHandler(filename)
    file_handler.setLevel(level)
    file_handler.setFormatter(_formatter())

    root_logger.addHandler(file_handler)


def enable_async_logging() -> None:
    """Move formatted log I/O off latency-sensitive asyncio tasks."""
    global _queue_handler, _queue_listener
    if _queue_listener is not None:
        return
    root_logger = logging.getLogger()
    downstream = tuple(root_logger.handlers)
    if not downstream:
        console = logging.StreamHandler()
        console.setFormatter(_formatter())
        downstream = (console,)
    queue: SimpleQueue[logging.LogRecord] = SimpleQueue()
    queue_handler = RedactingQueueHandler(queue)
    # Redact before the record leaves the caller thread. Downstream handlers
    # retain the formatter as defense in depth.
    root_logger.handlers.clear()
    root_logger.addHandler(queue_handler)
    listener = QueueListener(queue, *downstream, respect_handler_level=True)
    listener.start()
    _queue_handler = queue_handler
    _queue_listener = listener


def shutdown_async_logging() -> None:
    """Flush queued records during orderly process shutdown."""
    global _queue_handler, _queue_listener
    if _queue_listener is not None:
        _queue_listener.stop()
    _queue_listener = None
    _queue_handler = None


register(shutdown_async_logging)
