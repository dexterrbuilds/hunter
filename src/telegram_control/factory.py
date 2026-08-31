"""Optional Telegram composition for the Hunter process runtime."""

# Runtime factory types intentionally document the composition boundary.
# ruff: noqa: TC001

from __future__ import annotations

from collections.abc import Callable, Mapping

from application.runtime import ControlInterfaceFactory
from telegram_control.config import (
    TelegramConfig,
    resolve_bot_token,
    telegram_config_from_dict,
)
from telegram_control.ports import TelegramTransport
from telegram_control.ptb_adapter import PythonTelegramBotTransport
from telegram_control.service import LaunchResolver, TelegramControlService

TransportFactory = Callable[[str], TelegramTransport]


def telegram_control_factories_from_config(
    raw_config: object,
    *,
    environment: Mapping[str, str] | None = None,
    transport_factory: TransportFactory = PythonTelegramBotTransport,
    launch_resolver: LaunchResolver | None = None,
) -> tuple[ControlInterfaceFactory, ...]:
    """Build no runtime object unless Telegram is explicitly enabled."""
    config = telegram_config_from_dict(raw_config)
    if not config.enabled:
        return ()
    token = resolve_bot_token(config, environment)

    def compose(application: object) -> TelegramControlService:
        return TelegramControlService(
            config,
            application,
            transport_factory(token),
            launch_resolver=launch_resolver,
        )

    return (compose,)


def validate_telegram_config(raw_config: object) -> TelegramConfig:
    """Expose non-secret config validation to the central config loader."""
    return telegram_config_from_dict(raw_config)
