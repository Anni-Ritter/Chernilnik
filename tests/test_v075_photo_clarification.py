"""Regression: the 'whose photo?' prompt must never loop after an answer."""
from quickstart_core import (
    QuickDrafts, apply_author_photo_assignments, need_clarification,
    photo_reply_assignments, validate_plan, install_plan,
)
from storage import Store


def _draft(tmp_path, n=2):
    ds = QuickDrafts(tmp_path / 'quickstarts')
    ds.start(43, 0, seed='Авинель — твоя. Кирай — моя. Начало у границы Эльдоры.')
    for i in range(n):
        ds.add_photo(43, f'file{i + 1}', f'/tmp/portrait{i + 1}.png')
    return ds


def _plan(draft, llm_photos=None):
    return validate_plan({'title': 'Эльдора', 'premise': 'Встреча на границе.',
        'characters': [{'name': 'Авинель', 'role': 'bot'},
                       {'name': 'Кирай', 'role': 'user'}],
        'photos': llm_photos or []}, draft['photos'])


def test_single_photo_answer_persists_and_unblocks_next_start(tmp_path):
    ds = _draft(tmp_path, n=1)
    d = ds.get(43)
    p = _plan(d)
    assert need_clarification(p, d) == 'Чьи фотографии под номерами 1? Назови имена героев.'
    assert photo_reply_assignments('Кирай', p, d, allow_single_answer=True) == {1: 'Кирай'}
    d['photos'][0]['character'] = 'Кирай'
    ds.save(43, d)
    reopened = QuickDrafts(tmp_path / 'quickstarts').get(43)
    updated = validate_plan(p, reopened['photos'])
    assert need_clarification(updated, reopened) is None
    assert updated['photos'] == [{'index': 1, 'character': 'Кирай'}]


def test_author_pairs_override_llm_guess_before_first_clarification(tmp_path):
    ds = _draft(tmp_path)
    ds.add_note(43, 'Первая Кирай, вторая Авинель')
    d = ds.get(43)
    plan = _plan(d, [{'index': 2, 'character': 'Кирай'}])  # incorrect LLM guess
    assert apply_author_photo_assignments(d, plan)
    result = validate_plan(plan, d['photos'])
    assert result['photos'] == [{'index': 1, 'character': 'Кирай'},
                                 {'index': 2, 'character': 'Авинель'}]
    assert need_clarification(result, d) is None


def test_question_answers_are_numbered_and_can_arrive_in_any_order(tmp_path):
    ds = _draft(tmp_path)
    d = ds.get(43)
    p = _plan(d)
    assert photo_reply_assignments('2 — Авинель', p, d, allow_single_answer=True) == {2: 'Авинель'}
    d['photos'][1]['character'] = 'Авинель'
    new = validate_plan(p, d['photos'])
    assert need_clarification(new, d).startswith('Чьи фотографии под номерами 1')
    assert photo_reply_assignments('Кирай', new, d, allow_single_answer=True) == {1: 'Кирай'}
    d['photos'][0]['character'] = 'Кирай'
    assert need_clarification(validate_plan(new, d['photos']), d) is None


def test_unrelated_chat_and_negative_answer_not_silently_bound(tmp_path):
    ds = _draft(tmp_path, n=1)
    d = ds.get(43)
    p = _plan(d)
    for text in ('Кирай сбежала в лес.', 'Это не Кирай', 'Авинель или Кирай?', 'Да, погнали'):
        assert photo_reply_assignments(text, p, d, allow_single_answer=True) == {}


def test_mapping_remains_correct_during_story_install(tmp_path):
    db = Store(str(tmp_path / 'rp.sqlite3'))
    ds = _draft(tmp_path, n=1)
    d = ds.get(43)
    d['photos'][0]['character'] = 'Кирай'
    ds.save(43, d)
    plan = _plan(ds.get(43))
    sid = install_plan(db, ds, 43, ds.get(43), plan)
    chars = {row['name']: row for row in db.list_characters(sid)}
    assert len(db.photos(chars['Кирай']['id'])) == 1
    assert not db.photos(chars['Авинель']['id'])


def test_direct_correction_outranks_older_photo_notes(tmp_path):
    ds = _draft(tmp_path, n=1)
    ds.add_note(43, 'Первая Авинель')
    d = ds.get(43)
    p = _plan(d)
    assert apply_author_photo_assignments(d, p)
    assert d['photos'][0]['character'] == 'Авинель'
    d['photos'][0]['character'] = 'Кирай'
    d['photos'][0]['assigned_after_notes'] = len(d['notes'])
    assert not apply_author_photo_assignments(d, p)
    assert validate_plan(p, d['photos'])['photos'][0]['character'] == 'Кирай'
    ds.save(43, d)
    ds.add_note(43, 'Исправление: первое фото — Авинель')
    d = ds.get(43)
    assert apply_author_photo_assignments(d, p)
    assert d['photos'][0]['character'] == 'Авинель'
