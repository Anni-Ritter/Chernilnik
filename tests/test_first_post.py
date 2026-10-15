"""Opening a story must be non-repeatable and must not create phantom player moves."""
import asyncio
import importlib
import sys
import types

import pytest

from storage import Store


def test_opening_is_once_only_and_survives_restart(tmp_path):
    path = tmp_path / 'story.sqlite3'
    db = Store(str(path))
    sid = db.new_session(42, 'Зимняя история')
    db.set_session_field(sid, 'cast', 'Игрок ведёт Ирис, бот — Винсента')
    db.add_opening(sid, 'Винсент вышел на улицу.', 'fake-model', 123, 234)
    assert db.get_opening(sid)['bot_text'] == 'Винсент вышел на улицу.'
    assert db.recent_turns(sid) == []
    with pytest.raises(ValueError):
        db.add_opening(sid, 'Альтернативный первый пост.', 'fake-model', 1, 1)
    reopened = Store(str(path))
    assert reopened.get_opening(sid)['bot_text'] == 'Винсент вышел на улицу.'
    assert '## Первый пост соавтора' in reopened.export(sid)
    assert '### Ты' not in reopened.export(sid)


def test_opening_is_isolated_from_other_stories(tmp_path):
    db = Store(str(tmp_path / 'story.sqlite3'))
    first = db.new_session(42, 'История А')
    second = db.new_session(42, 'История Б')
    db.add_opening(first, 'Первая история.', 'fake-model', 0, 0)
    assert db.get_opening(second) is None
    assert 'Первая история.' not in db.export(second)


def test_engine_generates_first_post_without_player_message(tmp_path, monkeypatch):
    # In the lightweight offline test container the OpenAI SDK may not be installed.
    # Stub only its import; never make a network request.
    fake_openai = types.ModuleType('openai')
    fake_openai.AsyncOpenAI = type('FakeOpenAI', (), {})
    monkeypatch.setitem(sys.modules, 'openai', fake_openai)
    sys.modules.pop('engine', None)
    module = importlib.import_module('engine')

    db = Store(str(tmp_path / 'story.sqlite3'))
    sid = db.new_session(42, 'Винсент и Ирис')
    db.set_session_field(sid, 'cast', 'Я веду Ирис, бот ведёт Винсента; он не знает, что Ирис автоматон')
    db.set_session_field(sid, 'scene_time', 'Третий день зимы, поздний вечер')

    class MockResponses:
        async def create(self, **kwargs):
            self.last_kwargs = kwargs
            return types.SimpleNamespace(output_text='Винсент вышел из таверны. Он решил поискать Ирис.',
                                         usage=types.SimpleNamespace(input_tokens=117, output_tokens=33))

    responses = MockResponses()
    client = types.SimpleNamespace(responses=responses)
    engine = module.RoleplayEngine(db, client, 'fake-model', 'fake-model', auto_memory=False)
    opening, incoming, outgoing = asyncio.run(engine.generate_opening(sid))
    assert 'Ирис' in opening
    assert incoming == 117 and outgoing == 33
    assert 'Третий день зимы' in responses.last_kwargs['input']
    assert 'Пользователь ещё не совершал игрового хода' in responses.last_kwargs['input']
    assert 'Не предполагай, что персонаж пользователя' in responses.last_kwargs['input']
    db.add_opening(sid, opening, 'fake-model', incoming, outgoing)
    context = engine.context(sid, 'Ирис нахмурилась.')
    assert '[ПЕРВЫЙ ПОСТ СОАВТОРА]' in context
    assert opening in context
    assert 'Ирис нахмурилась.' in context
    assert len(db.recent_turns(sid)) == 0
