"""Shared lore, independent story memories, safe v0.5 migration and profiles."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace as NS

import sys, types
fake = types.ModuleType('openai')
fake.AsyncOpenAI = type('AsyncOpenAI', (), {})
sys.modules.setdefault('openai', fake)
from storage import Store
from engine import RoleplayEngine
from worlds import PROFILE_FIELDS


def test_world_shared_lore_and_story_local_history(tmp_path):
    db=Store(str(tmp_path/'rp.sqlite3'))
    wid=db.add_world('Сквозь миры')
    db.set_world_description(wid,'Эльдора — обособленное государство ангелов.')
    sid1=db.new_session(1,'Натан и Кирай')
    db.assign_story_world(sid1,wid)
    cid=db.add_character(sid1,'Нафаниил','bot')
    db.set_profile_field(cid,'occupation','Юстициар')
    sid2=db.new_session(1,'Винсент и Ирис')
    db.assign_story_world(sid2,wid)
    assert 'Эльдора' in db.get_world(wid)['description']
    assert not db.list_characters(sid2)
    db.attach_character(sid2,cid,'npc')
    assert db.list_characters(sid1)[0]['role']=='bot'
    assert db.list_characters(sid2)[0]['role']=='npc'
    db.add_turn(sid1,'Кирай прошла.','Натан смотрел ей вслед.','m',1,1)
    db.add_fact(sid1,'Натан был свидетелем разговора Кирай.',pinned=True)
    assert not db.recent_turns(sid2)
    assert not db.list_facts(sid2)
    assert db.profile_fields(cid)[0]['value']=='Юстициар'
    db.add_development(cid,sid1,1,'Сомневается, стоит ли доверять Кирай.',
                       'Натан смотрел ей вслед.','interpretation')
    assert db.development(cid,sid1)
    assert not db.development(cid,sid2)
    db.add_world_event(wid,'В Эльдоре сменился совет правителей.',sid1,1,'совет')
    assert db.list_world_events(wid)[0]['session_id']==sid1
    bot=RoleplayEngine(db,NS(), 'mock','mock',auto_memory=False)
    prompt=bot.inspect(sid2,post='Ирис остановилась.',opening=True)
    assert 'Эльдора — обособленное' in prompt
    assert 'В Эльдоре сменился совет' in prompt
    assert 'Сомневается, стоит ли доверять Кирай' not in prompt
    assert 'Натан был свидетелем' not in prompt
    assert 'Кирай прошла.' not in prompt
    assert 'Юстициар' in prompt


def test_character_unlink_keeps_library_and_photos(tmp_path):
    db=Store(str(tmp_path/'rp.sqlite3'))
    wid=db.add_world('Кариат')
    sid=db.new_session(1,'Тест')
    db.assign_story_world(sid,wid)
    cid=db.add_character(sid,'Винсент','bot')
    db.add_photo(cid,'file','/app/data/portraits/c.jpg')
    assert db.delete_character(sid,cid)==[]
    assert db.get_character(cid)
    assert db.photos(cid)
    assert not db.list_characters(sid)


def test_profile_author_and_observations_stay_separate(tmp_path):
    db=Store(str(tmp_path/'rp.sqlite3'))
    wid=db.add_world('Эльдора')
    sid=db.new_session(1,'Натан и Кирай')
    db.assign_story_world(sid,wid)
    cid=db.add_character(sid,'Нафаниил','bot')
    db.set_profile_field(cid,'fears','Неизвестно',sid=0,source='author')
    db.set_profile_field(cid,'goals','Следить за порядком',sid=sid,source='model')
    rows=db.profile_fields(cid,sid)
    assert {r['field'] for r in rows}=={'fears','goals'}
    assert any(r['source']=='author' for r in rows)
    assert db.add_development(cid,sid,None,'Поступок допускает разные объяснения.',
                              'Он замер.','interpretation')
    assert db.development(cid,sid)[0]['certainty']=='interpretation'


def test_old_database_automatically_backed_up_and_migrated(tmp_path):
    path=tmp_path/'rp.sqlite3'
    # Legacy database contains old sessions and portrait records.
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE sessions (id INTEGER PRIMARY KEY, title TEXT NOT NULL, world TEXT NOT NULL DEFAULT "", cast TEXT NOT NULL DEFAULT "", scene_time TEXT NOT NULL DEFAULT "",current_location TEXT NOT NULL DEFAULT "",created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        con.execute('INSERT INTO sessions(id,title) VALUES (1,"Старая история")')
        con.execute('CREATE TABLE characters (id INTEGER PRIMARY KEY,session_id INTEGER NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL DEFAULT "npc",description TEXT NOT NULL DEFAULT "",appearance_override TEXT NOT NULL DEFAULT "",created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(session_id,name COLLATE NOCASE))')
        con.execute('INSERT INTO characters(id,session_id,name,role) VALUES (1,1,"Натан","bot")')
        con.execute('PRAGMA user_version=4')
    db=Store(str(path))
    assert db.get_session(1)['title']=='Старая история'
    assert db.list_characters(1)[0]['name']=='Натан'
    backups=list((tmp_path/'backups').glob('*.sqlite3'))
    assert backups
    with sqlite3.connect(path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0]==6
    Store(str(path))
    assert len(list((tmp_path/'backups').glob('*.sqlite3')))==len(backups)


def test_memory_extraction_records_world_and_bot_insights_without_inventing_player(tmp_path):
    db=Store(str(tmp_path/'rp.sqlite3'))
    wid=db.add_world('Эльдора')
    sid=db.new_session(1,'Натан и Кирай')
    db.assign_story_world(sid,wid)
    npc=db.add_character(sid,'Натан','bot')
    usr=db.add_character(sid,'Кирай','user')
    post='Кирай отступила.'
    answer='Натан приказал закрыть границу. Он боялся собственной нерешительности.'
    payload={'facts':[], 'world_changes':[{'text':'Правительство закрыло границу Эльдоры.',
              'evidence':'Натан приказал закрыть границу.', 'certainty':'confirmed'}],
             'character_insights':[{'name':'Натан','field':'fears','text':'Боится своей нерешительности.',
                 'evidence':'Он боялся собственной нерешительности.', 'certainty':'confirmed'},
                 {'name':'Кирай','field':'fears','text':'Боится замкнутого пространства.',
                  'evidence':'Кирай отступила.', 'certainty':'interpretation'}]}
    class Responses:
        async def create(self, **k):
            return NS(output_text=json.dumps(payload), usage=NS(input_tokens=5,output_tokens=9))
    engine=RoleplayEngine(db,NS(responses=Responses()),'gpt','mem',auto_memory=True)
    db.add_turn(sid,post,answer,'gemini',1,1)
    n=asyncio.run(engine.memorize(sid,1,post,answer))
    assert n==0
    assert len(db.list_world_events(wid))==1
    assert db.development(npc,sid)
    assert db.profile_fields(npc,sid)
    assert db.development(usr,sid)==[]
    assert db.profile_fields(usr,sid)==[]


def test_world_assign_fails_closed_on_character_from_other_world(tmp_path):
    db=Store(str(tmp_path/'rp.sqlite3'))
    w1=db.add_world('А')
    w2=db.add_world('Б')
    s=db.new_session(1,'Первый')
    db.assign_story_world(s,w1)
    cid=db.add_character(s,'Герой','bot')
    import pytest
    with pytest.raises(ValueError):
        db.assign_story_world(s,w2)
    assert db.get_session(s)['world_id']==w1


def test_world_identity_not_duplicated_by_second_story(tmp_path):
    import pytest
    db=Store(str(tmp_path/'rp.sqlite3'))
    wid=db.add_world('Сквозь миры')
    a=db.new_session(1,'Одна ветка')
    b=db.new_session(1,'Другая ветка')
    db.assign_story_world(a,wid)
    db.assign_story_world(b,wid)
    hero=db.add_character(a,'Натан','bot')
    with pytest.raises(ValueError,match='библиотеке мира'):
        db.add_character(b,'натан','bot')
    db.attach_character(b,hero,role='npc')
    assert db.list_characters(b)[0]['id']==hero


def test_lore_rejects_oversize_instead_of_silently_cutting(tmp_path):
    import pytest
    db=Store(str(tmp_path/'rp.sqlite3'))
    wid=db.add_world('Мир')
    with pytest.raises(ValueError):
        db.add_world_lore(wid,'Страна','a'*16000)
    assert db.list_world_lore(wid)==[]


def test_questionnaire_groups_cover_all_fields():
    from worlds import PROFILE_GROUPS
    flattened=[key for _,(_,fields) in PROFILE_GROUPS.items() for key in fields]
    assert len(flattened)==len(set(flattened))
    assert set(flattened)==set(PROFILE_FIELDS)
