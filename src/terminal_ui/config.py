"""Configuration for the optional local-terminal interface."""

# Validation errors are deliberately direct and operator-actionable.
# ruff: noqa: FBT001, FBT003, TRY003, TRY004

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalUIConfig:
    """Bounded refresh and history settings for one terminal interface."""

    enabled: bool = False
    refresh_interval_seconds: float = 1.0
    balance_refresh_seconds: float = 30.0
    startup_timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 3.0
    event_queue_size: int = 256
    event_history_size: int = 100
    position_page_size: int = 25
    execution_page_size: int = 50
    telemetry_sample_size: int = 200
    stale_after_seconds: float = 60.0


def terminal_ui_config_from_dict(value: object) -> TerminalUIConfig:
    """Parse terminal settings without constructing the UI framework."""
    if value is None:
        return TerminalUIConfig()
    if not isinstance(value, Mapping):
        raise ValueError("terminal_ui must be a mapping")
    return TerminalUIConfig(
        enabled=_boolean(value, "enabled", False),
        refresh_interval_seconds=_number(
            value, "refresh_interval_seconds", 1.0, minimum=0.25, maximum=60
        ),
        balance_refresh_seconds=_number(
            value, "balance_refresh_seconds", 30.0, minimum=5, maximum=3600
        ),
        startup_timeout_seconds=_number(
            value, "startup_timeout_seconds", 5.0, minimum=1, maximum=60
        ),
        shutdown_timeout_seconds=_number(
            value, "shutdown_timeout_seconds", 3.0, minimum=1, maximum=30
        ),
        event_queue_size=_integer(
            value, "event_queue_size", 256, minimum=8, maximum=10_000
        ),
        event_history_size=_integer(
            value, "event_history_size", 100, minimum=10, maximum=2_000
        ),
        position_page_size=_integer(
            value, "position_page_size", 25, minimum=1, maximum=100
        ),
        execution_page_size=_integer(
            value, "execution_page_size", 50, minimum=1, maximum=200
        ),
        telemetry_sample_size=_integer(
            value, "telemetry_sample_size", 200, minimum=1, maximum=1_000
        ),
        stale_after_seconds=_number(
            value, "stale_after_seconds", 60.0, minimum=1, maximum=600
        ),
    )


def _boolean(value: Mapping, key: str, default: bool) -> bool:
    result = value.get(key, default)
    if not isinstance(result, bool):
        raise ValueError(f"terminal_ui.{key} must be true or false")
    return result


def _integer(
    value: Mapping, key: str, default: int, *, minimum: int, maximum: int
) -> int:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(f"terminal_ui.{key} must be an integer")
    if not minimum <= result <= maximum:
        raise ValueError(f"terminal_ui.{key} must be between {minimum} and {maximum}")
    return result


def _number(
    value: Mapping, key: str, default: float, *, minimum: float, maximum: float
) -> float:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int | float):
        raise ValueError(f"terminal_ui.{key} must be numeric")
    if not minimum <= float(result) <= maximum:
        raise ValueError(f"terminal_ui.{key} must be between {minimum} and {maximum}")
    return float(result)
