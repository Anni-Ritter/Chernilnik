"""Reuse the same Telegram message for inline menu navigation.

This adapter is intentionally used ONLY for callback-driven menus. Ordinary
story posts, progress notices and documents keep using regular Message methods.
"""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ReplyKeyboardRemove


class InlineMenuMessage:
    def __init__(self, original):
        self._original = original

    def __getattr__(self, name):
        return getattr(self._original, name)

    async def answer(self, text: str, **kwargs):
        markup = kwargs.get('reply_markup')
        # Reply-keyboard removal is a chat action, not valid edit markup.
        if isinstance(markup, ReplyKeyboardRemove):
            return await self._original.answer(text, **kwargs)
        edit_kwargs = dict(kwargs)
        edit_kwargs.pop('reply_to_message_id', None)
        edit_kwargs.pop('disable_notification', None)
        edit_kwargs.pop('protect_content', None)
        try:
            return await self._original.edit_text(text, **edit_kwargs)
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return self._original
            return await self._original.answer(text, **kwargs)
        except (AttributeError, TypeError):
            # A media message or an older Telegram object may not be editable.
            return await self._original.answer(text, **kwargs)
