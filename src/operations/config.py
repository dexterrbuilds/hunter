"""Restricted production profile extending the existing YAML loader."""

# Field-only validation errors are deliberately actionable.
# ruff: noqa: TRY003, FBT001

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_MAX_PORT = 65535


class DeploymentConfigError(ValueError):
    """A field-only diagnostic that never embeds configuration values."""


def require(condition: bool, field: str) -> None:
    """Reject invalid settings without including their values."""
    if not condition:
        raise DeploymentConfigError(f"Missing, invalid or unsupported setting: {field}")


def endpoint(value: object, field: str, schemes: set[str]) -> None:
    """Validate URL syntax, port and placeholders without DNS requests."""
    require(isinstance(value, str), field)
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in schemes
            and bool(parsed.hostname)
            and not parsed.fragment
            and not any(char.isspace() for char in value)
            and (parsed.port is None or 1 <= parsed.port <= _MAX_PORT)
        )
    except ValueError:
        valid = False
    require(valid and not placeholder(value), field)


def placeholder(value: str) -> bool:
    """Recognize explicit template values; never guess credential validity."""
    return any(
        marker in value.lower()
        for marker in (
            "${",
            "your_",
            "your-",
            "changeme",
            "replace_me",
            "example.com",
            "<",
            ">",
        )
    )


def number(value: object, field: str, low: float, high: float) -> None:
    """Reject booleans, NaN, infinities and out-of-range numerics."""
    require(
        type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
        field,
    )


def validate_persistent_path(path: Path) -> None:
    """Require absolute durable paths, not known temporary locations."""
    require(
        path.is_absolute()
        and ".." not in path.parts
        and path.suffix in {".sqlite3", ".sqlite", ".db"},
        "storage.shadow_database_path",
    )
    require(
        not any(part in {"tmp", "temp"} for part in path.parts),
        "storage.shadow_database_path (must be persistent)",
    )


def resolve_deployment_env(config: dict[str, Any]) -> None:
    """Resolve active production fields, leaving disabled credentials alone."""
    for key, value in config.items():
        if isinstance(value, dict):
            if value.get("enabled") is not False:
                resolve_deployment_env(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("enabled") is not False:
                    resolve_deployment_env(item)
        elif isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            name = value[2:-1]
            require(
                bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)),
                "environment variable reference",
            )
            if name not in os.environ:
                raise DeploymentConfigError(f"Environment variable '{name}' not found")
            config[key] = os.environ[name]


