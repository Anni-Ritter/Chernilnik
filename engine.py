"""Single-model literary generation; separate low-cost memory pass after the turn."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from openai import AsyncOpenAI
from storage import Store
from memory import EXTRACT_INSTRUCTIONS, extraction_prompt, select_memories, validated_extractions, save_extractions

log = logging.getLogger(__name__)
STYLE = (Path(__file__).parent / 'prompts' / 'style.md').read_text(encoding='utf-8')
SETTINGS = json.loads((Path(__file__).parent / 'settings.json').read_text(encoding='utf-8'))


class RoleplayEngine:
    def __init__(self, store: Store, client: AsyncOpenAI, model: str, memory_model: str,
                 auto_memory: bool = True, max_output_tokens: int = 1800):
        self.db, self.client = store, client
        self.model, self.memory_model = model, memory_model
        self.auto_memory = auto_memory
        self.max_output_tokens = max_output_tokens

    def context(self, sid: int, new_post: str):
        session = self.db.get_session(sid)
        recent = self.db.recent_turns(sid, 8)
        recent_query = new_post + '\n' + '\n'.join((row['user_text'] + row['bot_text'])[-500:] for row in recent[-2:])
        memories = select_memories(self.db.list_facts(sid), recent_query)
        transcript = []
        # Avoid growing API context without bound while preserving whole turns on disk.
        for row in recent:
            transcript.append(f"[ХОД {row['id']}, ПОЛЬЗОВАТЕЛЬ]\n{row['user_text']}\n"
                              f"[ХОД {row['id']}, СОАВТОР]\n{row['bot_text']}")
        notes = [
            f"ИСТОРИЯ: {session['title']}",
            f"МИР И ЕГО КАНОН:\n{session['world'] or 'Свободная ролевая, мир формируется по ходу. Не устанавливай лишний канон без необходимости.'}",
            f"РОЛИ И ГРАНИЦЫ АВТОРСТВА:\n{session['cast'] or 'Персонажи пользователя принадлежат пользователю. Ты ведёшь остальных персонажей и мир; имена и роли выясняются из сцены.'}",
            f"ЗАКРЕПЛЁННАЯ ХРОНОЛОГИЯ/ВРЕМЯ: {session['scene_time'] or 'Не задано. Не вычисляй время по числу сообщений.'}",
            f"ТЕКУЩАЯ ЛОКАЦИЯ: {session['current_location'] or 'Не задана.'}",
            'ДОЛГОСРОЧНАЯ ПАМЯТЬ (belief и uncertain НЕ считать объективными фактами):\n' + ('\n'.join(memories) if memories else 'Нет записей.'),
            'НЕДАВНИЕ ХОДЫ:\n' + ('\n\n'.join(transcript) if transcript else 'Начало истории.'),
            'НОВЫЙ ПОСТ ПОЛЬЗОВАТЕЛЯ:\n' + new_post,
        ]
        return '\n\n'.join(notes)

    def instructions(self):
        return (f"{STYLE}\n\n"
                "ПРАВИЛА РОЛЕВОЙ: ты равноправный литературный соавтор и ведущий мира, не сервис исполнения желаний. "
                "События не подгоняются под удобный исход; действия имеют реальные последствия. "
                "Твои персонажи инициативны, но не всеведущи. Их знания отделены от авторских и чужих мыслей. "
                "Если они жестоки, эгоистичны или трусливы, не делай их автоматически добрыми и покладистыми. "
                "Не принимай решений за персонажей пользователя: нельзя писать новые действия, реплики и мысли от их лица. "
                "Не утверждай, что персонаж пользователя согласился или испытал то, чего пользователь не описывал. "
                "Пиши только художественный пост, без служебных комментариев, предложений вариантов и вопросов к автору. "
                "Не путай число ходов с прошедшим в мире временем. Крупные скачки времени, смены сезонов и лет "
                "допустимы только при явно описанном переходе, не сами собой. "
                "В литературном тексте возможны неприятные и неоднозначные люди, преступления и последствия, "
                "не добавляй назидательную мораль от лица автора. Соблюдай применимые ограничения API. "
                f"Параметры: длина={SETTINGS['post_length']} (ориентир 200-400 слов), "
                f"инициатива={SETTINGS['initiative']}/5, детализация={SETTINGS['description']}/5, "
                "третье лицо, строгие границы управления персонажами пользователя.")

    async def generate(self, sid: int, post: str):
        response = await self.client.responses.create(
            model=self.model,
            instructions=self.instructions(),
            input=self.context(sid, post),
            max_output_tokens=self.max_output_tokens,
            store=False,
        )
        answer = (response.output_text or '').strip()
        if not answer:
            raise RuntimeError('Модель не вернула художественный текст. Проверь лимит токенов и доступ к модели.')
        in_tokens = getattr(getattr(response, 'usage', None), 'input_tokens', 0) or 0
        out_tokens = getattr(getattr(response, 'usage', None), 'output_tokens', 0) or 0
        return answer, in_tokens, out_tokens

    async def memorize(self, sid: int, turn_id: int, post: str, answer: str):
        if not self.auto_memory:
            return 0
        old_facts = select_memories(self.db.list_facts(sid), post + '\n' + answer, max_chars=5500)
        prompt = extraction_prompt(post, answer, old_facts)
        response = await self.client.responses.create(
            model=self.memory_model, instructions=EXTRACT_INSTRUCTIONS, input=prompt,
            max_output_tokens=1200, store=False,
            text={'format': {'type': 'json_object'}},
        )
        usage = getattr(response, 'usage', None)
        self.db.add_usage(sid, 'memory', self.memory_model,
                          getattr(usage, 'input_tokens', 0) or 0,
                          getattr(usage, 'output_tokens', 0) or 0)
        data = json.loads(response.output_text or '{}')
        validated = validated_extractions(data, post + '\n' + answer)
        save_extractions(self.db, sid, turn_id, validated)
        return len(validated)
