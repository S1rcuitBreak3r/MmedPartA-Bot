"""
Typing / chat-action indicator (spec §8, §14). Telegram's sendChatAction is visible
for only ~4-5 seconds, so to keep it up across a longer wait (a Claude generation call,
a PDF build) it must be re-sent on a loop. This async context manager does exactly that
and cleans itself up on exit — success or exception.

Usage:
    async with typing_indicator(bot, chat_id):
        data = await slow_thing()
    async with typing_indicator(bot, chat_id, ChatAction.UPLOAD_DOCUMENT):
        pdf = build_pdf()
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram.constants import ChatAction

from config import TYPING_REFRESH_SECONDS

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def typing_indicator(bot, chat_id: int, action: str = ChatAction.TYPING):
    async def _loop():
        try:
            while True:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=action)
                except Exception as exc:  # noqa: BLE001 - the indicator is best-effort, never fatal
                    logger.debug("send_chat_action failed (non-fatal): %s", exc)
                await asyncio.sleep(TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