def validate_deployment(config: dict[str, Any]) -> None:
    """Validate the M6 shadow subset before any wallet or service construction.

    Live deployment is deliberately unsupported by this preparation profile.
    Legacy development configurations without a deployment section retain their
    existing validation and authorization semantics.
    """
    allowed_root = {
        "name",
        "deployment",
        "storage",
        "risk",
        "platform",
        "filters",
        "wss_endpoint",
        "rpc_endpoint",
        "pumpportal",
        "execution",
        "telegram",
        "terminal_ui",
        "wallet_tracking",
        "wallet_fleet",
        "token_launch",
        "cleanup",
    }
    require(
        not set(config) - allowed_root, "production config (unknown/unsupported keys)"
    )
    for key in ("risk", "storage", "filters", "pumpportal", "cleanup"):
        require(isinstance(config.get(key, {}), dict), key)
    for key, keys in {
        "risk": {"enforce", "trading_enabled"},
        "storage": {"shadow_database_path"},
        "filters": {"listener_type"},
        "pumpportal": {"url"},
        "cleanup": {"mode"},
    }.items():
        require(
            not set(config.get(key, {})) - keys, f"{key} (unknown/unsupported keys)"
        )
    deployment = config.get("deployment")
    require(isinstance(deployment, dict), "deployment")
    allowed = {
        "environment",
        "mode",
        "live_trading_opt_in",
        "region_label",
        "stale_after_seconds",
        "report_interval_seconds",
        "shutdown_timeout_seconds",
        "queue_size",
        "minimum_free_bytes",
    }
    require(not set(deployment) - allowed, "deployment (unknown keys)")
    require(deployment.get("environment") == "production", "deployment.environment")
    require(
        deployment.get("mode", "shadow") == "shadow",
        "deployment.mode (M6 supports shadow only)",
    )
    require(
        deployment.get("live_trading_opt_in", False) is False,
        "deployment.live_trading_opt_in",
    )
    require(config.get("risk", {}).get("enforce") is True, "risk.enforce")
    require(
        config.get("risk", {}).get("trading_enabled", False) is False,
        "risk.trading_enabled",
    )
    require(not config.get("private_key"), "private_key (omit in shadow mode)")
    require(not config.get("env_file"), "env_file (use service EnvironmentFile)")
    for feature in (
        "telegram",
        "terminal_ui",
        "wallet_tracking",
        "wallet_fleet",
        "token_launch",
    ):
        value = config.get(feature, {})
        require(
            isinstance(value, dict) and value.get("enabled", False) is False,
            f"{feature}.enabled (unsupported in shadow)",
        )
    require(not config.get("benchmark"), "benchmark (omit in production shadow)")
    require(
        config.get("cleanup", {}).get("mode", "disabled") == "disabled", "cleanup.mode"
    )
    require(
        bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(config.get("name", "")))), "name"
    )
    require(
        bool(
            re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", str(deployment.get("region_label", "amsterdam"))
            )
        ),
        "deployment.region_label",
    )
    for field, default, low, high in (
        ("stale_after_seconds", 120, 1, 3600),
        ("report_interval_seconds", 30, 1, 3600),
        ("shutdown_timeout_seconds", 10, 3, 60),
        ("queue_size", 1024, 1, 65536),
        ("minimum_free_bytes", 100_000_000, 0, 10**15),
    ):
        number(deployment.get(field, default), f"deployment.{field}", low, high)
    require(type(deployment.get("queue_size", 1024)) is int, "deployment.queue_size")
    storage = config.get("storage", {})
    require(isinstance(storage, dict), "storage")
    path = Path(str(storage.get("shadow_database_path", "")))
    validate_persistent_path(path)
    require(
        "database_path" not in storage,
        "storage.database_path (use a separate shadow database)",
    )
    require(config.get("platform", "pump_fun") in {"pump_fun", "lets_bonk"}, "platform")
    listener = config.get("filters", {}).get("listener_type")
    require(
        listener in {"logs", "blocks", "pumpportal"},
        "filters.listener_type (M6 shadow: logs, blocks, pumpportal)",
    )
    require(
        not (listener == "logs" and config.get("platform") == "lets_bonk"),
        "filters.listener_type (platform pairing)",
    )
    if listener in {"logs", "blocks"}:
        endpoint(config.get("wss_endpoint"), "wss_endpoint", {"wss"})
    else:
        endpoint(config.get("pumpportal", {}).get("url"), "pumpportal.url", {"wss"})
    if "rpc_endpoint" in config:
        endpoint(config["rpc_endpoint"], "rpc_endpoint", {"https"})
    _validate_providers(config)


def _validate_providers(config: dict[str, Any]) -> None:
    """Validate declared adapters but never initialize their transports."""
    from execution.providers.factory import routing_config_from_dict  # noqa: PLC0415

    try:
        routing = routing_config_from_dict(config.get("execution"))
        for provider in routing.providers:
            if provider.enabled:
                endpoint(
                    provider.endpoint, "execution.providers.endpoint", {"https", "wss"}
                )
                number(
                    provider.request_timeout_ms,
                    "execution.providers.request_timeout_ms",
                    50,
                    60000,
                )
                number(provider.max_retries, "execution.providers.max_retries", 0, 100)
                for value in provider.headers.values():
                    require(
                        bool(value) and not placeholder(value),
                        "execution.providers.headers",
                    )
                if provider.warmup_endpoint:
                    endpoint(
                        provider.warmup_endpoint,
                        "execution.providers.warmup_endpoint",
                        {"https"},
                    )
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DeploymentConfigError(
            "Invalid setting: execution (provider configuration)"
        ) from None
