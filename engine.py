"""Literary generation and post-commit fact extraction (no scene-planner step)."""
from __future__ import annotations

import json
import logging
import os
import re

from openai import AsyncOpenAI
from storage import Store
from prompting import PromptComposer
from memory import EXTRACT_INSTRUCTIONS, extraction_prompt, select_memories, validated_extractions, save_extractions, keep_world_claims_at_source_level
from generation_models import GenerationChoice, GenerationSettings
from worlds import PROFILE_FIELDS
from generation_continuation import (
    CONTINUATION_PROMPT, automatic_continuations, followup_token_budget,
    reached_limit, message_text, append_continuation,
)

log = logging.getLogger(__name__)


class RoleplayEngine:
    def __init__(self, store: Store, client: AsyncOpenAI, model: str, memory_model: str,
                 auto_memory: bool = True, max_output_tokens: int = 8192):
        self.db, self.client = store, client
        self.model, self.memory_model = model, memory_model
        self.generation_settings: GenerationSettings | None = None
        self.polza_client: AsyncOpenAI | None = None
        self.last_generation_model = model
        self.auto_memory = auto_memory
        self.max_output_tokens = max(1, int(max_output_tokens))
        self.debug_prompts = os.getenv('PROMPT_DEBUG', 'true').lower() in ('true','1','yes','on')
        self.composer = PromptComposer(
            store, self.character_context,
            max_chars=int(os.getenv('PROMPT_MAX_CHARS', '29000')),
            examples_limit=int(os.getenv('STYLE_EXAMPLES', '2')),
            history_count=int(os.getenv('RECENT_TURNS', '8')),
            recall_count=int(os.getenv('RECALLED_TURNS', '3')),
        )

    def configure_generation(self, settings: GenerationSettings, polza_client: AsyncOpenAI | None):
        """Switches *only* literary generation; memory and photo use self.client."""
        self.generation_settings = settings
        self.polza_client = polza_client

    def generation_choice(self) -> GenerationChoice:
        return self.generation_settings.choice if self.generation_settings else GenerationChoice('openai', self.model)

    def select_generation_model(self, provider: str, model: str) -> GenerationChoice:
        if self.generation_settings is None:
            raise RuntimeError('Переключение моделей не настроено.')
        if provider == 'polza' and self.polza_client is None:
            raise ValueError('POLZA_API_KEY отсутствует в BotHost. Добавь ключ в переменные окружения и перезапусти бота.')
        return self.generation_settings.select(provider, model)

    def character_context(self, sid: int) -> str:
        """Character descriptions are AUTHOR knowledge, not omniscience for NPCs."""
        items = []
        for char in self.db.list_characters(sid):
            role = {'user': 'ИГРОК управляет', 'bot': 'СОАВТОР управляет',
                    'npc': 'СОАВТОР управляет NPC'}[char['role']]
            sections = [f"{char['name']} ({role})"]
            if char['description']:
                sections.append('Биография/характер от автора: ' + char['description'][:3500])
            fields = self.db.profile_fields(char['id'], sid)
            if fields:
                sections.append('АНКЕТА (≈ обозначает интерпретацию, не установленную истину):')
                for f in fields[:22]:
                    mark = ' ≈ гипотеза' if f['certainty'] != 'confirmed' else ''
                    sections.append(f'{PROFILE_FIELDS.get(f["field"],f["field"])}: {f["value"][:380]}{mark}')
            history = self.db.development(char['id'],sid,8)
            if history:
                sections.append('РАЗВИТИЕ В ЭТОМ СЮЖЕТЕ, подтверждено фрагментами ходов:')
                for item in reversed(history):
                    prefix = 'НАБЛЮДЕНИЕ' if item['certainty']=='confirmed' else 'ВОЗМОЖНОЕ ОБЪЯСНЕНИЕ, НЕ ФАКТ'
                    sections.append(prefix + ': ' + item['insight'][:310])
            if char['appearance_override']:
                sections.append('Внешность (уточнение автора выше приоритетом): ' + char['appearance_override'][:1900])
            for photo in self.db.photos(char['id'])[:5]:
                if photo['visual_description'] and photo['analysis_status'] == 'ok':
                    prefix = 'Основной визуальный референс' if photo['is_primary'] else 'Дополнительный референс'
                    sections.append(prefix + ': ' + photo['visual_description'][:900 if photo['is_primary'] else 220])
            items.append('\n'.join(sections))
        return ('\n\n'.join(items) if items else 'Карточек персонажей нет.') + (
            '\nЗнания автора о биографии и тайнах НЕ автоматически известны персонажам. '
            'Не обращай скрытые сведения в наблюдаемые признаки без игрового основания. '
            'Не приписывай персонажам игрока новых действий и мыслей.')

    def context(self, sid: int, post: str):
        """Compatibility wrapper for older diagnostic tests."""
        return self.composer.assemble(sid, post=post).user

    def instructions(self):
        return self.composer.instructions()

    def inspect(self, sid: int, post: str | None = None, *, opening: bool = False) -> str:
        """Preview only: does not spend API tokens or alter story."""
        return self.composer.assemble(sid, post=post, opening=opening).preview()

    async def _call(self, sid: int, *, post: str | None, opening: bool):
        compiled = self.composer.assemble(sid, post=post, opening=opening)
        choice = self.generation_choice()
        if self.debug_prompts:
            self.db.save_prompt_audit(sid, 'opening' if opening else 'turn',
                                      choice.display(), compiled.preview())

        is_polza = choice.provider == 'polza'
        if is_polza and self.polza_client is None:
            raise RuntimeError('Для Polza API нужно задать POLZA_API_KEY на BotHost.')
        max_followups = automatic_continuations()
        answer = ''
        total_input = total_output = 0
        budget = self.max_output_tokens

        for attempt in range(max_followups + 1):
            # A continuation is an extension of this SAME answer, never a new story turn.
            # Include the already generated text as assistant context, with instructions
            # to output only the missing suffix. No partial draft touches the SQLite DB.
            if is_polza:
                messages = [{'role': 'system', 'content': compiled.system},
                            {'role': 'user', 'content': compiled.user}]
                if answer:
                    messages += [
                        {'role': 'assistant', 'content': answer},
                        {'role': 'user', 'content': CONTINUATION_PROMPT},
                    ]
                response = await self.polza_client.chat.completions.create(
                    model=choice.model, messages=messages, max_tokens=budget)
                choice0 = response.choices[0] if response.choices else None
                piece = message_text(choice0.message.content) if choice0 else ''
                usage = getattr(response, 'usage', None)
                inp = getattr(usage, 'prompt_tokens', 0) or 0
                out = getattr(usage, 'completion_tokens', 0) or 0
            else:
                # No OpenAI Responses server-side storage; followups reconstruct
                # context client-side from the prior partial answer.
                input_data = compiled.user
                if answer:
                    input_data = [
                        {'role': 'user', 'content': compiled.user},
                        {'role': 'assistant', 'content': answer},
                        {'role': 'user', 'content': CONTINUATION_PROMPT},
                    ]
                response = await self.client.responses.create(
                    model=choice.model, instructions=compiled.system,
                    input=input_data, max_output_tokens=budget, store=False)
                piece = getattr(response, 'output_text', '') or ''
                usage = getattr(response, 'usage', None)
                inp = getattr(usage, 'input_tokens', 0) or 0
                out = getattr(usage, 'output_tokens', 0) or 0

            total_input += inp
            total_output += out
            if piece and not piece.isspace():
                answer = append_continuation(answer, piece)
            truncated = reached_limit(response, polza=is_polza)
            if not truncated:
                if not answer.strip():
                    raise RuntimeError('Модель не вернула художественный текст. Проверь ключ, ID модели и лимит токенов.')
                self.last_generation_model = choice.model
                return answer.strip(), total_input, total_output

            if attempt == max_followups:
                raise RuntimeError(
                    'Модель не завершила ответ даже после автоматических продолжений. '
                    'Пост не сохранён; проверь лимиты модели и GENERATION_AUTO_CONTINUES.')
            log.info('Model %s hit output token limit in story %s; auto-continue %d/%d',
                     choice.model, sid, attempt + 1, max_followups)
            budget = followup_token_budget(self.max_output_tokens)

        raise RuntimeError('Unexpected literary completion state.')

    async def generate_opening(self, sid: int):
        return await self._call(sid, post=None, opening=True)

    async def generate(self, sid: int, post: str):
        return await self._call(sid, post=post, opening=False)

    async def _extract_memory_payload(self, sid: int, prompt: str, source_text: str):
        """Prefer inexpensive OpenAI memory; use fixed Gemini only if it fails.

        Importantly, the prose model selector does not affect memory fallback:
        a temporary RP model such as Euryale must never extract canon.
        """
        from memory_pipeline import parse_payload, important_turn
        reason = None
        try:
            response = await self.client.responses.create(
                model=self.memory_model, instructions=EXTRACT_INSTRUCTIONS, input=prompt,
                max_output_tokens=int(os.getenv('MEMORY_OUTPUT_TOKENS', '3200')),
                store=False, text={'format': {'type': 'json_object'}},
            )
            usage = getattr(response, 'usage', None)
            self.db.add_usage(sid, 'memory', self.memory_model,
                              getattr(usage, 'input_tokens', 0) or 0,
                              getattr(usage, 'output_tokens', 0) or 0)
            data = parse_payload(getattr(response, 'output_text', '') or '')
            if (important_turn(source_text) and
                    all(not data[k] for k in ('facts','world_changes','character_insights'))):
                reason = 'OpenAI не извлёк фактов из значимого события'
            else:
                return data
        except Exception as exc:
            reason = f'{type(exc).__name__}: {exc}'
            log.warning('Primary memory extraction failed in story %s: %s', sid, reason[:200])
        # Polza is only a recovery channel, not a second paid pass for every post.
        enabled = os.getenv('MEMORY_FALLBACK_POLZA', 'true').lower() in ('1','yes','true','on')
        if not enabled or self.polza_client is None:
            raise RuntimeError(f'Не удалось сохранить память (OpenAI): {reason}')
        fallback_model = os.getenv('MEMORY_FALLBACK_MODEL', 'google/gemini-3.7-flash').strip()
        try:
            response = await self.polza_client.chat.completions.create(
                model=fallback_model,
                messages=[{'role':'system','content':EXTRACT_INSTRUCTIONS},
                          {'role':'user','content':prompt}],
                max_tokens=int(os.getenv('MEMORY_FALLBACK_TOKENS','3500')),
            )
            if not response.choices:
                raise ValueError('Пустой ответ Polza')
            content = response.choices[0].message.content or ''
            if not isinstance(content, str):
                raise ValueError('Неожиданный формат ответа Polza')
            usage = getattr(response, 'usage', None)
            self.db.add_usage(sid, 'memory_fallback', fallback_model,
                              getattr(usage, 'prompt_tokens', 0) or 0,
                              getattr(usage, 'completion_tokens', 0) or 0)
            return parse_payload(content)
        except Exception as exc:
            raise RuntimeError(f'Извлечение памяти не удалось через OpenAI и Polza: {type(exc).__name__}') from exc

    async def memorize(self, sid: int, turn_id: int | None, post: str, answer: str):
        """Audit memory results per turn, preserving the immutable original post."""
        if not self.auto_memory:
            return 0
        self._last_memory_saved = 0
        try:
            total = await self._memorize_inner(sid, turn_id, post, answer)
        except Exception as exc:
            self.db.record_memory_job(sid, turn_id, success=False, error=type(exc).__name__ + ': ' + str(exc))
            raise
        self.db.record_memory_job(sid, turn_id, success=True,
                                  count=getattr(self, "_last_memory_saved", total))
        return total

    async def _memorize_inner(self, sid: int, turn_id: int | None, post: str, answer: str):
        """Runs after the story is saved. Its failure must never erase the turn."""
        if not self.auto_memory:
            return 0
        old_facts = select_memories(self.db.list_facts(sid), post + '\n' + answer, max_chars=5500)
        cast = self.db.list_characters(sid)
        roster = '\n'.join(f"{c['name']}: {('герой игрока' if c['role']=='user' else 'герой бота')}"
                           for c in cast)
        world = self.db.get_world(self.db.get_session(sid)['world_id']) if self.db.get_session(sid)['world_id'] else None
        prompt = (extraction_prompt(post, answer, old_facts)
                  + '\n\nАКТИВНЫЕ ГЕРОИ (учитывай только этот список для анкеты):\n' + roster
                  + '\nМИР: ' + (world['name'] if world else 'не привязан'))
        data = await self._extract_memory_payload(sid, prompt, post + "\n" + answer)
        validated = validated_extractions(data, post + '\n' + answer)
        validated = keep_world_claims_at_source_level(validated, post, answer)
        save_extractions(self.db, sid, turn_id, validated)
        source_text = post + '\n' + answer
        saved_count = len(validated)
        # Milestones are not psychological diagnoses. If a serious confirmed
        # event explicitly NAMES a participant, show it in their story timeline
        # even when the model omits the optional character_insights section.
        # For an author-controlled hero, require evidence from THEIR post.
        for fact in validated:
            if fact['category'] not in ('event','relationship') or fact['certainty'] != 'confirmed':
                continue
            if fact['importance'] < 4:
                continue
            for character in cast:
                if not re.search(r'(?<!\w)' + re.escape(character['name']) + r'(?!\w)',
                                 fact['text'], flags=re.I):
                    continue
                if character['role'] == 'user' and fact['evidence'] not in post:
                    continue
                if self.db.add_development(character['id'], sid, turn_id,
                                           'Сюжетное событие: ' + fact['text'],
                                           fact['evidence'], 'confirmed'):
                    saved_count += 1
        # World changes are exceptional: only evidence-backed, objective facts;
        # unverified rumours are not promoted into global canon.
        if world and isinstance(data.get('world_changes'), list):
            for item in data['world_changes'][:2]:
                if not isinstance(item,dict):
                    continue
                evidence = str(item.get('evidence','')).strip()
                statement = str(item.get('text','')).strip()
                if (evidence and evidence in source_text and 12 <= len(statement) <= 500
                        and item.get('certainty') == 'confirmed'):
                    if self.db.add_world_event(world['id'],statement,sid,turn_id,evidence,status='confirmed'):
                        saved_count += 1
        if isinstance(data.get('character_insights'),list):
            names = {c['name'].casefold():c for c in cast}
            for item in data['character_insights'][:8]:
                if not isinstance(item,dict):
                    continue
                target = names.get(str(item.get('name','')).strip().casefold())
                text = str(item.get('text','')).strip()
                evidence = str(item.get('evidence','')).strip()
                certainty = item.get('certainty','interpretation')
                field = str(item.get('field','')).strip()
                if (not target or not evidence or evidence not in source_text
                        or not 10 <= len(text) <= 500 or certainty not in ('confirmed','interpretation')):
                    continue
                if target['role'] == 'user':
                    # For an author-controlled hero, ONLY their author can establish
                    # inner life/biography. The bot's post is not evidence.
                    if certainty != 'confirmed' or not post or evidence not in post:
                        continue
                if self.db.add_development(target['id'],sid,turn_id,text,evidence,certainty):
                    saved_count += 1
                if field in PROFILE_FIELDS and certainty == 'confirmed':
                    # Story-local evolving sheet for BOTH players. Never overwrite
                    # any manually authored / imported field or story correction.
                    current = self.db.profile_fields(target['id'],sid)
                    protected = {f['field'] for f in current
                                 if f['session_id']==0 or f['source'] not in ('model','story')}
                    if field not in protected:
                        self.db.set_profile_field(target['id'],field,text,sid=sid,
                                                  source='story' if target['role']=='user' else 'model',
                                                  certainty='confirmed',evidence=evidence)
        self._last_memory_saved = saved_count
        return len(validated)
