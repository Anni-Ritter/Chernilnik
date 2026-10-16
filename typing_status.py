"""Keep Telegram's 'typing...' indicator alive during a long API response."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

log = logging.getLogger(__name__)


@asynccontextmanager
async def typing_during(bot, chat_id, *, every: float = 4.0):
    stop = asyncio.Event()

    async def pulse():
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action='typing')
            except Exception:
                # Sending an indicator is best effort; do not lose a generation.
                log.debug('Telegram typing indicator failed', exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=every)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
