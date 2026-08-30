"""Validated infrastructure profiles and low-latency feed configuration."""

# Configuration parsing favors direct field-level error messages.
# ruff: noqa: FBT001, FBT003, TRY003

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from utils.redaction import register_config_secrets


class InfrastructureProfile(StrEnum):
    """Operational profiles; none of them enable trading by themselves."""

    STANDARD = "standard"
    BALANCED = "balanced"
    MAXIMUM_PERFORMANCE = "maximum_performance"


class FeedKind(StrEnum):
    """Supported creation-event transports."""

    RABBITSTREAM = "rabbitstream"
    TRITON_SHREDS = "triton_shreds"
    RIPTIDE = "riptide"
    YELLOWSTONE = "yellowstone"
    LOGS = "logs"
    BLOCKS = "blocks"
    PUMPPORTAL = "pumpportal"


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """One credential-bearing feed plus non-sensitive routing metadata."""

    feed_id: str
    kind: FeedKind
    endpoint: str = field(repr=False)
    token: str | None = field(default=None, repr=False)
    auth_type: str = "x-token"
    region: str | None = None
    required: bool = False
    enabled: bool = True
    queue_size: int = 1_024
    reconnect_delay_seconds: float = 1.0
    commitment: str = "processed"

    def __post_init__(self) -> None:
        if not self.feed_id or any(char.isspace() for char in self.feed_id):
            raise ValueError("feed id must be stable and non-blank")
        if not self.endpoint:
            raise ValueError(f"feed {self.feed_id} requires an endpoint")
        if self.queue_size <= 0:
            raise ValueError("feed queue_size must be positive")
        if self.reconnect_delay_seconds <= 0:
            raise ValueError("feed reconnect delay must be positive")
        if self.commitment not in {"processed", "confirmed", "finalized"}:
            raise ValueError("feed commitment is invalid")
        if (
            self.kind
            in {
                FeedKind.RABBITSTREAM,
                FeedKind.TRITON_SHREDS,
                FeedKind.RIPTIDE,
                FeedKind.YELLOWSTONE,
            }
            and not self.token
        ):
            raise ValueError(f"feed {self.feed_id} requires a credential token")
        register_config_secrets({"endpoint": self.endpoint, "token": self.token})


@dataclass(frozen=True, slots=True)
class InfrastructureConfig:
    """Maximum-performance runtime behavior, independent from trade settings."""

    profile: InfrastructureProfile = InfrastructureProfile.STANDARD
    region: str | None = None
    allow_degraded: bool = False
    maximum_blockhash_age_ms: int = 5_000
    claim_ttl_seconds: float = 600.0
    observation_queue_size: int = 4_096
    telemetry_queue_size: int = 8_192
    feeds: tuple[FeedConfig, ...] = ()

    def __post_init__(self) -> None:
        if self.maximum_blockhash_age_ms <= 0:
            raise ValueError("maximum blockhash age must be positive")
        if self.claim_ttl_seconds <= 0:
            raise ValueError("claim TTL must be positive")
        if self.observation_queue_size <= 0 or self.telemetry_queue_size <= 0:
            raise ValueError("infrastructure queue sizes must be positive")
        identifiers = [feed.feed_id for feed in self.feeds]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("feed IDs must be unique")
        if self.profile == InfrastructureProfile.MAXIMUM_PERFORMANCE and not any(
            feed.enabled for feed in self.feeds
        ):
            raise ValueError("maximum_performance requires at least one enabled feed")


def infrastructure_config_from_dict(
    value: dict[str, Any] | None,
) -> InfrastructureConfig:
    """Parse infrastructure settings without reading environment defaults."""
    if not value:
        return InfrastructureConfig()
    try:
        profile = InfrastructureProfile(value.get("profile", "standard"))
    except ValueError as error:
        raise ValueError(
            "infrastructure.profile must be standard, balanced, or maximum_performance"
        ) from error
    feeds_value = value.get("feeds", [])
    if not isinstance(feeds_value, list):
        raise TypeError("infrastructure.feeds must be a list")
    feeds = tuple(_feed_from_dict(item) for item in feeds_value)
    return InfrastructureConfig(
        profile=profile,
        region=_optional_string(value, "region"),
        allow_degraded=_boolean(value, "allow_degraded", False),
        maximum_blockhash_age_ms=_positive_int(
            value, "maximum_blockhash_age_ms", 5_000
        ),
        claim_ttl_seconds=_positive_number(value, "claim_ttl_seconds", 600.0),
        observation_queue_size=_positive_int(value, "observation_queue_size", 4_096),
        telemetry_queue_size=_positive_int(value, "telemetry_queue_size", 8_192),
        feeds=feeds,
    )


def _feed_from_dict(value: object) -> FeedConfig:
    if not isinstance(value, dict):
        raise TypeError("each infrastructure feed must be a mapping")
    try:
        kind = FeedKind(value["kind"])
        feed_id = str(value["id"])
        endpoint = str(value["endpoint"])
    except KeyError as error:
        raise ValueError(f"infrastructure feed is missing {error.args[0]}") from error
    return FeedConfig(
        feed_id=feed_id,
        kind=kind,
        endpoint=endpoint,
        token=_optional_string(value, "token"),
        auth_type=str(value.get("auth_type", "x-token")),
        region=_optional_string(value, "region"),
        required=_boolean(value, "required", False),
        enabled=_boolean(value, "enabled", True),
        queue_size=_positive_int(value, "queue_size", 1_024),
        reconnect_delay_seconds=_positive_number(value, "reconnect_delay_seconds", 1.0),
        commitment=str(value.get("commitment", "processed")),
    )


def _boolean(value: dict[str, Any], key: str, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise TypeError(f"infrastructure.{key} must be true or false")
    return item


def _positive_int(value: dict[str, Any], key: str, default: int) -> int:
    item = value.get(key, default)
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise ValueError(f"infrastructure.{key} must be a positive integer")
    return item


def _positive_number(value: dict[str, Any], key: str, default: float) -> float:
    item = value.get(key, default)
    if isinstance(item, bool) or not isinstance(item, int | float) or item <= 0:
        raise ValueError(f"infrastructure.{key} must be positive")
    return float(item)


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise TypeError(f"infrastructure.{key} must be a string")
    return item or None
