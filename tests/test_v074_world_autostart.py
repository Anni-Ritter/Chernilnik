"""Regression checks for start-after-world-picker bug from a live chat."""
from pathlib import Path
from quickstart_core import (QuickDrafts, choose_world_in_draft, infer_world_from_lore, wants_start)
from storage import Store


def _world_with_lore(tmp_path):
    db = Store(str(tmp_path / 'rp.sqlite3'))
    wid = db.add_world('Сквозь миры')
    db.add_world_lore(wid, 'Эльдора', 'Страна Светоносных и инквизиторов.')
    db.add_world_lore(wid, 'Цетролис', 'Рядом с Севтосом.')
    return db, wid


def test_world_inferred_from_mentioned_known_region(tmp_path):
    db, wid = _world_with_lore(tmp_path)
    notes = [
        'Авинель - одна из Светоносных Эльдоры, инквизитор.',
        'Кирай решила пройти из Цетролиса через Эльдору в Севтос.',
    ]
    assert infer_world_from_lore(db, notes) == wid
    assert infer_world_from_lore(db, ['Авинель идёт к границе Эльдоры']) == wid
    assert infer_world_from_lore(db, ['Хочу историю в Сквозь миры']) == wid


def test_no_guess_if_no_clue_or_conflicting_universes(tmp_path):
    db, wid = _world_with_lore(tmp_path)
    other = db.add_world('Параллельная вселенная')
    db.add_world_lore(other, 'Эльдора', 'Здесь другой канон. Не смешивать.')
    assert infer_world_from_lore(db, ['Кирай в Эльдоре']) is None
    assert infer_world_from_lore(db, ['Хочу новую историю о парне и девушке']) is None
    assert infer_world_from_lore(db, ['Магия и история, ну погнали']) is None
    assert infer_world_from_lore(db, ['Сквозь миры или Параллельная вселенная']) is None


def test_start_conversational_phrases_and_no_false_pos():
    for phrase in ['Погнали', 'Как хочешь, можем начинать', 'Можем начинать', 'Давай начнём!', 'можно начинать']:
        assert wants_start(phrase), phrase
    assert not wants_start('Кирай сказала: «Погнали!»')
    assert not wants_start('Её история может начинаться с засады')


def test_pending_start_survives_redeployment_in_existing_draft(tmp_path):
    drafts = QuickDrafts(tmp_path)
    d = drafts.start(77, None, seed='Кирай идёт через Эльдору, Авинель патрулирует.')
    d['pending_start'] = True
    drafts.save(77, d)
    reloaded = QuickDrafts(tmp_path).get(77)
    assert reloaded['pending_start'] is True
    assert 'Авинель' in reloaded['notes'][0]
    assert choose_world_in_draft(QuickDrafts(tmp_path), 77, 12) is True
    ready = QuickDrafts(tmp_path).get(77)
    assert ready['world_id'] == 12 and ready['phase'] == 'collect'
    assert 'Авинель' in ready['notes'][0]
    assert not ready.get('pending_start')
    assert choose_world_in_draft(QuickDrafts(tmp_path), 77, 12) is False
