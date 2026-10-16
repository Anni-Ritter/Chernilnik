from quickstart_core import (QuickDrafts, wants_start, wants_new_story, find_world,
                             decode_json, validate_plan, need_clarification,
                             install_plan, setup_prompt)
from storage import Store
import pytest


def test_natural_phrases_and_world_name():
    assert wants_start('Погнали!')
    assert wants_start('давай начнем')
    assert not wants_start('персонаж сказал: погнали!')
    assert wants_new_story('Хочу новую историю с ангелами')
    assert not wants_new_story('Ангел хотел бы просто поспать')
    assert find_world('Давай сыграем в «Сквозь миры»', [{'id':12,'name':'Сквозь миры'}]) == 12
    assert find_world('Сквозь миры и Мир теней', [{'id':12,'name':'Сквозь миры'}, {'id':15,'name':'Мир теней'}]) is None


def test_draft_persists_messages_and_images(tmp_path):
    store = QuickDrafts(tmp_path)
    d=store.start(321,9,seed='Хочу новую историю')
    assert d['phase'] == 'collect'
    store.add_note(321, 'Авинель — твой персонаж')
    store.add_note(321, 'Кирай — мой персонаж')
    store.add_photo(321,'tg-file','/app/data/photos/example.jpg','Вот Авинель')
    second = QuickDrafts(tmp_path).get(321)
    assert second['notes'][-1] == 'Кирай — мой персонаж'
    assert second['photos'][0]['near_text'].endswith('Кирай — мой персонаж')
    assert 'ФОТО 1:' in setup_prompt(second)
    store.clear(321)
    assert store.get(321) is None


def test_unknown_roles_and_unassigned_photo_get_question():
    draft={'phase':'collect','world_id':3,'photos':[{'index':1}]}
    plan=validate_plan({'characters':[{'name':'Авинель','role':'unknown'}]},draft['photos'])
    assert 'Авинель' in need_clarification(plan,draft)
    plan['characters'][0]['role']='bot'
    assert 'фотографии' in need_clarification(plan,draft)
    plan['photos'].append({'index':1,'character':'Авинель'})
    assert need_clarification(plan,draft) is None


def test_json_fences_and_no_fabricated_roles():
    obj=decode_json('```json\n{"characters":[{"name":"Кирай","role":"unknown"}]}\n```')
    plan=validate_plan(obj,[])
    assert plan['characters'][0]['role']=='unknown'
    assert plan['title']=='Новая история'
    with pytest.raises((ValueError, Exception)):
        decode_json('{ broken')


def test_plan_creates_world_story_cast_and_preserves_other_stories(tmp_path):
    db=Store(str(tmp_path/'roleplay.sqlite3'))
    wid=db.add_world('Сквозь миры')
    drafts=QuickDrafts(tmp_path/'quickstarts')
    uid=999
    draft=drafts.start(uid,wid,seed='Авинель твоя; Кирай моя')
    plan=validate_plan({'title':'Врата Эльдоры','premise':'Кирай приехала в Эльдору.',
        'characters':[{'name':'Авинель','role':'bot','description':'Строгая ангел','appearance':'Зелёные глаза'},
                      {'name':'Кирай','role':'user','description':'Ведьма'}]},[])
    sid=install_plan(db,drafts,uid,draft,plan)
    assert db.get_session(sid)['world_id'] == wid
    cast={c['name']:c for c in db.list_characters(sid)}
    assert cast['Авинель']['role']=='bot'
    assert cast['Кирай']['role']=='user'
    assert db.get_character(cast['Авинель']['id'])['appearance_override']=='Зелёные глаза'
    # Idempotent retry after an API or network failure must not duplicate the cast.
    again=install_plan(db,drafts,uid,drafts.get(uid),plan)
    assert again==sid
    assert len(db.list_characters(sid))==2
    # A new story in the same world REUSES the established identity, but not past turns.
    drafts.clear(uid)
    draft2=drafts.start(uid,wid)
    sid2=install_plan(db,drafts,uid,draft2,plan)
    assert sid2!=sid
    assert {c['id'] for c in db.list_characters(sid2)}=={c['id'] for c in db.list_characters(sid)}
    assert not db.recent_turns(sid2)
    assert db.get_character(cast['Авинель']['id'])['description']=='Строгая ангел'


def test_unassigned_images_never_silently_enter_other_character(tmp_path):
    db=Store(str(tmp_path/'db.sqlite3'))
    drafts=QuickDrafts(tmp_path/'qs')
    draft=drafts.start(100,0,seed='Авинель твоя, Кирай моя')
    drafts.add_photo(100,'tg-avatar','/some/path.jpg','Авинель')
    draft=drafts.get(100)
    plan=validate_plan({'title':'Тест','characters':[{'name':'Авинель','role':'bot'},
        {'name':'Кирай','role':'user'}],
        'photos':[{'index':1,'character':'Авинель'}]},draft['photos'])
    sid=install_plan(db,drafts,100,draft,plan)
    ch={c['name']:c for c in db.list_characters(sid)}
    assert len(db.photos(ch['Авинель']['id']))==1
    assert db.photos(ch['Кирай']['id'])==[]
    install_plan(db,drafts,100,drafts.get(100),plan)
    assert len(db.photos(ch['Авинель']['id']))==1
