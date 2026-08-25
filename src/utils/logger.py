"""Credential-safe logging utilities for Hunter."""

import logging

from utils.redaction import sanitize_text

# Global dict to store loggers
_loggers: dict[str, logging.Logger] = {}


class RedactingFormatter(logging.Formatter):
    """Format a complete log record, then remove sensitive values."""

    def format(self, record: logging.LogRecord) -> str:
        """Return a sanitized rendering, including exception text."""
        return sanitize_text(super().format(record))


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


def setup_file_logging(
    filename: str = "hunter.log", level: int = logging.INFO
) -> None:
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
