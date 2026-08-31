"""Explicit asyncio lifecycle adapter for python-telegram-bot."""

# The library API uses a positional boolean and runtime-visible callback types.
# ruff: noqa: FBT003, TC003, TRY003

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from telegram_control.models import IncomingCallback, IncomingMessage, MessageButton


class PythonTelegramBotTransport:
    """Own polling and Bot API resources without leaking them into Hunter."""

    def __init__(self, token: str) -> None:
        self._application: Application[Any, Any, Any, Any, Any, Any] = (
            ApplicationBuilder().token(token).concurrent_updates(False).build()
        )
        self._initialized = False
        self._started = False

    async def start(
        self,
        message_handler: Callable[[IncomingMessage], Awaitable[None]],
        callback_handler: Callable[[IncomingCallback], Awaitable[None]],
        error_handler: Callable[[BaseException], None],
    ) -> None:
        async def on_message(update: Update, _context: CallbackContext) -> None:
            message = update.effective_message
            user = update.effective_user
            chat = update.effective_chat
            if message is None or user is None or chat is None or message.text is None:
                return
            await message_handler(
                IncomingMessage(
                    user_id=user.id,
                    chat_id=chat.id,
                    chat_type=chat.type,
                    text=message.text,
                    username=user.username,
                )
            )

        async def on_callback(update: Update, _context: CallbackContext) -> None:
            query = update.callback_query
            user = update.effective_user
            chat = update.effective_chat
            if query is None or user is None or chat is None or query.data is None:
                return
            await callback_handler(
                IncomingCallback(
                    callback_id=query.id,
                    user_id=user.id,
                    chat_id=chat.id,
                    chat_type=chat.type,
                    data=query.data,
                    username=user.username,
                )
            )

        async def on_error(_update: object, context: CallbackContext) -> None:
            error = context.error
            error_handler(error if isinstance(error, BaseException) else RuntimeError())

        self._application.add_handler(MessageHandler(filters.COMMAND, on_message))
        self._application.add_handler(CallbackQueryHandler(on_callback))
        self._application.add_error_handler(on_error)
        await self._application.initialize()
        self._initialized = True
        await self._application.start()
        updater = self._application.updater
        if updater is None:
            raise RuntimeError("Telegram polling updater is unavailable")
        await updater.start_polling(
            allowed_updates=("message", "callback_query"),
            drop_pending_updates=True,
        )
        self._started = True

    async def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[MessageButton] = (),
    ) -> None:
        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            item.label, callback_data=item.callback_data
                        )
                        for item in buttons
                    ]
                ]
            )
        await self._application.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._application.bot.answer_callback_query(callback_id, text=text)

    async def close(self) -> None:
        updater = self._application.updater
        if updater is not None and updater.running:
            await updater.stop()
        if self._application.running:
            await self._application.stop()
        if self._initialized:
            await self._application.shutdown()
        self._initialized = False
        self._started = False
