"""Parse and validate structured memory-model responses.

Only the literal original posts are evidence; a model's JSON is not itself canon.
"""
from __future__ import annotations

import json
import re


def parse_payload(text: str) -> dict:
    raw = (text or '').strip()
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.I).strip()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('Модель памяти не вернула валидный JSON') from exc
    if not isinstance(payload, dict) or any(not isinstance(payload.get(k), list)
                                            for k in ('facts','world_changes','character_insights')):
        raise ValueError('Модель памяти вернула неполную структуру JSON')
    if 'scene_updates' not in payload:
        payload['scene_updates'] = []  # Legacy-compatible model response.
    if not isinstance(payload['scene_updates'], list):
        raise ValueError('Модель памяти вернула неправильный scene_updates')
    return payload


def important_turn(text: str) -> bool:
    """Conservative heuristic: an empty extraction after an evident plot turn merits review."""
    return bool(re.search(
        r'(?i)\b(?:напал[иао]?|атаковала?|сбежал[иао]?|побег|сбежавш|'
        r'погибл?[аио]?|умерл?[аио]?|убил[аио]?|смерт[ьи]|'
        r'плен[а-яё]*|химер[а-яё]*|предал[аио]?|'
        r'бежал[аио]?|пересекл?[аио]?|освободил[аио]?|'
        r'ранен[а-яё]*|пала\b|повержен[а-яё]*)\b', text or ''))
