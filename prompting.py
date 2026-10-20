"""A small, auditable roleplay prompt assembler.

Original Python implementation informed by general prompt layering and lore-retrieval
concepts used in roleplay frontends; no third-party source code is copied.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re

from memory import select_memories, tokens

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / 'prompts'


def read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding='utf-8').strip()


def trim(value: str, limit: int) -> str:
    value = (value or '').strip()
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(' ', 1)[0].rstrip() + '\n[Далее опущено из-за лимита контекста]'


@dataclass(frozen=True)
class CompiledPrompt:
    system: str
    user: str
    sections: tuple[str, ...]

    def preview(self) -> str:
        return ('ПОЛНЫЙ ОТЛАДОЧНЫЙ ПРОМПТ (личные сюжеты и секреты; не публикуй)\n'
                + 'Разделы: ' + ', '.join(self.sections) + '\n\n'
                + '=== SYSTEM / INSTRUCTIONS ===\n' + self.system
                + '\n\n=== USER / INPUT ===\n' + self.user)


class PromptComposer:
    def __init__(self, store, character_context, *, max_chars: int = 29000,
                 examples_limit: int = 2, history_count: int = 8, recall_count: int = 3):
        self.db = store
        self.character_context = character_context
        self.max_chars = max(10000, min(int(max_chars), 65000))
        self.examples_limit = max(0, min(int(examples_limit), 3))
        self.history_count = max(2, min(int(history_count), 14))
        self.recall_count = max(0, min(int(recall_count), 5))
        self._examples = self._load_examples()

    @staticmethod
    def _load_examples() -> list[dict]:
        file = PROMPTS_DIR / 'examples.json'
        if not file.exists():
            return []
        records = json.loads(file.read_text(encoding='utf-8'))
        return [item for item in records if isinstance(item, dict)
                and isinstance(item.get('text'), str) and item.get('text', '').strip()]

    def examples(self, query: str) -> str:
        if not self.examples_limit:
            return ''
        matches = []
        qt = tokens(query)
        # Live personal examples are stored in the durable SQLite database, not
        # code or FSM state; changes in Telegram take effect without redeploying.
        personalized = [dict(text=row['text'], tags=[]) for row in self.db.list_style_examples()]
        if not personalized:
            return ''  # Deleting every example really disables them; no bundled fallback.
        for index, item in enumerate(personalized):
            # User-added examples take precedence over bundled starter examples.
            priority = 1000 if index < len(personalized) else 0
            tag_match = len(tokens(' '.join(item.get('tags', []))) & qt)
            matches.append((priority + tag_match, -index, item))
        matches.sort(reverse=True, key=lambda x: (x[0], x[1]))
        selected = [trim(item['text'], 1300) for _, _, item in matches[:self.examples_limit]]
        if not selected:
            return ''
        return ('ОБРАЗЦЫ ЛИТЕРАТУРНОЙ МАНЕРЫ (НЕ события текущего мира, НЕ биографии, '
                'НЕ персонажи для копирования; только ритм речи и ход мыслей):\n\n'
                + '\n\n--- ДРУГОЙ НЕЗАВИСИМЫЙ ОБРАЗЕЦ ---\n\n'.join(selected))

    def instructions(self) -> str:
        return read_prompt('core.md') + '\n\n' + read_prompt('style.md')

    def assemble(self, sid: int, post: str | None = None, *, opening: bool = False) -> CompiledPrompt:
        session = self.db.get_session(sid)
        if session is None:
            raise ValueError('Unknown story')
        if opening and (self.db.get_opening(sid) or self.db.recent_turns(sid, 1)):
            raise ValueError('The story has already begun')

        transcript = []
        recent = [] if opening else self.db.recent_turns(sid, self.history_count)
        intro = self.db.get_opening(sid)
        if intro and len(recent) < self.history_count:
            transcript.append('[ПЕРВЫЙ ПОСТ СОАВТОРА]\n' + trim(intro['bot_text'], 2500))
        for row in recent:
            transcript.append(f"[ХОД {row['id']}, АВТОР]\n" + trim(row['user_text'], 2300)
                              + f"\n[ХОД {row['id']}, СОАВТОР]\n" + trim(row['bot_text'], 3000))
        history = '\n\n'.join(transcript) or '(Ролевая пока не началась)'
        query = ((post or '') + '\n' + (recent[-1]['bot_text'] if recent else '')[-1000:]).strip()
        relevant = [] if opening or self.recall_count <= 0 else self.db.search_turns(
            sid, query, exclude_ids=[row['id'] for row in recent], limit=self.recall_count)
        recovered = []
        for turn in relevant:
            recovered.append(f"[СТАРЫЙ ХОД {turn['id']}, дословный фрагмент. При противоречии "
                             f"с текущим положением дел учитывай хронологию.]\n"
                             f"АВТОР: {trim(turn['user_text'], 1000)}\n"
                             f"СОАВТОР: {trim(turn['bot_text'], 1150)}")

        memories = select_memories(self.db.list_facts(sid), query, max_chars=7500, max_items=35)
        world = self.db.get_world(session['world_id']) if session['world_id'] else None
        global_canon = ''
        if world:
            global_canon = f"МИР «{world['name']}» (общая энциклопедия):\n" + trim(world['description'], 7500)
            events = self.db.list_world_events(world['id'], limit=24)
            if events:
                global_canon += ('\n\nОБЩИЕ ГЛОБАЛЬНЫЕ СОБЫТИЯ (не автоматически знания каждого героя; '
                                 'для параллельных историй сверяй время):\n'
                                 + '\n'.join('• ' + e['text'] for e in reversed(events)))
        if world:
            search_query = (session['title'] + ' ' + session['current_location'] + ' ' +
                            session['scene_time'] + ' ' + (post or '')[:1100] + ' ' +
                            ' '.join(row['user_text'][-650:] for row in recent[-3:]) + ' ' +
                            self.character_context(sid)[:2700])
            relevant_lore = self.db.relevant_world_lore(world['id'], search_query)
            if relevant_lore:
                global_canon += '\n\nРЕЛЕВАНТНЫЕ СТАТЬИ ЭНЦИКЛОПЕДИИ:\n' + '\n\n'.join(relevant_lore)
        if session['world']:
            global_canon += ('\n\nУТОЧНЕНИЯ ИМЕННО ЭТОГО СЮЖЕТА:\n' + trim(session['world'], 3000))
        parts = [
            ('ПРАВИЛА ИСТОЧНИКОВ (не переопределяй канон ради персонажа)',
             'Объективный канон мира и подтверждённые события выше персонажных догадок. '
             'Авторское фактическое повествование может устанавливать обстоятельства; '
             'реплики КАЖДОГО героя — только его слова, не новая статья энциклопедии. '
             'Если твой герой спорит о чужой стране, он вправе быть уверенно неправым. '
             'Не вводи законы, тайные проверки и исключения ради победы в споре. '
             'Позволяй независимым событиям опровергать ожидания; '
             'герой не обязан соглашаться, извиняться или меняться мгновенно.'),
            ('СЕССИЯ', trim(session['title'], 150)),
            ('МИР (общий канон и сюжетные уточнения)', global_canon or
             'Свободная история: не придумывай фундаментальные законы мира без сюжетной необходимости.'),
            ('РАСПРЕДЕЛЕНИЕ РОЛЕЙ', trim(session['cast'], 2200) or
             'Игрок контролирует собственных героев. Соавтор контролирует своих персонажей, NPC и окружение.'),
            ('КАРТОЧКИ ПЕРСОНАЖЕЙ (включая СКРЫТЫЕ ОТ ГЕРОЕВ сведения)',
             trim(self.character_context(sid), 7000)),
            ('ВРЕМЯ В МИРЕ', trim(session['scene_time'], 300) or
             'Не установлено. Количество постов не означает течение дней или месяцев.'),
            ('ЛОКАЦИЯ', trim(session['current_location'], 300) or 'Не установлена.'),
            ('КЛЮЧЕВЫЕ ФАКТЫ (исторические события не отменяются текущим состоянием)',
             '\n'.join(memories) or 'Пока нет структурированных фактов.'),
            ('ПОИСК В СТАРЫХ ПОСТАХ', '\n\n'.join(recovered) or 'Сюжетно подходящих старых фрагментов нет.'),
            ('ПРИМЕРЫ ЖЕЛАЕМОЙ ПРОЗЫ', self.examples(query) or 'Встроенные примеры отключены.'),
            ('ПОСЛЕДНИЕ ХОДЫ', history),
        ]
        if opening:
            parts.append(('ТЕКУЩАЯ ЗАДАЧА',
                          'Начни историю самостоятельно от лица персонажей соавтора. '
                          'Пользователь ещё не совершал игрового хода. '
                          'Дай естественную точку входа. Не предполагай, что персонаж пользователя что-либо сделал или решил. Не назначай ему местонахождение, '
                          'мысли, действия или реплики. Описывай конкретную ситуацию, не предысторию.'))
        else:
            parts.append(('НОВЫЙ ПОСТ АВТОРА — ОБЯЗАТЕЛЬНО ОТРЕАГИРУЙ', post or ''))

        # Prioritize canonical info + newest actions if the accumulated data gets large.
        text = '\n\n'.join(f'### {name}\n{content}' for name, content in parts)
        if len(text) > self.max_chars:
            # Remove decorative/contextual sections before removing recent plot.
            # Keep the NEW user post and pinned facts intact even for gigantic worlds.
            shrink = ['ПРИМЕРЫ ЖЕЛАЕМОЙ ПРОЗЫ', 'ПОИСК В СТАРЫХ ПОСТАХ', 'ПОСЛЕДНИЕ ХОДЫ',
                      'КАРТОЧКИ ПЕРСОНАЖЕЙ (включая СКРЫТЫЕ ОТ ГЕРОЕВ сведения)',
                      'МИР (общий канон и сюжетные уточнения)']
            index_map = {name: i for i, (name, _) in enumerate(parts)}
            for key in shrink:
                if len(text) <= self.max_chars:
                    break
                idx = index_map[key]
                name, content = parts[idx]
                if key == 'ПОСЛЕДНИЕ ХОДЫ' and len(transcript) > 2:
                    # Oldest-first removal; previous code cut the RIGHT side,
                    # which could silently discard the newest character action.
                    while len(text) > self.max_chars and len(transcript) > 2:
                        transcript.pop(0)
                        parts[idx] = (name, '\n\n'.join(transcript))
                        text = '\n\n'.join(f'### {n}\n{v}' for n, v in parts)
                    if len(text) <= self.max_chars:
                        break
                name, content = parts[idx]
                overflow = len(text) - self.max_chars
                floor = 400 if key == 'ПОСЛЕДНИЕ ХОДЫ' else 250 if key.startswith(('КАРТОЧКИ','МИР')) else 0
                # For a single still-too-long fragment, preserve its newest tail.
                if key == 'ПОСЛЕДНИЕ ХОДЫ' and len(content) > max(floor, len(content)-overflow-100):
                    cap = max(floor, len(content)-overflow-100)
                    parts[idx] = (name, '[Старые фрагменты опущены]\n' + content[-cap:])
                else:
                    parts[idx] = (name, trim(content, max(floor, len(content)-overflow-100)) if len(content)>floor else content)
                text = '\n\n'.join(f'### {n}\n{v}' for n, v in parts)
        if len(text) > self.max_chars:
            raise ValueError('Контекст больше лимита. Сократи канон или последний пост; он не будет молча обрезан.')
        return CompiledPrompt(system=self.instructions(), user=text, sections=tuple(p[0] for p in parts))
