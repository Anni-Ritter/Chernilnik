"""Literary generation and post-commit fact extraction (no scene-planner step)."""
from __future__ import annotations

import json
import logging
import os

from openai import AsyncOpenAI
from storage import Store
from prompting import PromptComposer
from memory import EXTRACT_INSTRUCTIONS, extraction_prompt, select_memories, validated_extractions, save_extractions
from generation_models import GenerationChoice, GenerationSettings
from worlds import PROFILE_FIELDS

log = logging.getLogger(__name__)


class RoleplayEngine:
    def __init__(self, store: Store, client: AsyncOpenAI, model: str, memory_model: str,
                 auto_memory: bool = True, max_output_tokens: int = 1800):
        self.db, self.client = store, client
        self.model, self.memory_model = model, memory_model
        self.generation_settings: GenerationSettings | None = None
        self.polza_client: AsyncOpenAI | None = None
        self.last_generation_model = model
        self.auto_memory = auto_memory
        self.max_output_tokens = max_output_tokens
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
        if choice.provider == 'polza':
            if self.polza_client is None:
                raise RuntimeError('Для Polza API нужно задать POLZA_API_KEY на BotHost.')
            # Polza implements OpenAI-compatible Chat Completions, NOT Responses API.
            response = await self.polza_client.chat.completions.create(
                model=choice.model,
                messages=[{'role': 'system', 'content': compiled.system},
                          {'role': 'user', 'content': compiled.user}],
                max_tokens=self.max_output_tokens,
            )
            content = response.choices[0].message.content if response.choices else None
            if isinstance(content, str):
                answer = content.strip()
            elif isinstance(content, list):
                answer = '\n'.join(piece.get('text', '') if isinstance(piece, dict)
                                   else getattr(piece, 'text', '') or '' for piece in content).strip()
            else:
                answer = ''
            usage = getattr(response, 'usage', None)
            inp = getattr(usage, 'prompt_tokens', 0) or 0
            out = getattr(usage, 'completion_tokens', 0) or 0
        else:
            # Old OpenAI Responses API behavior stays intact, including no server-side storage.
            response = await self.client.responses.create(
                model=choice.model,
                instructions=compiled.system,
                input=compiled.user,
                max_output_tokens=self.max_output_tokens,
                store=False,
            )
            answer = (response.output_text or '').strip()
            usage = getattr(response, 'usage', None)
            inp = getattr(usage, 'input_tokens', 0) or 0
            out = getattr(usage, 'output_tokens', 0) or 0
        if not answer:
            raise RuntimeError('Модель не вернула художественный текст. Проверь ключ, ID модели и лимит токенов.')
        self.last_generation_model = choice.model
        return answer, inp, out

    async def generate_opening(self, sid: int):
        return await self._call(sid, post=None, opening=True)

    async def generate(self, sid: int, post: str):
        return await self._call(sid, post=post, opening=False)

    async def memorize(self, sid: int, turn_id: int | None, post: str, answer: str):
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
        response = await self.client.responses.create(
            model=self.memory_model, instructions=EXTRACT_INSTRUCTIONS, input=prompt,
            max_output_tokens=2000, store=False,
            text={'format': {'type': 'json_object'}},
        )
        usage = getattr(response, 'usage', None)
        self.db.add_usage(sid, 'memory', self.memory_model,
                          getattr(usage, 'input_tokens', 0) or 0,
                          getattr(usage, 'output_tokens', 0) or 0)
        data = json.loads(response.output_text or '{}')
        validated = validated_extractions(data, post + '\n' + answer)
        save_extractions(self.db, sid, turn_id, validated)
        source_text = post + '\n' + answer
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
                    self.db.add_world_event(world['id'],statement,sid,turn_id,evidence,status='confirmed')
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
                self.db.add_development(target['id'],sid,turn_id,text,evidence,certainty)
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
        return len(validated)
