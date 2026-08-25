"""Credential redaction helpers for Hunter logs and telemetry."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from threading import RLock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api-token",
    "api_token",
    "authorization",
    "bot_token",
    "credential",
    "geyser_token",
    "password",
    "private_key",
    "secret",
    "session",
    "telegram_token",
    "token",
    "wallet_secret",
)
_PLACEHOLDER_MARKERS = ("YOUR_", "PLACEHOLDER", "...")
_URL_PATTERN = re.compile(r"\b(?:https?|wss?|grpc)://[^\s<>\"']+", re.IGNORECASE)
_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|api[_-]?token|authorization|bot[_-]?token|"
    r"credential|geyser[_-]?token|password|private[_-]?key|secret|"
    r"telegram[_-]?token|wallet[_-]?secret)[\"']?\s*[:=]\s*[\"']?)([^\s,}&\"']+)"
)
_PATH_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")

_known_secrets: set[str] = set()
_secret_lock = RLock()


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part.replace("-", "_") in normalized for part in _SENSITIVE_KEY_PARTS)


def register_secret(value: object) -> None:
    """Register a configured secret for exact-value removal from future logs."""
    if value is None:
        return
    secret = str(value).strip()
    if len(secret) < 4:
        return
    upper = secret.upper()
    if any(marker in upper for marker in _PLACEHOLDER_MARKERS):
        return
    with _secret_lock:
        _known_secrets.add(secret)


def register_config_secrets(config: Mapping[str, object]) -> None:
    """Register sensitive scalar values and credential-bearing endpoints.

    Endpoint values are registered in full because provider API keys may live
    in userinfo, query parameters, or opaque URL paths.
    """

    def walk(value: object, key: str = "") -> None:
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                walk(nested_value, str(nested_key))
            return
        if isinstance(value, list | tuple | set):
            for nested_value in value:
                walk(nested_value, key)
            return
        if value is None:
            return
        normalized_key = key.lower()
        if _is_sensitive_key(key) or "endpoint" in normalized_key:
            register_secret(value)

    walk(config)


def redact_url(url: str) -> str:
    """Remove credentials and likely API-key material from a URL."""
    trailing = ""
    while url and url[-1] in ".,;)]}":
        trailing = url[-1] + trailing
        url = url[:-1]

    try:
        parts = urlsplit(url)
        hostname = parts.hostname or "unknown-host"
        netloc = hostname
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"

        path_segments = []
        for segment in parts.path.split("/"):
            if _PATH_SECRET_PATTERN.fullmatch(segment):
                path_segments.append(REDACTED)
            else:
                path_segments.append(segment)
        path = "/".join(path_segments)

        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, REDACTED if _is_sensitive_key(key) else value))

        safe = urlunsplit((parts.scheme, netloc, path, urlencode(query), ""))
        return safe + trailing
    except (TypeError, ValueError):
        return REDACTED + trailing


def sanitize_text(value: object) -> str:
    """Return a string with configured and recognizable credentials removed."""
    text = str(value)
    with _secret_lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, REDACTED)

    text = _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    text = _TELEGRAM_TOKEN_PATTERN.sub(REDACTED, text)
    text = _ASSIGNMENT_PATTERN.sub(lambda match: match.group(1) + REDACTED, text)
    return text


def endpoint_identifier(endpoint: str | None) -> str:
    """Create a stable credential-free identifier for an endpoint."""
    if not endpoint:
        return "unconfigured"
    raw = str(endpoint).strip()
    parseable = raw if "://" in raw else f"grpc://{raw}"
    try:
        parts = urlsplit(parseable)
        host = parts.hostname or "unknown-host"
        authority = f"{parts.scheme}://{host}"
        if parts.port is not None:
            authority = f"{authority}:{parts.port}"
    except (TypeError, ValueError):
        authority = "endpoint"
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{authority}#{fingerprint}"


def clear_registered_secrets() -> None:
    """Clear the registry for isolated tests."""
    with _secret_lock:
        _known_secrets.clear()
