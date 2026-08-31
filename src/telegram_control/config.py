"""Fail-closed Telegram configuration without token material."""

# Configuration failures are intentionally direct and operator-actionable.
# ruff: noqa: FBT001, FBT003, S105, TRY003, TRY004

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from utils.redaction import register_secret

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Non-secret configuration for one private-owner Telegram adapter."""

    enabled: bool = False
    owner_user_ids: frozenset[int] = frozenset()
    private_chat_only: bool = True
    token_env_var: str = "TELEGRAM_BOT_TOKEN"
    confirmation_ttl_seconds: float = 45.0
    kill_confirmation_ttl_seconds: float = 20.0
    maximum_pending_actions: int = 64
    notification_queue_size: int = 256
    command_limit: int = 30
    command_interval_seconds: float = 60.0
    startup_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 5.0
    notification_categories: frozenset[str] = frozenset({"all"})


def telegram_config_from_dict(value: object) -> TelegramConfig:
    """Parse and validate Telegram config without resolving its secret token."""
    if value is None:
        return TelegramConfig()
    if not isinstance(value, Mapping):
        raise ValueError("telegram must be a mapping")
    enabled = _boolean(value, "enabled", False)
    owners_raw = value.get("owner_user_ids", [])
    if not isinstance(owners_raw, list | tuple):
        raise ValueError("telegram.owner_user_ids must be a list")
    owners: set[int] = set()
    for owner in owners_raw:
        if isinstance(owner, bool) or not isinstance(owner, int) or owner <= 0:
            raise ValueError("telegram owner IDs must be positive integers")
        owners.add(owner)
    if enabled and not owners:
        raise ValueError("enabled Telegram requires at least one numeric owner ID")
    token_env_var = value.get("token_env_var", "TELEGRAM_BOT_TOKEN")
    if not isinstance(token_env_var, str) or not _ENV_NAME.fullmatch(token_env_var):
        raise ValueError("telegram.token_env_var must be an environment variable name")
    categories = value.get("notification_categories", ["all"])
    if not isinstance(categories, list | tuple | set) or not all(
        isinstance(item, str) and item.strip() for item in categories
    ):
        raise ValueError("telegram.notification_categories must be a string list")
    return TelegramConfig(
        enabled=enabled,
        owner_user_ids=frozenset(owners),
        private_chat_only=_boolean(value, "private_chat_only", True),
        token_env_var=token_env_var,
        confirmation_ttl_seconds=_number(
            value, "confirmation_ttl_seconds", 45.0, minimum=10, maximum=300
        ),
        kill_confirmation_ttl_seconds=_number(
            value, "kill_confirmation_ttl_seconds", 20.0, minimum=5, maximum=60
        ),
        maximum_pending_actions=_integer(
            value, "maximum_pending_actions", 64, minimum=1, maximum=1000
        ),
        notification_queue_size=_integer(
            value, "notification_queue_size", 256, minimum=1, maximum=10_000
        ),
        command_limit=_integer(value, "command_limit", 30, minimum=1, maximum=1000),
        command_interval_seconds=_number(
            value, "command_interval_seconds", 60.0, minimum=1, maximum=3600
        ),
        startup_timeout_seconds=_number(
            value, "startup_timeout_seconds", 10.0, minimum=1, maximum=120
        ),
        shutdown_timeout_seconds=_number(
            value, "shutdown_timeout_seconds", 5.0, minimum=1, maximum=60
        ),
        notification_categories=frozenset(item.strip().lower() for item in categories),
    )


def resolve_bot_token(
    config: TelegramConfig, environment: Mapping[str, str] | None = None
) -> str:
    """Resolve and register the token only when the adapter is enabled."""
    if not config.enabled:
        raise ValueError("Telegram is disabled")
    source = os.environ if environment is None else environment
    token = source.get(config.token_env_var, "").strip()
    if not token:
        raise ValueError(
            f"enabled Telegram requires secret environment variable {config.token_env_var}"
        )
    register_secret(token)
    return token


def _boolean(value: Mapping, key: str, default: bool) -> bool:
    result = value.get(key, default)
    if not isinstance(result, bool):
        raise ValueError(f"telegram.{key} must be true or false")
    return result


def _integer(
    value: Mapping, key: str, default: int, *, minimum: int, maximum: int
) -> int:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(f"telegram.{key} must be an integer")
    if not minimum <= result <= maximum:
        raise ValueError(f"telegram.{key} must be between {minimum} and {maximum}")
    return result


def _number(
    value: Mapping, key: str, default: float, *, minimum: float, maximum: float
) -> float:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int | float):
        raise ValueError(f"telegram.{key} must be numeric")
    if not minimum <= float(result) <= maximum:
        raise ValueError(f"telegram.{key} must be between {minimum} and {maximum}")
    return float(result)
