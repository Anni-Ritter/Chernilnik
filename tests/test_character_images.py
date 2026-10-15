"""v0.3 offline tests: character isolation, durable portraits, and vision inputs."""
import asyncio
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from storage import Store
from vision import describe_image, image_data_url, image_info


def test_upgrade_preserves_old_turns(tmp_path):
    path = tmp_path / 'roleplay.sqlite3'
    old_db = Store(str(path))
    sid = old_db.new_session(11, 'Старая ролевая')
    old_db.add_opening(sid, 'Это старое начало.', 'mock', 12, 13)
    old_db.add_turn(sid, 'Первый ход.', 'Первый ответ.', 'mock', 12, 14)
    reopened = Store(str(path))
    assert reopened.get_opening(sid)['bot_text'] == 'Это старое начало.'
    assert reopened.recent_turns(sid)[0]['bot_text'] == 'Первый ответ.'
    assert reopened.list_characters(sid) == []


def test_character_photos_isolated_between_stories_and_restart(tmp_path):
    path = tmp_path / 'story.sqlite3'
    store = Store(str(path))
    s1 = store.new_session(11, 'Мир А')
    s2 = store.new_session(11, 'Мир Б')
    cid = store.add_character(s1, 'Ирис', role='user')
    store.set_character_text(cid, 'description', 'Автоматон; для других персонажей это тайна.')
    portrait_path = tmp_path / 'irys.jpg'
    portrait_path.write_bytes(b'fixture')
    pid = store.add_photo(cid, 'tg-file-id', str(portrait_path),
                          'Тёмные длинные волосы, белая куртка', status='ok')
    assert store.list_characters(s2) == []
    assert store.photos(cid)[0]['telegram_file_id'] == 'tg-file-id'
    store = Store(str(path))
    assert store.get_character(cid)['name'] == 'Ирис'
    assert store.get_character(cid)['role'] == 'user'
    assert store.get_photo(pid)['visual_description'].startswith('Тёмные')
    assert 'Ирис' in store.export(s1)
    assert 'Тёмные' in store.export(s1)
    assert 'Ирис' not in store.export(s2)


def test_primary_photo_and_deletion_promotes_remaining(tmp_path):
    db = Store(str(tmp_path / 'db.sqlite3'))
    sid = db.new_session(11, 'Сессия')
    cid = db.add_character(sid, 'Винсент', 'bot')
    a = db.add_photo(cid, 'a', str(tmp_path / 'a.jpg'), 'Светлые волосы')
    b = db.add_photo(cid, 'b', str(tmp_path / 'b.jpg'), 'Чёрные волосы')
    assert db.get_photo(b)['is_primary'] == 1
    assert db.get_photo(a)['is_primary'] == 0
    db.make_primary_photo(cid, a)
    assert db.get_photo(a)['is_primary'] == 1
    assert db.get_photo(b)['is_primary'] == 0
    assert db.delete_photo(cid, a)['id'] == a
    assert db.get_photo(b)['is_primary'] == 1
    assert db.get_photo(a) is None


def test_cannot_assign_another_characters_photo_as_primary(tmp_path):
    db = Store(str(tmp_path / 'db.sqlite3'))
    sid = db.new_session(11, 'Тест')
    c1 = db.add_character(sid, 'Рейн')
    c2 = db.add_character(sid, 'Элира')
    pid = db.add_photo(c1, 'file', str(tmp_path / 'x.jpg'))
    with pytest.raises(ValueError):
        db.make_primary_photo(c2, pid)
    assert db.delete_photo(c2, pid) is None


def test_image_validation_and_data_url():
    stream = BytesIO()
    Image.new('RGB', (100, 120), (12, 25, 33)).save(stream, format='PNG')
    raw = stream.getvalue()
    assert image_info(raw) == '.png'
    assert image_data_url(raw).startswith('data:image/jpeg;base64,')
    with pytest.raises(ValueError):
        image_info(b'not really an image')


def test_image_response_contains_actual_image_without_repeated_calls():
    data = BytesIO()
    Image.new('RGB', (90, 120)).save(data, format='JPEG')

    class MockResponses:
        async def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(output_text='Рыжие волосы и чёрный плащ.',
                                   usage=SimpleNamespace(input_tokens=130, output_tokens=15))

    responses = MockResponses()
    client = SimpleNamespace(responses=responses)
    result = asyncio.run(describe_image(client, 'gpt-4o-mini', data.getvalue(), 'Ирис'))
    assert result == ('Рыжие волосы и чёрный плащ.', 130, 15)
    assert responses.kwargs['store'] is False
    assert responses.kwargs['input'][0]['content'][1]['type'] == 'input_image'
    assert 'base64,' in responses.kwargs['input'][0]['content'][1]['image_url']
