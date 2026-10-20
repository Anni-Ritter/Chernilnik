"""Reliable completion assembly for different OpenAI-compatible response formats.

Only response metadata may trigger automatic continuation. A deliberate literary
cliffhanger is not an indication that a second creative turn should be generated.
"""
from __future__ import annotations

import os
import re


CONTINUATION_PROMPT = (
    'ТЕХНИЧЕСКОЕ ПРОДОЛЖЕНИЕ ПРЕРВАННОГО ОТВЕТА. Это тот же самый пост, '
    'не новая сцена и не новый ход игрока. Начни ровно там, где оборвался текст. '
    'Не повторяй написанное, не начинай пост заново, не добавляй заголовки, '
    'пояснения или вступления. Не меняй уже описанные события, характеры, '
    'действия и реплики персонажей игрока. Заверши этот же пост естественно. '
    'Выдай ТОЛЬКО недостающую часть.'
)


def automatic_continuations() -> int:
    """Constrained safeguard against provider bugs and unexpectedly high bills."""
    try:
        return max(0, min(6, int(os.getenv('GENERATION_AUTO_CONTINUES', '3'))))
    except ValueError:
        return 3


def followup_token_budget(initial: int) -> int:
    """Old BotHost MAX_OUTPUT_TOKENS=1800 must not keep choking followups."""
    try:
        configured = int(os.getenv('GENERATION_CONTINUE_TOKENS', '8192'))
    except ValueError:
        configured = 8192
    return max(initial, min(16000, max(1024, configured)))


def reached_limit(response, *, polza: bool) -> bool:
    if polza:
        choices = getattr(response, 'choices', None) or []
        reason = getattr(choices[0], 'finish_reason', None) if choices else None
        return str(reason).casefold() in {
            'length', 'max_tokens', 'max_output_tokens', 'max_tokens_exceeded'
        }
    if str(getattr(response, 'status', '')).lower() == 'incomplete':
        return True
    details = getattr(response, 'incomplete_details', None)
    reason = getattr(details, 'reason', None)
    return str(reason).casefold() == 'max_output_tokens'


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(
            p.get('text', '') if isinstance(p, dict) else (getattr(p, 'text', '') or '')
            for p in content
        )
    return ''


def append_continuation(existing: str, extra: str) -> str:
    """Combine without doubling already repeated text or adding a spurious gap."""
    extra = re.sub(r'^\s*(?:продолжение|continuation)\s*:\s*', '', extra, flags=re.I)
    if not existing:
        return extra.strip()
    if not extra.strip():
        return existing
    prev = existing.rstrip()
    right = extra.lstrip()
    # The model sometimes starts its completion by repeating the previous phrase.
    max_overlap = min(len(prev), len(right), 700)
    for size in range(max_overlap, 7, -1):
        if prev[-size:] == right[:size]:
            return prev + right[size:]
    if extra[0].isspace():
        return prev + extra
    if prev[-1].isalnum() and right[0].islower():
        return prev + right  # final token was cut mid-word
    return prev + (' ' if prev[-1].isalnum() else '\n') + right
