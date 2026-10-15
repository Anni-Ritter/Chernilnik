"""v0.5: provider routing without changing existing SQLite or memory."""
import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as Obj

import pytest

from generation_models import GenerationSettings, validate_choice, DEFAULT_POLZA_MODEL
from storage import Store


def test_choice_persists_separately_without_changing_db(tmp_path):
    file = tmp_path / 'roleplay.sqlite3'
    db = Store(str(file))
    sid = db.new_session(21, 'Тайна Ирис')
    db.add_turn(sid, 'Сказала правду.', 'Не поверил.', 'old', 21, 31)
    with sqlite3.connect(file) as con:
        schema_before = list(con.execute("SELECT name, sql FROM sqlite_master ORDER BY name"))
    path = tmp_path / 'generation_model.json'
    models = GenerationSettings(path, 'gpt-6-luna')
    assert models.choice.model == DEFAULT_POLZA_MODEL
    models.select('openai', 'gpt-6-luna')
    again = GenerationSettings(path, 'gpt-6-luna')
    assert again.choice.provider == 'openai'
    again.select('polza', 'google/gemini-3.8-flash')
    assert GenerationSettings(path, 'gpt-6-luna').choice.model == 'google/gemini-3.8-flash'
    assert 'pza_' not in path.read_text()
    with sqlite3.connect(file) as con:
        schema_after = list(con.execute("SELECT name, sql FROM sqlite_master ORDER BY name"))
    assert schema_before == schema_after
    assert db.recent_turns(sid, 1)[0]['bot_text'] == 'Не поверил.'


def test_invalid_choice_or_corrupt_config_never_overwritten(tmp_path):
    path = tmp_path / 'generation_model.json'
    settings = GenerationSettings(path, 'gpt-6-luna')
    initial = path.read_bytes()
    for pair in [('polza', 'gemini'), ('polza', 'bad id'), ('any', 'google/gemini-3.7-flash')]:
        with pytest.raises(ValueError):
            settings.select(*pair)
        assert path.read_bytes() == initial
    path.write_text('{not valid json', encoding='utf-8')
    with pytest.raises(RuntimeError):
        GenerationSettings(path, 'gpt-6-luna')
    assert path.read_text() == '{not valid json'


class FakeResponse:
    def __init__(self):
        self.calls = []
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return Obj(output_text='ОТВЕТ OPENAI', usage=Obj(input_tokens=13, output_tokens=11))


class FakeChat:
    def __init__(self):
        self.calls = []
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return Obj(choices=[Obj(message=Obj(content='ОТВЕТ GEMINI'))],
                   usage=Obj(prompt_tokens=44, completion_tokens=36))


def test_polza_generation_openai_memory_and_vision_client_untouched(tmp_path):
    from engine import RoleplayEngine
    db = Store(str(tmp_path / 'roleplay.sqlite3'))
    sid = db.new_session(22, 'Контроль моделей')
    db.add_opening(sid, 'Начало истории.', 'before', 5, 5)
    openai_responses = FakeResponse()
    chat_completions = FakeChat()
    openai_client = Obj(responses=openai_responses)
    polza_client = Obj(chat=Obj(completions=chat_completions))
    settings = GenerationSettings(tmp_path / 'generation_model.json', 'gpt-6-luna')
    engine = RoleplayEngine(db, openai_client, 'gpt-6-luna', 'gpt-6-luna', auto_memory=False)
    engine.configure_generation(settings, polza_client)
    result = asyncio.run(engine.generate(sid, 'Ирис посмотрела на него.'))
    assert result == ('ОТВЕТ GEMINI', 44, 36)
    assert engine.last_generation_model == 'google/gemini-3.7-flash'
    assert len(chat_completions.calls) == 1
    sent = chat_completions.calls[0]
    assert sent['model'] == 'google/gemini-3.7-flash'
    assert sent['messages'][0]['role'] == 'system'
    assert 'Ирис посмотрела на него.' in sent['messages'][1]['content']
    assert len(openai_responses.calls) == 0
    assert engine.client is openai_client  # photo analyzer still gets original client
    settings.select('openai', 'gpt-6-luna')
    result = asyncio.run(engine.generate(sid, 'Ирис стояла у окна.'))
    assert result == ('ОТВЕТ OPENAI', 13, 11)
    assert len(openai_responses.calls) == 1
    assert openai_responses.calls[0]['model'] == 'gpt-6-luna'
    assert openai_responses.calls[0]['store'] is False
    assert engine.memory_model == 'gpt-6-luna'


def test_missing_polza_key_raises_without_story_changes(tmp_path):
    from engine import RoleplayEngine
    db = Store(str(tmp_path / 'roleplay.sqlite3'))
    sid = db.new_session(1, 'A')
    settings = GenerationSettings(tmp_path / 'generation_model.json', 'gpt-6-luna')
    e = RoleplayEngine(db, Obj(responses=FakeResponse()), 'gpt-6-luna', 'gpt-6-luna')
    e.configure_generation(settings, None)
    with pytest.raises(ValueError, match='POLZA_API_KEY'):
        e.select_generation_model('polza', 'google/gemini-3.8-flash')
    with pytest.raises(RuntimeError, match='POLZA_API_KEY'):
        asyncio.run(e.generate_opening(sid))
    assert not db.get_opening(sid)
    assert db.recent_turns(sid) == []
    e.select_generation_model('openai', 'gpt-6-luna')
    assert asyncio.run(e.generate_opening(sid))[0] == 'ОТВЕТ OPENAI'
