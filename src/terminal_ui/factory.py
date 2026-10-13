"""Lazy optional terminal composition for Hunter's process runtime."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING

from terminal_ui.config import TerminalUIConfig, terminal_ui_config_from_dict
from terminal_ui.ports import TerminalDriver
from terminal_ui.service import TerminalControlService

if TYPE_CHECKING:
    from application.runtime import ControlInterfaceFactory

DriverFactory = Callable[[], TerminalDriver]


def terminal_ui_factories_from_config(
    raw_config: object,
    *,
    driver_factory: DriverFactory | None = None,
) -> tuple[ControlInterfaceFactory, ...]:
    """Return no service and import no Textual module while disabled."""
    config = terminal_ui_config_from_dict(raw_config)
    if not config.enabled:
        return ()

    def compose(application: object) -> TerminalControlService:
        factory = driver_factory
        if factory is None:
            factory = import_module("terminal_ui.textual_adapter").TextualTerminalDriver
        return TerminalControlService(config, application, factory())

    return (compose,)


def validate_terminal_ui_config(raw_config: object) -> TerminalUIConfig:
    """Expose config validation without constructing a Textual application."""
    return terminal_ui_config_from_dict(raw_config)
