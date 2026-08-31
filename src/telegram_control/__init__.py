"""Secure optional Telegram control adapter for Hunter."""

from telegram_control.config import TelegramConfig, telegram_config_from_dict
from telegram_control.service import TelegramControlService

__all__ = ["TelegramConfig", "TelegramControlService", "telegram_config_from_dict"]
