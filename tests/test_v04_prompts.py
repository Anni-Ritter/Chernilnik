"""v0.4: prompt layering, automatic safety backup and per-story lexical recall."""
import asyncio
import importlib
from pathlib import Path
import sqlite3
import sys
import types

import pytest

from storage import Store
from prompting import PromptComposer


def composer(db):
    return PromptComposer(db, lambda sid: 'Винсент: наёмник; автоматон Ирис — тайна от него.',
                          history_count=2, recall_count=3)


def test_saved_histories_survive_upgrade(tmp_path):
    path = tmp_path / 'stories.sqlite3'
    db = Store(str(path))
    sid = db.new_session(1, 'Ирис и Винсент')
    db.add_opening(sid, 'Винсент вышел из дома.', 'mock', 2, 4)
    db.add_turn(sid, 'Ирис посмотрела на дверь.', 'Винсент не стал отвечать.', 'mock', 2, 4)
    db.add_fact(sid, 'Винсент похитил Ирис прежде и скрывает это от охраны.', pinned=True)
    # Make this look like a v0.3 database without the v0.4 tables and FTS.
    with sqlite3.connect(path) as conn:
        conn.executescript('DROP TRIGGER turns_search_insert; DROP TRIGGER turns_search_update; '
                           'DROP TRIGGER turns_search_delete; DROP TABLE turns_fts; DROP TABLE prompt_audit; '
                           'PRAGMA user_version=0;')
    reopened = Store(str(path))
    assert reopened.get_session(sid)['title'] == 'Ирис и Винсент'
    assert len(reopened.recent_turns(sid)) == 1
    assert 'Винсент похитил Ирис' in reopened.list_facts(sid)[0]['text']
    backups = list((tmp_path / 'backups').glob('*.before_v04_*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 1
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    Store(str(path))
    assert len(list((tmp_path / 'backups').glob('*.before_v04_*.sqlite3'))) == 1


def test_fts_recalls_distant_story_event_without_leaking_other_world(tmp_path):
    db = Store(str(tmp_path / 'story.sqlite3'))
    one = db.new_session(1, 'Первая история')
    two = db.new_session(1, 'Другая история')
    first_id = db.add_turn(one, 'Винсент похитил Ирис ночью, они прибыли в башню.',
                           'Он закрыл дверь на ключ.', 'm', 0, 0)
    db.add_turn(two, 'Ирис похитила Винсента в шахте.', 'Он закричал.', 'm', 0, 0)
    for i in range(20):
        db.add_turn(one, f'Они обсуждали дорогу и планы. Ход {i}', 'За окном гулял ветер.', 'm', 0, 0)
    rows = db.search_turns(one, 'Вспомнил как похитил Ирис в башню', limit=3, exclude_ids=[])
    assert rows and rows[0]['id'] == first_id
    assert all(row['session_id'] == one for row in rows)


def test_compiled_prompt_includes_examples_facts_and_chronology(tmp_path):
    db = Store(str(tmp_path / 'rp.sqlite3'))
    sid = db.new_session(7, 'Сюжет с Ирис')
    db.set_session_field(sid, 'scene_time', 'День 3 зимы')
    db.set_session_field(sid, 'cast', 'Винсент не знает, что Ирис автоматон')
    db.add_fact(sid, 'Винсент похитил Ирис, и она этого не забыла.', pinned=True)
    db.add_opening(sid, 'Винсент долго не мог решиться.', 'm', 0, 0)
    result = composer(db).assemble(sid, 'Ирис спросила, кто её похитил.')
    assert 'Винсент похитил Ирис' in result.user
    assert 'День 3 зимы' in result.user
    assert 'Леон уже около недели ходит' in result.user  # exemplar, NOT canon
    assert 'НЕ события текущего мира' in result.user
    assert 'Ирис спросила' in result.user
    assert 'тряпк' not in result.system
    assert 'Не добавляй шуток по расписанию' in result.system
    assert result.user.find('КЛЮЧЕВЫЕ ФАКТЫ') < result.user.find('НОВЫЙ ПОСТ АВТОРА')


def test_prompt_audit_is_per_story_and_replaced_not_appended(tmp_path):
    db = Store(str(tmp_path / 'rp.sqlite3'))
    a, b = db.new_session(1, 'A'), db.new_session(1, 'B')
    db.save_prompt_audit(a, 'opening', 'model1', 'first')
    db.save_prompt_audit(a, 'turn', 'model1', 'second')
    assert db.get_prompt_audit(a)['prompt_text'] == 'second'
    assert db.get_prompt_audit(b) is None
    assert db.make_backup().exists()


def test_opening_cannot_displace_player_or_repeat(tmp_path):
    db = Store(str(tmp_path / 'rp.sqlite3'))
    sid = db.new_session(1, 'A')
    prompt = composer(db).assemble(sid, opening=True).user
    assert 'Пользователь ещё не совершал игрового хода' in prompt
    assert 'Не предполагай, что персонаж пользователя' in prompt
    db.add_opening(sid, 'Текст', 'm', 0, 0)
    with pytest.raises(ValueError):
        composer(db).assemble(sid, opening=True)


def test_engine_one_generation_and_debug_no_extra_api_calls(tmp_path, monkeypatch):
    fake_openai = types.ModuleType('openai')
    fake_openai.AsyncOpenAI = type('Fake', (), {})
    monkeypatch.setitem(sys.modules, 'openai', fake_openai)
    sys.modules.pop('engine', None)
    module = importlib.import_module('engine')
    db = Store(str(tmp_path / 'rp.sqlite3'))
    sid = db.new_session(7, 'Story')
    db.add_opening(sid, 'Начало', 'm', 0, 0)

    class Responses:
        def __init__(self):
            self.calls = []
        async def create(self, **kw):
            self.calls.append(kw)
            return types.SimpleNamespace(output_text='Винсент молчал.',
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=7))

    responses = Responses()
    e = module.RoleplayEngine(db, types.SimpleNamespace(responses=responses), 'mock', 'mock', auto_memory=False)
    body, before, after = asyncio.run(e.generate(sid, 'Ирис вошла.'))
    assert (body, before, after) == ('Винсент молчал.', 10, 7)
    assert len(responses.calls) == 1
    assert 'Винсент молчал' not in responses.calls[0]['input']
    assert 'ОБРАЗЦЫ ЛИТЕРАТУРНОЙ МАНЕРЫ' in responses.calls[0]['input']
    trace = db.get_prompt_audit(sid)
    assert '=== SYSTEM / INSTRUCTIONS ===' in trace['prompt_text']
    assert 'Ирис вошла.' in trace['prompt_text']


def test_custom_style_example_overrides_bundled_and_survives_restart(tmp_path):
    path = tmp_path / 'style.sqlite3'
    store = Store(str(path))
    sid = store.new_session(123, 'Третья история')
    own = 'Она не стала объяснять ему очевидное. Пускай сам разбирается, раз уж решил влезть в чужие дела. ' * 2
    key = store.add_style_example(123, own)
    assert 'СОБСТВЕННОЕ' not in own
    assembled = composer(store).assemble(sid, opening=True).user
    assert own.strip()[:100] in assembled
    reopened = Store(str(path))
    assert reopened.list_style_examples(123)[0]['text'] == own.strip()
    assert reopened.delete_style_example(999, key) is False  # only the owner can delete
    assert reopened.delete_style_example(123, key) is True
    assert key not in [row['id'] for row in reopened.list_style_examples(123)]


def test_long_story_keeps_latest_turn_when_compacting(tmp_path):
    db = Store(str(tmp_path / 'long.sqlite3'))
    sid = db.new_session(1, 'Длинная история')
    for idx in range(12):
        db.add_turn(sid, 'А' * 1600, ('Текст_' + str(idx) + '_' + 'Б' * 2600), 'm', 0, 0)
    result = PromptComposer(db, lambda sid: 'Винсент обычный человек.',
                            max_chars=11000, history_count=12, recall_count=0).assemble(
                                sid, 'Ирис решила остановиться.')
    assert 'Текст_11_' in result.user
    assert 'Ирис решила остановиться.' in result.user
    assert len(result.user) <= 11000
