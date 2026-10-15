from memory import select_memories, validated_extractions
from storage import Store


def test_session_isolation_and_persistent_history(tmp_path):
    path = tmp_path / 'story.db'
    db = Store(str(path))
    a = db.new_session(10, 'Каменный город')
    db.add_fact(a, 'Винсент похитил Ирму и удерживает её в башне', importance=5)
    db.add_turn(a, 'Он похитил девушку', 'Он запер дверь.', 'mock', 0, 0)
    b = db.new_session(10, 'Другая история')
    assert db.list_facts(b) == []
    assert len(db.recent_turns(b)) == 0
    db2 = Store(str(path))
    assert len(db2.recent_turns(a)) == 1
    assert db2.list_facts(a)[0]['text'].startswith('Винсент')


def test_pinned_and_crucial_retrieval(tmp_path):
    db = Store(str(tmp_path / 'db.sqlite3'))
    sid = db.new_session(15, 'Ролевая')
    db.add_fact(sid, 'Винсент похитил Ирму и прячет от стражи', importance=5)
    db.add_fact(sid, 'На камне лежала маленькая синяя пылинка', importance=3)
    db.add_fact(sid, 'У замка отсутствует второй ключ', importance=5, pinned=True)
    memories = select_memories(db.list_facts(sid), 'Ирма попробовала выбраться')
    assert any('Винсент похитил' in m for m in memories)
    assert any('второй ключ' in m for m in memories)
    assert not any('синяя пылинка' in m for m in memories)


def test_extraction_requires_literal_evidence():
    source = 'Он вытащил Ирму из колодца. Потом сел на траву.'
    payload = {'facts': [
        {'text': 'Он спас Ирму от падения в колодец', 'importance': 5,
         'category': 'event', 'certainty': 'confirmed', 'known_by': '', 'evidence': 'Он вытащил Ирму из колодца'},
        {'text': 'Её похитили инопланетяне', 'importance': 5,
         'category': 'event', 'certainty': 'confirmed', 'known_by': '', 'evidence': 'инопланетяне'},
        {'text': 'Трава зелёная', 'importance': 1,
         'category': 'object', 'certainty': 'confirmed', 'known_by': '', 'evidence': 'траву'}
    ]}
    facts = validated_extractions(payload, source)
    assert len(facts) == 1
    assert facts[0]['text'].startswith('Он спас')


def test_drafts_not_added_as_turns(tmp_path):
    db = Store(str(tmp_path / 'draft.db'))
    sid = db.new_session(15, 'Сессия')
    db.start_draft(15, sid)
    db.append_draft(15, 'Первая часть')
    db.append_draft(15, 'Вторая часть')
    assert db.draft_active(15)['text'] == 'Первая часть\n\nВторая часть'
    assert len(db.recent_turns(sid)) == 0
    db.clear_draft(15)
    assert db.draft_active(15) is None


def test_clock_not_advanced_by_message_count(tmp_path):
    db = Store(str(tmp_path / 'time.db'))
    sid = db.new_session(15, 'Сессия')
    db.set_session_field(sid, 'scene_time', 'Третий день зимы')
    for _ in range(15):
        db.add_turn(sid, 'Ход', 'Ответ', 'mock', 0, 0)
    assert db.get_session(sid)['scene_time'] == 'Третий день зимы'


def test_captor_memory_survives_thirty_turns(tmp_path):
    db = Store(str(tmp_path / 'long_story.db'))
    sid = db.new_session(15, 'Долгая история')
    db.add_fact(sid, 'Винсент изначально похитил Ирму; похищение было его решением', importance=5)
    db.set_session_field(sid, 'scene_time', 'Четвертый день зимы')
    for i in range(30):
        db.add_turn(sid, f'Разговор номер {i}', f'Ответ номер {i}', 'fake', 0, 0)
    assert len(db.recent_turns(sid, 8)) == 8
    memories = select_memories(db.list_facts(sid), 'Ирма спросила, почему это случилось')
    assert any('Винсент изначально похитил' in m for m in memories)
    assert db.get_session(sid)['scene_time'] == 'Четвертый день зимы'
