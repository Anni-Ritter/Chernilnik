"""Memory retrieval and evidence-anchored automatic extraction (not an infallible canon oracle)."""
from __future__ import annotations

import json
import re
from typing import Any

from storage import Store

STOPWORDS = {
    'после', 'когда', 'этого', 'который', 'потому', 'даже', 'будет', 'теперь',
    'просто', 'очень', 'чтобы', 'перед', 'между', 'через', 'здесь', 'сейчас',
    'было', 'были', 'была', 'только', 'потом', 'вроде', 'пока', 'снова',
    'нужно', 'могла', 'может', 'своего', 'которого', 'сказал', 'начала',
    'парень', 'девушка', 'персонаж', 'когда', 'которые', 'когда',
}


def tokens(value: str) -> set[str]:
    return {x for x in re.findall(r'[а-яёa-z0-9]{4,}', value.lower()) if x not in STOPWORDS}


def select_memories(rows, query: str, max_chars: int = 10000, max_items: int = 65):
    """Pinned then crucial then relevant; physical budget never silently counts all facts."""
    qt = tokens(query)
    scored = []
    for row in rows:
        overlap = len(tokens(row['text']) & qt)
        pinned = bool(row['pinned'])
        imp = int(row['importance'])
        if not pinned and imp < 5 and overlap == 0:
            continue
        score = (1000 if pinned else 0) + (500 if imp == 5 else 0) + 16 * overlap + imp * 10
        scored.append((score, int(row['id']), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected, total = [], 0
    for _, _, row in scored:
        line = f"[#{row['id']} ход={row['source_turn_id'] or 'закреплено вручную'} важность={row['importance']} достоверность={row['certainty']}"
        if row['known_by']:
            line += f" знают: {row['known_by']}"
        line += f"] {row['text']}"
        if len(selected) >= max_items or total + len(line) > max_chars:
            continue
        selected.append(line)
        total += len(line)
    return selected


def validated_extractions(payload: Any, source_text: str) -> list[dict[str, Any]]:
    """No evidence from this exact turn -> no automatic fact."""
    if not isinstance(payload, dict) or not isinstance(payload.get('facts'), list):
        return []
    found = []
    seen = set()
    for item in payload['facts'][:8]:
        if not isinstance(item, dict):
            continue
        statement = str(item.get('text', '')).strip()
        evidence = str(item.get('evidence', '')).strip()
        category = str(item.get('category', 'event')).strip()
        certainty = str(item.get('certainty', 'confirmed')).strip()
        known_by = str(item.get('known_by', '')).strip()
        try:
            priority = int(item.get('importance', 1))
        except (ValueError, TypeError):
            continue
        if not (9 <= len(statement) <= 550 and 3 <= priority <= 5):
            continue
        if not evidence or evidence not in source_text:
            continue
        if certainty not in ('confirmed', 'belief', 'uncertain'):
            continue
        if statement.casefold() in seen:
            continue
        seen.add(statement.casefold())
        found.append({'text': statement, 'evidence': evidence, 'category': category or 'event',
                      'importance': priority, 'certainty': certainty, 'known_by': known_by})
    return found


EXTRACT_INSTRUCTIONS = '''Ты индексатор памяти для литературной ролевой. Выделяй только факты, подтверждённые текстом ТЕКУЩЕГО хода. Ответ: строго JSON-объект {"facts": [], "world_changes": [], "character_insights": []}. Каждому элементу нужны ключи:
text (одно атомарное утверждение по-русски: кто, что сделал, с кем, где, почему — если известно),
category (event/relationship/identity/goal/world/secret/time/object),
importance (3, 4 или 5), certainty (confirmed/belief/uncertain), known_by (кто знает, или пусто), evidence (ДОСЛОВНЫЙ непрерывный фрагмент из предоставленного текста).

5 — необратимые поворотные события: похищение и кто похититель, убийство и кто убийца, клятва, раскрытая тайна, важнейшие отношения, постоянный закон мира, источник конфликта.
4 — устойчивые цели, травмы/ранения, последствия, значимые отношения, ограничения, текущий заложник, существенные перемены.
3 — временная, но сюжетно полезная деталь (например, значимая для текущей сцены улика).
1–2: НЕ СОХРАНЯЙ — незначимый цвет камня, погода, декоративные предметы, жесты, случайные реплики.

Не превращай обвинения, подозрения, ложь, мысли героя в объективную истину: присваивай belief/uncertain. Не додумывай. Не переписывай старые сведения без доказательств. Прошедшие события формулируй как ПРОШЕДШИЕ: "похитил", а не бессрочно "держит в плену", если текущий статус неизвестен. Новое событие (побег/освобождение) не отменяет историю того, кто изначально похитил персонажа. Не фиксируй «зима прошла», если прошли только посты: количество сообщений не равно прошедшему времени. Максимум 6 фактов за ход. Также заполняй массив world_changes (обычно пустой) только для объективно свершившихся ГЛОБАЛЬНЫХ перемен мира: смена власти, война, катастрофа, новый/изменённый закон. Элемент {"text":"...", "certainty":"confirmed", "evidence":"дословный фрагмент"}. НЕ записывай план, слух, мнение, гипотезу, частный эпизод или переход времени без свидетельств. Максимум 2.

Дополнительно массив character_insights (обычно пустой), до 5 за ход: {"name":"ТОЧНОЕ имя одного из активных персонажей", "field":"одно из age/gender/species/origin/residence/occupation/family/values/goals/fears/defenses/attachments/boundaries/conflicts/habits/self_image или пустая строка", "text":"конкретное наблюдение, раскрытый факт или обоснованная интерпретация", "certainty":"confirmed/interpretation", "evidence":"дословная цитата из текущих постов"}. Сохраняй значимые устойчивые черты, биографию и явные изменения; НЕ каждый вздох, не декоративные детали. Предпочитай подтвердившиеся факты. Интерпретация — не окончательный диагноз и не факт. О героях игрока НЕ ДОДУМЫВАЙ их тайные мотивы, переживания, биографию. Если нет ничего значимого — пустые массивы.'''


def extraction_prompt(user_post: str, bot_post: str, already_known: list[str]):
    return ("ИЗВЕСТНЫЕ ВАЖНЫЕ ФАКТЫ, НЕ ДУБЛИРУЙ ИХ БЕЗ ИЗМЕНЕНИЯ:\n"
            + '\n'.join(already_known[:30]) + "\n\nТЕКУЩИЙ ПОСТ ПОЛЬЗОВАТЕЛЯ:\n"
            + user_post + "\n\nТЕКУЩИЙ ПОСТ СОАВТОРА:\n" + bot_post)


def save_extractions(store: Store, sid: int, turn_id: int, facts: list[dict]):
    for fact in facts:
        store.add_fact(sid, fact['text'], category=fact['category'], importance=fact['importance'],
                       certainty=fact['certainty'], known_by=fact['known_by'],
                       source_turn_id=turn_id, evidence=fact['evidence'])
