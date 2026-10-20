"""v0.7.8: literary and conversational replies must not be cut mid-thought."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as Obj
import pytest

from generation_models import GenerationSettings
from generation_continuation import append_continuation
from engine import RoleplayEngine
from storage import Store
from setup_dialogue import generate_setup_reply, SETUP_MAX_TOKENS, SETUP_RETRY_MAX_TOKENS


def fake_polza(content, finish_reason='stop', input_tokens=12, output_tokens=31):
    return Obj(choices=[Obj(message=Obj(content=content), finish_reason=finish_reason)],
               usage=Obj(prompt_tokens=input_tokens, completion_tokens=output_tokens))


class ChatQueue:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError('unexpected API call')
        return self.responses.pop(0)


class ResponsesQueue:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError('unexpected API call')
        return self.responses.pop(0)


def make_story(tmp_path, *, provider='polza', chat=None, responses=None, budget=1800):
    db = Store(str(tmp_path / 'db.sqlite3'))
    sid = db.new_session(1234, 'Сюжет без обрывов')
    settings = GenerationSettings(tmp_path / 'generation.json', 'gpt-6-luna')
    settings.select(provider, 'google/gemini-3.7-flash' if provider == 'polza' else 'gpt-6-luna')
    client = Obj(responses=responses) if responses else Obj(responses=ResponsesQueue())
    engine = RoleplayEngine(db, client, 'gpt-6-luna', 'gpt-6-luna', auto_memory=False,
                            max_output_tokens=budget)
    engine.configure_generation(settings, Obj(chat=Obj(completions=chat)) if chat else None)
    return db, sid, engine


def test_polza_auto_continues_old_1800_limit_and_sums_usage(tmp_path, monkeypatch):
    monkeypatch.setenv('GENERATION_AUTO_CONTINUES', '3')
    monkeypatch.setenv('GENERATION_CONTINUE_TOKENS', '8192')
    fake = ChatQueue(
        fake_polza('Авинель подняла клинок и произнесла: «Кирай, не сме', 'length', 100, 55),
        fake_polza('й шевелиться!»\n\nСтража насторожилась.', 'stop', 140, 23),
    )
    db, sid, engine = make_story(tmp_path, chat=fake)
    result = asyncio.run(engine.generate(sid, 'Кирай остановилась.'))
    assert result == ('Авинель подняла клинок и произнесла: «Кирай, не смей шевелиться!»'
                      '\n\nСтража насторожилась.', 240, 78)
    assert fake.calls[0]['max_tokens'] == 1800
    assert fake.calls[1]['max_tokens'] == 8192
    assert fake.calls[1]['messages'][-2]['role'] == 'assistant'
    assert 'не новая сцена' in fake.calls[1]['messages'][-1]['content']
    assert db.recent_turns(sid) == []  # engine alone does not commit partial turns


def test_openai_responses_auto_continues_without_server_storage(tmp_path):
    fake = ResponsesQueue(
        Obj(output_text='Началась гроза, ветер разошёл', status='incomplete',
            incomplete_details=Obj(reason='max_output_tokens'),
            usage=Obj(input_tokens=10, output_tokens=4)),
        Obj(output_text='ся. Авинель остановилась.', status='completed',
            incomplete_details=None, usage=Obj(input_tokens=12, output_tokens=5)),
    )
    _, sid, engine = make_story(tmp_path, provider='openai', responses=fake)
    text, in_tokens, out_tokens = asyncio.run(engine.generate_opening(sid))
    assert text == 'Началась гроза, ветер разошёлся. Авинель остановилась.'
    assert (in_tokens, out_tokens) == (22, 9)
    assert fake.calls[0]['store'] is False and fake.calls[1]['store'] is False
    assert fake.calls[1]['input'][-2]['role'] == 'assistant'
    assert fake.calls[1]['max_output_tokens'] == 8192


def test_stop_without_period_is_an_intentional_ending_not_auto_extended(tmp_path):
    fake = ChatQueue(fake_polza('Авинель посмотрела на ведьму'))
    _, sid, engine = make_story(tmp_path, chat=fake)
    answer, *_ = asyncio.run(engine.generate_opening(sid))
    assert answer == 'Авинель посмотрела на ведьму'
    assert len(fake.calls) == 1


def test_maximum_repeated_cutoffs_are_not_written_to_story(tmp_path, monkeypatch):
    monkeypatch.setenv('GENERATION_AUTO_CONTINUES', '2')
    fake = ChatQueue(*(fake_polza('незаконченный фрагмент', 'length') for _ in range(3)))
    db, sid, engine = make_story(tmp_path, chat=fake)
    with pytest.raises(RuntimeError, match='не завершила ответ'):
        asyncio.run(engine.generate_opening(sid))
    assert db.get_opening(sid) is None
    assert db.recent_turns(sid) == []


def test_setup_uses_continuation_not_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('GENERATION_AUTO_CONTINUES', '3')
    fake = ChatQueue(fake_polza('Интересная завязка, но не', 'length'),
                     fake_polza(' стоит торопиться с конфликтом.', 'stop'))
    db, sid, engine = make_story(tmp_path, chat=fake)
    result = asyncio.run(generate_setup_reply(engine, db,
                                              {'world_id': None, 'notes': ['test'], 'photos': []},
                                              'Что думаешь?'))
    assert result == 'Интересная завязка, но не стоит торопиться с конфликтом.'
    assert fake.calls[0]['max_tokens'] == SETUP_MAX_TOKENS
    assert fake.calls[1]['max_tokens'] == SETUP_RETRY_MAX_TOKENS
    assert fake.calls[1]['messages'][-2]['role'] == 'assistant'


def test_overlap_deduplication():
    assert append_continuation('Она посмотрела на Кирай.', 'на Кирай. И шагнула ближе.') == ('Она посмотрела на Кирай. И шагнула ближе.')
